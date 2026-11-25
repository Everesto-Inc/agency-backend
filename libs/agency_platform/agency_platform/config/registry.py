"""Process-wide access to the shared parameter files and prompts."""
from functools import lru_cache

from agency_platform.config.params import ParamFile
from agency_platform.config.prompts import PromptStore
from agency_platform.config.schemas import EvalsFile, GuardrailsFile, PlansFile, PrivacyFile, TopicsFile
from agency_platform.config.settings import get_settings


@lru_cache
def plans_file() -> ParamFile[PlansFile]:
    return ParamFile(get_settings().config_dir / "plans.yaml", PlansFile)


@lru_cache
def topics_file() -> ParamFile[TopicsFile]:
    return ParamFile(get_settings().config_dir / "topics.yaml", TopicsFile)


@lru_cache
def prompt_store() -> PromptStore:
    return PromptStore(get_settings().prompts_dir)


def plans() -> PlansFile:
    return plans_file().get()


def topics() -> TopicsFile:
    return topics_file().get()


@lru_cache
def privacy_file() -> ParamFile[PrivacyFile]:
    return ParamFile(get_settings().config_dir / "privacy.yaml", PrivacyFile)


@lru_cache
def evals_file() -> ParamFile[EvalsFile]:
    return ParamFile(get_settings().config_dir / "evals.yaml", EvalsFile)


def privacy() -> PrivacyFile:
    return privacy_file().get()


def evals() -> EvalsFile:
    return evals_file().get()


@lru_cache
def guardrails_file() -> ParamFile[GuardrailsFile]:
    return ParamFile(get_settings().config_dir / "guardrails.yaml", GuardrailsFile)


def guardrails() -> GuardrailsFile:
    return guardrails_file().get()
