from __future__ import annotations

import time
from dataclasses import asdict
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from carmen_agent.actions.extractor import action_from_extraction, extract_action
from carmen_agent.actions.notifications import render_action, render_digest
from carmen_agent.actions.ranking import should_interrupt
from carmen_agent.actions.resolver import deterministic_resolution, semantic_resolution
from carmen_agent.connectors.base import SourceEvent, SourceMessage
from carmen_agent.hosted.agent import draft_reply
from carmen_agent.hosted.crypto import TokenCipher
from carmen_agent.hosted.slack_api import SlackAPI, SlackAPIError
from carmen_agent.hosted.storage import HostedStore, Job
from carmen_agent.infra.definitions import HOSTED_AGENTS
from carmen_agent.infra.durable import DurableStepRunner, DurableToolExecutor
from carmen_agent.infra.hosted_tools import (
    SlackPermalink,
    SlackPostAssistantDM,
    SlackRecentContext,
    SlackThreadContext,
)
from carmen_agent.infra.runtime import AttributedSystemActor, RunContext, ToolRuntime
from carmen_agent.infra.tools import Tool, stable_idempotency_key
from carmen_agent.providers import ModelProvider


class HostedWorker:
    def __init__(
        self,
        *,
        store: HostedStore,
        cipher: TokenCipher,
        slack: SlackAPI,
        provider: ModelProvider,
    ) -> None:
        self.store = store
        self.cipher = cipher
        self.slack = slack
        self.provider = provider

    def run_once(self) -> bool:
        job = self.store.claim_job()
        if job is None:
            return False
        context = self._context(job)
        try:
            definition = HOSTED_AGENTS.resolve(job.kind)
            config = definition.build_config(context)
            self.store.start_agent_run(
                context=context,
                agent_name=definition.name,
                agent_version=definition.version,
                config={
                    "model": asdict(config.model),
                    "tools": [tool.name for tool in config.tools],
                    "skills": list(config.skills),
                    "max_iterations": config.max_iterations,
                },
            )
            DurableStepRunner(self.store, job.run_id).step(
                "execute",
                lambda: self._execute_job(job),
                idempotency_key=stable_idempotency_key(
                    job.run_id, "execute", job.kind
                ),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            retrying = job.attempts < 3
            self.store.finish_agent_run(job.run_id, error=error, retrying=retrying)
            self.store.finish_job(job.id, error=error, retry=retrying)
        else:
            self.store.finish_agent_run(job.run_id)
            self.store.finish_job(job.id)
        return True

    def _execute_job(self, job: Job) -> dict[str, bool]:
        if job.kind == "draft":
            self._draft(job)
        elif job.kind == "daily_digest":
            self._daily_digest(job)
        elif job.kind == "extract_action":
            self._extract_action(job)
        elif job.kind == "reconcile":
            self._reconcile(job)
        else:
            raise ValueError(f"Unknown job kind {job.kind!r}.")
        return {"completed": True}

    def _context(self, job: Job) -> RunContext:
        return RunContext(
            run_id=job.run_id,
            tenant_id=job.team_id,
            user_id=job.user_id,
            actor=AttributedSystemActor(service="hosted_worker", user_id=job.user_id),
            surface=job.kind,
            thread_id=str(job.payload.get("thread_id") or "") or None,
            permissions=frozenset({"slack.read", "slack.assistant_dm"}),
        )

    def _run_tool(
        self, job: Job, tool: Tool, *, suffix: str
    ):
        self.store.heartbeat_job(job.id)
        key = stable_idempotency_key(job.run_id, f"execute:{suffix}", tool.name)
        runtime = ToolRuntime(
            context=self._context(job),
            tool_call_id="call-" + key[:24],
            idempotency_key=key,
            services={"slack": self.slack},
        )
        return DurableToolExecutor(self.store, "execute").execute(tool, runtime)

    def run_forever(self, *, poll_seconds: float = 2.0) -> None:
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)

    def _tokens(self, job: Job) -> tuple[str, str]:
        installation = self.store.get_installation(job.team_id)
        if installation is None:
            raise ValueError("Slack installation no longer exists.")
        user = self.store.get_user(job.team_id, job.user_id)
        if user is None:
            raise ValueError("Connected Slack user no longer exists.")
        bot_token = self.cipher.decrypt(installation.bot_token_encrypted)
        user_token = (
            self.cipher.decrypt(user.user_token_encrypted)
            if user.user_token_encrypted
            else ""
        )
        return bot_token, user_token

    def _draft(self, job: Job) -> None:
        bot_token, user_token = self._tokens(job)
        channel_id = str(job.payload.get("channel_id", ""))
        request = str(job.payload.get("request", ""))
        context: list[dict[str, str]] = []
        context_token = user_token or bot_token
        if channel_id:
            try:
                context = self._run_tool(
                    job,
                    SlackRecentContext(
                        token=context_token,
                        channel_id=channel_id,
                    ),
                    suffix="draft-context",
                )
            except SlackAPIError:
                # A draft can still be generated from the explicit request and
                # authored style examples when channel history is unavailable.
                context = []
        examples = self.store.recent_authored_messages(
            team_id=job.team_id,
            user_id=job.user_id,
            limit=40,
        )
        body = draft_reply(
            self.provider,
            request=request,
            context=context,
            authored_examples=examples,
        )
        draft_id = self.store.save_draft(
            team_id=job.team_id,
            user_id=job.user_id,
            channel_id=channel_id,
            body=body,
            draft_id="draft-" + job.run_id[:24],
        )
        self._run_tool(
            job,
            SlackPostAssistantDM(
                token=bot_token,
                user_id=job.user_id,
                text=(
                f"*Private draft suggestion* · `{draft_id}`\n\n{body}\n\n"
                "Feedback: `/draft-feedback " + draft_id + " accepted|modified|rejected`"
                ),
                client_msg_id="msg-" + stable_idempotency_key(
                    job.run_id, "draft-delivery", "slack_dm"
                )[:28],
            ),
            suffix="draft-delivery",
        )

    def _daily_digest(self, job: Job) -> None:
        bot_token, _ = self._tokens(job)
        actions = self.store.list_actions(team_id=job.team_id, user_id=job.user_id)
        evidence = {
            action.id: item
            for action in actions
            if (item := self.store.latest_action_evidence(action.id)) is not None
        }
        digest = render_digest(actions, evidence)
        self._run_tool(
            job,
            SlackPostAssistantDM(
                token=bot_token,
                user_id=job.user_id,
                text=f"*Your daily commitments*\n\n{digest}",
                client_msg_id="msg-" + stable_idempotency_key(
                    job.run_id, "digest-delivery", "slack_dm"
                )[:28],
            ),
            suffix="digest-delivery",
        )

    def _extract_action(self, job: Job) -> None:
        _, user_token = self._tokens(job)
        payload = job.payload
        permalink = ""
        context: list[SourceMessage] = []
        channel_id = str(payload.get("channel_id", ""))
        thread_id = str(payload.get("thread_id", ""))
        if user_token and channel_id:
            try:
                permalink = self._run_tool(
                    job,
                    SlackPermalink(
                        token=user_token,
                        channel_id=channel_id,
                        message_id=str(payload.get("source_id", "")),
                    ),
                    suffix="evidence-permalink",
                )
                if thread_id:
                    raw_context = self._run_tool(
                        job,
                        SlackThreadContext(
                            token=user_token,
                            channel_id=channel_id,
                            thread_id=thread_id,
                            limit=30,
                        ),
                        suffix="extraction-thread",
                    )
                    context = [
                        SourceMessage(
                            id=str(item.get("ts", "")),
                            actor_id=str(item.get("user", "")),
                            text=str(item.get("text", "")),
                            occurred_at=str(item.get("ts", "")),
                        )
                        for item in raw_context
                    ]
            except (SlackAPIError, AttributeError):
                permalink = ""
                context = []
        event = SourceEvent(
            source="slack",
            tenant_id=job.team_id,
            target_user_id=job.user_id,
            source_id=str(payload.get("source_id", "")),
            actor_id=str(payload.get("actor_id", "")),
            text=str(payload.get("text", "")),
            channel_id=channel_id,
            thread_id=thread_id,
            permalink=permalink,
            occurred_at=str(payload.get("occurred_at", "")),
            direct=bool(payload.get("direct")),
            context=tuple(context),
        )
        extracted = action_from_extraction(event, extract_action(self.provider, event))
        if extracted is not None:
            self.store.save_action(*extracted)

    def _reconcile(self, job: Job) -> None:
        _, user_token = self._tokens(job)
        if not user_token:
            return
        for action in self.store.list_actions(
            team_id=job.team_id, user_id=job.user_id, include_candidates=False
        ):
            self.store.heartbeat_job(job.id)
            if action.status == "waiting" and action.snoozed_until:
                try:
                    snoozed_until = datetime.fromisoformat(
                        action.snoozed_until.replace("Z", "+00:00")
                    )
                    if snoozed_until.tzinfo is None:
                        snoozed_until = snoozed_until.replace(tzinfo=timezone.utc)
                except ValueError:
                    snoozed_until = None
                if snoozed_until is not None and snoozed_until <= datetime.now(timezone.utc):
                    self.store.transition_action(
                        action_id=action.id,
                        status="open",
                        reason="Snooze expired.",
                    )
                    action = replace(action, status="open", snoozed_until="")
            if action.source != "slack" or not action.channel_id or not action.thread_id:
                continue
            try:
                raw = self._run_tool(
                    job,
                    SlackThreadContext(
                        token=user_token,
                        channel_id=action.channel_id,
                        thread_id=action.thread_id,
                    ),
                    suffix=f"reconcile-{action.id}",
                )
            except (SlackAPIError, AttributeError):
                continue
            messages = [
                SourceMessage(
                    id=str(item.get("ts", "")),
                    actor_id=str(item.get("user", "")),
                    text=str(item.get("text", "")),
                )
                for item in raw
            ]
            resolution = deterministic_resolution(action, messages)
            if resolution is None:
                resolution = semantic_resolution(self.provider, action, messages)
            if resolution is not None:
                self.store.transition_action(
                    action_id=action.id,
                    status=resolution.status,
                    reason=resolution.reason,
                    evidence_source_id=resolution.evidence_id,
                )
                continue
            if should_interrupt(action):
                policy_key = "urgent:" + (action.due_at[:10] or action.source_id)
                if not self.store.notification_was_delivered(
                    action_id=action.id, channel="slack_dm", policy_key=policy_key
                ):
                    evidence = self.store.latest_action_evidence(action.id)
                    bot_token, _ = self._tokens(job)
                    self._run_tool(
                        job,
                        SlackPostAssistantDM(
                            token=bot_token,
                            user_id=job.user_id,
                            text="*Time-sensitive follow-up*\n\n"
                            + render_action(action, evidence),
                            client_msg_id="msg-" + stable_idempotency_key(
                                job.run_id,
                                f"urgent-{action.id}-{policy_key}",
                                "slack_dm",
                            )[:28],
                        ),
                        suffix=f"urgent-{action.id}-{policy_key}",
                    )
                    self.store.claim_notification(
                        action_id=action.id,
                        channel="slack_dm",
                        policy_key=policy_key,
                    )


