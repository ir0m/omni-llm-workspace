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
from ..agents import policy
from ..agents.base import Agent, Turn
from ..agents.handover import build_prompt
from ..agents.opencode import FREE, PROVIDER_IDS, OpenCodeAgent
from ..db import now_ms
from ..history import chat_history
from ..models import AgentSession, Conversation, Message
from ..providers.base import ChatProvider, ChatRequest
from ..runs import Run, RunManager

TITLE_LENGTH = 40
router = APIRouter(prefix="/api/conversations")


def db(request: Request):
    yield from request.app.state.get_session()


def conversation_dict(conversation: Conversation, runs: RunManager) -> dict[str, Any]:
    active = runs.active_for(conversation.id)
    return {"id": conversation.id, "title": conversation.title, "mode": conversation.mode,
            "project": conversation.project, "temporary": conversation.temporary, "pinned": conversation.pinned,
            "hidden": conversation.hidden, "forked_from": conversation.forked_from, "target": conversation.target,
            "approval_mode": conversation.approval_mode, "allow_rules": conversation.allow_rules or [],
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
    approval_mode: Literal["ask", "auto_edit", "auto"] | None = None


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None
    hidden: bool | None = None
    temporary: bool | None = None
    mode: Literal["chat", "code"] | None = None
    # A project name, or "" to take the conversation out of its project
    project: str | None = Field(default=None, max_length=100)
    approval_mode: Literal["ask", "auto_edit", "auto"] | None = None
    clear_allow_rules: bool = False


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
    approval = policy.clamp(body.approval_mode, request.app.state.settings) if body.approval_mode else None
    conversation = Conversation(id=str(uuid.uuid4()), title=body.title.strip(), temporary=body.temporary,
                                mode=body.mode, project=project, approval_mode=approval, sort_key=float(now),
                                created_at=now, updated_at=now)
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
    if changes.pop("clear_allow_rules", False):
        conversation.allow_rules = []
    if "approval_mode" in changes:  # never looser than OMNI_APPROVAL_MAX
        conversation.approval_mode = policy.clamp(changes.pop("approval_mode"), request.app.state.settings)
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
    """Stores the user's message and starts the reply in the background. Follow it at /api/runs/{id}/events.

    Chat turns go to the chosen provider. Code turns go to an agent in the conversation's project:
    OpenCode runs API and local models (and its own free models when allowed)."""
    state = request.app.state
    settings = state.settings
    runs: RunManager = state.runs
    if runs.active_for(conversation_id):
        raise HTTPException(status_code=409, detail="reply_in_progress")

    def peek() -> tuple[str, str | None]:
        with state.sessions() as session:
            conversation = find(session, conversation_id)
            return conversation.mode, conversation.project

    mode, project = await run_in_threadpool(peek)
    provider: ChatProvider | None = None
    agent: Agent | None = None
    agent_model = body.model
    if mode == "chat":
        provider = state.registry.get(body.provider)
        if provider is None:
            raise HTTPException(status_code=400, detail="provider_not_configured")
    else:
        if not project or not projects.exists(settings.omni_projects_dir, project):
            raise HTTPException(status_code=400, detail="project_not_found")
        opencode: OpenCodeAgent | None = state.agents.get("opencode")
        if opencode is None or not opencode.installed():
            raise HTTPException(status_code=400, detail="code_needs_agent")
        if body.provider == FREE and settings.omni_allow_opencode_free:
            agent = opencode
        elif body.provider in PROVIDER_IDS and state.registry.get(body.provider) is not None:
            agent, agent_model = opencode, OpenCodeAgent.model_ref(body.provider, body.model)
        else:
            raise HTTPException(status_code=400, detail="code_needs_agent")
    model_info = await state.registry.model(body.provider, body.model) if provider else None

    def prepare() -> dict[str, Any]:
        with state.sessions.begin() as session:
            conversation = find(session, conversation_id)
            earlier = list(conversation.messages)
            seq = (earlier[-1].seq if earlier else 0) + 1
            now = now_ms()
            user = Message(id=str(uuid.uuid4()), conversation_id=conversation.id, seq=seq, role="user",
                           content=[{"type": "text", "text": body.text}], mode=conversation.mode, created_at=now)
            reply = Message(id=str(uuid.uuid4()), conversation_id=conversation.id, seq=seq + 1, role="assistant",
                            content=[], provider=body.provider, model=body.model, agent=agent.id if agent else None,
                            mode=conversation.mode, status="streaming", created_at=now)
            conversation.messages.extend([user, reply])
            if not conversation.title:
                first_line = body.text.strip().splitlines()[0] if body.text.strip() else ""
                conversation.title = first_line[:TITLE_LENGTH]
            conversation.target = {"provider": body.provider, "model": body.model, "effort": body.effort}
            conversation.updated_at = now
            conversation.sort_key = max(conversation.sort_key, float(now))
            session.flush()
            prepared: dict[str, Any] = {"user": message_dict(user), "reply": message_dict(reply), "seq": reply.seq,
                                        "temporary": conversation.temporary, "approval_mode": conversation.approval_mode,
                                        "rules": list(conversation.allow_rules or [])}
            if agent is None:
                prepared["history"] = chat_history(earlier + [user])
                return prepared
            record = next((item for item in conversation.agent_sessions if item.agent == agent.id), None)
            native = record.native_id if record and not conversation.temporary else None
            unseen = [message for message in earlier if message.seq > record.synced_seq] if native else earlier
            prepared.update(native=native, count=len(unseen), prompt=build_prompt(unseen, body.text),
                            fresh_prompt=build_prompt(earlier, body.text))
            return prepared

    prepared = await run_in_threadpool(prepare)
    reply_id = prepared["reply"]["id"]
    native_box = {"id": prepared.get("native")}

    def save(status: str, content: list[dict], usage: dict | None, error: dict | None) -> None:
        with state.sessions.begin() as session:
            message = session.get(Message, reply_id)
            if message is not None:
                message.status, message.content, message.usage, message.error = status, content, usage, error
            if agent is None or prepared["temporary"] or not native_box["id"]:
                return
            record = session.scalar(select(AgentSession).where(AgentSession.conversation_id == conversation_id,
                                                               AgentSession.agent == agent.id))
            if record is None:
                session.add(AgentSession(conversation_id=conversation_id, agent=agent.id, native_id=native_box["id"],
                                         synced_seq=prepared["seq"]))
            else:
                record.native_id, record.synced_seq = native_box["id"], prepared["seq"]

    if agent is None:
        chat_request = ChatRequest(model=body.model, messages=prepared["history"], system=body.system, effort=body.effort)

        def chat_stream(run: Run):
            return provider.stream(chat_request, model_info)

        run = runs.start(conversation_id, reply_id, chat_stream, save)
        return {"run_id": run.id, "user_message": prepared["user"], "assistant_message": prepared["reply"]}

    rules: list[dict[str, Any]] = prepared["rules"]
    cwd = projects.project_path(settings.omni_projects_dir, project)

    def decide(permission):
        return policy.decide(permission, prepared["approval_mode"], rules, settings)

    def on_session(session_id: str) -> None:
        native_box["id"] = session_id

    async def agent_stream(run: Run):
        if prepared["count"]:
            yield {"sessionUpdate": "handover", "agent": agent.label, "count": prepared["count"]}
        turn = Turn(conversation_id=conversation_id, mode="code", cwd=cwd, model=agent_model, prompt=prepared["prompt"],
                    fresh_prompt=prepared["fresh_prompt"], native_session=native_box["id"],
                    temporary=prepared["temporary"], effort=body.effort, ask_permission=run.ask, on_session=on_session)
        async for update in agent.run(turn):
            yield update

    run = runs.start(conversation_id, reply_id, agent_stream, save, decide=decide)
    run.rules = rules
    return {"run_id": run.id, "user_message": prepared["user"], "assistant_message": prepared["reply"]}
