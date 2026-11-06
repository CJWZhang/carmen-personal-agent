"""Small deterministic guards for text shown directly to the user."""
from __future__ import annotations

import re

_NON_LATIN_SCRIPT_RE = re.compile(
    r"[\u0400-\u052f\u0600-\u06ff\u0e00-\u0e7f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]"
)
_LATIN_EXTENDED_RE = re.compile(r"[\u00c0-\u024f]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")
_CONTROL_MARKERS = (
    "<think",
    "<tool_call",
    "<tool_response",
    "<function_call",
    "<|",
)
_VALID_SHORT_ANSWERS = {"ok", "okay", "yes", "no", "sure", "thanks", "done"}
_INCOMPLETE_END_RE = re.compile(
    r"(?:--|—|\b(?:a|an|and|for|of|or|the|to|with|your))\s*$",
    re.IGNORECASE,
)


def response_is_usable(
    text: str,
    reference_text: str = "",
    *,
    max_chars: int = 8_000,
) -> bool:
    """Reject malformed, cross-script, and meaningless one-token generations."""
    cleaned = text.strip()
    lowered = cleaned.lower()
    if not cleaned or len(cleaned) > max_chars:
        return False
    if any(marker in lowered for marker in _CONTROL_MARKERS):
        return False
    if _NON_LATIN_SCRIPT_RE.search(cleaned) and not _NON_LATIN_SCRIPT_RE.search(
        reference_text
    ):
        return False
    # The current corpus is English. Qwen/LoRA corruption can surface as a single
    # accented Latin fragment (for example "âm"), which the older ASCII check
    # accidentally accepted because one character happened to be ASCII.
    if _LATIN_EXTENDED_RE.search(cleaned) and not _LATIN_EXTENDED_RE.search(
        reference_text
    ):
        return False
    words = _ASCII_WORD_RE.findall(cleaned)
    if not words:
        return False
    if len(words) == 1 and words[0].lower() not in _VALID_SHORT_ANSWERS:
        return False
    if _INCOMPLETE_END_RE.search(cleaned):
        return False
    return True
