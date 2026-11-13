"""Turning the stored conversation into what a provider receives."""

from __future__ import annotations

from collections.abc import Iterable

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


def chat_history(messages: Iterable[Message]) -> list[dict[str, str]]:
    """Plain text turns for a chat model, whichever model wrote them.

    Thinking is left out: it belongs to the model that produced it. Turns without text are skipped,
    the history starts with the user, and turns of the same role in a row are joined.
    """
    turns: list[dict[str, str]] = []
    for message in messages:
        for role, content in segments(message):
            text = "".join(block.get("text", "") for block in content if block.get("type") == "text").strip()
            if not text or role not in ("user", "assistant"):
                continue
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"] += "\n\n" + text
            else:
                turns.append({"role": role, "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns
