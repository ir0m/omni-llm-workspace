import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from omniws.agents.base import PermissionRequest
from omniws.agents.claude import ClaudeCodeAgent
from omniws.agents.codex import CodexAgent
from omniws.agents.jsonrpc import RpcError
from omniws.agents.session_history import HistoryTurn, SessionInfo, claude_history, codex_history
from omniws.db import now_ms
from omniws.events import message_chunk
from omniws.main import create_app
from omniws.models import AgentSession
from omniws.providers.base import ProviderError
from omniws.providers.registry import Registry

from .conftest import PASSWORD, FakeProvider, make_settings
from .test_agents import follow


def blocks(text):
    return [{"type": "text", "text": text}]


class NativeAgent:
    label = "テストCLI"

    def __init__(self, agent_id, cwd):
        self.id, self.cwd = agent_id, cwd
        self.native_id = f"{agent_id}-native"
        self.turns = [HistoryTurn("old", blocks("元の質問"), blocks("元の回答"), model="m1")]
        self.received = []
        self.enabled = True
        self.failure = False

    def installed(self):
        return self.enabled

    async def list_sessions(self, cwd):
        return [SessionInfo(self.native_id, f"{self.id}の会話", str(self.cwd.resolve()), 1000, 2000, "m1")]

    async def session_history(self, native_id, cwd):
        assert native_id == self.native_id
        if self.failure:
            raise ProviderError("not_found", "元のセッションが見つかりません")
        return self.turns

    async def run(self, turn):
        self.received.append(turn)
        assert turn.native_session == self.native_id
        answer = await turn.ask_permission(PermissionRequest("shell", "execute", "test", command="echo test"))
        text = "許可" if answer.outcome == "allow" else "拒否"
        prompt = turn.text if self.id == "codex" else turn.prompt
        native_turn = f"app-{len(self.turns)}"
        if turn.on_turn_id:
            turn.on_turn_id(native_turn)
        self.turns.append(HistoryTurn(native_turn, blocks(prompt), blocks(text), model="m1"))
        yield message_chunk(text)


@pytest.fixture
def native_client(tmp_path):
    settings = make_settings(tmp_path)
    cwd = settings.omni_projects_dir / "app"
    cwd.mkdir(parents=True)
    agents = {key: NativeAgent(key, cwd) for key in ("codex", "claude_code")}
    app = create_app(settings, web_dist=Path("/nonexistent"),
                     registry=Registry(settings, {"fake": FakeProvider()}), agents=agents)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        yield client, agents


def discover(client):
    assert client.post("/api/sessions/discover").status_code == 200
    return client.get("/api/conversations").json()


def test_discovery_is_project_scoped_and_idempotent(native_client):
    client, agents = native_client
    client.post("/api/projects", json={"name": "other", "git_init": False})
    listed = discover(client)
    assert len(listed) == 2
    assert {item["project"] for item in listed} == {"app"}
    assert len(discover(client)) == 2
    for item in listed:
        detail = client.get(f"/api/conversations/{item['id']}").json()
        assert [message["content"] for message in detail["messages"]] == [blocks("元の質問"), blocks("元の回答")]
        assert client.get(f"/api/conversations/{item['id']}").json()["messages"] == detail["messages"]


@pytest.mark.parametrize("agent_id", ["codex", "claude_code"])
def test_resume_keeps_native_id_policy_and_does_not_duplicate_history(native_client, agent_id):
    client, agents = native_client
    item = next(item for item in discover(client) if item["target"]["provider"] == agent_id)
    cid = item["id"]
    asked = []
    start = client.post(f"/api/conversations/{cid}/messages", json={"text": "続けて", "provider": agent_id, "model": "m1"})
    events = follow(client, start.json()["run_id"], lambda event: asked.append(event) or "deny")
    assert events[-1][1]["status"] == "complete"
    turn = agents[agent_id].received[-1]
    assert turn.require_resume and turn.native_session == agents[agent_id].native_id
    assert turn.unseen == [] and turn.prompt == "続けて"
    assert len(asked) == 1
    detail = client.get(f"/api/conversations/{cid}").json()
    assert len(detail["messages"]) == 4
    agents[agent_id].turns.append(HistoryTurn("cli-later", blocks("CLIで追加"), blocks("追加の回答")))
    detail = client.get(f"/api/conversations/{cid}").json()
    assert len(detail["messages"]) == 6
    assert detail["messages"][-1]["content"] == blocks("追加の回答")
    started = client.post(f"/api/conversations/{cid}/messages", json={"text": "もう一度", "provider": agent_id, "model": "m1"})
    follow(client, started.json()["run_id"], lambda _: "deny")
    assert agents[agent_id].received[-1].unseen == []
    assert len(client.get(f"/api/conversations/{cid}").json()["messages"]) == 8


