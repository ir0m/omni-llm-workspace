"""Replies run as background tasks, so they keep going when the browser goes away.

Every event is buffered with an increasing id. A browser that reconnects with Last-Event-ID gets what it
missed and then the live events. Finished runs stay replayable for a while.

Events: `status` (running / complete / error / cancelled), `update` (ACP-shaped session updates),
`permission` (an agent asks; answer with resolve()) and `permission_resolved`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from . import blocks
from .agents.base import Decision, PermissionRequest
from .events import Update
from .providers.base import ProviderError

log = logging.getLogger(__name__)
KEEP_FINISHED_SECONDS = 15 * 60

# (status, content blocks, usage, error) -> stores the reply
SaveReply = Callable[[str, list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None], None]
# A permission request -> ("allow" | "deny" | "ask", reason)
Decide = Callable[[PermissionRequest], tuple[str, str | None]]


@dataclass
class Run:
    id: str
    conversation_id: str
    message_id: str
    decide: Decide | None = None
    events: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)
    status: str = "running"
    finished_at: float | None = None
    task: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    pending: dict[str, tuple[PermissionRequest, asyncio.Future]] = field(default_factory=dict)
    # What "always allow" remembered for this conversation; the policy reads the same list
    rules: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, name: str, data: dict[str, Any]) -> None:
        self.events.append((len(self.events) + 1, name, data))
        self.changed.set()
        self.changed = asyncio.Event()

    @property
    def done(self) -> bool:
        return self.finished_at is not None

    async def ask(self, request: PermissionRequest) -> Decision:
        verdict, reason = self.decide(request) if self.decide else ("ask", None)
        if verdict == "allow":
            return Decision("allow", automatic=True)
        if verdict == "deny":
            self.emit("update", {"sessionUpdate": "tool_call_update", "toolCallId": request.tool_call_id,
                                 "note": f"拒否: {reason}"})
            return Decision("deny", message=reason, automatic=True)
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (request, future)
        self.emit("permission", {"requestId": request_id, **request.to_dict()})
        try:
            decision = await future
        except asyncio.CancelledError:
            self.emit("permission_resolved", {"requestId": request_id, "outcome": "cancelled"})
            raise
        finally:
            self.pending.pop(request_id, None)
        self.emit("permission_resolved", {"requestId": request_id, "outcome": decision.outcome})
        if decision.outcome == "deny":
            self.emit("update", {"sessionUpdate": "tool_call_update", "toolCallId": request.tool_call_id,
                                 "note": "拒否しました"})
        return decision


StreamFactory = Callable[[Run], AsyncIterator[Update]]


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}

    def active_for(self, conversation_id: str) -> Run | None:
        return next((run for run in self.runs.values()
                     if run.conversation_id == conversation_id and not run.done), None)

    def start(self, conversation_id: str, message_id: str, stream: StreamFactory, save: SaveReply,
              decide: Decide | None = None) -> Run:
        self._forget_old()
        run = Run(id=str(uuid.uuid4()), conversation_id=conversation_id, message_id=message_id, decide=decide)
        self.runs[run.id] = run
        run.task = asyncio.create_task(self._execute(run, stream, save))
        return run

    def cancel(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if run is None or run.done or run.task is None:
            return False
        run.task.cancel()
        return True

    def pending_request(self, run_id: str, request_id: str) -> PermissionRequest | None:
        run = self.runs.get(run_id)
        entry = run.pending.get(request_id) if run else None
        return entry[0] if entry else None

    def resolve(self, run_id: str, request_id: str, decision: Decision) -> bool:
        run = self.runs.get(run_id)
        entry = run.pending.get(request_id) if run else None
        if entry is None or entry[1].done():
            return False
        entry[1].set_result(decision)
        return True

    async def subscribe(self, run: Run, after: int = 0) -> AsyncIterator[tuple[int, str, dict[str, Any]]]:
        sent = after
        while True:
            changed = run.changed
            # Count as we go: events can arrive while the consumer holds an earlier one
            while sent < len(run.events):
                sent += 1
                yield run.events[sent - 1]
            if run.done:
                return
            await changed.wait()

    async def _execute(self, run: Run, stream: StreamFactory, save: SaveReply) -> None:
        content: list[dict[str, Any]] = []
        usage: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        run.emit("status", {"status": "running", "messageId": run.message_id})

        def take(update: Update) -> None:
            nonlocal usage
            if update.get("sessionUpdate") == "usage_update":
                usage = blocks.merge_usage(usage, update)
            else:
                blocks.apply(content, update)

        original_emit = run.emit

        def emit(name: str, data: dict[str, Any]) -> None:  # updates the policy emits land in the reply too
            if name == "update":
                take(data)
            original_emit(name, data)

        run.emit = emit  # type: ignore[method-assign]
        try:
            async for update in stream(run):
                run.emit("update", update)
            status = "complete"
        except asyncio.CancelledError:
            status = "cancelled"
        except ProviderError as exc:
            status, error = "error", exc.to_dict()
        except Exception as exc:  # a bug or an unexpected reply; keep what arrived
            log.exception("run %s failed", run.id)
            status, error = "error", {"code": "internal", "message": type(exc).__name__}
        for _request, future in list(run.pending.values()):
            future.cancel()
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
