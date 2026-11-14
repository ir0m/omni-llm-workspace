"""Turning the stored conversation into what a provider receives."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .models import Message


def segments(message: Message):
    """Keep instructions inserted into an assistant reply in their original order and role."""
    content = []
    for block in message.content or []:
        if block.get("type") == "user_input":
            if content:
                yield message.role, content
                content = []
            yield "user", [{"type": "text", "text": block.get("text", "")}]
        else:
            content.append(block)
    if content:
        yield message.role, content


def text_of(message: Message) -> str:
    return "".join(block.get("text", "") for block in message.content or [] if block.get("type") == "text").strip()


def as_parts(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


def chat_history(messages: Iterable[Message]) -> list[dict[str, Any]]:
    """Turns for a chat model, whichever model wrote them.

    Thinking is left out: it belongs to the model that produced it. Turns without text are skipped,
    the history starts with the user, and turns of the same role in a row are joined. A user turn with
    attachments has a list of parts: text, then {"type": "attachment", ...} references that each provider
    turns into its own parts when it sends the request.
    """
    turns: list[dict[str, Any]] = []
    for message in messages:
        for role, content in segments(message):
            text = "".join(block.get("text", "") for block in content if block.get("type") == "text").strip()
            files = [dict(block) for block in content if block.get("type") == "attachment"] if role == "user" else []
            if (not text and not files) or role not in ("user", "assistant"):
                continue
            value: str | list[dict[str, Any]] = ([{"type": "text", "text": text}] if text else []) + files if files else text
            if turns and turns[-1]["role"] == role:
                previous = turns[-1]["content"]
                if isinstance(previous, str) and isinstance(value, str):
                    turns[-1]["content"] = previous + "\n\n" + value
                else:
                    turns[-1]["content"] = as_parts(previous) + as_parts(value)
            else:
                turns.append({"role": role, "content": value})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns
