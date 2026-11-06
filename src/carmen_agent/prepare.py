from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from carmen_agent.paths import PROJECT_ROOT, STYLE_EXEMPLARS_PATH as DEFAULT_EXEMPLARS
from carmen_agent.configuration import config_value

URL_RE = re.compile(r"<(?:https?://|mailto:)[^>]+>|https?://\S+", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)")
USER_MENTION_RE = re.compile(r"<@[A-Z0-9]+(?:\|[^>]+)?>")
CHANNEL_MENTION_RE = re.compile(r"<#[A-Z0-9]+(?:\|[^>]+)?>")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
LONG_HEX_RE = re.compile(r"\b[0-9a-f]{24,}\b", re.I)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
EMOJI_ALIAS_RE = re.compile(r":([a-zA-Z0-9_+\-]+):")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z']+")

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|rk|pk)-(?:live|test|proj)?-?[A-Za-z0-9_-]{16,}\b", re.I),
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*\S+", re.I),
)

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for",
    "from", "had", "has", "have", "he", "her", "him", "his", "i", "if", "in",
    "is", "it", "its", "me", "my", "of", "on", "or", "our", "she", "so", "that",
    "the", "their", "them", "they", "this", "to", "us", "was", "we", "were", "will",
    "with", "you", "your",
}


@dataclass(frozen=True)
class Message:
    id: str
    channel_type: str
    timestamp: str
    text: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "Message":
        return cls(
            id=str(value["id"]),
            channel_type=str(value.get("channel_type", "channel")),
            timestamp=str(value.get("timestamp", "")),
            text=str(value["text"]),
        )


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def normalize_text(text: str) -> str:
    """Remove identifiers while preserving punctuation, casing, and writing style."""
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = URL_RE.sub("<LINK>", text)
    text = EMAIL_RE.sub("<EMAIL>", text)
    text = PHONE_RE.sub("<PHONE>", text)
    text = USER_MENTION_RE.sub("@person", text)
    text = CHANNEL_MENTION_RE.sub("#channel", text)
    text = UUID_RE.sub("<ID>", text)
    text = LONG_HEX_RE.sub("<ID>", text)
    text = JWT_RE.sub("<TOKEN>", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_link_only(text: str) -> bool:
    return bool(re.fullmatch(r"(?:<LINK>|[\s.,;:!?()\[\]{}\-])+", text))


def load_messages(path: Path) -> tuple[list[Message], Counter[str]]:
    stats: Counter[str] = Counter()
    messages: list[Message] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            stats["raw"] += 1
            try:
                item = Message.from_mapping(json.loads(line))
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            if contains_secret(item.text):
                stats["dropped_secret"] += 1
                continue
            cleaned = normalize_text(item.text)
            if len(cleaned) < 2:
                stats["dropped_too_short"] += 1
                continue
            if is_link_only(cleaned):
                stats["dropped_link_only"] += 1
                continue
            fingerprint = hashlib.sha256(cleaned.casefold().encode()).hexdigest()
            if fingerprint in seen:
                stats["dropped_duplicate"] += 1
                continue
            seen.add(fingerprint)
            messages.append(Message(item.id, item.channel_type, item.timestamp, cleaned))
    stats["kept"] = len(messages)
    return messages, stats


def _safe_rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def load_style_exemplars(path: Path, messages: Iterable[Message]) -> list[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    requested = payload.get("examples", [])
    if not isinstance(requested, list):
        return []
    message_text = {message.text.casefold(): message.text for message in messages}
    exemplars: list[str] = []
    for value in requested:
        if not isinstance(value, str):
            continue
        cleaned = normalize_text(value)
        matched = message_text.get(cleaned.casefold())
        if matched and matched not in exemplars:
            exemplars.append(matched)
    return exemplars[:12]


def build_style_profile(
    messages: Iterable[Message],
    style_exemplars: Iterable[str] = (),
) -> dict[str, object]:
    values = list(messages)
    texts = [message.text for message in values]
    alpha_starts = [next((char for char in text if char.isalpha()), "") for text in texts]
    emoji = Counter(match.group(1) for text in texts for match in EMOJI_ALIAS_RE.finditer(text))
    allowed_emoji = [name for name, _ in emoji.most_common()]
    words = Counter(
        word.casefold()
        for text in texts
        for word in WORD_RE.findall(text)
        if word.casefold() not in STOPWORDS and len(word) > 2
    )
    total = len(texts)
    average_chars = round(sum(map(len, texts)) / total, 1) if total else 0.0
    lowercase_starts = sum(1 for char in alpha_starts if char and char.islower())
    all_lower = sum(1 for text in texts if any(c.isalpha() for c in text) and text == text.lower())
    profile: dict[str, object] = {
        "message_count": total,
        "writing_registers": dict(Counter(message.channel_type for message in values)),
        "average_characters": average_chars,
        "short_message_rate": _safe_rate(sum(len(text) <= 40 for text in texts), total),
        "lowercase_start_rate": _safe_rate(lowercase_starts, sum(bool(c) for c in alpha_starts)),
        "all_lowercase_rate": _safe_rate(all_lower, total),
        "exclamation_rate": _safe_rate(sum("!" in text for text in texts), total),
        "question_rate": _safe_rate(sum("?" in text for text in texts), total),
        "top_slack_emoji": emoji.most_common(12),
        "allowed_slack_emoji": allowed_emoji,
        "emoji_policy": "Only exact aliases observed in the user's authored writing are allowed.",
        "style_exemplars": list(style_exemplars),
        "top_content_words": words.most_common(12),
    }
    profile["system_prompt"] = render_system_prompt(profile)
    return profile


def render_system_prompt(profile: Mapping[str, object]) -> str:
    name = config_value("CARMEN_USER_NAME", "user_name", "the user")
    avg = float(profile.get("average_characters", 0.0))
    short_rate = float(profile.get("short_message_rate", 0.0))
    lower_rate = float(profile.get("lowercase_start_rate", 0.0))
    emoji_items = profile.get("top_slack_emoji", [])
    emoji_names = [str(item[0]) for item in emoji_items if isinstance(item, (list, tuple)) and item]
    style_exemplars = [
        str(item)
        for item in profile.get("style_exemplars", [])
        if isinstance(item, str) and item.strip()
    ]
    register = "usually concise" if short_rate >= 0.55 or avg <= 80 else "moderately detailed"
    casing = "often starts messages in lowercase" if lower_rate >= 0.55 else "uses normal conversational casing"
    emoji_note = (
        "When it fits naturally, use only these exact Slack aliases: "
        + ", ".join(f":{name}:" for name in emoji_names[:12])
        + ". Never invent an emoji alias; several are Ramp-specific."
        if emoji_names
        else "Do not force emojis."
    )
    example_note = (
        f" Tone-only examples from {name}'s sanitized messages: "
        + " | ".join(json.dumps(item, ensure_ascii=False) for item in style_exemplars)
        + ". Do not copy their subject matter."
        if style_exemplars
        else ""
    )
    return (
        f"You are a local writing assistant that drafts in {name}'s writing voice. "
        f"Their observed style is {register} and {casing}. {emoji_note}{example_note} "
        "Match the medium: use professional email conventions for email and conversational phrasing for chat. "
        "Match the formality and emotional energy of the current conversation. Preserve the user's meaning, "
        "answer the current message directly, and do not invent personal facts, coworkers, projects, or private "
        "details from training data. Return only the draft message unless asked to explain."
    )


def infer_intent(text: str) -> str:
    lowered = text.casefold()
    if "?" in text:
        return "a question or question response"
    if re.search(r"\b(thank|thanks|ty|tysm)\b", lowered):
        return "a grateful response"
    if re.search(r"\b(sounds good|got it|perfect|yeah|ok|okay)\b", lowered):
        return "an acknowledgement or agreement"
    if EMOJI_ALIAS_RE.search(text) or re.search(r"\b(lol|lmao|haha+)\b", lowered):
        return "an expressive casual response"
    if len(text) <= 30:
        return "a short conversational response"
    return "a natural conversational response"


def make_example(message: Message, system_prompt: str) -> dict[str, object]:
    register = {
        "dm": "casual one-to-one Slack DM",
        "group_dm": "casual group Slack DM",
        "channel": "workplace Slack channel",
        "email": "professional email",
    }.get(message.channel_type, "work message")
    return {
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": (
                    f"Write {infer_intent(message.text)} for a {register}. "
                    "Context is intentionally withheld; focus on voice and register."
                ),
            },
            {"role": "assistant", "content": message.text},
        ]
    }


