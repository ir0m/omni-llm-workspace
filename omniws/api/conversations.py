"""Conversations: list, create, rename, pin, hide, reorder, fork, delete, and send a message."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import projects
from ..db import now_ms
from ..history import chat_history
from ..models import Conversation, Message
from ..providers.base import ChatRequest
from ..runs import RunManager

TITLE_LENGTH = 40
router = APIRouter(prefix="/api/conversations")


def db(request: Request):
    yield from request.app.state.get_session()


def conversation_dict(conversation: Conversation, runs: RunManager) -> dict[str, Any]:
    active = runs.active_for(conversation.id)
    return {"id": conversation.id, "title": conversation.title, "mode": conversation.mode,
            "project": conversation.project, "temporary": conversation.temporary, "pinned": conversation.pinned,
            "hidden": conversation.hidden, "forked_from": conversation.forked_from, "target": conversation.target,
            "created_at": conversation.created_at, "updated_at": conversation.updated_at,
            "active_run": active.id if active else None}


def message_dict(message: Message) -> dict[str, Any]:
    return {"id": message.id, "seq": message.seq, "role": message.role, "content": message.content,
            "provider": message.provider, "model": message.model, "agent": message.agent, "mode": message.mode,
            "status": message.status, "error": message.error, "usage": message.usage,
            "created_at": message.created_at}


def check_project(request: Request, name: str | None) -> str | None:
    if not name:
        return None
    if not projects.exists(request.app.state.settings.omni_projects_dir, name):
        raise HTTPException(status_code=400, detail="project_not_found")
    return name


def find(session: Session, conversation_id: str) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return conversation


class NewConversation(BaseModel):
    title: str = Field(default="", max_length=200)
    temporary: bool = False
    mode: Literal["chat", "code"] = "chat"
    project: str | None = Field(default=None, max_length=100)


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None
    hidden: bool | None = None
    temporary: bool | None = None
    mode: Literal["chat", "code"] | None = None
    # A project name, or "" to take the conversation out of its project
    project: str | None = Field(default=None, max_length=100)


class Order(BaseModel):
    ids: list[str] = Field(max_length=1000)


class Fork(BaseModel):
    # Copy the conversation up to and including this message (all of it when omitted)
    message_id: str | None = None


class Send(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    provider: str
    model: str = Field(min_length=1, max_length=200)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    system: str | None = Field(default=None, max_length=20_000)


@router.get("")
def list_conversations(request: Request, include_hidden: bool = False, session: Session = Depends(db)):
    query = select(Conversation).order_by(Conversation.pinned.desc(), Conversation.sort_key.desc())
    if not include_hidden:
        query = query.where(Conversation.hidden.is_(False))
    runs: RunManager = request.app.state.runs
    return [conversation_dict(conversation, runs) for conversation in session.scalars(query)]


@router.post("", status_code=201)
def create_conversation(body: NewConversation, request: Request, session: Session = Depends(db)):
    now = now_ms()
    project = check_project(request, body.project)
    conversation = Conversation(id=str(uuid.uuid4()), title=body.title.strip(), temporary=body.temporary,
                                mode=body.mode, project=project, sort_key=float(now), created_at=now, updated_at=now)
    session.add(conversation)
    session.commit()
    return conversation_dict(conversation, request.app.state.runs)


@router.put("/order")
def reorder(body: Order, session: Session = Depends(db)):
    """Puts the given conversations in this order, first on top; others keep their place."""
    conversations = {item.id: item for item in session.scalars(select(Conversation).where(Conversation.id.in_(body.ids)))}
    top = max(float(now_ms()), session.scalar(select(func.max(Conversation.sort_key))) or 0.0)
    for position, conversation_id in enumerate(body.ids):
        if conversation_id in conversations:
            conversations[conversation_id].sort_key = top - position
    session.commit()
    return {"ok": True}


@router.get("/{conversation_id}")
def get_conversation(conversation_id: str, request: Request, session: Session = Depends(db)):
    conversation = find(session, conversation_id)
    return {**conversation_dict(conversation, request.app.state.runs),
            "messages": [message_dict(message) for message in conversation.messages]}


@router.patch("/{conversation_id}")
def update_conversation(conversation_id: str, body: ConversationPatch, request: Request,
                        session: Session = Depends(db)):
    conversation = find(session, conversation_id)
    changes = body.model_dump(exclude_none=True)
    if "project" in changes:
        conversation.project = check_project(request, changes.pop("project"))
        if conversation.project is None:
            conversation.mode = "chat"
    for name, value in changes.items():
        setattr(conversation, name, value.strip() if isinstance(value, str) else value)
    conversation.updated_at = now_ms()
    session.commit()
    return conversation_dict(conversation, request.app.state.runs)


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request, session: Session = Depends(db)):
    conversation = find(session, conversation_id)
    runs: RunManager = request.app.state.runs
    active = runs.active_for(conversation_id)
    if active:
        runs.cancel(active.id)
    session.delete(conversation)
    session.commit()
    return {"ok": True}


@router.post("/{conversation_id}/fork", status_code=201)
def fork_conversation(conversation_id: str, body: Fork, request: Request, session: Session = Depends(db)):
    source = find(session, conversation_id)
    messages = [message for message in source.messages if message.status != "streaming"]
    if body.message_id:
        cut = next((message.seq for message in messages if message.id == body.message_id), None)
        if cut is None:
            raise HTTPException(status_code=404, detail="message_not_found")
        messages = [message for message in messages if message.seq <= cut]
    now = now_ms()
    copy = Conversation(id=str(uuid.uuid4()), title=f"{source.title or '無題'}（分岐）"[:200], mode=source.mode,
                        project=source.project, temporary=source.temporary, forked_from=source.id,
                        target=source.target, sort_key=float(now), created_at=now, updated_at=now)
    session.add(copy)
    for message in messages:
        session.add(Message(id=str(uuid.uuid4()), conversation_id=copy.id, seq=message.seq, role=message.role,
                            content=message.content, provider=message.provider, model=message.model,
                            agent=message.agent, mode=message.mode, status=message.status, error=message.error,
                            usage=message.usage, created_at=message.created_at))
    session.commit()
    return conversation_dict(copy, request.app.state.runs)


@router.post("/{conversation_id}/messages", status_code=202)
async def send_message(conversation_id: str, body: Send, request: Request):
    """Stores the user's message and starts the reply in the background. Follow it at /api/runs/{id}/events."""
    state = request.app.state
    runs: RunManager = state.runs
    provider = state.registry.get(body.provider)
    if provider is None:
        raise HTTPException(status_code=400, detail="provider_not_configured")
    if runs.active_for(conversation_id):
        raise HTTPException(status_code=409, detail="reply_in_progress")
    model = await state.registry.model(body.provider, body.model)

    def prepare() -> tuple[dict, dict, list[dict[str, str]]]:
        with state.sessions.begin() as session:
            conversation = find(session, conversation_id)
            if conversation.mode == "code":  # chat providers can't edit files; agents take Code turns
                raise HTTPException(status_code=400, detail="code_needs_agent")
            seq = (conversation.messages[-1].seq if conversation.messages else 0) + 1
            now = now_ms()
            user = Message(id=str(uuid.uuid4()), conversation_id=conversation.id, seq=seq, role="user",
                           content=[{"type": "text", "text": body.text}], mode=conversation.mode, created_at=now)
            reply = Message(id=str(uuid.uuid4()), conversation_id=conversation.id, seq=seq + 1, role="assistant",
                            content=[], provider=body.provider, model=body.model, mode=conversation.mode,
                            status="streaming", created_at=now)
            conversation.messages.extend([user, reply])
            if not conversation.title:
                first_line = body.text.strip().splitlines()[0] if body.text.strip() else ""
                conversation.title = first_line[:TITLE_LENGTH]
            conversation.target = {"provider": body.provider, "model": body.model, "effort": body.effort}
            conversation.updated_at = now
            conversation.sort_key = max(conversation.sort_key, float(now))
            session.flush()
            history = chat_history(message for message in conversation.messages if message.id != reply.id)
            return message_dict(user), message_dict(reply), history

    user, reply, history = await run_in_threadpool(prepare)

    def save(status: str, content: list[dict], usage: dict | None, error: dict | None) -> None:
        with state.sessions.begin() as session:
            message = session.get(Message, reply["id"])
            if message is not None:
                message.status, message.content, message.usage, message.error = status, content, usage, error

    chat_request = ChatRequest(model=body.model, messages=history, system=body.system, effort=body.effort)
    run = runs.start(conversation_id, reply["id"], provider, chat_request, model, save)
    return {"run_id": run.id, "user_message": user, "assistant_message": reply}
