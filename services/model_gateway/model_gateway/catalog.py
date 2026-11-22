"""Schema for config/models.yaml: model catalog, tier candidates, routing policy."""
from pydantic import BaseModel, Field, model_validator

from agency_platform.llm.tiers import Tier


class ModelSpec(BaseModel):
    litellm_model: str
    provider: str
    tier: Tier
    enabled: bool = True
    capabilities: list[str] = Field(default_factory=list)
    timeout_s: float = 60
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None

    def supports(self, needs: set[str]) -> bool:
        return needs.issubset(self.capabilities)

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float | None:
        if self.input_cost_per_mtok is None or self.output_cost_per_mtok is None:
            return None
        return (prompt_tokens * self.input_cost_per_mtok + completion_tokens * self.output_cost_per_mtok) / 1e6


class JudgeConfig(BaseModel):
    enabled: bool = True
    cache_ttl_s: int = 3600
    max_input_chars: int = 3000


class ClassifierConfig(BaseModel):
    low_max_score: float = Field(0.30, ge=0, le=1)
    high_min_score: float = Field(0.65, ge=0, le=1)
    weights: dict[str, float]
    length_saturation_chars: int = 6000
    reasoning_terms: list[str] = Field(default_factory=list)
    llm_judge: JudgeConfig = JudgeConfig()

    @model_validator(mode="after")
    def _ordered(self) -> "ClassifierConfig":
        if self.low_max_score >= self.high_min_score:
            raise ValueError("low_max_score must be < high_min_score")
        return self


class RoutingConfig(BaseModel):
    default_tier: Tier = Tier.MID
    escalate_on_error: bool = True
    max_escalations: int = Field(1, ge=0, le=2)
    tasks: dict[str, Tier] = Field(default_factory=dict)
    classifier: ClassifierConfig


class Canary(BaseModel):
    """Send `percent`% of a tier's traffic to `model` first (sticky per run),
    to compare it against the incumbent on real traffic before promoting it."""
    tier: Tier
    model: str
    percent: int = Field(..., ge=0, le=100)
    enabled: bool = True


class LLMCacheConfig(BaseModel):
    """Exact-match cache for deterministic calls (temperature 0, no tools)."""
    enabled: bool = True
    ttl_s: int = Field(86400, ge=60)
    max_response_chars: int = 200_000


class ModelsFile(BaseModel):
    models: dict[str, ModelSpec]
    tiers: dict[Tier, list[str]]
    routing: RoutingConfig
    canaries: list[Canary] = Field(default_factory=list)
    cache: LLMCacheConfig = LLMCacheConfig()

    def canary_for(self, tier: Tier) -> Canary | None:
        return next((c for c in self.canaries if c.enabled and c.tier == tier and c.percent > 0), None)

    @model_validator(mode="after")
    def _consistent(self) -> "ModelsFile":
        for tier, ids in self.tiers.items():
            for model_id in ids:
                if model_id not in self.models:
                    raise ValueError(f"tier '{tier}' references unknown model '{model_id}'")
        for tier in Tier:
            if not self.tiers.get(tier):
                raise ValueError(f"tier '{tier}' has no candidate models")
        for c in self.canaries:
            if c.model not in self.models:
                raise ValueError(f"canary references unknown model '{c.model}'")
        return self