def test_other_provider_history_is_handed_over_but_native_history_is_not(native_client):
    client, agents = native_client
    item = next(item for item in discover(client) if item["target"]["provider"] == "codex")
    cid = item["id"]
    client.get(f"/api/conversations/{cid}")
    client.patch(f"/api/conversations/{cid}", json={"mode": "chat"})
    started = client.post(f"/api/conversations/{cid}/messages", json={"text": "別モデルに相談", "provider": "fake", "model": "m1"})
    follow(client, started.json()["run_id"])
    agents["codex"].turns.append(HistoryTurn("cli", blocks("CLIで相談"), blocks("CLI回答")))
    client.patch(f"/api/conversations/{cid}", json={"mode": "code"})
    started = client.post(f"/api/conversations/{cid}/messages", json={"text": "実装", "provider": "codex", "model": "m1"})
    follow(client, started.json()["run_id"], lambda _: "deny")
    unseen = agents["codex"].received[-1].unseen
    assert len(unseen) == 2 and unseen[0]["text"] == "別モデルに相談"


def test_steered_imported_codex_turn_is_not_duplicated_on_refresh(native_client):
    client, agents = native_client
    agent = agents['codex']
    original = agent.run
    async def run_with_input(turn):
        instructions = []
        async def steer(text):
            instructions.append({'type': 'user_input', 'text': text})
            return True
        turn.on_input_ready(steer)
        try:
            async for update in original(turn):
                yield update
            agent.turns[-1].assistant = instructions + agent.turns[-1].assistant
        finally:
            turn.on_input_ready(None)
    agent.run = run_with_input
    cid = next(item['id'] for item in discover(client) if item['target']['provider'] == 'codex')
    started = client.post(f'/api/conversations/{cid}/messages', json={
        'text': '続けて', 'provider': 'codex', 'model': 'm1'}).json()
    def steer_and_allow(_):
        result = client.post(f'/api/runs/{started["run_id"]}/input', json={
            'id': str(uuid.uuid4()), 'text': '重複させない'})
        assert result.json()['status'] == 'delivered'
        return 'allow'
    follow(client, started['run_id'], steer_and_allow)
    for _ in range(2):
        messages = client.get(f'/api/conversations/{cid}').json()['messages']
        assert len(messages) == 4
        assert messages[-1]['content'] == [{'type': 'user_input', 'text': '重複させない'}, *blocks('許可')]


def test_local_operations_survive_discovery_and_delete_does_not_resurrect(native_client):
    client, _ = native_client
    item = discover(client)[0]
    cid = item["id"]
    client.patch(f"/api/conversations/{cid}", json={"title": "自分の名前", "hidden": True, "pinned": True})
    assert len(discover(client)) == 1
    hidden = client.get("/api/conversations?include_hidden=true").json()
    renamed = next(item for item in hidden if item["id"] == cid)
    assert renamed["title"] == "自分の名前" and renamed["pinned"]
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert len(discover(client)) == 1


def test_already_linked_app_sessions_are_not_duplicated(native_client):
    client, agents = native_client
    created = client.post("/api/conversations", json={"mode": "code", "project": "app"}).json()
    with client.app.state.sessions.begin() as session:
        session.add(AgentSession(conversation_id=created["id"], agent="codex", native_id=agents["codex"].native_id))
    assert len(discover(client)) == 2
    client.delete(f"/api/conversations/{created['id']}")
    assert len(discover(client)) == 1


