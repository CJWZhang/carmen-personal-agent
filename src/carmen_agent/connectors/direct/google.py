from __future__ import annotations

import base64
import os
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from typing import Any

from carmen_agent.connectors.direct.base import (
    DirectConnectorError,
    DirectSyncResult,
    JsonTransport,
    UrllibJsonTransport,
)
from carmen_agent.skills.schemas import InboxItem


class GoogleTokenProvider:
    def __init__(self, transport: JsonTransport) -> None:
        self.transport = transport
        self._access_token = ""

    def available(self) -> bool:
        return bool(
            os.environ.get("CARMEN_GOOGLE_ACCESS_TOKEN")
            or (
                os.environ.get("CARMEN_GOOGLE_CLIENT_ID")
                and os.environ.get("CARMEN_GOOGLE_CLIENT_SECRET")
                and os.environ.get("CARMEN_GOOGLE_REFRESH_TOKEN")
            )
        )

    def token(self) -> str:
        explicit = os.environ.get("CARMEN_GOOGLE_ACCESS_TOKEN", "").strip()
        if explicit:
            return explicit
        if self._access_token:
            return self._access_token
        client_id = os.environ.get("CARMEN_GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("CARMEN_GOOGLE_CLIENT_SECRET", "").strip()
        refresh_token = os.environ.get("CARMEN_GOOGLE_REFRESH_TOKEN", "").strip()
        if not all((client_id, client_secret, refresh_token)):
            raise DirectConnectorError("Google direct connector is not authorized.")
        payload = self.transport.request(
            "https://oauth2.googleapis.com/token",
            method="POST",
            form={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        self._access_token = str(payload.get("access_token") or "")
        if not self._access_token:
            raise DirectConnectorError("Google did not return an access token.")
        return self._access_token


def _header(message: dict[str, Any], name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    for item in headers if isinstance(headers, list) else []:
        if isinstance(item, dict) and str(item.get("name", "")).casefold() == name.casefold():
            try:
                return str(make_header(decode_header(str(item.get("value", "")))))
            except (LookupError, UnicodeError):
                return str(item.get("value", ""))
    return ""


def _decode_body(part: object) -> str:
    if not isinstance(part, dict):
        return ""
    mime = str(part.get("mimeType") or "")
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    encoded = str(body.get("data") or "")
    if encoded and mime.startswith("text/plain"):
        try:
            return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode(
                "utf-8", errors="replace"
            )
        except (ValueError, UnicodeError):
            return ""
    for child in part.get("parts", []) if isinstance(part.get("parts"), list) else []:
        decoded = _decode_body(child)
        if decoded:
            return decoded
    return ""


class GmailDirectConnector:
    source = "gmail"

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or UrllibJsonTransport()
        self.tokens = GoogleTokenProvider(self.transport)

    def available(self) -> bool:
        return self.tokens.available()

    def sync(self, *, cursor: str = "", hours: int = 36) -> DirectSyncResult:
        started = time.monotonic()
        deadline = started + 55.0
        token = self.tokens.token()
        headers = {"Authorization": f"Bearer {token}"}
        query = f"in:inbox newer_than:{max(1, (hours + 23) // 24)}d -in:spam -in:trash -category:promotions"
        listing = self.transport.request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            query={"q": query, "maxResults": 50},
        )
        items: list[InboxItem] = []
        candidates = listing.get("messages", [])
        for candidate in candidates[:50] if isinstance(candidates, list) else []:
            if time.monotonic() >= deadline:
                break
            if not isinstance(candidate, dict) or not candidate.get("id"):
                continue
            message = self.transport.request(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{candidate['id']}",
                headers=headers,
                query={"format": "full"},
            )
            message_id = str(message.get("id") or "")
            if not message_id:
                continue
            internal_ms = str(message.get("internalDate") or "")
            occurred = ""
            if internal_ms.isdigit():
                occurred = datetime.fromtimestamp(
                    int(internal_ms) / 1000, tz=timezone.utc
                ).isoformat()
            labels = {str(label).upper() for label in message.get("labelIds", [])}
            body = _decode_body(message.get("payload")) or str(message.get("snippet") or "")
            items.append(InboxItem(
                source="gmail",
                id=message_id,
                sender=_header(message, "From")[:512],
                subject=_header(message, "Subject")[:2_000],
                body=body.strip()[:20_000],
                url=f"https://mail.google.com/mail/#all/{message_id}",
                ts=occurred,
                thread_id=str(message.get("threadId") or message_id),
                kind="email",
                status="open",
                metadata={
                    "conversation_id": str(message.get("threadId") or message_id),
                    "latest_activity_at": occurred,
                    "user_replied_after_ask": "SENT" in labels,
                },
            ))
        next_cursor = cursor
        if time.monotonic() < deadline:
            profile = self.transport.request(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers
            )
            next_cursor = str(profile.get("historyId") or cursor)
        return DirectSyncResult(
            source="gmail", items=tuple(items), cursor=next_cursor,
            detail=(
                f"Read {len(items)} of {len(candidates) if isinstance(candidates, list) else 0} "
                "bounded recent inbox candidate(s) directly from Gmail."
            ),
        )


class CalendarDirectConnector:
    source = "calendar"

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or UrllibJsonTransport()
        self.tokens = GoogleTokenProvider(self.transport)

    def available(self) -> bool:
        return self.tokens.available()

    def sync(self, *, cursor: str = "", hours: int = 36) -> DirectSyncResult:
        token = self.tokens.token()
        headers = {"Authorization": f"Bearer {token}"}
        now = datetime.now(timezone.utc)
        listing = self.transport.request(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers=headers,
            query={
                "timeMin": now.isoformat(),
                "timeMax": (now + timedelta(days=3)).isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 250,
            },
        )
        items: list[InboxItem] = []
        for event in listing.get("items", []):
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("id") or "")
            title = str(event.get("summary") or "").strip()
            if not event_id or not title:
                continue
            attendees = event.get("attendees") if isinstance(event.get("attendees"), list) else []
            self_attendee = next(
                (attendee for attendee in attendees if isinstance(attendee, dict) and attendee.get("self")),
                {},
            )
            response = str(self_attendee.get("responseStatus") or "")
            cancelled = str(event.get("status") or "").lower() == "cancelled"
            start = event.get("start") if isinstance(event.get("start"), dict) else {}
            occurred = str(start.get("dateTime") or start.get("date") or "")
            organizer = event.get("organizer") if isinstance(event.get("organizer"), dict) else {}
            updated = str(event.get("updated") or occurred)
            items.append(InboxItem(
                source="calendar",
                id=event_id,
                sender=str(organizer.get("displayName") or organizer.get("email") or "")[:512],
                subject=title[:2_000],
                body=str(event.get("description") or "")[:20_000],
                url=str(event.get("htmlLink") or "")[:4_096],
                ts=occurred,
                kind="event_decision" if response in {"needsAction", "tentative"} else "meeting_prep",
                status="done" if cancelled or response == "declined" else "open",
                participants=[
                    str(attendee.get("displayName") or attendee.get("email") or "")[:256]
                    for attendee in attendees[:100]
                    if isinstance(attendee, dict)
                ],
                related_keys=[f"calendar-event:{event_id}"],
                metadata={
                    "response_status": response,
                    "organizer": str(organizer.get("email") or "")[:512],
                    "latest_activity_at": updated,
                    "deadline_at": occurred,
                    "expires_at": occurred,
                    "cancelled": cancelled,
                    "needs_decision": response in {"needsAction", "tentative"},
                    "series_id": str(event.get("recurringEventId") or "")[:512],
                    "optional": bool(self_attendee.get("optional")),
                },
            ))
        return DirectSyncResult(
            source="calendar", items=tuple(items),
            cursor=str(listing.get("nextSyncToken") or cursor),
            detail=f"Read {len(items)} upcoming event(s) directly from Google Calendar.",
        )


__all__ = ["CalendarDirectConnector", "GmailDirectConnector", "GoogleTokenProvider"]
