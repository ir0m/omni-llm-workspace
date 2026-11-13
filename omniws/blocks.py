"""Folding streamed updates into the content blocks stored on a reply.

The browser applies the same rules (web/src/blocks.ts), so a reply looks the same while it streams
and after it is saved.

Blocks:
  {"type": "text", "text"}                    consecutive message chunks
  {"type": "thinking", "text"}                consecutive thought chunks
  {"type": "tool_call", "id", "title", "kind", "status", "locations", "command", "path", "diff", "output", "note"}
  {"type": "plan", "entries": [{"content", "status", "priority"}]}   the latest plan (one per reply)
  {"type": "handover", "agent", "count"}      earlier turns handed to this agent
"""

from __future__ import annotations

from typing import Any

from .events import Update

TOOL_FIELDS = ("title", "kind", "status", "locations", "command", "path", "diff", "output", "note")


def apply(blocks: list[dict[str, Any]], update: Update) -> None:
    kind = update.get("sessionUpdate")
    if kind in ("agent_message_chunk", "agent_thought_chunk"):
        block_type = "text" if kind == "agent_message_chunk" else "thinking"
        text = (update.get("content") or {}).get("text", "")
        if blocks and blocks[-1]["type"] == block_type:
            blocks[-1]["text"] += text
        else:
            blocks.append({"type": block_type, "text": text})
    elif kind in ("tool_call", "tool_call_update"):
        block = next((b for b in blocks if b["type"] == "tool_call" and b["id"] == update["toolCallId"]), None)
        if block is None:
            block = {"type": "tool_call", "id": update["toolCallId"], "title": "", "kind": "other", "status": "pending"}
            blocks.append(block)
        for field in TOOL_FIELDS:
            if update.get(field) is not None:
                block[field] = update[field]
    elif kind == "plan":
        block = next((b for b in blocks if b["type"] == "plan"), None)
        if block is None:
            blocks.insert(0, {"type": "plan", "entries": update.get("entries", [])})
        else:
            block["entries"] = update.get("entries", [])
    elif kind == "handover":
        blocks.append({"type": "handover", "agent": update.get("agent"), "count": update.get("count", 0)})


def merge_usage(usage: dict[str, Any] | None, update: Update) -> dict[str, Any]:
    merged = dict(usage or {})
    merged.update({key: value for key, value in update.items() if key != "sessionUpdate" and value is not None})
    return merged
