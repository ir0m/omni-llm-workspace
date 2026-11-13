"""Replies run as background tasks, so they keep going when the browser goes away.

Every update is buffered with an increasing id. A browser that reconnects with Last-Event-ID gets
what it missed and then the live updates. Finished runs stay replayable for a while.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from .events import Update
from .providers.base import ChatProvider, ChatRequest, ModelInfo, ProviderError

log = logging.getLogger(__name__)
KEEP_FINISHED_SECONDS = 15 * 60

# (status, content blocks, usage, error) -> stores the reply
SaveReply = Callable[[str, list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None], None]


@dataclass
class Run:
    id: str
    conversation_id: str
    message_id: str
    events: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)
    status: str = "running"
    finished_at: float | None = None
    task: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def emit(self, name: str, data: dict[str, Any]) -> None:
        self.events.append((len(self.events) + 1, name, data))
        self.changed.set()
        self.changed = asyncio.Event()

    @property
    def done(self) -> bool:
        return self.finished_at is not None


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}

    def active_for(self, conversation_id: str) -> Run | None:
        return next((run for run in self.runs.values()
                     if run.conversation_id == conversation_id and not run.done), None)

    def start(self, conversation_id: str, message_id: str, provider: ChatProvider, request: ChatRequest,
              model: ModelInfo | None, save: SaveReply) -> Run:
        self._forget_old()
        run = Run(id=str(uuid.uuid4()), conversation_id=conversation_id, message_id=message_id)
        self.runs[run.id] = run
        run.task = asyncio.create_task(self._execute(run, provider, request, model, save))
        return run

    def cancel(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if run is None or run.done or run.task is None:
            return False
        run.task.cancel()
        return True

    async def subscribe(self, run: Run, after: int = 0) -> AsyncIterator[tuple[int, str, dict[str, Any]]]:
        sent = after
        while True:
            changed = run.changed
            for event in run.events[sent:]:
                yield event
            sent = len(run.events)
            if run.done:
                return
            await changed.wait()

    async def _execute(self, run: Run, provider: ChatProvider, request: ChatRequest, model: ModelInfo | None,
                       save: SaveReply) -> None:
        text: list[str] = []
        thought: list[str] = []
        usage: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        run.emit("status", {"status": "running", "messageId": run.message_id})
        try:
            async for update in provider.stream(request, model):
                kind = update.get("sessionUpdate")
                if kind == "agent_message_chunk":
                    text.append(update["content"]["text"])
                elif kind == "agent_thought_chunk":
                    thought.append(update["content"]["text"])
                elif kind == "usage_update":
                    usage = {key: value for key, value in update.items() if key != "sessionUpdate"}
                run.emit("update", update)
            status = "complete"
        except asyncio.CancelledError:
            status = "cancelled"
        except ProviderError as exc:
            status, error = "error", exc.to_dict()
        except Exception as exc:  # a bug or an unexpected reply; keep what arrived
            log.exception("run %s failed", run.id)
            status, error = "error", {"code": "internal", "message": type(exc).__name__}
        content = ([{"type": "thinking", "text": "".join(thought)}] if thought else []) + \
                  ([{"type": "text", "text": "".join(text)}] if text else [])
        try:
            await asyncio.to_thread(save, status, content, usage, error)
        except Exception:
            log.exception("saving run %s failed", run.id)
        run.status = status
        run.finished_at = time.monotonic()
        run.emit("status", {"status": status, "messageId": run.message_id, "usage": usage, "error": error})

    def _forget_old(self) -> None:
        limit = time.monotonic() - KEEP_FINISHED_SECONDS
        for run_id in [run.id for run in self.runs.values() if run.done and run.finished_at < limit]:
            del self.runs[run_id]
