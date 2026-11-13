"""Instructions sent during a reply, with a persistent fallback queue."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..models import Conversation

router = APIRouter(prefix="/api")


class Input(BaseModel):
    id: UUID
    text: str = Field(min_length=1, max_length=100_000)


@router.post("/runs/{run_id}/input")
async def submit(run_id: str, body: Input, request: Request):
    state = request.app.state
    run = state.runs.runs.get(run_id)
    if run is None:
        raise HTTPException(404, "run_not_found")
    text, input_id = body.text.strip(), str(body.id)
    if not text:
        raise HTTPException(422, "空の指示は送れません")
    async with run.input_lock:
        if input_id in run.input_receipts:
            return run.input_receipts[input_id]
        if run.done or not run.accepting_input:
            raise HTTPException(409, "返答が終了しました。入力は残してあるので、もう一度送信してください")
        item = {"id": input_id, "run_id": run_id, "text": text, "status": "queued"}
        with state.sessions.begin() as session:
            conversation = session.get(Conversation, run.conversation_id)
            if conversation is None:
                raise HTTPException(404, "conversation_not_found")
            pending = conversation.pending_inputs or []
            if len(pending) >= 50 or sum(len(entry["text"]) for entry in pending) + len(text) > 100_000:
                raise HTTPException(409, "予約がいっぱいです。予約を取り消すか、返答の終了を待ってください")
            conversation.pending_inputs = [*pending, item]
        status = "queued"
        # Preserve ordering once an earlier instruction has had to be queued.
        if run.steer and not any(entry["run_id"] == run_id and entry["status"] == "queued" for entry in pending):
            try:
                accepted = await asyncio.wait_for(run.steer(text), timeout=35)
                if accepted:
                    status = "delivered"
                    run.emit("update", {"sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": text}})
            except Exception:
                # Transport failure may mean the agent received it. Never silently send it twice.
                status = "returned"
        with state.sessions.begin() as session:
            conversation = session.get(Conversation, run.conversation_id)
            if conversation:
                conversation.pending_inputs = [
                    {**entry, "status": status} if entry["id"] == input_id else entry
                    for entry in conversation.pending_inputs if entry["id"] != input_id or status != "delivered"]
        response = {"id": input_id, "status": status}
        run.input_receipts[input_id] = response
        run.emit("input_changed", response)
        return response


@router.delete("/conversations/{conversation_id}/inputs/{input_id}")
async def remove(conversation_id: str, input_id: str, request: Request):
    state = request.app.state
    async with state.external_sessions.lock:
        run = state.runs.active_for(conversation_id)
        lock = run.input_lock if run else asyncio.Lock()
        async with lock:
            with state.sessions.begin() as session:
                conversation = session.get(Conversation, conversation_id)
                if conversation is None:
                    raise HTTPException(404, "conversation_not_found")
                removed = any(item["id"] == input_id for item in conversation.pending_inputs)
                conversation.pending_inputs = [item for item in conversation.pending_inputs if item["id"] != input_id]
            if run:
                run.emit("input_changed", {"id": input_id, "status": "removed"})
    return {"ok": True, "removed": removed}
