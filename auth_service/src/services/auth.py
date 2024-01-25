import datetime
from abc import ABC, abstractmethod
from functools import wraps
from typing import Any

from async_fastapi_jwt_auth import AuthJWT
from async_fastapi_jwt_auth.exceptions import JWTDecodeError
from fastapi import HTTPException
from src.models.user import User
from src.schemas.result import Error, GenericResult
from src.schemas.token import Token, TokenJti
from src.schemas.user import UserHistoryCreateDto
from src.services.cache import TokenStorageABC
from src.services.user import UserServiceABC


class AuthServiceABC(ABC):
    @abstractmethod
    def login(
        self, *, login: str, password: str, user_agent: str
    ) -> GenericResult[Token]:
        ...

    @abstractmethod
    def logout(self):
        ...

    @abstractmethod
    def refresh(self, access_jti: str | None) -> Token:
        ...

    @abstractmethod
    def require_auth(self):
        ...

    @abstractmethod
    def optional_auth(self):
        ...

    @abstractmethod
    def get_user(self) -> User | None:
        ...


class AuthService(AuthServiceABC):
    def __init__(
        self,
        auth_jwt_service: AuthJWT,
        token_storage: TokenStorageABC,
        user_service: UserServiceABC,
    ):
        self._auth_jwt_service = auth_jwt_service
        self._token_storage = token_storage
        self._user_service = user_service

    async def _generate_token(self, user_id: Any):
        access_token = await self._auth_jwt_service.create_access_token(subject=user_id)
        refresh_token = await self._auth_jwt_service.create_refresh_token(
            subject=user_id
        )
        return Token(access_token=access_token, refresh_token=refresh_token)

    async def _refresh_token_required(self):
        try:
            await self._auth_jwt_service.jwt_refresh_token_required()
            if self._check_token_expiracy():
                raise HTTPException(status_code=401, detail="Unauthorized")
        except JWTDecodeError as err:
            raise HTTPException(status_code=401, detail=err.message)

    async def _check_token_expiracy(self) -> bool:
        jti = await self._get_jti()
        return await self._token_storage.check_expiration(jti=jti)

    async def _get_jti(self) -> str:
        return (await self._auth_jwt_service.get_raw_jwt())["jti"]

    # TODO: do refractoring for example decorator.
    async def login(
        self,
        *,
        login: str,
        password: str,
        user_agent: str,
    ) -> GenericResult[Token]:
        user = await self._user_service.get_user_by_login(login=login)
        if not user or not user.check_password(password):
            return GenericResult.failure(
                error=Error(
                    error_code="WrongUsernameOrPassword",
                    reason="Имя пользователя и / или пароль неверны",
                )
            )
        user_history = UserHistoryCreateDto(
            user_id=user.id,
            attempted=datetime.datetime.now(datetime.UTC),
            user_agent=user_agent,
            success=True,
        )
        _ = await self._user_service.insert_user_login(
            user_id=user.id, history_row=user_history
        )
        return GenericResult.success(await self._generate_token(user_id=str(user.id)))

    async def logout(self) -> None:
        await self.require_auth()
        access_jti = (await self._auth_jwt_service.get_raw_jwt())["jti"]
        token_jti = TokenJti(access_token_jti=access_jti, refresh_token_jti=None)
        return await self._token_storage.store_token(token=token_jti)

    async def refresh(self, access_jti: str) -> Token:
        await self._refresh_token_required()
        refresh_jti = await self._get_jti()
        token_jti = TokenJti(access_jti=access_jti, refresh_jti=refresh_jti)
        await self._token_storage.store_token(token_jti=token_jti)
        user_subject = await self._auth_jwt_service.get_jwt_subject()
        return await self._generate_token(user_id=user_subject)

    async def require_auth(self):
        try:
            await self._auth_jwt_service.jwt_required()
            if await self._check_token_expiracy():
                raise HTTPException(status_code=401, detail="Unathorized")
        except JWTDecodeError as err:
            raise HTTPException(status_code=401, detail=err.message)

    async def optional_auth(self):
        return await self._auth_jwt_service.jwt_optional()

    async def get_user(self) -> User | None:
        await self.require_auth()
        user_subject = await self._auth_jwt_service.get_jwt_subject()
        user: GenericResult[User] = await self._user_service.get_user(
            user_id=user_subject
        )
        return user.response


def require_roles(roles: list[str]):
    def auth_decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            auth_service = kwargs["auth_service"]
            current_user: User = await auth_service.get_user()
            if not current_user:
                raise HTTPException(status_code=401, detail="Unauthorized")
            for role in current_user.roles:
                if role.name in roles:
                    return await func(*args, **kwargs)
            raise HTTPException(status_code=403, detail="User have not access")

        return wrapper

    return auth_decorator