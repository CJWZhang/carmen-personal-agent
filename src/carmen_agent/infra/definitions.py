from __future__ import annotations

from carmen_agent.infra.agent import AgentRegistry, ModelConfig, StaticAgentDefinition
from carmen_agent.infra.hosted_tools import (
    SlackPermalink,
    SlackPostAssistantDM,
    SlackRecentContext,
    SlackThreadContext,
)


class DraftAgent(StaticAgentDefinition):
    name = "draft"
    version = 1
    model = ModelConfig(primary="configured-provider", temperature=0.55, max_tokens=240)
    system_prompt = (
        "Draft a private reply suggestion in the user's observed writing style. "
        "Style examples are not facts, and drafts must never be sent automatically."
    )
    tools = (SlackRecentContext, SlackPostAssistantDM)


class ActionExtractionAgent(StaticAgentDefinition):
    name = "extract_action"
    version = 1
    model = ModelConfig(primary="configured-provider", temperature=0.0, max_tokens=300)
    system_prompt = (
        "Extract only directly assigned asks and explicit user commitments into typed actions."
    )
    tools = (SlackPermalink, SlackThreadContext)


class DailyDigestAgent(StaticAgentDefinition):
    name = "daily_digest"
    version = 1
    model = ModelConfig(primary="none", temperature=0.0, max_tokens=0)
    system_prompt = "Render an evidence-backed private digest from the durable action ledger."
    tools = (SlackPostAssistantDM,)


class ReconciliationAgent(StaticAgentDefinition):
    name = "reconcile"
    version = 1
    model = ModelConfig(primary="configured-provider", temperature=0.0, max_tokens=180)
    system_prompt = (
        "Re-check open action sources, apply deterministic resolution first, and use semantic "
        "resolution only when it cites high-confidence user-authored evidence."
    )
    tools = (SlackThreadContext, SlackPostAssistantDM)


HOSTED_AGENTS = AgentRegistry(
    (DraftAgent, ActionExtractionAgent, DailyDigestAgent, ReconciliationAgent)
)