class LiveAgent(NativeAgent):
    """A CLI that writes its session to disk as soon as a turn starts, like Claude Code."""

    def __init__(self, agent_id, cwd, announce):
        super().__init__(agent_id, cwd)
        self.sessions, self.announce = [], announce

    async def list_sessions(self, cwd):
        return list(self.sessions)

    async def run(self, turn):
        stamp = now_ms()
        self.sessions.append(SessionInfo("live", "CLIが付けた要約", str(self.cwd.resolve()), stamp, stamp))
        if self.announce:
            turn.on_session("live")
        await turn.ask_permission(PermissionRequest("shell", "execute", "test", command="echo test"))
        yield message_chunk("完了")


@pytest.mark.parametrize("announce", [True, False])
def test_running_app_session_is_not_imported_as_another_conversation(native_client, announce):
    client, agents = native_client
    agent = LiveAgent("claude_code", agents["claude_code"].cwd, announce)
    agents["claude_code"] = agent
    del agents["codex"]
    created = client.post("/api/conversations", json={"mode": "code", "project": "app"}).json()
    started = client.post(f"/api/conversations/{created['id']}/messages",
                          json={"text": "作業して", "provider": "claude_code", "model": "m1"})
    during = []

    def scan_while_running(_event):
        during.append(discover(client))
        if announce:  # stored when the agent reports it, not when the reply ends
            with client.app.state.sessions() as session:
                during.append(session.scalar(select(AgentSession.native_id)))
        return "allow"

    events = follow(client, started.json()["run_id"], scan_while_running)
    assert events[-1][1]["status"] == "complete"
    assert [item["id"] for item in during[0]] == [created["id"]]
    if announce:
        assert during[1] == "live"
        assert [item["id"] for item in discover(client)] == [created["id"]]
    else:
        # The app never learned the id, so the session really is the CLI's: a later scan imports it.
        assert len(discover(client)) == 2


def test_read_failure_preserves_history_and_blocks_send(native_client):
    client, agents = native_client
    item = discover(client)[0]
    cid, provider = item["id"], item["target"]["provider"]
    before = client.get(f"/api/conversations/{cid}").json()
    agents[provider].failure = True
    after = client.get(f"/api/conversations/{cid}").json()
    assert after["messages"] == before["messages"] and after["sync_error"]
    response = client.post(f"/api/conversations/{cid}/messages", json={"text": "続けて", "provider": provider, "model": "m1"})
    assert response.status_code == 409 and not agents[provider].received


def test_disabled_agent_is_not_scanned_and_active_cli_cannot_receive_send(native_client):
    client, agents = native_client
    agents["claude_code"].enabled = False
    listed = discover(client)
    assert len(listed) == 1
    agents["codex"].turns[-1].status = "streaming"
    response = client.post(f"/api/conversations/{listed[0]['id']}/messages",
                           json={"text": "続けて", "provider": "codex", "model": "m1"})
    assert response.status_code == 409


def test_native_parsers_preserve_tool_results_without_reading_files(tmp_path):
    history = codex_history({"model": "m1", "turns": [{"id": "t", "status": "completed", "items": [
        {"id": "u", "type": "userMessage", "content": blocks("質問")},
        {"id": "c", "type": "commandExecution", "command": "pwd", "status": "completed", "aggregatedOutput": "app"},
        {"id": "a", "type": "agentMessage", "text": "回答"},
    ]}]}, tmp_path)
    assert history[0].user == blocks("質問") and history[0].assistant[0]["output"] == "app"
    messages = [
        SimpleNamespace(type="user", uuid="u", message={"content": "質問"}),
        SimpleNamespace(type="assistant", uuid="a1", message={"model": "m1", "content": [
            {"type": "tool_use", "id": "tool", "name": "Write", "input": {"file_path": "/nonexistent", "content": "x"}}]}),
        SimpleNamespace(type="user", uuid="r", message={"content": [
            {"type": "tool_result", "tool_use_id": "tool", "content": "完了"}]}),
        SimpleNamespace(type="assistant", uuid="a2", message={"content": blocks("回答")}),
    ]
    history = claude_history(messages, tmp_path)
    assert len(history) == 1 and history[0].id == "u"
    assert history[0].assistant[0]["output"] == "完了" and "diff" not in history[0].assistant[0]


