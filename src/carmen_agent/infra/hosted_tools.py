from __future__ import annotations

from dataclasses import dataclass

from carmen_agent.infra.runtime import ToolRuntime
from carmen_agent.infra.tools import Capability, Tool, ToolPolicy, ToolScope

SLACK_READ = ToolPolicy(
    scopes=(ToolScope.USER, ToolScope.TENANT),
    read_only=True,
    capabilities=frozenset({Capability.READ_PRIVATE_DATA, Capability.EXTERNAL_EGRESS}),
    required_permissions=frozenset({"slack.read"}),
)

SLACK_ASSISTANT_DM = ToolPolicy(
    scopes=(ToolScope.USER, ToolScope.TENANT),
    read_only=False,
    capabilities=frozenset({Capability.EXTERNAL_EGRESS, Capability.EXTERNAL_WRITE}),
    required_permissions=frozenset({"slack.assistant_dm"}),
    # This writes only as the assistant into its private DM. Sending as the user
    # remains a separate explicit-approval tool/action adapter.
    approval="none",
)


@dataclass(frozen=True)
class SlackRecentContext(Tool):
    name = "slack_recent_context"
    description = "Read bounded recent Slack context visible to the connected user."
    policy = SLACK_READ

    token: str
    channel_id: str
    limit: int = 10

    def run(self, runtime: ToolRuntime):
        return runtime.service("slack").recent_context(
            token=self.token, channel_id=self.channel_id, limit=self.limit
        )


@dataclass(frozen=True)
class SlackThreadContext(Tool):
    name = "slack_thread_context"
    description = "Read a bounded Slack thread for action resolution."
    policy = SLACK_READ

    token: str
    channel_id: str
    thread_id: str
    limit: int = 100

    def run(self, runtime: ToolRuntime):
        return runtime.service("slack").thread_context(
            token=self.token,
            channel_id=self.channel_id,
            thread_id=self.thread_id,
            limit=self.limit,
        )


@dataclass(frozen=True)
class SlackPermalink(Tool):
    name = "slack_permalink"
    description = "Resolve a stable Slack permalink for bounded action evidence."
    policy = SLACK_READ

    token: str
    channel_id: str
    message_id: str

    def run(self, runtime: ToolRuntime):
        return runtime.service("slack").permalink(
            token=self.token, channel_id=self.channel_id, message_id=self.message_id
        )


@dataclass(frozen=True)
class SlackPostAssistantDM(Tool):
    name = "slack_post_assistant_dm"
    description = "Post a private message as Carmen Assistant, never as the connected user."
    policy = SLACK_ASSISTANT_DM

    token: str
    user_id: str
    text: str
    client_msg_id: str = ""

    def run(self, runtime: ToolRuntime):
        runtime.service("slack").post_dm(
            token=self.token, user_id=self.user_id, text=self.text,
            client_msg_id=self.client_msg_id,
        )
        return {"delivered": True, "user_id": self.user_id}
