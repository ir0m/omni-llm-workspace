import json
import stat
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniws.agents.codex import CodexAgent, command_kind, item_update, unwrap
from omniws.main import create_app
from omniws.providers.registry import Registry

from .conftest import PASSWORD, FakeProvider, make_settings
from .test_agents import follow

FAKE = Path(__file__).with_name("fake_codex_app_server.py")


def executable(tmp_path: Path) -> str:
    script = tmp_path / "fake-codex"
    script.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def requests(folder: Path) -> list[dict]:
    path = folder / ".codex-requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def codex_client(tmp_path):
    config = make_settings(tmp_path)
    registry = Registry(config, {"fake": FakeProvider("fake")})
    agent = CodexAgent(config, executable=executable(tmp_path))
    app = create_app(config, web_dist=Path("/nonexistent"), registry=registry, agents={"codex": agent})
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        client.post("/api/projects", json={"name": "app", "git_init": False})
        yield client


def test_helpers(tmp_path):
    assert unwrap("/bin/bash -lc 'cat a.py'") == "cat a.py"
    assert unwrap('/bin/bash -lc "npm test -- -x"') == "npm test -- -x"
    assert unwrap("ls -la") == "ls -la"
    assert command_kind([{"type": "read"}, {"type": "listFiles"}]) == "read"
    assert command_kind([{"type": "search"}, {"type": "read"}]) == "search"
    assert command_kind([{"type": "read"}, {"type": "unknown"}]) == "execute"
    assert command_kind([]) == "execute"
    update = item_update("tool_call_update", {"type": "fileChange", "id": "f", "status": "completed",
                                              "changes": [{"path": str(tmp_path / "a.py"), "kind": {"type": "add"}, "diff": "+x"}]}, tmp_path)
    assert update["kind"] == "edit" and update["path"] == "a.py" and update["diff"].startswith("--- /dev/null\n+++ b/a.py")


def test_native_steer_uses_current_thread_and_turn(codex_client):
    client = codex_client
    cid = client.post('/api/conversations', json={'mode': 'code', 'project': 'app'}).json()['id']
    started = client.post(f'/api/conversations/{cid}/messages', json={
        'text': '作業して', 'provider': 'codex', 'model': 'gpt-b'}).json()

    def steer_and_allow(event):
        response = client.post(f'/api/runs/{started["run_id"]}/input', json={
            'id': str(uuid.uuid4()), 'text': '追加指示'})
        assert response.json()['status'] == 'delivered'
        return 'allow'

    follow(client, started['run_id'], steer_and_allow)
    log = requests(client.app.state.settings.omni_projects_dir / 'app')
    steer = next(item['params'] for item in log if item['method'] == 'turn/steer')
    assert steer == {'threadId': 'thread-1', 'expectedTurnId': 'turn1', 'input': [{'type': 'text', 'text': '追加指示'}]}


def test_executor_entry_shows_plan_models_and_usage(codex_client):
    entry = next(item for item in codex_client.get("/api/executors").json() if item["id"] == "codex")
    assert entry["label"] == "Codex（Plus）" and entry["kind"] == "subscription" and entry["modes"] == ["chat", "code"]
    assert [model["id"] for model in entry["models"]] == ["gpt-b", "gpt-a"]  # default first, hidden left out
    assert entry["models"][1]["efforts"] == ["low", "high"]
    assert entry["usage"]["windows"] == [{"label": "5時間", "usedPercent": 92, "resetsAt": 1},
                                         {"label": "週", "usedPercent": 16, "resetsAt": 2}]


def test_chat_turn_runs_read_only_without_tools(codex_client):
    client = codex_client
    conversation = client.post("/api/conversations", json={}).json()
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "こんにちは", "provider": "codex", "model": "gpt-b"}).json()
    events = follow(client, started["run_id"])
    assert events[-1][1]["status"] == "complete"
    chat_dir = client.app.state.settings.omni_data_dir / "agent-chat"
    start = next(item for item in requests(chat_dir) if item["method"] == "thread/start")["params"]
    assert start["sandbox"] == "read-only" and start["approvalPolicy"] == "never"
    assert start["config"]["features"]["shell_tool"] is False and start["config"]["web_search"] == "live"
    reply = client.get(f"/api/conversations/{conversation['id']}").json()["messages"][-1]
    assert reply["agent"] == "codex" and reply["content"][-1] == {"type": "text", "text": "受け取った: こんにちは"}
    assert reply["usage"]["contextWindow"] == 258400 and reply["usage"]["contextUsed"] == 900


