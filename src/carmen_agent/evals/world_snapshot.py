from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from carmen_agent.providers import ModelProvider


def model_call_key(
    messages: list[dict[str, str]], *, max_tokens: int, temperature: float
) -> str:
    encoded = json.dumps(
        {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ModelCallSnapshot:
    key: str
    messages: tuple[dict[str, str], ...]
    max_tokens: int
    temperature: float
    output: str


@dataclass
class WorldSnapshot:
    version: int = 1
    model_calls: list[ModelCallSnapshot] = field(default_factory=list)
    source_records: dict[str, Any] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": self.version,
                    "model_calls": [asdict(item) for item in self.model_calls],
                    "source_records": self.source_records,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> WorldSnapshot:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            version=int(raw.get("version", 1)),
            model_calls=[
                ModelCallSnapshot(
                    key=str(item["key"]),
                    messages=tuple(item["messages"]),
                    max_tokens=int(item["max_tokens"]),
                    temperature=float(item["temperature"]),
                    output=str(item["output"]),
                )
                for item in raw.get("model_calls", [])
            ],
            source_records=dict(raw.get("source_records", {})),
        )


class RecordingProvider:
    """Record exact model inputs/outputs next to a production or reviewed run."""

    def __init__(self, provider: ModelProvider, snapshot: WorldSnapshot) -> None:
        self.provider = provider
        self.snapshot = snapshot

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 400,
        temperature: float = 0.4,
    ) -> str:
        output = self.provider.generate(
            messages, max_tokens=max_tokens, temperature=temperature
        )
        self.snapshot.model_calls.append(
            ModelCallSnapshot(
                key=model_call_key(
                    messages, max_tokens=max_tokens, temperature=temperature
                ),
                messages=tuple(dict(item) for item in messages),
                max_tokens=max_tokens,
                temperature=temperature,
                output=output,
            )
        )
        return output


class SnapshotProvider:
    """Replay only exact captured calls; live network/model access is impossible."""

    def __init__(self, snapshot: WorldSnapshot) -> None:
        self.outputs = {item.key: item.output for item in snapshot.model_calls}

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 400,
        temperature: float = 0.4,
    ) -> str:
        key = model_call_key(messages, max_tokens=max_tokens, temperature=temperature)
        try:
            return self.outputs[key]
        except KeyError as exc:
            raise KeyError(
                "This model call is absent from the frozen world snapshot; live fallback is disabled."
            ) from exc
