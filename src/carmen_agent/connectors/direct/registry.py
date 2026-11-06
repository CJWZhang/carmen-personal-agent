from __future__ import annotations

from carmen_agent.connectors.direct.base import DirectConnector
from carmen_agent.connectors.direct.github import GitHubDirectConnector
from carmen_agent.connectors.direct.google import CalendarDirectConnector, GmailDirectConnector
from carmen_agent.connectors.direct.slack import SlackDirectConnector
from carmen_agent.skills.schemas import Source


class DirectConnectorRegistry:
    def __init__(self, connectors: tuple[DirectConnector, ...] | None = None) -> None:
        values = connectors or (
            SlackDirectConnector(), GmailDirectConnector(),
            CalendarDirectConnector(), GitHubDirectConnector(),
        )
        self._connectors = {connector.source: connector for connector in values}

    def get(self, source: Source) -> DirectConnector | None:
        return self._connectors.get(source)

    def available_sources(self) -> set[Source]:
        return {
            source for source, connector in self._connectors.items()
            if connector.available()
        }


__all__ = ["DirectConnectorRegistry"]
