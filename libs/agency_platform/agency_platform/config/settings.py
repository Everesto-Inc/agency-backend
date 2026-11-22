"""Process settings: secrets and environment wiring (read once at startup).

Three kinds of configuration, three homes -- never mixed:
  * Secrets / env wiring  -> environment variables (this module). Sourced from
    a local .env (scripts/gen_secrets.py), the Infisical Operator in Kubernetes.
  * Tunable parameters     -> config/*.yaml (agency_platform.config.params),
    hot-reloaded, mounted as ConfigMaps.
  * Prompts                -> prompts/*.yaml (agency_platform.config.prompts),
    versioned and hot-reloaded the same way.
"""
import base64
import binascii
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agency_platform.db.database import parse_url

Environment = Literal["dev", "staging", "prod"]

_INSECURE_DEFAULTS = {"", "change-me", "dev-secret-key", "dev-jwt-secret-change-me"}


class PlatformSettings(BaseSettings):
    """Settings every service needs. Services subclass this to add their own."""

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "agency-service"
    environment: Environment = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    # Parameters & prompts (mounted read-only; hot-reloaded)
    config_dir: Path = Path("config")
    prompts_dir: Path = Path("prompts")

    # Persistence
    # MySQL (local) / OCI HeatWave MySQL (prod):
    #   mysql://user:pass@host:3306/agency?ssl-mode=VERIFY_CA&ssl-ca=/etc/agency/db-ca.pem
    database_url: SecretStr = SecretStr("mysql://agency:localdev@localhost:3306/agency")
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    db_statement_timeout_ms: int = 30_000
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")

    # User auth. Prod uses asymmetric keys: only the edge gateway holds the
    # private key (can mint tokens); every other service only verifies.
    jwt_private_key: SecretStr | None = None  # PEM, EdDSA (edge gateway only)
    jwt_public_key: SecretStr | None = None  # PEM, EdDSA (all services)
    jwt_secret_key: SecretStr | None = None  # HS256 fallback, dev only
    jwt_issuer: str = "agency-edge"
    jwt_audience: str = "agency-api"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    # Model gateway client (topic services + edge). The secret signs short-lived
    # per-run call tokens; the gateway holds the matching secret per client id.
    model_gateway_url: str = "http://model-gateway:8000"
    model_gateway_client_secret: SecretStr | None = None

    # HTTP hardening
    cors_allowed_origins: str = ""
    expose_docs: bool = True
    max_request_body_bytes: int = 64 * 1024
    trusted_proxy_count: int = 1

    # Observability
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "autonomous-agency"
    otel_exporter_otlp_endpoint: str | None = None

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_dev(self) -> bool:
        return self.environment == "dev"

    @field_validator("jwt_private_key", "jwt_public_key", mode="before")
    @classmethod
    def _decode_pem(cls, value: object) -> object:
        """Accept PEM as-is or base64-encoded (single-line env vars / Infisical)."""
        if isinstance(value, str) and value and not value.lstrip().startswith("-----BEGIN"):
            try:
                return base64.b64decode(value, validate=True).decode()
            except (binascii.Error, UnicodeDecodeError):
                raise ValueError("JWT key must be PEM or base64-encoded PEM") from None
        return value

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> "PlatformSettings":
        """Fail fast at boot rather than run prod with dev-grade security."""
        if self.is_dev:
            return self
        problems: list[str] = []
        if self.jwt_public_key is None:
            problems.append("JWT_PUBLIC_KEY must be set outside dev (asymmetric tokens required)")
        if self.jwt_secret_key is not None:
            problems.append("JWT_SECRET_KEY (HS256) is dev-only; remove it outside dev")
        try:
            ssl_mode = parse_url(self.database_url.get_secret_value()).ssl_mode
        except ValueError as exc:
            problems.append(f"DATABASE_URL invalid: {exc}")
        else:
            if ssl_mode not in {"REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"}:
                problems.append("DATABASE_URL must use ssl-mode=REQUIRED|VERIFY_CA|VERIFY_IDENTITY outside dev")
        if "*" in self.cors_origins:
            problems.append("CORS_ALLOWED_ORIGINS must not contain '*' outside dev")
        secret = self.model_gateway_client_secret
        if secret is not None and secret.get_secret_value() in _INSECURE_DEFAULTS:
            problems.append("MODEL_GATEWAY_CLIENT_SECRET uses an insecure default")
        if problems:
            raise ValueError("Unsafe configuration: " + "; ".join(problems))
        return self


S = TypeVar("S", bound=PlatformSettings)


@lru_cache
def _cached(cls: type[PlatformSettings]) -> PlatformSettings:
    return cls()


def get_settings(cls: type[S] = PlatformSettings) -> S:  # type: ignore[assignment]
    """Cached settings instance for the given settings class."""
    return _cached(cls)  # type: ignore[return-value]
