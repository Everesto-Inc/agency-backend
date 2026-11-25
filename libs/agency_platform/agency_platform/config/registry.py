"""Process-wide access to the shared parameter files and prompts."""
from functools import lru_cache

from agency_platform.config.models_schema import ModelsFile
from agency_platform.config.params import ParamFile
from agency_platform.config.prompts import PromptStore
from agency_platform.config.schemas import EvalsFile, GuardrailsFile, MemoryFile, PlansFile, PrivacyFile, TopicsFile
from agency_platform.config.settings import get_settings


def _named(f: ParamFile, section: str) -> ParamFile:
    f.section = section   # Admin → Settings section (overrides + per-request drafts)
    return f


@lru_cache
def plans_file() -> ParamFile[PlansFile]:
    return _named(ParamFile(get_settings().config_dir / "plans.yaml", PlansFile), "plans")


@lru_cache
def topics_file() -> ParamFile[TopicsFile]:
    return _named(ParamFile(get_settings().config_dir / "topics.yaml", TopicsFile), "topics")


@lru_cache
def prompt_store() -> PromptStore:
    return PromptStore(get_settings().prompts_dir)


def plans() -> PlansFile:
    return plans_file().get()


def topics() -> TopicsFile:
    return topics_file().get()


@lru_cache
def privacy_file() -> ParamFile[PrivacyFile]:
    return _named(ParamFile(get_settings().config_dir / "privacy.yaml", PrivacyFile), "privacy")


@lru_cache
def evals_file() -> ParamFile[EvalsFile]:
    return _named(ParamFile(get_settings().config_dir / "evals.yaml", EvalsFile), "evals")


def privacy() -> PrivacyFile:
    return privacy_file().get()


def evals() -> EvalsFile:
    return evals_file().get()


@lru_cache
def guardrails_file() -> ParamFile[GuardrailsFile]:
    return _named(ParamFile(get_settings().config_dir / "guardrails.yaml", GuardrailsFile), "guardrails")


def guardrails() -> GuardrailsFile:
    return guardrails_file().get()


@lru_cache
def memory_file() -> ParamFile[MemoryFile]:
    return _named(ParamFile(get_settings().config_dir / "memory.yaml", MemoryFile), "memory")


def memory() -> MemoryFile:
    return memory_file().get()


@lru_cache
def models_file() -> ParamFile[ModelsFile]:
    return _named(ParamFile(get_settings().config_dir / "models.yaml", ModelsFile), "models")
