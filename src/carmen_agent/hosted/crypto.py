from __future__ import annotations

from typing import Protocol


class TokenCipher(Protocol):
    def encrypt(self, value: str) -> str: ...

    def decrypt(self, value: str) -> str: ...


class FernetTokenCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("A token-encryption key is required.")
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise RuntimeError(
                "Hosted token encryption requires the `hosted` dependency extra."
            ) from exc
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError(
                "CARMEN_TOKEN_ENCRYPTION_KEY must be a Fernet key."
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
