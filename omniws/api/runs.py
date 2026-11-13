"""Following a reply as Server-Sent Events, and stopping it."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..agents import policy
from ..agents.base import Decision
from ..models import Conversation
from ..runs import RunManager

HEARTBEAT_SECONDS = 15
router = APIRouter(prefix="/api/runs")


def sse(event_id: int, name: str, data: dict) -> str:
    return f"id: {event_id}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = 0):
    runs: RunManager = request.app.state.runs
    run = runs.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    last_seen = request.headers.get("last-event-id")
    start = int(last_seen) if last_seen and last_seen.isdigit() else after

    async def stream() -> AsyncIterator[str]:
        events = runs.subscribe(run, start).__aiter__()
        pending = asyncio.ensure_future(events.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
                if not done:
                    yield ": keep-alive\n\n"
                    continue
                try:
                    event_id, name, data = pending.result()
                except StopAsyncIteration:
                    return
                yield sse(event_id, name, data)
                pending = asyncio.ensure_future(events.__anext__())
        finally:
            pending.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{run_id}/cancel")
def cancel_run(run_id: str, request: Request):
    runs: RunManager = request.app.state.runs
    if run_id not in runs.runs:
        raise HTTPException(status_code=404, detail="run_not_found")
    return {"cancelled": runs.cancel(run_id)}


class Answer(BaseModel):
    outcome: Literal["allow", "allow_always", "deny"]
    message: str | None = Field(default=None, max_length=4000)


@router.post("/{run_id}/permissions/{request_id}")
async def answer_permission(run_id: str, request_id: str, body: Answer, request: Request):
    """The user's answer to an agent's permission request. "allow_always" is remembered for the conversation."""
    state = request.app.state
    runs: RunManager = state.runs
    asked = runs.pending_request(run_id, request_id)
    if asked is None:
        raise HTTPException(status_code=404, detail="request_not_found")
    run = runs.runs[run_id]
    rule = policy.rule_for(asked) if body.outcome == "allow_always" else None
    if rule is not None:  # compound commands can't be remembered; they are allowed once
        if rule not in run.rules:
            run.rules.append(rule)

        def remember() -> None:
            with state.sessions.begin() as session:
                conversation = session.get(Conversation, run.conversation_id)
                if conversation is not None:
                    conversation.allow_rules = list(run.rules)

        await run_in_threadpool(remember)
    return {"ok": runs.resolve(run_id, request_id, Decision(body.outcome, body.message))}
