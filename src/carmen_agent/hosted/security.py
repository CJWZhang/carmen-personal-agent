from __future__ import annotations

import hashlib
import hmac
import time


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        request_time = int(timestamp)
    except ValueError:
        return False
    current = int(time.time()) if now is None else now
    if abs(current - request_time) > tolerance_seconds:
        return False
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    expected = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def hash_oauth_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()
