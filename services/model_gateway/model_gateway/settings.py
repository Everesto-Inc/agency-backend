from pydantic import SecretStr

from agency_platform.config.settings import PlatformSettings


class GatewaySettings(PlatformSettings):
    service_name: str = "model-gateway"
    max_request_body_bytes: int = 2 * 1024 * 1024  # agent prompts can carry crawled pages

    # Provider keys: the ONLY service that holds them. Gemini is the only provider;
    # adding one = a key field here + its entry in provider_key() + models in config/models.yaml.
    gemini_api_key: SecretStr | None = None

    # "client_id=secret,client_id=secret" -- one secret per calling service.
    model_gateway_clients: SecretStr = SecretStr("")

    recorder_batch_size: int = 200
    recorder_flush_interval_s: float = 1.0

    def provider_key(self, provider: str) -> str | None:
        secret = {
            "gemini": self.gemini_api_key,
        }.get(provider)
        return secret.get_secret_value() if secret else None

    def client_secrets(self) -> dict[str, str]:
        pairs = [p.strip() for p in self.model_gateway_clients.get_secret_value().split(",") if p.strip()]
        result: dict[str, str] = {}
        for pair in pairs:
            client_id, sep, secret = pair.partition("=")
            if not sep or not client_id or len(secret) < 32:
                raise ValueError(f"Invalid MODEL_GATEWAY_CLIENTS entry for '{client_id}' (secret must be >= 32 chars)")
            result[client_id.strip()] = secret.strip()
        return result