def enqueue_due_daily_digests(
    store: HostedStore,
    *,
    now: datetime | None = None,
) -> int:
    current = now or datetime.now(timezone.utc)
    count = 0
    for user in store.list_users():
        try:
            zone = ZoneInfo(user.timezone)
        except ZoneInfoNotFoundError:
            zone = timezone.utc
        local = current.astimezone(zone)
        if local.hour != user.daily_hour:
            continue
        if store.enqueue_daily_digest_once(
            team_id=user.team_id,
            user_id=user.user_id,
            local_date=local.date().isoformat(),
        ):
            count += 1
    return count


def enqueue_reconciliation(
    store: HostedStore,
    *,
    now: datetime | None = None,
) -> int:
    current = now or datetime.now(timezone.utc)
    window = current.strftime("%Y-%m-%dT%H")
    count = 0
    for user in store.list_users():
        if store.get_source_cursor(
            team_id=user.team_id, user_id=user.user_id, source="reconcile"
        ) == window:
            continue
        store.enqueue_job(
            team_id=user.team_id,
            user_id=user.user_id,
            kind="reconcile",
            payload={"window": window},
        )
        store.set_source_cursor(
            team_id=user.team_id,
            user_id=user.user_id,
            source="reconcile",
            cursor=window,
        )
        count += 1
    return count
