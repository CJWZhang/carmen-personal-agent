from __future__ import annotations

import os
import platform
import sys
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carmen_agent.chat import (
    DEFAULT_ADAPTER,
    DEFAULT_MODEL,
    DEFAULT_PROFILE,
    _load_runtime,
    clean_model_output,
    load_profile,
    sanitize_slack_emoji,
)
from carmen_agent.providers import ModelProvider, build_provider
from carmen_agent.configuration import config_value


def _prefer_offline_when_cache_is_warm(model_name: str) -> None:
    """Skip HuggingFace network calls when the model already lives in the local cache.

    The daily launchd job otherwise fails whenever the Koi TLS proxy handshake breaks;
    if the weights are already downloaded there is no reason to reach out at all.
    """
    if os.environ.get("HF_HUB_OFFLINE"):
        return
    hub_root = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    slug = "models--" + model_name.replace("/", "--")
    if (hub_root / slug).is_dir():
        os.environ["HF_HUB_OFFLINE"] = "1"


@dataclass
class ModelSession:
    model: Any
    tokenizer: Any
    stream_generate: Any
    make_sampler: Any
    system_prompt: str
    allowed_emoji: set[str]

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 256,
        temperature: float = 0.6,
        top_p: float = 0.9,
        sanitize_emoji: bool = True,
    ) -> str:
        prompt = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True)
        sampler = self.make_sampler(temp=temperature, top_p=top_p)
        pieces: list[str] = []
        for response in self.stream_generate(
            model=self.model,
            tokenizer=self.tokenizer,
            prompt=prompt,
            max_tokens=max_tokens,
            sampler=sampler,
        ):
            pieces.append(response.text)
        text = clean_model_output("".join(pieces))
        if sanitize_emoji:
            text = sanitize_slack_emoji(text, self.allowed_emoji)
        return text


@dataclass
class ProviderModelSession:
    provider: ModelProvider
    system_prompt: str
    allowed_emoji: set[str]

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 256,
        temperature: float = 0.6,
        top_p: float = 0.9,
        sanitize_emoji: bool = True,
    ) -> str:
        text = clean_model_output(
            self.provider.generate(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
            )
        )
        if sanitize_emoji:
            text = sanitize_slack_emoji(text, self.allowed_emoji)
        return text


def default_local_provider() -> str:
    configured = config_value("CARMEN_MODEL_PROVIDER", "model_provider").lower()
    if configured:
        return configured
    if (
        sys.platform == "darwin"
        and platform.machine().lower() in {"arm64", "aarch64"}
        and importlib.util.find_spec("mlx_lm") is not None
    ):
        return "mlx"
    return "ollama"


def load_session(
    *,
    model_name: str | None = None,
    adapter: Path = DEFAULT_ADAPTER,
    profile_path: Path = DEFAULT_PROFILE,
    base_only: bool = False,
    provider_name: str | None = None,
) -> ModelSession | ProviderModelSession:
    selected_provider = (provider_name or default_local_provider()).strip().lower()
    profile = load_profile(profile_path)
    allowed_emoji = {
        str(name)
        for name in profile.get("allowed_slack_emoji", [])
        if isinstance(name, str)
    }
    if selected_provider != "mlx":
        return ProviderModelSession(
            provider=build_provider(selected_provider, model_name=model_name),
            system_prompt=str(profile["system_prompt"]),
            allowed_emoji=allowed_emoji,
        )
    selected_model = model_name or config_value(
        "CARMEN_MODEL_NAME", "model_name", DEFAULT_MODEL
    )
    _prefer_offline_when_cache_is_warm(selected_model)
    load, stream_generate, make_sampler = _load_runtime()
    adapter_file = adapter / "adapters.safetensors"
    if not base_only and not adapter_file.is_file():
        raise SystemExit(
            f"No trained adapter found at {adapter_file}.\n"
            "Run ./run-train.sh first. For a prompt-only preview, explicitly pass --base-only."
        )
    adapter_path = None if base_only else str(adapter)
    model, tokenizer = load(selected_model, adapter_path=adapter_path)
    return ModelSession(
        model=model,
        tokenizer=tokenizer,
        stream_generate=stream_generate,
        make_sampler=make_sampler,
        system_prompt=str(profile["system_prompt"]),
        allowed_emoji=allowed_emoji,
    )
