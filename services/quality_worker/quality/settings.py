from pathlib import Path

from pydantic import SecretStr

from agency_platform.config.settings import PlatformSettings, get_settings


class QualitySettings(PlatformSettings):
    service_name: str = "quality-worker"
    edge_url: str = "http://edge-gateway:8000"
    # Service account (plan 'internal') used to run eval cases and own judge calls.
    # Create it once: `python -m edge_gateway.cli create-service-user <email>`.
    eval_user_email: str = "evals@agency.internal"
    eval_user_password: SecretStr | None = None
    evals_dir: Path = Path("evals")
    git_sha: str | None = None
    alert_webhook_url: SecretStr | None = None


def settings() -> QualitySettings:
    return get_settings(QualitySettings)
