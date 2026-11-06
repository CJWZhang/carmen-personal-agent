from __future__ import annotations

import json
import secrets
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from carmen_agent.actions.notifications import render_action
from carmen_agent.connectors.slack import normalize_slack_event, target_user_id
from carmen_agent.hosted.config import ConfigurationError, HostedSettings
from carmen_agent.hosted.crypto import FernetTokenCipher, TokenCipher
from carmen_agent.hosted.security import verify_slack_signature
from carmen_agent.hosted.slack_api import SlackAPI, SlackAPIError
from carmen_agent.hosted.storage import (
    HostedStore,
    HostedUser,
    Installation,
    build_store,
)

StartResponse = Callable[[str, list[tuple[str, str]]], Any]


def _response(
    start_response: StartResponse,
    status: str,
    body: str | bytes,
    *,
    content_type: str = "application/json; charset=utf-8",
    headers: list[tuple[str, str]] | None = None,
) -> Iterable[bytes]:
    encoded = body.encode("utf-8") if isinstance(body, str) else body
    response_headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(encoded))),
        ("Cache-Control", "no-store"),
    ]
    response_headers.extend(headers or [])
    start_response(status, response_headers)
    return [encoded]


def _json_response(
    start_response: StartResponse,
    status: str,
    payload: dict[str, Any],
) -> Iterable[bytes]:
    return _response(
        start_response,
        status,
        json.dumps(payload, ensure_ascii=False),
    )


def _read_body(environ: dict[str, Any], *, maximum: int = 1_000_000) -> bytes:
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError as exc:
        raise ValueError("Invalid content length.") from exc
    if length < 0 or length > maximum:
        raise ValueError("Request body is too large.")
    return environ["wsgi.input"].read(length)


