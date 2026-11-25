"""FastAPI dependencies for authentication and subscription entitlement."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agency_platform.auth.accounts import AccountDirectory, AccountRepository
from agency_platform.auth.tokens import TokenService
from agency_platform.cache import get_redis
from agency_platform.config.drafts import HEADER as DRAFT_HEADER
from agency_platform.config.drafts import activate, parse_header
from agency_platform.config.registry import plans
from agency_platform.config.schemas import PlanConfig
from agency_platform.config.settings import get_settings
from agency_platform.db import get_db
from agency_platform.errors import ForbiddenError, SubscriptionRequiredError, UnauthorizedError
from agency_platform.observability.logging import user_id_var


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str
    role: str
    plan: str  # effective plan (expired subscriptions already downgraded)

    @property
    def plan_config(self) -> PlanConfig:
        return plans().get(self.plan)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def require_topic(self, topic: str) -> None:
        if not self.plan_config.allows_topic(topic):
            raise SubscriptionRequiredError(
                f"Your '{self.plan}' plan does not include '{topic}'. Upgrade to access it.",
                topic=topic,
                plan=self.plan,
            )


@lru_cache
def get_token_service() -> TokenService:
    return TokenService(get_settings())


def get_account_directory() -> AccountDirectory:
    return AccountDirectory(AccountRepository(get_db()), get_redis())


async def authenticate_token(
    token: str, tokens: TokenService, directory: AccountDirectory
) -> AuthenticatedUser:
    claims = tokens.decode_access(token)
    account = await directory.get(claims.user_id)
    if account is None or not account.is_active:
        raise UnauthorizedError("Account not found or disabled")
    user_id_var.set(account.id)
    return AuthenticatedUser(id=account.id, email=account.email, role=account.role, plan=account.effective_plan)


# Declared security scheme -> one "Authorize" button in Swagger UI. auto_error=False so a
# missing token goes through our own 401 envelope instead of FastAPI's default 403.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="Access token from POST /api/v1/auth/login (or /register). Expires after 15 minutes; "
                "use /api/v1/auth/refresh for a new one.",
)


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
    tokens: TokenService = Depends(get_token_service),
    directory: AccountDirectory = Depends(get_account_directory),
) -> AuthenticatedUser:
    """Bearer-header auth only. Tokens in query strings leak into logs/proxies.
    A config draft header (testing a config) is honoured for admin / service accounts only."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Missing bearer token")
    user = await authenticate_token(credentials.credentials, tokens, directory)
    if (draft_id := parse_header(request.headers.get(DRAFT_HEADER))) is not None:
        if user.role not in ("admin", "service"):
            raise ForbiddenError("Config drafts can only be tested by admins and service accounts")
        if await activate(draft_id) is None:
            raise ForbiddenError(f"Config draft {draft_id} not found")
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> AuthenticatedUser:
    if not user.is_admin:
        raise ForbiddenError("Admin role required")
    return user


AdminUser = Annotated[AuthenticatedUser, Depends(require_admin)]
