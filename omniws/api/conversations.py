"""Conversations: list, create, rename, pin, hide, reorder, fork, delete, and send a message."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import context as context_state
from .. import mcp_tools, native_actions, projects
from ..auth import load_secret
from ..agents import policy
from ..agents.base import Agent, Turn
from ..agents.handover import build_prompt, turn_items
from ..agents.opencode import FREE, PROVIDER_IDS, OpenCodeAgent
from ..db import now_ms
from ..history import chat_history
from ..models import AgentSession, Conversation, ExternalSession, Message
from ..external_sessions import origin, remember_deleted, same_origin
from ..providers.base import ChatProvider, ChatRequest
from ..runs import Run, RunManager

log = logging.getLogger(__name__)
TITLE_LENGTH = 40
# Agents that take both Chat and Code turns themselves (OpenCode only runs Code turns for providers)
DIRECT_AGENTS = ("codex", "claude_code", "claude_code_api")
router = APIRouter(prefix="/api/conversations")


def db(request: Request):
    yield from request.app.state.get_session()


def mcp_servers_for(state, off: list[str]) -> list[dict[str, Any]]:
    """The MCP servers a Chat turn uses: enabled in settings, allowed by .env, not turned off here."""
    from ..app_settings import read
    if not state.settings.omni_allow_mcp:
        return []
    with state.sessions() as session:
        servers = read(session, mcp_tools.KEY, [])
    return [server for server in servers if server.get("enabled", True) and server["id"] not in off
            and mcp_tools.usable(server, state.settings)]


def conversation_dict(conversation: Conversation, runs: RunManager) -> dict[str, Any]:
    active = runs.active_for(conversation.id)
    return {"id": conversation.id, "title": conversation.title, "mode": conversation.mode,
            "has_native_session": any(item.agent in DIRECT_AGENTS and not item.ephemeral for item in conversation.agent_sessions),
            "project": conversation.project, "temporary": conversation.temporary, "pinned": conversation.pinned,
            "hidden": conversation.hidden, "forked_from": conversation.forked_from, "target": conversation.target,
            "approval_mode": conversation.approval_mode, "allow_rules": conversation.allow_rules or [],
            "mcp_off": conversation.mcp_off or [],
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
    project: str | None = Field(default=None, max_length=200)
    approval_mode: Literal["ask", "auto_edit", "auto"] | None = None
    mcp_off: list[Annotated[str, Field(max_length=20)]] = Field(default_factory=list, max_length=100)


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None
    hidden: bool | None = None
    temporary: bool | None = None
    mode: Literal["chat", "code"] | None = None
    # A project name, or "" to take the conversation out of its project
    project: str | None = Field(default=None, max_length=200)
    approval_mode: Literal["ask", "auto_edit", "auto"] | None = None
    clear_allow_rules: bool = False
    mcp_off: list[Annotated[str, Field(max_length=20)]] | None = Field(default=None, max_length=100)


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
                                mcp_off=list(dict.fromkeys(body.mcp_off)),
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
async def get_conversation(conversation_id: str, request: Request):
    state = request.app.state
    async with state.external_sessions.lock:
        error = await state.external_sessions.sync(conversation_id)
        def read():
            with state.sessions() as session:
                conversation = find(session, conversation_id)
                return {**conversation_dict(conversation, state.runs), "sync_error": error,
                        "messages": [message_dict(message) for message in conversation.messages],
                        "context": conversation.context if context_state.valid(conversation.context, conversation.messages) else None,
                        "pending_inputs": conversation.pending_inputs or []}
        return await run_in_threadpool(read)


@router.patch("/{conversation_id}")
async def update_conversation(conversation_id: str, body: ConversationPatch, request: Request,
                              session: Session = Depends(db)):
    async with request.app.state.external_sessions.lock:
        conversation = find(session, conversation_id)
        if body.title is not None:
            if request.app.state.runs.active_for(conversation_id):
                raise HTTPException(409, "reply_in_progress")
            try:
                await native_actions.rename(request.app.state, session, conversation, body.title.strip())
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "CLI側の改名に失敗しました。一部だけ変更された可能性があります。更新して確認してください")
        return _update_conversation(conversation_id, body, request, session)


def _update_conversation(conversation_id: str, body: ConversationPatch, request: Request,
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
async def delete_conversation(conversation_id: str, request: Request, native: bool = False, session: Session = Depends(db)):
    async with request.app.state.external_sessions.lock:
        conversation = find(session, conversation_id)
        if native:
            if request.app.state.runs.active_for(conversation_id):
                raise HTTPException(409, "reply_in_progress")
            try:
                await native_actions.delete(request.app.state, session, conversation)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "CLI側の削除に失敗しました。アプリの会話は残しています。CLI側の状態を確認してください")
        return _delete_conversation(conversation_id, request, session)


def _delete_conversation(conversation_id: str, request: Request, session: Session = Depends(db)):
    conversation = find(session, conversation_id)
    runs: RunManager = request.app.state.runs
    active = runs.active_for(conversation_id)
    if active:
        runs.cancel(active.id)
    remember_deleted(session, conversation, request.app.state.settings.omni_projects_dir)
    session.delete(conversation)
    session.commit()
    return {"ok": True}


@router.post("/{conversation_id}/fork", status_code=201)
async def fork_conversation(conversation_id: str, body: Fork, request: Request, session: Session = Depends(db)):
    async with request.app.state.external_sessions.lock:
        if request.app.state.runs.active_for(conversation_id):
            raise HTTPException(409, "reply_in_progress")
        error = await request.app.state.external_sessions.sync(conversation_id)
        if error:
            raise HTTPException(409, error)
        return await _fork_conversation(conversation_id, body, request, session)


async def _fork_conversation(conversation_id: str, body: Fork, request: Request, session: Session = Depends(db)):
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
                        target=source.target, approval_mode=source.approval_mode, sort_key=float(now), created_at=now, updated_at=now)
    session.add(copy)
    copies = {message.id: str(uuid.uuid4()) for message in messages}
    for message in messages:
        session.add(Message(id=copies[message.id], conversation_id=copy.id, seq=message.seq, role=message.role,
                            content=message.content, provider=message.provider, model=message.model,
                            agent=message.agent, mode=message.mode, status=message.status, error=message.error,
                            usage=message.usage, created_at=message.created_at))
    made = []
    try:
        made = await native_actions.fork(request.app.state, session, source, copy, messages, copies)
        session.commit()
    except Exception as exc:
        session.rollback()
        await native_actions.discard(made)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(502, "CLI側の分岐に失敗しました。元の会話は変更していません") from exc
    return conversation_dict(copy, request.app.state.runs)


@router.post("/{conversation_id}/messages", status_code=202)
async def send_message(conversation_id: str, body: Send, request: Request):
    async with request.app.state.external_sessions.lock:
        await request.app.state.external_sessions.sync(conversation_id, sending=True)
        return await _send_message(conversation_id, body, request)


async def _send_message(conversation_id: str, body: Send, request: Request, *, consume_inputs: set[str] | None = None):
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
    if mode == "code" and (not project or not projects.exists(settings.omni_projects_dir, project)):
        raise HTTPException(status_code=400, detail="project_not_found")
    if body.provider in (*DIRECT_AGENTS, "agy") and body.provider in state.agents:
        agent = state.agents[body.provider]  # Codex, Claude Code, agy: Chat and Code
        if not agent.installed():
            raise HTTPException(status_code=400, detail="agent_not_installed")
    elif mode == "chat":
        provider = state.registry.get(body.provider)
        if provider is None:
            raise HTTPException(status_code=400, detail="provider_not_configured")
    else:
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
            if consume_inputs:
                conversation.pending_inputs = [item for item in conversation.pending_inputs if item["id"] not in consume_inputs]
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
                format = getattr(provider, "context_format", None)
                checkpoint = (conversation.native_context or {}).get(context_state.native_key(body.provider, body.model, format))
                if mcp_servers_for(state, conversation.mcp_off or []):
                    checkpoint = None
                prepared["history"] = context_state.replay(earlier, conversation.context, checkpoint,
                    body.provider, body.model, body.system, format) + chat_history([user])
                prepared["mcp_off"] = list(conversation.mcp_off or [])
                return prepared
            record = next((item for item in conversation.agent_sessions if item.agent == agent.id), None)
            native = record.native_id if record and not conversation.temporary else None
            unseen = [message for message in earlier if message.seq > record.synced_seq
                      and not same_origin(message.native_origin, agent.id, native)] if native else earlier
            shared = context_state.selected(earlier, conversation.context, record.synced_seq if native else 0)
            if native:
                shared = [m for m in shared if not same_origin(m.native_origin, agent.id, native)]
            fresh = context_state.selected(earlier, conversation.context)
            prepared.update(native=native, count=len(unseen), prompt=build_prompt(shared, body.text),
                            fresh_prompt=build_prompt(fresh, body.text), unseen=turn_items(shared),
                            earlier=turn_items(fresh))
            external = session.scalar(select(ExternalSession).where(
                ExternalSession.conversation_id == conversation.id, ExternalSession.agent == agent.id,
                ExternalSession.native_id == native)) if native else None
            prepared["require_resume"] = bool(external and conversation.mode == "code"
                                                and conversation.project == external.project)
            if prepared["require_resume"]:
                external.pending = [*external.pending, {
                    "text": body.text if agent.id == "codex" else prepared["prompt"],
                    "user": user.id, "assistant": reply.id}]
            return prepared

    prepared = await run_in_threadpool(prepare)
    reply_id = prepared["reply"]["id"]
    native_box = {"id": prepared.get("native"), "turn": None}
    external_sessions = state.external_sessions
    send_token = None

    def store_native(session) -> None:
        record = session.scalar(select(AgentSession).where(AgentSession.conversation_id == conversation_id,
                                                           AgentSession.agent == agent.id))
        if record is None:
            session.add(AgentSession(conversation_id=conversation_id, agent=agent.id, native_id=native_box["id"],
                                     synced_seq=prepared["seq"]))
        else:
            record.native_id, record.synced_seq = native_box["id"], prepared["seq"]

    def save(status: str, content: list[dict], usage: dict | None, error: dict | None) -> None:
        try:
            _save(status, content, usage, error)
        finally:
            if send_token:
                external_sessions.end_send(send_token)

    def _save(status: str, content: list[dict], usage: dict | None, error: dict | None) -> None:
        with state.sessions.begin() as session:
            message = session.get(Message, reply_id)
            if message is not None:
                message.status, message.content, message.usage, message.error = status, content, usage, error
                if agent is not None and native_box["id"] and native_box["turn"] and not prepared["temporary"]:
                    message.native_origin = {**origin(agent.id, native_box["id"]), "turn": native_box["turn"]}
            if prepared.get("require_resume"):
                external = session.scalar(select(ExternalSession).where(
                    ExternalSession.conversation_id == conversation_id, ExternalSession.agent == agent.id))
                if external:
                    pending = []
                    for item in external.pending:
                        if item["assistant"] == reply_id:
                            if native_box["turn"]:
                                item = {**item, "native_turn": native_box["turn"]}
                            elif agent.id == "codex":
                                continue  # turn/start failed: there is no native turn to reconcile
                        pending.append(item)
                    external.pending = pending
            if agent is None:
                if status == "complete" and chat_request.replay_messages is not None and not chat_request.tools:
                    conversation = session.get(Conversation, conversation_id)
                    checkpoint = {"provider": body.provider, "model": body.model, "system": body.system,
                        "format": getattr(provider, "context_format", None), "through_seq": prepared["seq"], "fingerprint": context_state.fingerprint(conversation.messages),
                        "messages": chat_request.replay_messages}
                    key = context_state.native_key(body.provider, body.model, getattr(provider, "context_format", None))
                    conversation.native_context = {**(conversation.native_context or {}), key: checkpoint}
                return
            if prepared["temporary"] or not native_box["id"]:
                return
            store_native(session)

    async def after_finish(finished: Run) -> None:
        # Serialized with ordinary sends, refreshes and cancelling queue entries.
        async with external_sessions.lock:
            with state.sessions() as session:
                conversation = session.get(Conversation, conversation_id)
                if conversation is None:
                    return
                queued = [item for item in conversation.pending_inputs
                          if item["run_id"] == finished.id and item["status"] == "queued"]
                unchanged = conversation.mode == mode and conversation.project == project
            if not queued:
                return
            if finished.status == "complete" and unchanged and not runs.active_for(conversation_id):
                try:
                    await external_sessions.sync(conversation_id, sending=True)
                    await _send_message(conversation_id,
                        body.model_copy(update={"text": "\n\n".join(item["text"] for item in queued)}),
                        request, consume_inputs={item["id"] for item in queued})
                    return
                except Exception:
                    log.exception("starting queued input for %s failed", conversation_id)
            with state.sessions.begin() as session:
                conversation = session.get(Conversation, conversation_id)
                if conversation:
                    conversation.pending_inputs = [
                        {**item, "status": "returned"} if item["run_id"] == finished.id else item
                        for item in conversation.pending_inputs]

    if agent is None:
        chat_request = ChatRequest(model=body.model, messages=prepared["history"], system=body.system, effort=body.effort)

        servers = mcp_servers_for(state, prepared["mcp_off"]) if hasattr(provider, "tool_results") else []
        if servers:
            rules = prepared["rules"]
            always = {f"{server['id']}/{tool}" for server in servers for tool in server.get("always_allow", [])}
            secret = load_secret(settings)

            async def chat_stream_with_tools(run: Run):
                async with mcp_tools.ToolSet(servers, secret) as tools:
                    async for update in mcp_tools.chat_with_tools(provider, chat_request, model_info, tools, run.ask):
                        yield update

            run = runs.start(conversation_id, reply_id, chat_stream_with_tools, save,
                             decide=lambda permission: policy.decide_mcp(permission, rules, settings, always))
            run.rules = rules
            run.after_finish = after_finish
            return {"run_id": run.id, "user_message": prepared["user"], "assistant_message": prepared["reply"]}

        def chat_stream(run: Run):
            return provider.stream(chat_request, model_info)

        run = runs.start(conversation_id, reply_id, chat_stream, save)
        run.after_finish = after_finish
        return {"run_id": run.id, "user_message": prepared["user"], "assistant_message": prepared["reply"]}

    rules: list[dict[str, Any]] = prepared["rules"]
    if mode == "code":
        cwd = projects.project_path(settings.omni_projects_dir, project)
    else:  # agents chat in an empty folder of their own
        cwd = settings.omni_data_dir / "agent-chat"
        cwd.mkdir(parents=True, exist_ok=True)

    def decide(permission):
        return policy.decide(permission, prepared["approval_mode"], rules, settings, cwd)

    def on_session(session_id: str) -> None:
        changed = native_box["id"] != session_id
        native_box["id"] = session_id
        if prepared["temporary"]:
            return
        if changed:
            # Store the id now, not when the reply ends: the CLI has already written the session to disk
            # and discovery would otherwise import it as a second conversation.
            try:
                with state.sessions.begin() as session:
                    if session.get(Conversation, conversation_id) is not None:
                        store_native(session)
            except Exception:
                log.exception("storing the session id of %s failed", conversation_id)  # save() retries at the end
        external_sessions.claim(send_token, agent.id, session_id)

    def on_turn_id(turn_id: str) -> None:
        native_box["turn"] = turn_id

    async def agent_stream(run: Run):
        if prepared["count"]:
            yield {"sessionUpdate": "handover", "agent": agent.label, "count": prepared["count"]}
        turn = Turn(conversation_id=conversation_id, mode=mode, cwd=cwd, model=agent_model, text=body.text,
                    prompt=prepared["prompt"], fresh_prompt=prepared["fresh_prompt"], unseen=prepared["unseen"],
                    earlier=prepared["earlier"], native_session=native_box["id"], temporary=prepared["temporary"],
                    effort=body.effort, ask_permission=run.ask, on_session=on_session,
                    require_resume=prepared.get("require_resume", False), on_turn_id=on_turn_id,
                    on_input_ready=run.set_steer)
        async for update in agent.run(turn):
            yield update

    if not prepared["temporary"]:
        send_token = external_sessions.begin_send(agent.id, cwd)
    run = runs.start(conversation_id, reply_id, agent_stream, save, decide=decide)
    run.after_finish = after_finish
    run.rules = rules
    return {"run_id": run.id, "user_message": prepared["user"], "assistant_message": prepared["reply"]}


class CompactContext(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=200)
    keep_turns: int = Field(default=3, ge=1, le=20)


@router.post("/{conversation_id}/context")
async def compact_context(conversation_id: str, body: CompactContext, request: Request):
    import asyncio
    from ..agents.base import Decision
    state = request.app.state
    async with state.external_sessions.lock:
        if state.runs.active_for(conversation_id):
            raise HTTPException(409, "返答が終わってから圧縮してください")
        error = await state.external_sessions.sync(conversation_id)
        if error:
            raise HTTPException(409, error)
        with state.sessions() as session:
            conversation = find(session, conversation_id)
            messages = list(conversation.messages)
            user_indices = [i for i, m in enumerate(messages) if m.role == "user"]
            if len(user_indices) <= body.keep_turns:
                raise HTTPException(400, "圧縮する古い会話がありません。直近の会話は原文で残します")
            prefix = messages[:user_indices[-body.keep_turns]]
            snapshot = context_state.fingerprint(messages)
            through = prefix[-1].seq
            previous_context = conversation.context
            previous = conversation.context if context_state.valid(conversation.context, messages) else None
            text = previous["text"] if previous and previous["through_seq"] <= through else ""
            source = context_state.source_text([m for m in prefix if not text or m.seq > previous["through_seq"]])
    provider = state.registry.get(body.provider)
    agent = state.agents.get(body.provider) if body.provider in (*DIRECT_AGENTS, "agy") else None
    if not provider and not (agent and agent.installed()):
        raise HTTPException(400, "要約に使える実行元を選んでください")
    if len(source) > 1_000_000:
        raise HTTPException(400, "履歴が大きすぎます。会話を分岐して範囲を小さくしてください")
    usage = {"inputTokens": 0, "outputTokens": 0}
    async def deny(_):
        return Decision("deny", message="Summarization only; tools are disabled.")
    try:
        async with asyncio.timeout(600):
            for offset in range(0, len(source), 24000):
                prompt = context_state.SUMMARY_INSTRUCTIONS + "\n\nExisting handover:\n" + text + "\n\nTranscript continuation:\n" + source[offset:offset + 24000]
                if provider:
                    query = ChatRequest(model=body.model, messages=[{"role": "user", "content": prompt}])
                    stream = provider.stream(query, await state.registry.model(body.provider, body.model))
                else:
                    cwd = state.settings.omni_data_dir / "agent-chat"
                    cwd.mkdir(parents=True, exist_ok=True)
                    turn = Turn(conversation_id=conversation_id, mode="chat", cwd=cwd, model=body.model,
                        text=prompt, prompt=prompt, fresh_prompt=prompt, unseen=[], earlier=[], native_session=None,
                        temporary=True, effort=None, ask_permission=deny, on_session=lambda _: None)
                    stream = agent.run(turn)
                result = ""
                async for update in stream:
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        result += update.get("content", {}).get("text", "")
                    elif update.get("sessionUpdate") == "usage_update":
                        for key in usage:
                            usage[key] += update.get(key, 0)
                if not result.strip() or len(result) > 12000:
                    raise HTTPException(502, "要約を生成できませんでした。元の履歴は保持されています")
                text = result.strip()
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("summary failed: %s", type(exc).__name__)
        raise HTTPException(502, "要約に失敗しました。元の履歴は保持されています") from exc
    async with state.external_sessions.lock:
        with state.sessions.begin() as session:
            conversation = find(session, conversation_id)
            if (state.runs.active_for(conversation_id) or context_state.fingerprint(conversation.messages) != snapshot
                    or conversation.context != previous_context):
                raise HTTPException(409, "処理中に会話が更新されました。もう一度圧縮してください")
            conversation.context = {"text": text, "through_seq": through,
                "fingerprint": context_state.fingerprint(prefix), "provider": body.provider, "model": body.model,
                "usage": usage, "created_at": now_ms(), "source_chars": len(context_state.source_text(prefix))}
            # Native replay is derived from the old prefix; rebuild it from the new portable summary.
            conversation.native_context = None
            return conversation.context


@router.delete("/{conversation_id}/context")
async def clear_context(conversation_id: str, request: Request):
    state = request.app.state
    async with state.external_sessions.lock:
        if state.runs.active_for(conversation_id):
            raise HTTPException(409, "返答が終わってから変更してください")
        with state.sessions.begin() as session:
            conversation = find(session, conversation_id)
            conversation.context = None
            conversation.native_context = None
    return {"ok": True}


@router.post("/{conversation_id}/context/native")
async def compact_native_context(conversation_id: str, body: CompactContext, request: Request):
    import asyncio
    state = request.app.state
    # Serializes with sending and imported-session synchronization, including CLI mutations.
    async with state.external_sessions.lock:
        if state.runs.active_for(conversation_id):
            raise HTTPException(409, "返答が終わってから圧縮してください")
        with state.sessions() as session:
            conversation = find(session, conversation_id)
            provider = state.registry.get(body.provider)
            key = context_state.native_key(body.provider, body.model, getattr(provider, "context_format", None))
            checkpoint = (conversation.native_context or {}).get(key)
            record = next((r for r in conversation.agent_sessions if r.agent == body.provider), None)
            native_id = record.native_id if record else None
            cwd = projects.project_path(state.settings.omni_projects_dir, conversation.project) if conversation.project else state.settings.omni_data_dir / "agent-chat"
            snapshot = context_state.fingerprint(conversation.messages)
            through = conversation.messages[-1].seq if conversation.messages else 0
            temporary = conversation.temporary
            checkpoint_valid = context_state.valid(checkpoint, conversation.messages) and checkpoint.get("through_seq") == through
        provider = state.registry.get(body.provider)
        agent = state.agents.get(body.provider)
        try:
            async with asyncio.timeout(150):
                if provider and hasattr(provider, "compact"):
                    if not checkpoint_valid or checkpoint["provider"] != body.provider or checkpoint["model"] != body.model:
                        raise HTTPException(400, "この実行元・モデルで会話を続けてから圧縮してください")
                    query = ChatRequest(model=body.model, messages=checkpoint["messages"], system=checkpoint.get("system"))
                    compacted = await provider.compact(query, await state.registry.model(body.provider, body.model))
                    with state.sessions.begin() as session:
                        conversation = find(session, conversation_id)
                        if context_state.fingerprint(conversation.messages) != snapshot:
                            raise HTTPException(409, "会話が更新されました")
                        conversation.native_context = {**(conversation.native_context or {}), key: {**checkpoint, "messages": compacted}}
                elif agent and hasattr(agent, "compact") and agent.installed() and native_id and not temporary:
                    if not state.settings.omni_allow_native_session_changes:
                        raise HTTPException(403, "CLIのセッション変更は無効です")
                    await agent.compact(native_id, cwd)
                else:
                    raise HTTPException(400, "この実行元では専用の圧縮に対応していません。共通要約を使ってください")
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("native compaction failed: %s", type(exc).__name__)
            raise HTTPException(502, "専用の圧縮に失敗しました。モデル・CLIの対応状況を確認してください") from exc
    return {"ok": True}