class HostedApplication:
    def __init__(
        self,
        *,
        settings: HostedSettings,
        store: HostedStore,
        cipher: TokenCipher,
        slack: SlackAPI,
    ) -> None:
        self.settings = settings
        self.store = store
        self.cipher = cipher
        self.slack = slack

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        try:
            if method == "GET" and path == "/healthz":
                return _json_response(start_response, "200 OK", {"status": "ok"})
            if method == "GET" and path == "/slack/install":
                return self._install(start_response)
            if method == "GET" and path == "/slack/oauth/callback":
                return self._oauth_callback(environ, start_response)
            if method == "POST" and path in {"/slack/commands", "/slack/events"}:
                body = _read_body(environ)
                if not self._verified(environ, body):
                    return _json_response(
                        start_response,
                        "401 Unauthorized",
                        {"error": "invalid_signature"},
                    )
                if path == "/slack/commands":
                    return self._command(body, start_response)
                return self._event(body, start_response)
            return _json_response(start_response, "404 Not Found", {"error": "not_found"})
        except (ValueError, json.JSONDecodeError):
            return _json_response(start_response, "400 Bad Request", {"error": "bad_request"})
        except SlackAPIError:
            return _json_response(
                start_response,
                "502 Bad Gateway",
                {"error": "slack_request_failed"},
            )
        except Exception:
            # Never serialize tokens, request bodies, or provider details to the client.
            return _json_response(
                start_response,
                "500 Internal Server Error",
                {"error": "internal_error"},
            )

    def _verified(self, environ: dict[str, Any], body: bytes) -> bool:
        return verify_slack_signature(
            signing_secret=self.settings.slack_signing_secret,
            timestamp=str(environ.get("HTTP_X_SLACK_REQUEST_TIMESTAMP", "")),
            signature=str(environ.get("HTTP_X_SLACK_SIGNATURE", "")),
            body=body,
        )

    def _install(self, start_response: StartResponse) -> Iterable[bytes]:
        state = secrets.token_urlsafe(32)
        self.store.save_oauth_state(state)
        query = urllib.parse.urlencode(
            {
                "client_id": self.settings.slack_client_id,
                "scope": ",".join(self.settings.bot_scopes),
                "user_scope": ",".join(self.settings.user_scopes),
                "redirect_uri": self.settings.redirect_uri,
                "state": state,
            }
        )
        location = "https://slack.com/oauth/v2/authorize?" + query
        return _response(
            start_response,
            "302 Found",
            b"",
            headers=[("Location", location)],
        )

    def _oauth_callback(
        self,
        environ: dict[str, Any],
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        query = urllib.parse.parse_qs(str(environ.get("QUERY_STRING", "")))
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code or not state or not self.store.consume_oauth_state(state):
            return _response(
                start_response,
                "400 Bad Request",
                "Slack installation could not be verified.",
                content_type="text/plain; charset=utf-8",
            )
        result = self.slack.exchange_oauth_code(
            client_id=self.settings.slack_client_id,
            client_secret=self.settings.slack_client_secret,
            code=code,
            redirect_uri=self.settings.redirect_uri,
        )
        if not result.team_id or not result.user_id or not result.bot_token:
            raise SlackAPIError("Slack OAuth response omitted required installation data.")
        self.store.save_installation(
            Installation(
                team_id=result.team_id,
                team_name=result.team_name,
                enterprise_id=result.enterprise_id,
                bot_user_id=result.bot_user_id,
                bot_token_encrypted=self.cipher.encrypt(result.bot_token),
                user_token_encrypted="",
                installing_user_id=result.user_id,
                scopes=result.scopes,
            )
        )
        self.store.save_user(
            HostedUser(
                team_id=result.team_id,
                user_id=result.user_id,
                user_token_encrypted=(
                    self.cipher.encrypt(result.user_token) if result.user_token else ""
                ),
                scopes=",".join(self.settings.user_scopes),
            )
        )
        return _response(
            start_response,
            "200 OK",
            "Carmen Assistant is connected. You can close this window.",
            content_type="text/plain; charset=utf-8",
        )

    def _command(
        self,
        body: bytes,
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        form = urllib.parse.parse_qs(body.decode("utf-8"))
        command = (form.get("command") or [""])[0]
        team_id = (form.get("team_id") or [""])[0]
        user_id = (form.get("user_id") or [""])[0]
        channel_id = (form.get("channel_id") or [""])[0]
        request = (form.get("text") or [""])[0].strip()
        supported = {
            "/draft", "/catchup", "/done", "/snooze", "/why",
            "/not-a-task", "/not-mine", "/handled", "/wrong-priority",
            "/draft-feedback", "/confirm",
        }
        if command not in supported:
            return _json_response(
                start_response,
                "200 OK",
                {"response_type": "ephemeral", "text": "Unsupported command."},
            )
        if (
            not team_id
            or not user_id
            or not channel_id
            or self.store.get_user(team_id, user_id) is None
        ):
            return _json_response(
                start_response,
                "200 OK",
                {
                    "response_type": "ephemeral",
                    "text": "Connect Carmen Assistant before using Carmen commands.",
                },
            )
        if command == "/draft":
            self.store.enqueue_job(
                team_id=team_id,
                user_id=user_id,
                kind="draft",
                payload={"channel_id": channel_id, "request": request[:2_000]},
            )
            message = "Drafting a private suggestion…"
        elif command == "/catchup":
            self.store.enqueue_job(
                team_id=team_id,
                user_id=user_id,
                kind="daily_digest",
                payload={"requested": True},
            )
            message = "Building your private catch-up from open, evidenced actions…"
        elif command == "/draft-feedback":
            draft_id, _, feedback = request.partition(" ")
            outcome, _, edited_body = feedback.partition(" ")
            if not self.store.record_draft_feedback(
                team_id=team_id, user_id=user_id, draft_id=draft_id,
                outcome=outcome, edited_body=edited_body
            ):
                message = "Draft feedback must be: `/draft-feedback <id> accepted|modified|rejected [edited text]`."
            else:
                message = f"Recorded `{outcome}` feedback for draft `{draft_id}`."
        else:
            selector, _, remainder = request.partition(" ")
            action = self.store.get_action(
                team_id=team_id, user_id=user_id, selector=selector
            ) if selector else None
            if action is None:
                return _json_response(
                    start_response,
                    "200 OK",
                    {"response_type": "ephemeral", "text": "Action not found. Use `/catchup` to see current action IDs."},
                )
            if command == "/why":
                evidence = self.store.latest_action_evidence(action.id)
                message = render_action(action, evidence)
            elif command == "/snooze":
                try:
                    minutes = max(1, min(10_080, int(remainder or "60")))
                except ValueError:
                    minutes = 60
                until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
                self.store.transition_action(
                    action_id=action.id,
                    status="waiting",
                    reason=f"Snoozed by user for {minutes} minutes.",
                    snoozed_until=until,
                )
                self.store.record_action_feedback(action_id=action.id, outcome="snoozed")
                message = f"Snoozed `{action.id}` for {minutes} minutes."
            else:
                transition = {
                    "/done": ("completed", "done", "Marked done by user."),
                    "/handled": ("responded", "already_handled", "Already handled by user."),
                    "/not-a-task": ("dismissed", "not_a_task", "Dismissed: not a task."),
                    "/not-mine": ("dismissed", "not_mine", "Dismissed: not assigned to user."),
                    "/wrong-priority": ("open", "wrong_priority", "Priority feedback recorded."),
                    "/confirm": ("open", "confirmed", "Candidate confirmed as an action."),
                }[command]
                self.store.transition_action(
                    action_id=action.id,
                    status=transition[0],  # type: ignore[arg-type]
                    reason=transition[2],
                )
                self.store.record_action_feedback(action_id=action.id, outcome=transition[1])
                message = f"Updated `{action.id}`: {transition[2]}"
        return _json_response(
            start_response,
            "200 OK",
            {
                "response_type": "ephemeral",
                "text": message,
            },
        )

    def _event(
        self,
        body: bytes,
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        payload = json.loads(body.decode("utf-8"))
        if payload.get("type") == "url_verification":
            challenge = str(payload.get("challenge", ""))
            return _response(
                start_response,
                "200 OK",
                challenge,
                content_type="text/plain; charset=utf-8",
            )
        team_id = str(payload.get("team_id", ""))
        event_id = str(payload.get("event_id", ""))
        if not team_id or not event_id:
            raise ValueError("Event omitted tenant identifiers.")
        if not self.store.record_event_once(event_id, team_id):
            return _json_response(start_response, "200 OK", {"ok": True})
        event = payload.get("event") or {}
        if not isinstance(event, dict):
            raise ValueError("Invalid event.")
        if event.get("type") == "app_uninstalled":
            self.store.delete_installation(team_id)
        elif (
            event.get("type") == "message"
            and not event.get("subtype")
            and not event.get("bot_id")
        ):
            actor_id = str(event.get("user", ""))
            installation = self.store.get_installation(team_id)
            fallback = installation.installing_user_id if installation else ""
            target_id = target_user_id(payload, fallback)
            target = self.store.get_user(team_id, target_id) if target_id else None
            normalized = normalize_slack_event(payload=payload, target_user=target_id)
            # Privacy invariant: persist only messages authored by an explicitly
            # connected user in the style corpus. Coworker text lives only in a
            # short-lived extraction job unless it yields bounded action evidence.
            if normalized is not None and target is not None and actor_id == target_id:
                self.store.save_authored_message(
                    team_id=team_id,
                    user_id=target_id,
                    message_id=normalized.source_id,
                    channel_id=normalized.channel_id,
                    text=normalized.text,
                    posted_at=normalized.occurred_at,
                )
            if normalized is not None and target is not None:
                self.store.enqueue_job(
                    team_id=team_id,
                    user_id=target_id,
                    kind="extract_action",
                    payload={
                        "source": normalized.source,
                        "source_id": normalized.source_id,
                        "actor_id": normalized.actor_id,
                        "text": normalized.text,
                        "channel_id": normalized.channel_id,
                        "thread_id": normalized.thread_id,
                        "occurred_at": normalized.occurred_at,
                        "direct": normalized.direct,
                    },
                )
        return _json_response(start_response, "200 OK", {"ok": True})


def build_application() -> HostedApplication:
    settings = HostedSettings.from_env()
    return HostedApplication(
        settings=settings,
        store=build_store(settings.database_url),
        cipher=FernetTokenCipher(settings.token_encryption_key),
        slack=SlackAPI(),
    )


_application: HostedApplication | None = None


def application(
    environ: dict[str, Any],
    start_response: StartResponse,
) -> Iterable[bytes]:
    global _application
    try:
        if _application is None:
            _application = build_application()
        return _application(environ, start_response)
    except ConfigurationError:
        return _json_response(
            start_response,
            "503 Service Unavailable",
            {"error": "service_not_configured"},
        )
