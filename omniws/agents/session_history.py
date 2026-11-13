"""Convert native history to the app's existing message blocks (no model calls)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..blocks import apply


@dataclass
class SessionInfo:
    id: str
    title: str
    cwd: str
    created_at: int
    updated_at: int
    model: str | None = None


@dataclass
class HistoryTurn:
    id: str
    user: list[dict[str, Any]] = field(default_factory=list)
    assistant: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    status: str = "complete"
    created_at: int | None = None
    source_ids: list[str] = field(default_factory=list)


def text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(str(item.get("text", "")) for item in content or []
                     if isinstance(item, dict) and item.get("type") in ("text", "input_text", "output_text"))


def codex_history(thread: dict, cwd: Path) -> list[HistoryTurn]:
    from .codex import item_update

    turns = []
    for raw in thread.get("turns") or []:
        # thread.model is the current model, not telemetry for each historical turn.
        turn = HistoryTurn(raw["id"],
                           status={"failed": "error", "interrupted": "interrupted", "inProgress": "streaming"}
                           .get(raw.get("status"), "complete"),
                           created_at=int(raw["startedAt"] * 1000) if raw.get("startedAt") else None)
        for item in raw.get("items") or []:
            kind = item.get("type")
            if kind == "userMessage":
                text = text_content(item.get("content"))
                if text:
                    if turn.user:
                        turn.assistant.append({"type": "user_input", "text": text})
                    else:
                        turn.user.append({"type": "text", "text": text})
            elif kind == "agentMessage":
                turn.assistant.append({"type": "text", "text": item.get("text", "")})
            elif kind == "reasoning":
                summary = item.get("summary") or []
                text = "\n".join(part if isinstance(part, str) else part.get("text", "") for part in summary)
                if text:
                    turn.assistant.append({"type": "thinking", "text": text})
            elif update := item_update("tool_call", item, cwd):
                apply(turn.assistant, update)
        if turn.user or turn.assistant:
            turns.append(turn)
    return turns


def claude_history(messages: list, cwd: Path) -> list[HistoryTurn]:
    from .claude import KINDS, clip, relative

    turns: list[HistoryTurn] = []
    current: HistoryTurn | None = None
    for message in messages:
        raw = message.message if isinstance(message.message, dict) else {}
        content = raw.get("content") or []
        text = text_content(content)
        if message.type == "user" and text:
            current = HistoryTurn(message.uuid, user=[{"type": "text", "text": text}])
            turns.append(current)
        if current is None:
            current = HistoryTurn(message.uuid)
            turns.append(current)
        current.source_ids.append(message.uuid)
        if message.type == "assistant":
            current.model = raw.get("model") or current.model
            if isinstance(content, str):
                current.assistant.append({"type": "text", "text": content})
                continue
        for block in content if isinstance(content, list) else []:
            kind = block.get("type")
            if message.type == "assistant" and kind in ("text", "thinking"):
                current.assistant.append({"type": kind, "text": block.get(kind, "")})
            elif kind == "tool_use":
                data, name = block.get("input") or {}, block.get("name", "ツール")
                # History must not read today's file contents to reconstruct an old Write diff.
                apply(current.assistant, {"sessionUpdate": "tool_call", "toolCallId": block["id"],
                      "title": name, "kind": KINDS.get(name, "other"), "status": "completed",
                      "command": data.get("command"),
                      "path": relative(data.get("file_path") or data.get("path"), cwd)})
            elif kind == "tool_result":
                apply(current.assistant, {"sessionUpdate": "tool_call_update", "toolCallId": block["tool_use_id"],
                      "status": "failed" if block.get("is_error") else "completed",
                      "output": clip(text_content(block.get("content")))})
    return turns
