"""Following a reply as Server-Sent Events, and stopping it."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

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
