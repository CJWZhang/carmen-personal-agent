from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class SlackAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthResult:
    team_id: str
    team_name: str
    enterprise_id: str
    bot_user_id: str
    bot_token: str
    user_id: str
    user_token: str
    scopes: str


class SlackAPI:
    def __init__(self, *, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def _post(
        self,
        endpoint: str,
        *,
        fields: dict[str, Any],
        token: str = "",
    ) -> dict[str, Any]:
        body = urllib.parse.urlencode(
            {
                key: json.dumps(value) if isinstance(value, (dict, list)) else value
                for key, value in fields.items()
                if value is not None
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "carmen-assistant/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise SlackAPIError(f"Slack request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            error = payload.get("error", "unexpected_response") if isinstance(payload, dict) else "unexpected_response"
            raise SlackAPIError(f"Slack API returned {error}.")
        return payload

    def exchange_oauth_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
    ) -> OAuthResult:
        payload = self._post(
            "https://slack.com/api/oauth.v2.access",
            fields={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        team = payload.get("team") or {}
        enterprise = payload.get("enterprise") or {}
        authed_user = payload.get("authed_user") or {}
        return OAuthResult(
            team_id=str(team.get("id", "")),
            team_name=str(team.get("name", "")),
            enterprise_id=str(enterprise.get("id", "")),
            bot_user_id=str(payload.get("bot_user_id", "")),
            bot_token=str(payload.get("access_token", "")),
            user_id=str(authed_user.get("id", "")),
            user_token=str(authed_user.get("access_token", "")),
            scopes=str(payload.get("scope", "")),
        )

    def call(
        self,
        method: str,
        *,
        token: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        return self._post(
            f"https://slack.com/api/{method}",
            fields=fields,
            token=token,
        )

    def recent_context(
        self,
        *,
        token: str,
        channel_id: str,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        payload = self.call(
            "conversations.history",
            token=token,
            fields={"channel": channel_id, "limit": limit},
        )
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            return []
        result: list[dict[str, str]] = []
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            text = str(message.get("text", "")).strip()
            if text:
                result.append(
                    {
                        "user": str(message.get("user", "")),
                        "text": text[:2_000],
                        "ts": str(message.get("ts", "")),
                    }
                )
        return result

    def thread_context(
        self,
        *,
        token: str,
        channel_id: str,
        thread_id: str,
        limit: int = 100,
    ) -> list[dict[str, str]]:
        payload = self.call(
            "conversations.replies",
            token=token,
            fields={"channel": channel_id, "ts": thread_id, "limit": limit},
        )
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            return []
        return [
            {
                "user": str(message.get("user", "")),
                "text": str(message.get("text", ""))[:2_000],
                "ts": str(message.get("ts", "")),
            }
            for message in messages
            if isinstance(message, dict) and str(message.get("text", "")).strip()
        ]

    def permalink(self, *, token: str, channel_id: str, message_id: str) -> str:
        payload = self.call(
            "chat.getPermalink",
            token=token,
            fields={"channel": channel_id, "message_ts": message_id},
        )
        return str(payload.get("permalink", ""))

    def post_ephemeral(
        self,
        *,
        token: str,
        channel_id: str,
        user_id: str,
        text: str,
    ) -> None:
        self.call(
            "chat.postEphemeral",
            token=token,
            fields={
                "channel": channel_id,
                "user": user_id,
                "text": text[:3_000],
            },
        )

    def post_dm(
        self, *, token: str, user_id: str, text: str,
        client_msg_id: str = ""
    ) -> None:
        opened = self.call(
            "conversations.open",
            token=token,
            fields={"users": user_id},
        )
        channel = opened.get("channel") or {}
        channel_id = str(channel.get("id", ""))
        if not channel_id:
            raise SlackAPIError("Slack did not return a DM channel.")
        self.call(
            "chat.postMessage",
            token=token,
            fields={
                "channel": channel_id,
                "text": text[:3_000],
                "client_msg_id": client_msg_id or None,
            },
        )
