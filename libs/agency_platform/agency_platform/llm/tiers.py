"""Model tiers: the shared vocabulary between agents, plans and the model gateway.

Agents never name concrete models. They ask for a *task* ("intent_parsing",
"reasoning", ...) or a *tier*, and the gateway maps that to a model via
config/models.yaml. Swapping Gemini for something else is then a config edit.
"""
from enum import StrEnum


class Tier(StrEnum):
    LOW = "low"  # cheap/fast: classification, extraction, parsing
    MID = "mid"  # balanced: synthesis, tool use
    HIGH = "high"  # strongest reasoning: planning, multi-step analysis

    @property
    def rank(self) -> int:
        return _ORDER.index(self)

    def escalate(self, steps: int = 1) -> "Tier":
        return _ORDER[min(self.rank + steps, len(_ORDER) - 1)]

    def clamp(self, ceiling: "Tier") -> "Tier":
        return self if self.rank <= ceiling.rank else ceiling


_ORDER = [Tier.LOW, Tier.MID, Tier.HIGH]

# Model aliases understood by the gateway's OpenAI-compatible API.
AUTO_ALIAS = "auto"
TIER_PREFIX = "tier-"
TASK_PREFIX = "task-"


def tier_alias(tier: Tier) -> str:
    return f"{TIER_PREFIX}{tier.value}"


def task_alias(task: str) -> str:
    return f"{TASK_PREFIX}{task}"
