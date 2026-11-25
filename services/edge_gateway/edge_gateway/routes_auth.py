"""Identity: register, login, refresh (rotating), logout, me, data export & account erasure."""
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field, field_validator

from agency_platform.agents.sessions import SessionStore
from agency_platform.auth.accounts import AccountRepository, RefreshTokenStore
from agency_platform.auth.deps import CurrentUser, get_account_directory, get_token_service
from agency_platform.auth.passwords import MAX_PASSWORD_BYTES
from agency_platform.auth.tokens import TokenService
from agency_platform.billing.usage import RateLimiter
from agency_platform.cache import get_redis
from agency_platform.config.registry import plans
from agency_platform.config.settings import get_settings
from agency_platform.db import get_db
from agency_platform.errors import UnauthorizedError
from agency_platform.service import client_ip, openapi
from edge_gateway.routes_images import image_store
from edge_gateway.schemas import AccountExport, Me, TokenPair

router = APIRouter()

_LOGIN_WINDOW_S = 15 * 60


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=10)

    @field_validator("password")
    @classmethod
    def _bcrypt_limit(cls, v: str) -> str:
        if len(v.encode()) > MAX_PASSWORD_BYTES:
            raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
        return v


class PasswordChange(BaseModel):
    current_password: str = Field(..., max_length=256)
    new_password: str = Field(..., min_length=10)

    @field_validator("new_password")
    @classmethod
    def _bcrypt_limit(cls, v: str) -> str:
        if len(v.encode()) > MAX_PASSWORD_BYTES:
            raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
        return v


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=256)


class RefreshBody(BaseModel):
    refresh_token: str = Field(..., min_length=20, max_length=200)


def accounts() -> AccountRepository:
    return AccountRepository(get_db())


def refresh_store() -> RefreshTokenStore:
    return RefreshTokenStore(get_db(), get_settings().refresh_token_ttl_days)


def limiter() -> RateLimiter:
    return RateLimiter(get_redis())


Tokens = Annotated[TokenService, Depends(get_token_service)]
Accounts = Annotated[AccountRepository, Depends(accounts)]
Refresh = Annotated[RefreshTokenStore, Depends(refresh_store)]
Limiter = Annotated[RateLimiter, Depends(limiter)]


async def _pair(user_id: str, email: str, role: str, plan: str, tokens: TokenService, refresh: RefreshTokenStore) -> TokenPair:
    return TokenPair(
        access_token=tokens.issue_access(user_id, email, role),
        refresh_token=await refresh.issue(user_id),
        expires_in=tokens.access_ttl_seconds,
        plan=plan,
    )


AUTH, ACCOUNT = ["Auth"], ["Account"]


@router.post("/register", response_model=TokenPair, status_code=201, tags=AUTH, summary="Create an account",
             responses=openapi.errors(409, 422, 429))
async def register(body: Credentials, request: Request, repo: Accounts, tokens: Tokens, refresh: Refresh, rl: Limiter):
    """New accounts start on the signup plan from config/plans.yaml. Returns tokens right away."""
    s = get_settings()
    await rl.hit(f"register:{client_ip(request, s.trusted_proxy_count, s.client_ip_header)}", 5, 3600)
    account = await repo.create(body.email, body.password, plans().signup_plan)
    return await _pair(account.id, account.email, account.role, account.effective_plan, tokens, refresh)


@router.post("/login", response_model=TokenPair, tags=AUTH, summary="Log in",
             responses=openapi.errors(401, 422, 429))
async def login(body: LoginBody, request: Request, repo: Accounts, tokens: Tokens, refresh: Refresh, rl: Limiter):
    s = get_settings()
    ip = client_ip(request, s.trusted_proxy_count, s.client_ip_header)
    email_key = f"login:email:{body.email.lower()}"
    await rl.hit(f"login:ip:{ip}", 30, _LOGIN_WINDOW_S)
    await rl.hit(email_key, 10, _LOGIN_WINDOW_S)  # brute-force guard per account
    account = await repo.authenticate(body.email, body.password)
    await rl.reset(email_key, _LOGIN_WINDOW_S)
    return await _pair(account.id, account.email, account.role, account.effective_plan, tokens, refresh)


@router.post("/refresh", response_model=TokenPair, tags=AUTH, summary="Get a new token pair",
             responses=openapi.errors(401, 422))
async def refresh_tokens(body: RefreshBody, tokens: Tokens, refresh: Refresh):
    """Refresh tokens are single-use. Re-using an old one revokes the whole session (theft detection)."""
    user_id, new_refresh = await refresh.rotate(body.refresh_token)
    account = await get_account_directory().get(user_id)
    if account is None or not account.is_active:
        await refresh.revoke_all(user_id)
        raise UnauthorizedError("Account disabled")
    return TokenPair(
        access_token=tokens.issue_access(account.id, account.email, account.role),
        refresh_token=new_refresh,
        expires_in=tokens.access_ttl_seconds,
        plan=account.effective_plan,
    )


@router.post("/logout", status_code=204, tags=AUTH, summary="Log out (revoke the session)",
             responses=openapi.errors(422))
async def logout(body: RefreshBody, refresh: Refresh) -> None:
    await refresh.revoke(body.refresh_token)


@router.get("/me", tags=ACCOUNT, summary="My account and plan", responses=openapi.AUTHED)
async def me(user: CurrentUser) -> Me:
    plan = user.plan_config
    return Me(
        id=user.id,
        email=user.email,
        role=user.role,
        plan=user.plan,
        plan_display_name=plan.display_name,
        entitled_topics=plan.topics,
        max_model_tier=plan.max_tier,
    )


@router.put("/me/password", status_code=204, tags=ACCOUNT, summary="Change my password (logs out every session)",
            responses={**openapi.AUTHED, **openapi.errors(422, 429)})
async def change_password(body: PasswordChange, user: CurrentUser, repo: Accounts, refresh: Refresh, rl: Limiter) -> None:
    """Needs the current password. Every refresh token is revoked: other devices must log in again."""
    await rl.hit(f"password:{user.id}", 5, 3600)
    await repo.authenticate(user.email, body.current_password)  # re-auth
    await repo.set_password(user.id, body.new_password)
    await refresh.revoke_all(user.id)


class EraseBody(BaseModel):
    password: str = Field(..., max_length=256, description="Re-authentication for a destructive action")
    confirm: bool = Field(..., description="Must be true")


@router.get("/me/export", tags=ACCOUNT, summary="Download all my data (account, runs, usage)",
            responses=openapi.AUTHED)
async def export_me(user: CurrentUser, repo: Accounts, rl: Limiter) -> AccountExport:
    await rl.hit(f"export:{user.id}", 5, 3600)
    return await repo.export(user.id)


@router.delete("/me", status_code=204, tags=ACCOUNT, summary="Permanently delete my account and personal data",
               responses={**openapi.AUTHED, **openapi.errors(422)})
async def erase_me(body: EraseBody, user: CurrentUser, repo: Accounts, refresh: Refresh, request: Request) -> None:
    if not body.confirm:
        raise UnauthorizedError("Confirmation required")
    await repo.authenticate(user.email, body.password)  # re-auth before a destructive action
    await refresh.revoke_all(user.id)
    await repo.erase(user.id)
    await SessionStore(get_redis()).forget_user(user.id)
    await image_store(request).delete_user(user.id)   # pictures the user sent (bucket + rows)
    await get_account_directory().invalidate(user.id)
