"""Model gateway service entrypoint."""
import litellm
from fastapi import FastAPI

from agency_platform.billing.usage import UsageMeter
from agency_platform.cache import get_redis
from agency_platform.config.registry import models_file, plans, privacy, prompt_store, topics
from agency_platform.config.settings import get_settings
from agency_platform.db import get_db
from agency_platform.repositories import LlmCallRepository
from agency_platform.service import ServiceSpec, create_service_app
from model_gateway.api import GatewayState, router
from model_gateway.classifier import ComplexityClassifier
from model_gateway.executor import Executor
from model_gateway.llm_cache import LLMCache
from model_gateway.policy import RoutingPolicy
from model_gateway.recorder import CallRecorder
from model_gateway.settings import GatewaySettings

settings = get_settings(GatewaySettings)


async def startup(app: FastAPI) -> None:
    models = models_file()   # shared ParamFile: Admin → Settings overrides + drafts apply
    models.get()  # fail fast on an invalid catalog
    plans()
    topics()
    privacy()
    secrets = settings.client_secrets()
    if not secrets:
        raise RuntimeError("MODEL_GATEWAY_CLIENTS is empty: no service could call the gateway")

    if settings.langsmith_tracing and settings.langsmith_api_key:
        litellm.callbacks = ["langsmith"]  # every call, from every framework, traced

    meter = UsageMeter(get_redis())
    recorder = CallRecorder(LlmCallRepository(get_db()), meter, settings.recorder_batch_size, settings.recorder_flush_interval_s)
    recorder.start()
    app.state.recorder = recorder
    app.state.gateway = GatewayState(
        models=models,
        policy=RoutingPolicy(ComplexityClassifier(get_redis())),
        executor=Executor(settings, recorder, LLMCache(get_redis())),
        meter=meter,
        prompts=prompt_store(),
        client_secrets=secrets,
    )


async def shutdown(app: FastAPI) -> None:
    await app.state.recorder.stop()


app = create_service_app(
    settings,
    ServiceSpec(
        title="Model Gateway (internal)",
        description=(
            "The one router every agent uses for LLM calls (OpenAI-compatible). Callers ask for "
            "`auto`, `tier-<low|mid|high>` or `task-<name>`, never a model; the gateway picks the model, "
            "falls back within the tier, records tokens + cost per topic, and enforces plan ceilings. "
            "Cluster-internal: never routed by the public ingress."
        ),
        tags=[
            {"name": "OpenAI-compatible", "description": "Drop-in for any OpenAI client (base_url = this service)."},
            {"name": "Routing", "description": "Inspect routing decisions without calling a model."},
        ],
        routers=[(router, "/v1")],
        on_startup=[startup],
        on_shutdown=[shutdown],
    ),
)
