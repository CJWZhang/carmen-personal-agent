from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from carmen_agent.skills.schemas import InboxItem, Source


class DirectConnectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class DirectSyncResult:
    source: Source
    items: tuple[InboxItem, ...]
    cursor: str = ""
    detail: str = ""


class JsonTransport(Protocol):
    def request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, object] | None = None,
        method: str = "GET",
        form: dict[str, str] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]: ...


class UrllibJsonTransport:
    """Small dependency-free HTTPS JSON transport with bounded responses."""

    max_response_bytes = 10 * 1024 * 1024

    def request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, object] | None = None,
        method: str = "GET",
        form: dict[str, str] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https":
            raise DirectConnectorError("Direct connectors require HTTPS endpoints.")
        if query:
            encoded_query = urllib.parse.urlencode(
                {key: value for key, value in query.items() if value not in (None, "")}
            )
            url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, encoded_query, parsed.fragment)
            )
        body = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2_000).decode("utf-8", errors="replace")
            raise DirectConnectorError(
                f"Provider returned HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DirectConnectorError(f"Provider request failed: {exc}") from exc
        if len(raw) > self.max_response_bytes:
            raise DirectConnectorError("Provider response exceeded its size limit.")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DirectConnectorError("Provider returned invalid JSON.") from exc
        if not isinstance(value, dict):
            raise DirectConnectorError("Provider returned an invalid response shape.")
        return value


class DirectConnector(Protocol):
    source: Source

    def available(self) -> bool: ...
    def sync(self, *, cursor: str = "", hours: int = 36) -> DirectSyncResult: ...


__all__ = [
    "DirectConnector", "DirectConnectorError", "DirectSyncResult", "JsonTransport",
    "UrllibJsonTransport",
]
