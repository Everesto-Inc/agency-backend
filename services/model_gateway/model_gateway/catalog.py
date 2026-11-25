"""Schema for config/models.yaml — lives in agency_platform (Admin → Settings edits and checks it too)."""
from agency_platform.config.models_schema import (  # noqa: F401 — re-exported for the gateway's modules
    Canary,
    ClassifierConfig,
    JudgeConfig,
    LLMCacheConfig,
    ModelsFile,
    ModelSpec,
    RoutingConfig,
)
