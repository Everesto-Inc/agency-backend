"""User access tokens (JWT).

Production: EdDSA (Ed25519). Only the edge gateway holds the private key and
can mint tokens; every other service verifies with the public key, so a
compromised topic service cannot forge identities. Dev may fall back to HS256.

Access tokens are short-lived and deliberately do NOT carry the plan: the
subscription is resolved fresh from the account directory on each request, so
upgrades, downgrades and cancellations take effect immediately.
"""
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from agency_platform.config.settings import PlatformSettings
from agency_platform.errors import UnauthorizedError

_LEEWAY_S = 10


@dataclass(frozen=True)
class AccessClaims:
    user_id: str
    email: str
    role: str
    jti: str


class TokenService:
    def __init__(self, settings: PlatformSettings) -> None:
        self._issuer = settings.jwt_issuer
        self._audience = settings.jwt_audience
        self._ttl = timedelta(minutes=settings.access_token_ttl_minutes)
        if settings.jwt_public_key is not None:
            self._alg = "EdDSA"
            self._verify_key: str = settings.jwt_public_key.get_secret_value()
            self._sign_key = settings.jwt_private_key.get_secret_value() if settings.jwt_private_key else None
        elif settings.jwt_secret_key is not None and settings.is_dev:
            self._alg = "HS256"
            self._verify_key = settings.jwt_secret_key.get_secret_value()
            self._sign_key = self._verify_key
        else:
            raise RuntimeError("No JWT key configured (JWT_PUBLIC_KEY, or JWT_SECRET_KEY in dev)")

    @property
    def can_issue(self) -> bool:
        return self._sign_key is not None

    @property
    def access_ttl_seconds(self) -> int:
        return int(self._ttl.total_seconds())

    def issue_access(self, user_id: str, email: str, role: str) -> str:
        if self._sign_key is None:
            raise RuntimeError("This service holds no signing key and cannot issue tokens")
        now = datetime.now(UTC)
        payload = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": user_id,
            "email": email,
            "role": role,
            "typ": "access",
            "jti": uuid.uuid4().hex,
            "iat": now,
            "nbf": now,
            "exp": now + self._ttl,
        }
        return jwt.encode(payload, self._sign_key, algorithm=self._alg)

    def decode_access(self, token: str) -> AccessClaims:
        try:
            payload = jwt.decode(
                token,
                self._verify_key,
                algorithms=[self._alg],  # pinned: never trust the token's own alg header
                audience=self._audience,
                issuer=self._issuer,
                leeway=_LEEWAY_S,
                options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]},
            )
        except jwt.PyJWTError:
            raise UnauthorizedError("Invalid or expired token") from None
        if payload.get("typ") != "access":
            raise UnauthorizedError("Invalid token type")
        return AccessClaims(
            user_id=payload["sub"], email=payload.get("email", ""), role=payload.get("role", "user"), jti=payload["jti"]
        )
