from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class HostedSettings:
    public_base_url: str
    database_url: str
    slack_client_id: str
    slack_client_secret: str
    slack_signing_secret: str
    token_encryption_key: str
    bot_scopes: tuple[str, ...]
    user_scopes: tuple[str, ...]

    @property
    def redirect_uri(self) -> str:
        return self.public_base_url.rstrip("/") + "/slack/oauth/callback"

    @classmethod
    def from_env(cls) -> "HostedSettings":
        values = {
            "public_base_url": os.environ.get("CARMEN_PUBLIC_BASE_URL", "").strip(),
            "database_url": os.environ.get("DATABASE_URL", "").strip(),
            "slack_client_id": os.environ.get("SLACK_CLIENT_ID", "").strip(),
            "slack_client_secret": os.environ.get("SLACK_CLIENT_SECRET", "").strip(),
            "slack_signing_secret": os.environ.get("SLACK_SIGNING_SECRET", "").strip(),
            "token_encryption_key": os.environ.get(
                "CARMEN_TOKEN_ENCRYPTION_KEY", ""
            ).strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            rendered = ", ".join(name.upper() for name in missing)
            raise ConfigurationError(f"Missing hosted configuration: {rendered}")
        bot_scopes = _scopes(
            os.environ.get(
                "SLACK_BOT_SCOPES",
                "commands,chat:write,im:write",
            )
        )
        user_scopes = _scopes(
            os.environ.get(
                "SLACK_USER_SCOPES",
                "channels:history,groups:history,im:history,mpim:history",
            )
        )
        return cls(
            **values,
            bot_scopes=bot_scopes,
            user_scopes=user_scopes,
        )


def _scopes(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
