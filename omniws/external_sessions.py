"""Index project-scoped CLI sessions and reconcile history with locally streamed turns."""

import asyncio
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from . import projects
from .agents.session_history import text_content
from .db import now_ms
from .models import AgentSession, Conversation, ExternalSession, Message
from .providers.base import ProviderError

START_GRACE_MS = 10_000  # CLI timestamps may be rounded down to the second

def family(agent: str) -> str:
    return "claude" if agent.startswith("claude_code") else agent


def origin(agent: str, native_id: str) -> dict[str, str]:
    return {"agent": family(agent), "session": native_id}


def same_origin(value: dict | None, agent: str, native_id: str) -> bool:
    expected = origin(agent, native_id)
    return bool(value) and all(value.get(key) == item for key, item in expected.items())


class ExternalSessions:
    def __init__(self, state):
        self.state = state
        # Discovery, history refresh and preparing sends must not race one another.
        self.lock = asyncio.Lock()
        # A CLI writes its transcript before the app learns the session id. Until that id is stored,
        # discovery must not import the app's own running session as a separate conversation.
        self.starting: dict[str, tuple[str, str, int]] = {}  # send token -> (family, cwd, started at)
        self.claimed: set[tuple[str, str]] = set()  # (family, native id) started by the app

    def begin_send(self, agent_id: str, cwd: Path) -> str:
        token = str(uuid.uuid4())
        self.starting[token] = (family(agent_id), str(Path(cwd).resolve()), now_ms())
        return token

    def claim(self, token: str, agent_id: str, native_id: str) -> None:
        """Call after the id is stored: the claim covers the gap until discovery reads it."""
        self.claimed.add((family(agent_id), native_id))
        self.end_send(token)

    def end_send(self, token: str) -> None:
        self.starting.pop(token, None)

    def owned_by_app(self, key: tuple[str, str], cwd: str, item) -> bool:
        if key in self.claimed:
            return True
        created = item.created_at or now_ms()
        # Defer, not drop: a genuine CLI session is imported by a later scan once the send has its id.
        return any(owner == key[0] and path == cwd and created >= started - START_GRACE_MS
                   for owner, path, started in list(self.starting.values()))

    async def discover(self) -> dict:
        state = self.state
        errors, added = [], 0
        agents = []
        for key in ("codex", "claude_code", "claude_code_api"):
            agent = state.agents.get(key)
            if agent and agent.installed() and hasattr(agent, "list_sessions"):
                if not any(family(other.id) == family(agent.id) for other in agents):
                    agents.append(agent)
        folders = await projects.list_projects(state.settings.omni_projects_dir)
        async with self.lock:
            for project in folders:
                cwd = Path(project["path"])
                for agent in agents:
                    try:
                        items = await agent.list_sessions(cwd)
                        added += await run_in_threadpool(self.index, agent.id, project["name"], cwd, items)
                    except Exception:
                        # Never return CLI stderr or transcript contents in an error banner.
                        errors.append(f"{project['name']}: {agent.label} の会話一覧を読み込めませんでした")
        return {"added": added, "errors": errors}

    def index(self, agent_id, project, cwd, items) -> int:
        added = 0
        with self.state.sessions.begin() as session:
            indexed = {(row.family, row.native_id): row for row in session.scalars(select(ExternalSession))}
            known = {(family(row.agent), row.native_id) for row in session.scalars(select(AgentSession))}
            for item in items:
                key = (family(agent_id), item.id)
                if Path(item.cwd).resolve() != cwd.resolve():
                    continue
                if key in indexed:
                    record = indexed[key]
                    conversation = session.get(Conversation, record.conversation_id) if record.conversation_id else None
                    if conversation and item.updated_at > conversation.updated_at:
                        conversation.updated_at = item.updated_at
                        conversation.sort_key = max(conversation.sort_key, float(item.updated_at))
                    continue
                if key in known or self.owned_by_app(key, str(cwd.resolve()), item):
                    continue
                now = now_ms()
                created, updated = item.created_at or now, item.updated_at or now
                conversation = Conversation(id=str(uuid.uuid4()), title=item.title.strip()[:200], mode="code",
                    project=project, created_at=created, updated_at=updated, sort_key=float(updated),
                    target={"provider": agent_id, "model": item.model or "", "effort": None})
                session.add(conversation)
                session.flush()
                session.add(AgentSession(conversation_id=conversation.id, agent=agent_id, native_id=item.id, synced_seq=0))
                session.add(ExternalSession(family=key[0], agent=agent_id, native_id=item.id,
                    conversation_id=conversation.id, project=project, cwd=str(cwd.resolve()), turns={}, pending=[]))
                known.add(key)
                added += 1
        return added

    async def sync(self, conversation_id: str, *, sending: bool = False) -> str | None:
        """Caller holds lock. A failed read preserves existing history and prevents a blind send."""
        state = self.state
        if state.runs.active_for(conversation_id):
            return None

        def records():
            with state.sessions() as session:
                return list(session.scalars(select(ExternalSession).where(
                    ExternalSession.conversation_id == conversation_id)))

        for record in await run_in_threadpool(records):
            agent = state.agents.get(record.agent)
            if not agent or not agent.installed():
                error = "このセッションの実行元が無効です。設定を確認してください"
            else:
                try:
                    cwd = projects.project_path(state.settings.omni_projects_dir, record.project)
                    if not cwd.is_dir() or str(cwd.resolve()) != record.cwd:
                        raise ProviderError("not_found", "元のプロジェクトフォルダが見つかりません")
                    turns = await agent.session_history(record.native_id, cwd)
                    if sending and any(turn.status == "streaming" for turn in turns):
                        raise ProviderError("bad_request", "このセッションは別の画面で実行中です")
                    await run_in_threadpool(self.merge, record.id, turns)
                    continue
                except ProviderError as exc:
                    error = exc.message
                except Exception:
                    error = "元のセッションの履歴を読み込めませんでした"
            if sending:
                raise HTTPException(status_code=409, detail=error)
            return error
        return None

    def merge(self, record_id, turns):
        with self.state.sessions.begin() as session:
            record = session.get(ExternalSession, record_id)
            if not record or not record.conversation_id:
                return
            conversation = session.get(Conversation, record.conversation_id)
            messages = list(conversation.messages)
            by_id = {message.id: message for message in messages}
            seq = messages[-1].seq if messages else 0
            linked, pending = dict(record.turns), list(record.pending)
            changed = False
            for turn in turns:
                mapping = linked.get(turn.id)
                if mapping is None:
                    # Codex supplies a turn id; Claude supplies an assistant message UUID.
                    # Older SDK versions without UUIDs fall back to the complete SDK prompt.
                    own = next((item for item in pending if
                        (item["native_turn"] in [turn.id, *turn.source_ids] if item.get("native_turn")
                         else text_content(turn.user) == item["text"])), None)
                    if own:
                        pending.remove(own)
                        linked[turn.id] = {"user": own["user"], "assistant": own["assistant"], "owned": True}
                        continue
                    mapping = {"user": str(uuid.uuid4()), "assistant": str(uuid.uuid4()), "owned": False}
                    linked[turn.id] = mapping
                    for role in ("user", "assistant"):
                        seq += 1
                        message = Message(id=mapping[role], conversation_id=conversation.id, seq=seq, role=role,
                            content=[], mode="code", provider=record.agent if role == "assistant" else None,
                            agent=record.agent if role == "assistant" else None,
                            native_origin=origin(record.agent, record.native_id),
                            created_at=turn.created_at or (now_ms() if record.turns else conversation.updated_at))
                        session.add(message)
                        messages.append(message)
                        by_id[message.id] = message
                    changed = True
                if mapping.get("owned"):
                    continue  # keep the app's streamed tool approvals, usage and error metadata
                for role in ("user", "assistant"):
                    message = by_id.get(mapping[role])
                    if message is None:
                        continue
                    content = getattr(turn, role)
                    # There is no app Run for a CLI process: don't leave an unresumable streaming bubble.
                    status = "interrupted" if turn.status == "streaming" else turn.status
                    if message.content != content:
                        changed = True
                    message.content = content
                    message.status = status if role == "assistant" else "complete"
                    message.model = turn.model if role == "assistant" else None
                if turn.model and not (conversation.target or {}).get("model"):
                    conversation.target = {**(conversation.target or {}), "model": turn.model}
            record.turns, record.pending = linked, pending
            if changed and record.turns and messages:
                latest = max(message.created_at for message in messages)
                conversation.updated_at = max(conversation.updated_at, latest)
                conversation.sort_key = max(conversation.sort_key, float(latest))


def remember_deleted(session, conversation, root):
    """A locally deleted conversation must not come back on the next CLI scan."""
    if not conversation.project:
        return
    for native in conversation.agent_sessions:
        if family(native.agent) not in ("codex", "claude"):
            continue
        existing = session.scalar(select(ExternalSession).where(
            ExternalSession.family == family(native.agent), ExternalSession.native_id == native.native_id))
        if existing is None:
            session.add(ExternalSession(family=family(native.agent), agent=native.agent, native_id=native.native_id,
                conversation_id=None, project=conversation.project,
                cwd=str(projects.project_path(root, conversation.project).resolve()), turns={}, pending=[]))
