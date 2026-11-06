from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from carmen_agent.connectors.direct.base import (
    DirectConnectorError, DirectSyncResult, JsonTransport, UrllibJsonTransport,
)
from carmen_agent.skills.schemas import InboxItem


def _slack_stamp(value: str) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


class SlackDirectConnector:
    source = "slack"

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or UrllibJsonTransport()

    def available(self) -> bool:
        return bool(os.environ.get("CARMEN_SLACK_USER_TOKEN"))

    def _call(self, method: str, *, query: dict[str, object] | None = None) -> dict[str, Any]:
        token = os.environ.get("CARMEN_SLACK_USER_TOKEN", "").strip()
        result = self.transport.request(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {token}"},
            query=query,
        )
        if result.get("ok") is not True:
            raise DirectConnectorError(
                f"Slack API {method} failed: {str(result.get('error') or 'unknown_error')[:200]}"
            )
        return result

    def sync(self, *, cursor: str = "", hours: int = 36) -> DirectSyncResult:
        profile = self._call("auth.test")
        user_id = str(profile.get("user_id") or "")
        after = (datetime.now(timezone.utc) - timedelta(hours=hours)).date().isoformat()
        response = self._call(
            "search.messages",
            query={"query": f"to:me after:{after}", "count": 100, "sort": "timestamp"},
        )
        matches = response.get("messages", {}).get("matches", [])
        items: list[InboxItem] = []
        for message in matches[:100] if isinstance(matches, list) else []:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("ts") or "")
            text = str(message.get("text") or "").strip()
            channel = message.get("channel") if isinstance(message.get("channel"), dict) else {}
            channel_id = str(channel.get("id") or "")
            if not message_id or not text or str(message.get("user_id") or "") == user_id:
                continue
            thread_id = str(message.get("thread_ts") or message_id)
            items.append(InboxItem(
                source="slack",
                id=message_id,
                sender=str(message.get("username") or message.get("user_name") or "")[:512],
                subject=str(channel.get("name") or "Direct message")[:2_000],
                body=text[:20_000],
                url=str(message.get("permalink") or "")[:4_096],
                ts=_slack_stamp(message_id),
                channel=channel_id or str(channel.get("name") or "")[:512],
                thread_id=thread_id[:512],
                kind="message",
                status="open",
                related_keys=[f"slack-thread:{channel_id}:{thread_id}"],
                metadata={
                    "conversation_id": channel_id[:512],
                    "latest_activity_at": _slack_stamp(message_id),
                    "next_action": "reply",
                    "user_replied_after_ask": False,
                },
            ))
        return DirectSyncResult(
            source="slack", items=tuple(items), cursor=datetime.now(timezone.utc).isoformat(),
            detail=f"Read {len(items)} recent directed message(s) directly from Slack.",
        )

    def collect_authored_messages(
        self, *, days: int = 180, maximum: int = 500
    ) -> list[dict[str, str]]:
        """Collect only messages authored by the authenticated Slack user."""
        profile = self._call("auth.test")
        user_id = str(profile.get("user_id") or "")
        if not user_id:
            raise DirectConnectorError("Slack did not identify the authenticated user.")
        after = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        messages: list[dict[str, str]] = []
        seen: set[str] = set()
        for page in range(1, 6):
            response = self._call(
                "search.messages",
                query={
                    "query": f"from:me after:{after}", "count": 100,
                    "page": page, "sort": "timestamp", "sort_dir": "desc",
                },
            )
            container = response.get("messages")
            matches = container.get("matches", []) if isinstance(container, dict) else []
            if not isinstance(matches, list) or not matches:
                break
            for message in matches:
                if not isinstance(message, dict):
                    continue
                author = str(message.get("user_id") or "")
                if (
                    (author and author != user_id)
                    or message.get("bot_id")
                    or str(message.get("subtype") or "") in {
                        "bot_message", "message_changed", "message_deleted",
                    }
                ):
                    continue
                message_id = str(message.get("ts") or "")
                authored_lines = [
                    line for line in str(message.get("text") or "").splitlines()
                    if not line.lstrip().startswith((">", "&gt;"))
                ]
                text = "\n".join(authored_lines).strip()
                if not message_id or not text or message_id in seen:
                    continue
                channel = message.get("channel") if isinstance(message.get("channel"), dict) else {}
                channel_type = (
                    "dm" if channel.get("is_im") else
                    "group_dm" if channel.get("is_mpim") else "channel"
                )
                seen.add(message_id)
                messages.append({
                    "id": message_id[:512],
                    "channel_type": channel_type,
                    "timestamp": _slack_stamp(message_id),
                    "text": text[:4_000],
                })
                if len(messages) >= max(1, min(maximum, 500)):
                    return messages
            paging = container.get("paging") if isinstance(container, dict) else {}
            if not isinstance(paging, dict) or page >= int(paging.get("pages") or page):
                break
        return messages


__all__ = ["SlackDirectConnector"]