def test_codex_listing_paginates_and_respects_project(tmp_path):
    agent = CodexAgent(make_settings(tmp_path), executable="fake")
    calls = []
    class Connection:
        async def call(self, method, params, **kwargs):
            calls.append((method, params))
            return {"data": [{"id": params.get("cursor") or "first", "cwd": str(tmp_path), "preview": "title"}],
                    "nextCursor": None if params.get("cursor") else "next"}
    async def connect():
        return Connection()
    agent.connect = connect
    items = asyncio.run(agent.list_sessions(tmp_path))
    assert [item.id for item in items] == ["first", "next"]
    assert all(call[1]["cwd"] == str(tmp_path) for call in calls)


def test_claude_history_uses_scoped_sdk_and_not_subscription_credentials(tmp_path, monkeypatch):
    from omniws.agents import claude
    calls = []
    def listing(**kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(session_id="s", custom_title=None, summary="test", cwd=str(tmp_path),
                                created_at=1, last_modified=2)]
    monkeypatch.setattr(claude.sdk, "list_sessions", listing)
    monkeypatch.setattr(claude.sdk, "get_session_messages", lambda *args, **kwargs: [])
    agent = ClaudeCodeAgent(make_settings(tmp_path, omni_allow_claude_subscription=True), "subscription")
    assert asyncio.run(agent.session_history("s", tmp_path)) == []
    assert calls == [{"directory": str(tmp_path.resolve()), "include_worktrees": False}]


def test_strict_codex_resume_does_not_fall_back_to_new_thread(tmp_path):
    agent = CodexAgent(make_settings(tmp_path), executable="fake")
    methods = []
    class Connection:
        async def call(self, method, params, **kwargs):
            methods.append((method, params))
            raise RpcError(-1, "missing")
    turn = SimpleNamespace(mode="code", cwd=tmp_path, temporary=False, native_session="native", require_resume=True)
    with pytest.raises(ProviderError):
        asyncio.run(agent.thread(Connection(), turn))
    assert [method for method, _ in methods] == ["thread/resume"]
    assert methods[0][1]["approvalPolicy"] == "untrusted"


def test_codex_reads_all_history_pages_without_resuming(tmp_path):
    agent = CodexAgent(make_settings(tmp_path), executable="fake")
    calls = []
    class Connection:
        async def call(self, method, params, **kwargs):
            calls.append((method, params))
            if method == "thread/read":
                return {"thread": {"cwd": str(tmp_path), "model": "m1"}}
            assert method == "thread/turns/list" and params["itemsView"] == "full"
            key = params.get("cursor") or "first"
            return {"data": [{"id": key, "status": "completed", "itemsView": "full",
                              "items": [{"type": "agentMessage", "text": key}]}],
                    "nextCursor": "next" if key == "first" else None}
    async def connect():
        return Connection()
    agent.connect = connect
    history = asyncio.run(agent.session_history("native", tmp_path))
    assert [turn.id for turn in history] == ["first", "next"]
    assert [method for method, _ in calls] == ["thread/read", "thread/turns/list", "thread/turns/list"]


def test_strict_claude_resume_does_not_start_a_new_session(tmp_path):
    from omniws.agents.claude import _ResumeFailed
    agent = ClaudeCodeAgent(make_settings(tmp_path), "subscription")
    attempts = []
    async def run(turn, resume):
        attempts.append(resume)
        raise _ResumeFailed()
        yield  # an async generator, like the real SDK stream
    agent._run = run
    async def exercise():
        async for _ in agent.run(SimpleNamespace(temporary=False, native_session="original", require_resume=True)):
            pass
    with pytest.raises(ProviderError):
        asyncio.run(exercise())
    assert attempts == ["original"]


def test_message_uuid_deduplicates_claude_even_if_recorded_prompt_is_wrapped(native_client):
    client, agents = native_client
    item = next(item for item in discover(client) if item["target"]["provider"] == "claude_code")
    cid = item["id"]
    started = client.post(f"/api/conversations/{cid}/messages",
                          json={"text": "質問", "provider": "claude_code", "model": "m1"})
    follow(client, started.json()["run_id"], lambda _: "deny")
    turn = agents["claude_code"].turns[-1]
    turn.source_ids = [turn.id]
    turn.id = "native-user-uuid"
    turn.user = blocks("<context>質問</context>")
    assert len(client.get(f"/api/conversations/{cid}").json()["messages"]) == 4


def test_discovery_requires_login(native_client):
    client, _ = native_client
    client.post("/api/auth/logout")
    assert client.post("/api/sessions/discover").status_code == 401
