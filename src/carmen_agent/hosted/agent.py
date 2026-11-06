from __future__ import annotations

import re

from carmen_agent.chat import clean_model_output
from carmen_agent.providers import ModelProvider
from carmen_agent.infra.context import ContextEngine

_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def select_style_examples(
    examples: list[str],
    request: str,
    *,
    limit: int = 8,
) -> list[str]:
    request_tokens = _tokens(request)
    ranked = sorted(
        enumerate(examples),
        key=lambda pair: (
            len(_tokens(pair[1]) & request_tokens),
            pair[0],
        ),
        reverse=True,
    )
    return [text for _, text in ranked[:limit] if text.strip()]


def draft_reply(
    provider: ModelProvider,
    *,
    request: str,
    context: list[dict[str, str]],
    authored_examples: list[str],
) -> str:
    examples = select_style_examples(authored_examples, request)
    example_text = "\n".join(f"- {example[:400]}" for example in examples)
    context_text = "\n".join(
        f"{message.get('user') or 'unknown'}: {message.get('text', '')}"
        for message in context
    )
    messages = ContextEngine().assemble_prompt(
        system_prompt=(
                "Draft one concise Slack reply in the user's writing style. "
                "Use the examples only for tone and phrasing, never as facts. "
                "Do not claim work is complete, invent commitments, or reveal private "
                "training examples. Return only the draft with no analysis or tags."
        ),
        current_user_message=(
                f"Representative messages authored by this user:\n{example_text or '(none yet)'}\n\n"
                f"Recent Slack context:\n{context_text or '(not available)'}\n\n"
                f"Draft request:\n{request or 'Reply naturally to the most recent message.'}"
        ),
    )
    output = clean_model_output(
        provider.generate(messages, max_tokens=240, temperature=0.55)
    )
    if not output:
        raise ValueError("The model returned no usable draft.")
    return output


def daily_digest(
    provider: ModelProvider,
    *,
    authored_messages: list[str],
) -> str:
    messages_text = "\n".join(f"- {message[:500]}" for message in authored_messages[-40:])
    messages = ContextEngine().assemble_prompt(
        system_prompt=(
                "Create a private daily work reminder from messages authored by the user. "
                "List only explicit promises, stated next steps, reviews they volunteered "
                "to do, and follow-ups they said they would make. Do not infer incoming "
                "messages or claim access to sources not shown. Use short Slack bullets. "
                "If there are no clear commitments, say so plainly."
        ),
        current_user_message=f"Recent authored messages:\n{messages_text or '(none collected)'}",
    )
    output = clean_model_output(
        provider.generate(messages, max_tokens=500, temperature=0.2)
    )
    return output or "No explicit commitments found in the collected messages."
