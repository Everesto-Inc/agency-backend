"""Ping every enabled model in config/models.yaml with a 1-token request.

    docker compose exec model-gateway python -m model_gateway.models_check

Validates model ids and provider keys after editing the catalog -- catches a
typo'd or retired model before agents hit it in production.
"""
import asyncio
import sys
import time

import litellm

from agency_platform.config.registry import models_file
from model_gateway.settings import GatewaySettings


async def main() -> int:
    settings = GatewaySettings()
    models = models_file().get()
    failures = 0
    for model_id, spec in models.models.items():
        if not spec.enabled:
            print(f"SKIP  {model_id:<20} disabled")
            continue
        key = settings.provider_key(spec.provider)
        if not key:
            print(f"SKIP  {model_id:<20} no {spec.provider} API key")
            continue
        start = time.perf_counter()
        try:
            await litellm.acompletion(model=spec.litellm_model, api_key=key, max_tokens=1, timeout=30,
                                      messages=[{"role": "user", "content": "ping"}])
            print(f"OK    {model_id:<20} {spec.litellm_model} ({(time.perf_counter() - start) * 1000:.0f} ms)")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {model_id:<20} {spec.litellm_model}: {type(exc).__name__}: {str(exc)[:160]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
