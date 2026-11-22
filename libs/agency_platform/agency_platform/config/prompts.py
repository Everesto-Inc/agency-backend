"""Versioned prompts-as-config (prompts/<topic>.yaml) with A/B experiments.

Every prompt carries the file's `version` (and, under an experiment, the
variant name), attached to each LLM call's tracking record -- so latency, cost,
error rate, user feedback and judge scores are comparable per prompt version
and per variant, not just per model.

File shape:
    version: "2.1"
    prompts:
      synthesize:
        system: |
          ...
        temperature: 0.1
    experiments:
      synthesize:                  # A/B test on the 'synthesize' prompt
        enabled: true
        variants:
          control: {weight: 50}    # = prompts.synthesize as written
          concise:                 # overrides any PromptSpec field
            weight: 50
            system: |
              ...

Assignment is deterministic per subject (user id): sticky across requests and
replicas, no assignment store needed.
"""
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from agency_platform.config.params import ParamFile

CONTROL = "control"


class PromptSpec(BaseModel):
    system: str
    user_template: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class VariantSpec(BaseModel):
    weight: int = Field(..., ge=0, le=100)
    system: str | None = None
    user_template: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class Experiment(BaseModel):
    enabled: bool = True
    variants: dict[str, VariantSpec]

    @model_validator(mode="after")
    def _weights(self) -> "Experiment":
        if sum(v.weight for v in self.variants.values()) != 100:
            raise ValueError("experiment variant weights must sum to 100")
        return self


class PromptFile(BaseModel):
    version: str
    prompts: dict[str, PromptSpec] = Field(default_factory=dict)
    experiments: dict[str, Experiment] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _experiments_target_prompts(self) -> "PromptFile":
        for name in self.experiments:
            if name not in self.prompts:
                raise ValueError(f"experiment on unknown prompt '{name}'")
        return self


@dataclass(frozen=True)
class Prompt:
    topic: str
    name: str
    version: str
    spec: PromptSpec
    variant: str | None = None

    @property
    def system(self) -> str:
        return self.spec.system

    def render_user(self, **values: Any) -> str:
        if self.spec.user_template is None:
            raise ValueError(f"prompt {self.topic}/{self.name} has no user_template")
        return self.spec.user_template.format(**values)

    @property
    def ref(self) -> str:
        """Identifier recorded with each LLM call: 'ev_research/synthesize@2.1' or '...@2.1~concise'."""
        base = f"{self.topic}/{self.name}@{self.version}"
        return f"{base}~{self.variant}" if self.variant else base


def bucket(*parts: str) -> int:
    """Stable 0-99 bucket for deterministic traffic splitting."""
    return int(hashlib.sha256(":".join(parts).encode()).hexdigest()[:8], 16) % 100


def _pick_variant(exp: Experiment, topic: str, name: str, subject: str) -> str:
    b = bucket(topic, name, subject)
    acc = 0
    for vname, v in exp.variants.items():
        acc += v.weight
        if b < acc:
            return vname
    return CONTROL


class PromptStore:
    def __init__(self, prompts_dir: Path) -> None:
        self._dir = prompts_dir
        self._files: dict[str, ParamFile[PromptFile]] = {}

    def _file(self, topic: str) -> PromptFile:
        if topic not in self._files:
            self._files[topic] = ParamFile(self._dir / f"{topic}.yaml", PromptFile)
        return self._files[topic].get()

    def get(self, topic: str, name: str, subject: str | None = None) -> Prompt:
        pf = self._file(topic)
        if name not in pf.prompts:
            raise KeyError(f"prompt '{name}' not defined in {topic}.yaml")
        spec = pf.prompts[name]
        exp = pf.experiments.get(name)
        if exp is None or not exp.enabled or subject is None:
            return Prompt(topic=topic, name=name, version=pf.version, spec=spec)
        variant = _pick_variant(exp, topic, name, subject)
        overrides = exp.variants[variant].model_dump(exclude={"weight"}, exclude_none=True) if variant in exp.variants else {}
        return Prompt(topic=topic, name=name, version=pf.version,
                      spec=spec.model_copy(update=overrides), variant=variant)

    def fingerprint(self, topic: str, subject: str | None) -> str:
        """Everything that changes a topic's prompts for this subject: version +
        assigned variants. Part of response-cache keys, so a prompt edit or a
        different experiment arm never serves a stale cached answer."""
        try:
            pf = self._file(topic)
        except Exception:
            return "none"
        arms = sorted(
            f"{n}={_pick_variant(e, topic, n, subject)}"
            for n, e in pf.experiments.items() if e.enabled and subject is not None
        )
        content = hashlib.sha256(pf.model_dump_json().encode()).hexdigest()[:12]  # catches un-bumped edits
        return f"{pf.version}|{content}|{','.join(arms)}"