def split_name(message: Message, seed: int) -> str:
    digest = hashlib.sha256(f"{seed}:{message.id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % 1000
    if bucket < 900:
        return "train"
    if bucket < 950:
        return "valid"
    return "test"


def write_jsonl(path: Path, records: Iterable[Mapping[str, object]]) -> int:
    items = list(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return len(items)


def prepare(input_path: Path, output_dir: Path, seed: int = 42) -> dict[str, object]:
    messages, stats = load_messages(input_path)
    if len(messages) < 50:
        raise ValueError(
            f"Only {len(messages)} usable writing samples; collect at least 50 before fine-tuning."
        )
    exemplars = load_style_exemplars(DEFAULT_EXEMPLARS, messages)
    profile = build_style_profile(messages, exemplars)
    system_prompt = str(profile["system_prompt"])
    splits: dict[str, list[dict[str, object]]] = {"train": [], "valid": [], "test": []}
    for message in messages:
        splits[split_name(message, seed)].append(make_example(message, system_prompt))
    counts = {name: write_jsonl(output_dir / f"{name}.jsonl", records) for name, records in splits.items()}
    (output_dir / "style_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report: dict[str, object] = {"filtering": dict(stats), "splits": counts, "style_profile": profile}
    (output_dir / "preparation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sanitize authored writing and prepare MLX-LM chat JSONL."
    )
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data/raw/slack_messages.jsonl")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data/processed")
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = prepare(args.input, args.output, args.seed)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
