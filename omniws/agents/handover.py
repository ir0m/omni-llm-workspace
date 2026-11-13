"""Handing earlier turns to an agent that hasn't seen them (another model or agent wrote them)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..models import Message

TEXT_LIMIT = 6000
KIND_LABEL = {"read": "読んだ", "search": "検索した", "edit": "編集した", "delete": "削除した", "move": "移動した",
              "execute": "実行した", "fetch": "取得した"}


def tool_summary(block: dict[str, Any]) -> str:
    what = block.get("command") or block.get("path") or block.get("title") or block.get("kind")
    verb = KIND_LABEL.get(block.get("kind", ""), "使った")
    status = {"completed": "", "failed": "（失敗）"}.get(block.get("status", ""), f"（{block.get('status')}）")
    if block.get("note"):
        status += f"（{block['note']}）"
    return f"- {what} を{verb}{status}"


def describe(message: Message) -> str:
    text = "".join(block.get("text", "") for block in message.content or [] if block.get("type") == "text").strip()
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT] + "…（省略）"
    if message.role == "user":
        return f"[user]\n{text}"
    who = " / ".join(part for part in (message.agent, message.model) if part)
    tools = [tool_summary(block) for block in message.content or [] if block.get("type") == "tool_call"]
    lines = [f"[assistant: {who}]" if who else "[assistant]"]
    if tools:
        lines.append("操作:\n" + "\n".join(tools))
    if text:
        lines.append(text)
    return "\n".join(lines)


def build_prompt(earlier: Iterable[Message], user_text: str) -> str:
    turns = [describe(message) for message in earlier if message.status != "streaming"]
    turns = [turn for turn in turns if turn.strip() not in ("[user]", "[assistant]")]
    if not turns:
        return user_text
    joined = "\n\n".join(turns)
    return ("<earlier_conversation>\nThese turns happened in this app before you joined, possibly with other models "
            "or agents. Treat them as the conversation so far; tool steps are summaries.\n\n"
            f"{joined}\n</earlier_conversation>\n\n{user_text}")