def test_code_turn_asks_the_policy_and_hands_over(codex_client):
    client = codex_client
    conversation = client.post("/api/conversations", json={"mode": "code", "project": "app", "approval_mode": "auto_edit"}).json()
    # Another model wrote the first turn
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "x", "provider": "codex", "model": "gpt-b"}).json()
    follow(client, started["run_id"], lambda event: "allow")
    asked = []
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "テストを直して", "provider": "codex", "model": "gpt-a", "effort": "high"}).json()
    events = follow(client, started["run_id"], lambda event: asked.append(event) or "deny")
    assert events[-1][1]["status"] == "complete"
    # auto_edit: reading and editing inside the project go through; only the command is asked (and denied here)
    assert [event["kind"] for event in asked] == ["execute"] and asked[0]["command"] == "python3 -m pytest -q"
    project = client.app.state.settings.omni_projects_dir / "app"
    log = requests(project)
    answers = {item["method"]: item["params"]["decision"] for item in log if item["method"].startswith("answer/")}
    assert answers == {"answer/read": "accept", "answer/edit": "accept", "answer/run": "decline"}
    starts = [item["params"] for item in log if item["method"] == "thread/start"]
    assert len(starts) == 1 and starts[0]["sandbox"] == "workspace-write" and starts[0]["approvalPolicy"] == "untrusted"
    assert starts[0]["config"]["sandbox_workspace_write"]["network_access"] is True
    turn = [item["params"] for item in log if item["method"] == "turn/start"][-1]
    assert turn["model"] == "gpt-a" and turn["effort"] == "high" and turn["input"][0]["text"] == "テストを直して"
    reply = client.get(f"/api/conversations/{conversation['id']}").json()["messages"][-1]
    kinds = [block["type"] for block in reply["content"]]
    assert kinds[0] == "plan" and "tool_call" in kinds
    tools = {block["id"]: block for block in reply["content"] if block["type"] == "tool_call"}
    assert tools["c1"]["kind"] == "read" and tools["f1"]["kind"] == "edit" and "+a + b" in tools["f1"]["diff"]
    assert tools["c2"]["status"] == "failed" and tools["c2"]["note"] == "拒否しました"


def test_handover_injects_turns_from_other_models(codex_client):
    client = codex_client
    conversation = client.post("/api/conversations", json={}).json()
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "最初の質問", "provider": "fake", "model": "m1"}).json()
    follow(client, started["run_id"])
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "続けて", "provider": "codex", "model": "gpt-b"}).json()
    follow(client, started["run_id"])
    chat_dir = client.app.state.settings.omni_data_dir / "agent-chat"
    injected = next(item for item in requests(chat_dir) if item["method"] == "thread/inject_items")["params"]["items"]
    assert [item["role"] for item in injected] == ["user", "assistant"]
    assert injected[0]["content"][0] == {"type": "input_text", "text": "最初の質問"}
    reply = client.get(f"/api/conversations/{conversation['id']}").json()["messages"][-1]
    assert reply["content"][0] == {"type": "handover", "agent": "Codex", "count": 2}


def test_usage_limit_is_a_rate_limit_error(codex_client):
    client = codex_client
    conversation = client.post("/api/conversations", json={}).json()
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "上限", "provider": "codex", "model": "gpt-b"}).json()
    events = follow(client, started["run_id"])
    assert events[-1][1]["status"] == "error" and events[-1][1]["error"]["code"] == "rate_limit"


def test_temporary_threads_are_ephemeral_without_memory(codex_client):
    client = codex_client
    conversation = client.post("/api/conversations", json={"temporary": True}).json()
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "x", "provider": "codex", "model": "gpt-b"}).json()
    follow(client, started["run_id"])
    chat_dir = client.app.state.settings.omni_data_dir / "agent-chat"
    start = next(item for item in requests(chat_dir) if item["method"] == "thread/start")["params"]
    assert start["ephemeral"] is True and start["config"]["features"]["memories"] is False
    assert start["config"]["history"] == {"persistence": "none"}


def test_switching_chat_to_code_starts_a_writable_thread(codex_client):
    client = codex_client
    conversation = client.post("/api/conversations", json={}).json()
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "相談", "provider": "codex", "model": "gpt-b"}).json()
    follow(client, started["run_id"])
    client.patch(f"/api/conversations/{conversation['id']}", json={"mode": "code", "project": "app"})
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "実装して", "provider": "codex", "model": "gpt-b"}).json()
    follow(client, started["run_id"], lambda event: "allow")
    project = client.app.state.settings.omni_projects_dir / "app"
    log = requests(project)
    start = next(item["params"] for item in log if item["method"] == "thread/start")
    assert start["sandbox"] == "workspace-write" and start["cwd"] == str(project)
    injected = next(item["params"]["items"] for item in log if item["method"] == "thread/inject_items")
    assert [item["role"] for item in injected] == ["user", "assistant"]  # the Chat turns come along



def test_chat_web_search_can_be_disabled(tmp_path):
    from .conftest import make_settings
    from .test_claude import turn
    on = CodexAgent(make_settings(tmp_path)).thread_settings(turn(tmp_path, mode="chat"))
    off = CodexAgent(make_settings(tmp_path, omni_chat_web_search=False)).thread_settings(turn(tmp_path, mode="chat"))
    assert on["config"]["web_search"] == "live" and off["config"]["web_search"] == "disabled"
    assert on["sandbox"] == "read-only" and on["config"]["features"]["shell_tool"] is False


def test_back_to_back_messages_stay_separate_paragraphs(codex_client):
    client = codex_client
    conversation = client.post("/api/conversations", json={}).json()
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "二通", "provider": "codex", "model": "gpt-b"}).json()
    assert follow(client, started["run_id"])[-1][1]["status"] == "complete"
    reply = client.get(f"/api/conversations/{conversation['id']}").json()["messages"][-1]
    assert reply["content"][-1] == {"type": "text", "text": "受け取った: 二通\n\n## 見出し"}
