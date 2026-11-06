from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from carmen_agent.connectors.direct.base import DirectSyncResult, JsonTransport, UrllibJsonTransport
from carmen_agent.skills.schemas import InboxItem


class GitHubDirectConnector:
    source = "github"

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or UrllibJsonTransport()

    def available(self) -> bool:
        return bool(
            os.environ.get("CARMEN_GITHUB_TOKEN")
            and os.environ.get("CARMEN_GITHUB_USERNAME")
        )

    def sync(self, *, cursor: str = "", hours: int = 36) -> DirectSyncResult:
        token = os.environ.get("CARMEN_GITHUB_TOKEN", "").strip()
        username = os.environ.get("CARMEN_GITHUB_USERNAME", "").strip()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        candidates: dict[str, tuple[dict[str, Any], bool]] = {}
        for query, review_requested in (
            (f"is:pr is:open author:{username}", False),
            (f"is:pr is:open review-requested:{username}", True),
        ):
            response = self.transport.request(
                "https://api.github.com/search/issues",
                headers=headers,
                query={"q": query, "per_page": 100, "sort": "updated", "order": "desc"},
            )
            for item in response.get("items", []):
                if isinstance(item, dict) and item.get("html_url"):
                    candidates[str(item["html_url"])] = (item, review_requested)
        items: list[InboxItem] = []
        for url, (record, review_requested) in candidates.items():
            repository_url = str(record.get("repository_url") or "")
            repository = repository_url.rsplit("/repos/", 1)[-1] if "/repos/" in repository_url else ""
            number = str(record.get("number") or "")
            user = record.get("user") if isinstance(record.get("user"), dict) else {}
            updated = str(record.get("updated_at") or "")
            items.append(InboxItem(
                source="github",
                id=f"{repository}#{number}" if repository and number else url,
                sender=str(user.get("login") or "")[:512],
                subject=str(record.get("title") or "")[:2_000],
                body=str(record.get("body") or "")[:20_000],
                url=url[:4_096],
                ts=updated,
                kind="pull_request",
                status="open",
                related_keys=[f"github:{repository}:pr:{number}"] if repository and number else [],
                metadata={
                    "repository": repository[:512],
                    "number": number[:64],
                    "state": str(record.get("state") or "open")[:128],
                    "latest_activity_at": updated,
                    "review_requested": review_requested,
                    "assigned": False,
                    "is_draft": bool(record.get("draft")),
                    "next_action": "review" if review_requested else "await_review",
                    "reason": (
                        "A current review is explicitly requested from you."
                        if review_requested
                        else "This is one of your open pull requests."
                    ),
                },
            ))
        return DirectSyncResult(
            source="github", items=tuple(items), cursor=datetime.now(timezone.utc).isoformat(),
            detail=f"Read {len(items)} owned or review-requested pull request(s) directly from GitHub.",
        )


__all__ = ["GitHubDirectConnector"]
