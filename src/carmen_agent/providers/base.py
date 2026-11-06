from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import dataclass
from typing import Protocol

from carmen_agent.configuration import config_value


class ProviderError(RuntimeError):
    pass


class ModelProvider(Protocol):
    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 400,
        temperature: float = 0.4,
        top_p: float = 0.9,
    ) -> str: ...


@dataclass
class OpenAICompatibleProvider:
    """Minimal provider for OpenAI-compatible chat-completions APIs.

    This is inference only. The configured service receives the prompt content,
    so operators must choose a provider and data policy appropriate for them.
    """

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: int = 60

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 400,
        temperature: float = 0.4,
        top_p: float = 0.9,
    ) -> str:
        hostname = (urllib.parse.urlsplit(self.base_url).hostname or "").lower()
        local_endpoint = hostname in {"localhost", "127.0.0.1", "::1"}
        if not self.api_key and not local_endpoint:
            raise ProviderError("CARMEN_MODEL_API_KEY is not configured.")
        endpoint = self.base_url.rstrip("/") + "/chat/completions"
        request_payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        if local_endpoint:
            # Ollama enables thinking by default for models such as Qwen 3. A
            # bounded structured-output turn can otherwise spend its entire
            # token budget in `reasoning` and return an empty final `content`.
            request_payload["reasoning_effort"] = "none"
        body = json.dumps(request_payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
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
            raise ProviderError(f"Model provider request failed: {type(exc).__name__}") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Model provider returned an unexpected response.") from exc
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("Model provider returned an empty response.")
        return content.strip()


class MLXProvider:
    """Apple-Silicon-only provider that lazily wraps the local MLX session."""

    def __init__(self) -> None:
        self._session = None

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 400,
        temperature: float = 0.4,
        top_p: float = 0.9,
    ) -> str:
        if self._session is None:
            from carmen_agent.skills.model import load_session

            self._session = load_session(provider_name="mlx")
        return self._session.generate(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )


def build_provider(
    name: str | None = None,
    *,
    model_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ModelProvider:
    provider_name = (
        name or config_value("CARMEN_MODEL_PROVIDER", "model_provider")
    ).strip().lower()
    if provider_name == "mlx":
        return MLXProvider()
    if provider_name == "ollama":
        return OpenAICompatibleProvider(
            api_key=api_key if api_key is not None else os.environ.get("CARMEN_MODEL_API_KEY", ""),
            model=model_name or config_value("CARMEN_MODEL_NAME", "model_name", "qwen3:4b"),
            base_url=base_url or config_value(
                "CARMEN_MODEL_BASE_URL", "model_base_url", "http://127.0.0.1:11434/v1"
            ),
        )
    if provider_name in {"openai", "openai-compatible"}:
        model = model_name or config_value("CARMEN_MODEL_NAME", "model_name")
        if not model:
            raise ProviderError("CARMEN_MODEL_NAME is required.")
        return OpenAICompatibleProvider(
            api_key=api_key if api_key is not None else os.environ.get("CARMEN_MODEL_API_KEY", ""),
            model=model,
            base_url=base_url or config_value(
                "CARMEN_MODEL_BASE_URL", "model_base_url", "https://api.openai.com/v1"
            ),
        )
    raise ProviderError(
        "Set CARMEN_MODEL_PROVIDER to mlx, ollama, or openai-compatible."
    )
