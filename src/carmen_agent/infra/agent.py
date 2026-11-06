from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Iterable

from carmen_agent.infra.runtime import RunContext
from carmen_agent.infra.tools import Tool


@dataclass(frozen=True)
class ModelConfig:
    primary: str
    temperature: float = 0.2
    max_tokens: int = 400


@dataclass(frozen=True)
class ResolvedConfig:
    system_prompt: str
    tools: tuple[type[Tool], ...]
    skills: tuple[str, ...]
    model: ModelConfig
    max_iterations: int


class AgentDefinition(ABC):
    name: ClassVar[str]
    version: ClassVar[int] = 1
    model: ClassVar[ModelConfig]
    system_prompt: ClassVar[str]
    tools: ClassVar[tuple[type[Tool], ...]] = ()
    skills: ClassVar[tuple[str, ...]] = ()
    max_iterations: ClassVar[int] = 1

    @abstractmethod
    def _build_config(self, context: RunContext) -> ResolvedConfig: ...

    def build_config(self, context: RunContext) -> ResolvedConfig:
        config = self._build_config(context)
        permitted = [
            tool for tool in config.tools
            if tool.policy.required_permissions <= context.permissions
            and tool.available(context)
        ]
        return ResolvedConfig(
            system_prompt=config.system_prompt,
            tools=tuple(sorted(set(permitted), key=lambda item: item.name)),
            skills=tuple(sorted(set(config.skills))),
            model=config.model,
            max_iterations=config.max_iterations,
        )


class StaticAgentDefinition(AgentDefinition):
    """Convenience definition for a surface whose config is class-level data."""

    def _build_config(self, context: RunContext) -> ResolvedConfig:
        return ResolvedConfig(
            system_prompt=self.system_prompt,
            tools=self.tools,
            skills=self.skills,
            model=self.model,
            max_iterations=self.max_iterations,
        )


class AgentRegistry:
    def __init__(self, definitions: Iterable[type[AgentDefinition]] = ()) -> None:
        self._definitions: dict[str, type[AgentDefinition]] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: type[AgentDefinition]) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"Duplicate agent definition {definition.name!r}.")
        self._definitions[definition.name] = definition

    def resolve(self, name: str) -> AgentDefinition:
        try:
            return self._definitions[name]()
        except KeyError as exc:
            raise KeyError(f"Unknown agent definition {name!r}.") from exc
