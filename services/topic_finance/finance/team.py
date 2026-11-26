"""The AutoGen team. Each agent gets its own model-gateway *task*, so one
conversation deliberately mixes tiers:

    MarketDataAnalyst   task-data_analysis       (mid)  tool calling
    Strategist          task-investment_analysis (high) the actual reasoning
    ComplianceReviewer  task-compliance_review   (low)  cheap yes/no gate

RoundRobin: analyst -> strategist -> compliance, repeating until compliance
says APPROVED or the message cap is hit.
"""
from collections.abc import Callable

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.models import ChatCompletionClient, ModelInfo
from autogen_ext.models.openai import OpenAIChatCompletionClient

from agency_platform.agents.base import RunContext
from finance.params import params
from finance.tools import FinanceTools

ANALYST, STRATEGIST, COMPLIANCE = "MarketDataAnalyst", "Strategist", "ComplianceReviewer"

# Our aliases aren't OpenAI model names, so AutoGen needs capabilities declared.
GATEWAY_MODEL_INFO = ModelInfo(
    vision=False, function_calling=True, json_output=True, structured_output=False,
    family="unknown", multiple_system_messages=False,
)

ClientFactory = Callable[[RunContext, str], ChatCompletionClient]


def gateway_client(ctx: RunContext, task: str) -> ChatCompletionClient:
    cfg = ctx.llm.openai_compat(task=task)
    return OpenAIChatCompletionClient(
        model=cfg.model, base_url=cfg.base_url, api_key=cfg.api_key, model_info=GATEWAY_MODEL_INFO,
        temperature=0.1, max_retries=1, timeout=120,
    )


def market_context() -> str:
    """Pure: the configured countries + default, appended to the analyst's instructions."""
    m = params().markets
    known = ", ".join(f"{c} ({v.currency})" for c, v in m.by_country.items())
    return (f"\n\nMarkets you can search by country: {known}. If the user names no country (in the question or "
            f"the conversation), use {m.default_country} and say so in your summary.")


def skill_file_tool(ctx: RunContext) -> Callable:
    async def read_skill_file(skill: str, path: str, offset: int = 0) -> dict:
        """Read a reference file of a skill in use (e.g. references/06-valuation.md). Long files come in parts:
        call again with offset = next_offset."""
        return await ctx.read_skill_file(skill, path, offset)
    return read_skill_file


def build_team(ctx: RunContext, tools: FinanceTools, client_factory: ClientFactory = gateway_client) -> RoundRobinGroupChat:
    skills = ctx.skill_instructions()           # '' unless triage picked a skill for this question
    analyst = AssistantAgent(
        ANALYST,
        model_client=client_factory(ctx, "data_analysis"),
        tools=tools.as_list() + ([skill_file_tool(ctx)] if ctx.skills else []),
        system_message=ctx.prompt("market_analyst").system + market_context() + skills,
        reflect_on_tool_use=True,
        max_tool_iterations=params().team.max_tool_iterations,
        description="Fetches market data with tools and reports the figures.",
    )
    strategist = AssistantAgent(
        STRATEGIST,
        model_client=client_factory(ctx, "investment_analysis"),
        system_message=ctx.prompt("strategist").system + skills,
        description="Writes the analysis using only reported figures.",
    )
    compliance = AssistantAgent(
        COMPLIANCE,
        model_client=client_factory(ctx, "compliance_review"),
        system_message=ctx.prompt("compliance").system,
        description="Approves or requests revision of the analysis.",
    )
    termination = TextMentionTermination("APPROVED", sources=[COMPLIANCE]) | MaxMessageTermination(
        params().team.max_messages
    )
    return RoundRobinGroupChat([analyst, strategist, compliance], termination_condition=termination)
