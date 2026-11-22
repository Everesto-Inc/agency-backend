"""LangSmith wiring. LangGraph/LangChain pick these env vars up natively; the
model gateway additionally logs every LLM call (from any framework) to LangSmith
via litellm's callback, so CrewAI and AutoGen calls are traced uniformly."""
import logging
import os

from agency_platform.config.settings import PlatformSettings

logger = logging.getLogger(__name__)


def init_tracing(settings: PlatformSettings) -> bool:
    if not (settings.langsmith_tracing and settings.langsmith_api_key):
        os.environ["LANGSMITH_TRACING"] = "false"
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = f"{settings.langsmith_project}-{settings.environment}"
    logger.info("langsmith tracing enabled", extra={"project": os.environ["LANGSMITH_PROJECT"]})
    return True
