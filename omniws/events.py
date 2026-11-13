"""Streaming updates sent to the browser.

The update kinds and their `sessionUpdate` / `content` fields follow ACP's `session/update`
(agent_message_chunk, agent_thought_chunk, usage_update), so updates from ACP agents (P2) can pass
through with little translation. Field names inside are camelCase like ACP. The token fields of
usage_update are this app's own.
"""

from __future__ import annotations

from typing import Any

Update = dict[str, Any]


def message_chunk(text: str) -> Update:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def thought_chunk(text: str) -> Update:
    return {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}}


def usage_update(*, input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
                 cache_write_tokens: int = 0, reasoning_tokens: int = 0, cost_usd: float | None = None,
                 context_window: int | None = None, stop_reason: str | None = None) -> Update:
    update: Update = {"sessionUpdate": "usage_update", "inputTokens": input_tokens, "outputTokens": output_tokens,
                      "cacheReadTokens": cache_read_tokens, "cacheWriteTokens": cache_write_tokens,
                      "reasoningTokens": reasoning_tokens}
    if cost_usd is not None:
        update["costUsd"] = cost_usd
    if context_window is not None:
        update["contextWindow"] = context_window
    if stop_reason is not None:
        update["stopReason"] = stop_reason
    return update
