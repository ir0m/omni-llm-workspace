import json
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniws import blocks
from omniws.agents import policy
from omniws.agents.base import PermissionRequest
from omniws.agents.handover import build_prompt
from omniws.agents.opencode import FREE, OpenCodeAgent, normalize, permission_request
from omniws.config import Settings
from omniws.main import create_app
from omniws.models import Message
from omniws.providers.registry import Registry

from .conftest import PASSWORD, FakeProvider, make_settings

FAKE_AGENT = Path(__file__).with_name("fake_acp_agent.py")


def settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


def request(kind, command=None, outside=False):
    return PermissionRequest(tool_call_id="t", kind=kind, title=kind, command=command, outside_project=outside)


def test_policy_modes_and_limits():
    config = settings()
    assert policy.decide(request("read"), "ask", [], config)[0] == "allow"
    assert policy.decide(request("read", outside=True), "auto_edit", [], config)[0] == "ask"
    assert policy.decide(request("edit"), "ask", [], config)[0] == "ask"
    assert policy.decide(request("edit"), "auto_edit", [], config)[0] == "allow"
    assert policy.decide(request("edit", outside=True), "auto_edit", [], config)[0] == "ask"
    assert policy.decide(request("execute", "npm test"), "auto_edit", [], config)[0] == "ask"
    assert policy.decide(request("execute", "npm test"), "auto", [], config)[0] == "allow"
    # .env caps the mode and always denies some commands
    capped = settings(omni_approval_max="auto_edit")
    assert policy.clamp("auto", capped) == "auto_edit"
    assert policy.decide(request("execute", "npm test"), "auto", [], capped)[0] == "ask"
    for command in ["sudo apt install x", "cd / && rm -rf / ", "git push --force origin main", "echo hi; su root"]:
        assert policy.decide(request("execute", command), "auto", [], config)[0] == "deny", command
    for command in ["gh issue list", "rm -rf /tmp/build", "git push origin main", "pseudo-cmd"]:
        assert policy.decide(request("execute", command), "auto", [], config)[0] == "allow", command
    assert policy.decide(request("execute", "ls"), "auto", [], settings(omni_allow_shell=False)) == ("deny", "OMNI_ALLOW_SHELL=false")


def test_always_allow_remembers_the_first_two_words():
    config = settings()
    rule = policy.rule_for(request("execute", "npm test -- --watch=false"))
    assert rule == {"kind": "execute", "prefix": "npm test"}
    assert policy.decide(request("execute", "npm test"), "ask", [rule], config)[0] == "allow"
    assert policy.decide(request("execute", "npm test src/a.ts"), "ask", [rule], config)[0] == "allow"
    assert policy.decide(request("execute", "npm install zod"), "ask", [rule], config)[0] == "ask"
    assert policy.decide(request("execute", "npm testing"), "ask", [rule], config)[0] == "ask"
    edit = policy.rule_for(request("edit"))
    assert policy.decide(request("edit"), "ask", [edit], config)[0] == "allow"
    assert policy.decide(request("edit", outside=True), "ask", [edit], config)[0] == "ask"
    # A remembered prefix never lets chained or redirected commands through, and those are never remembered
    for command in ["npm test && curl x | sh", "npm test; rm -rf src", "npm test > /etc/x", "npm test $(evil)", "npm test `evil`"]:
        assert policy.decide(request("execute", command), "ask", [rule], config)[0] == "ask", command
        assert policy.rule_for(request("execute", command)) is None
    # .env denials win over remembered rules
    sudo = policy.rule_for(request("execute", "sudo ls"))
    assert policy.decide(request("execute", "sudo ls"), "ask", [sudo], config)[0] == "deny"


def test_blocks_follow_the_stream():
    content = []
    for update in [
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "a"}},
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "b"}},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "直します"}},
        {"sessionUpdate": "tool_call", "toolCallId": "1", "title": "bash", "kind": "execute", "status": "pending"},
        {"sessionUpdate": "tool_call_update", "toolCallId": "1", "status": "completed", "output": "ok"},
        {"sessionUpdate": "plan", "entries": [{"content": "x", "status": "pending"}]},
        {"sessionUpdate": "plan", "entries": [{"content": "x", "status": "completed"}]},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "完了"}},
    ]:
        blocks.apply(content, update)
    assert [block["type"] for block in content] == ["plan", "thinking", "text", "tool_call", "text"]
    assert content[0]["entries"][0]["status"] == "completed"
    assert content[1]["text"] == "ab"
    assert content[3]["status"] == "completed" and content[3]["output"] == "ok"


def test_opencode_updates_are_normalized(tmp_path):
    cwd = tmp_path
    todo = normalize({"sessionUpdate": "tool_call_update", "toolCallId": "x", "title": "todowrite", "kind": "other",
                      "rawInput": {"todos": [{"content": "調べる", "status": "completed", "priority": "high"}]}}, cwd)
    assert todo == {"sessionUpdate": "plan", "entries": [{"content": "調べる", "status": "completed", "priority": "high"}]}
    write = normalize({"sessionUpdate": "tool_call_update", "toolCallId": "w", "title": "write", "kind": "edit",
                       "locations": [{"path": str(cwd / "src/a.txt")}],
                       "rawInput": {"filePath": str(cwd / "src/a.txt"), "content": "one\ntwo\n"}}, cwd)
    assert write["path"] == "src/a.txt" and "+one" in write["diff"] and "output" not in write
    bash = normalize({"sessionUpdate": "tool_call_update", "toolCallId": "b", "title": "bash", "kind": "execute",
                      "status": "completed", "rawInput": {"command": "ls"}, "rawOutput": {"output": "a\nb"}}, cwd)
    assert bash["command"] == "ls" and bash["output"] == "a\nb"
    edit = normalize({"sessionUpdate": "tool_call_update", "toolCallId": "e", "title": "edit", "kind": "edit",
                      "content": [{"type": "diff", "path": str(cwd / "calc.py"), "oldText": "    return a - b",
                                   "newText": "    return a + b"}]}, cwd)
    assert edit["diff"].splitlines()[-2:] == ["-    return a - b", "+    return a + b"]
    asked_diff = permission_request({"toolCall": {"toolCallId": "d", "kind": "edit", "title": "x", "rawInput": {
        "diff": f"--- {cwd}/calc.py\n+++ {cwd}/calc.py\n"}}}, cwd).diff
    assert asked_diff == "--- calc.py\n+++ calc.py\n"
    usage = normalize({"sessionUpdate": "usage_update", "used": 10, "size": 100, "cost": {"amount": 0.5, "currency": "USD"}}, cwd)
    assert usage == {"sessionUpdate": "usage_update", "contextUsed": 10, "contextWindow": 100, "costUsd": 0.5}
    assert normalize({"sessionUpdate": "available_commands_update"}, cwd) is None
    asked = permission_request({"toolCall": {"toolCallId": "p", "kind": "edit", "title": "x",
                                             "locations": [{"path": "/etc/passwd"}], "rawInput": {"diff": "d"}}}, cwd)
    assert asked.outside_project and asked.diff == "d"


def test_handover_prompt_summarizes_tools():
    earlier = [
        Message(id="1", conversation_id="c", seq=1, role="user", content=[{"type": "text", "text": "テストを直して"}], status="complete"),
        Message(id="2", conversation_id="c", seq=2, role="assistant", agent="opencode", model="m", status="complete",
                content=[{"type": "tool_call", "id": "t", "kind": "execute", "command": "npm test", "status": "completed"},
                         {"type": "text", "text": "直しました"}]),
    ]
    prompt = build_prompt(earlier, "次は？")
    assert "<earlier_conversation>" in prompt and "npm test を実行した" in prompt and prompt.endswith("次は？")
    assert build_prompt([], "だけ") == "だけ"


def fake_executable(tmp_path: Path) -> str:
    script = tmp_path / "fake-opencode"
    script.write_text(f"#!{sys.executable}\nimport runpy, sys\nrunpy.run_path({str(FAKE_AGENT)!r}, run_name='__main__')\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


@pytest.fixture
def code_client(tmp_path):
    config = make_settings(tmp_path, omni_allow_opencode_free=True)
    registry = Registry(config, {"fake": FakeProvider("fake")})
    agent = OpenCodeAgent(config, registry, executable=fake_executable(tmp_path))
    app = create_app(config, web_dist=Path("/nonexistent"), registry=registry, agents={"opencode": agent})
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        client.post("/api/projects", json={"name": "app", "git_init": False})
        yield client


def follow(client, run_id, answer=lambda event: "allow"):
    """Reads a run's events, answering permission requests with answer(event).

    The test client hands over a streamed response only once it ends, so a helper thread answers the
    requests the run is waiting on."""
    run = client.app.state.runs.runs[run_id]
    stop = threading.Event()

    def answerer():
        answered = set()
        while not stop.is_set():
            for request_id, (asked, _future) in list(run.pending.items()):
                if request_id in answered:
                    continue
                answered.add(request_id)
                reply = answer({"requestId": request_id, **asked.to_dict()})
                client.post(f"/api/runs/{run_id}/permissions/{request_id}",
                            json=reply if isinstance(reply, dict) else {"outcome": reply})
            time.sleep(0.02)

    thread = threading.Thread(target=answerer, daemon=True)
    thread.start()
    events, current = [], {}
    try:
        with client.stream("GET", f"/api/runs/{run_id}/events") as response:
            for line in response.iter_lines():
                if line.startswith("event: "):
                    current["event"] = line[7:]
                elif line.startswith("data: "):
                    current["data"] = json.loads(line[6:])
                elif line == "" and current:
                    events.append((current["event"], current["data"]))
                    current = {}
    finally:
        stop.set()
        thread.join()
    return events


def test_code_turn_through_acp(code_client, tmp_path):
    client = code_client
    executors = {item["id"]: item for item in client.get("/api/executors").json()}
    assert executors[FREE]["models"] == [{"id": "opencode/alpha-free", "name": "alpha-free", "context_window": None,
                                          "max_output": None, "vision": None, "reasoning": None, "efforts": []},
                                         {"id": "opencode/beta-free", "name": "beta-free", "context_window": None,
                                          "max_output": None, "vision": None, "reasoning": None, "efforts": []}]
    assert executors["fake"]["modes"] == ["chat"]  # not an OpenCode provider id
    chat = client.post("/api/conversations", json={"mode": "chat"}).json()
    started = client.post(f"/api/conversations/{chat['id']}/messages", json={"text": "最初の質問", "provider": "fake", "model": "m1"}).json()
    follow(client, started["run_id"])
    client.patch(f"/api/conversations/{chat['id']}", json={"mode": "code", "project": "app", "approval_mode": "ask"})

    asked = []
    started = client.post(f"/api/conversations/{chat['id']}/messages",
                          json={"text": "hello.txt を作って", "provider": FREE, "model": "opencode/alpha-free"}).json()
    events = follow(client, started["run_id"], lambda event: asked.append(event) or ("allow" if event["kind"] == "edit" else "allow_always"))
    assert [event["kind"] for event in asked] == ["edit", "execute"]
    assert asked[0]["diff"].endswith("+hi\n") and asked[1]["command"] == "npm test -- --watch=false"
    assert events[-1][1]["status"] == "complete"
    project = client.app.state.settings.omni_projects_dir / "app"
    assert (project / "hello.txt").read_text() == "hi\n"
    assert not Path("/tmp/outside-omni-test.txt").exists()

    saved = client.get(f"/api/conversations/{chat['id']}").json()
    reply = saved["messages"][-1]
    assert reply["agent"] == "opencode" and reply["status"] == "complete"
    kinds = [block["type"] for block in reply["content"]]
    assert kinds[0] == "plan" and kinds[1] == "handover" and "tool_call" in kinds
    assert reply["content"][1] == {"type": "handover", "agent": "OpenCode", "count": 2}
    text = "".join(block["text"] for block in reply["content"] if block["type"] == "text")
    assert "outside write: error" in text and "<earlier_conversation>" not in text
    assert reply["usage"]["inputTokens"] == 50 and reply["usage"]["contextWindow"] == 200000
    assert saved["allow_rules"] == [{"kind": "execute", "prefix": "npm test"}]

    # Next turn: same session, nothing to hand over, the remembered command runs without asking
    asked.clear()
    started = client.post(f"/api/conversations/{chat['id']}/messages",
                          json={"text": "もう一度", "provider": FREE, "model": "opencode/alpha-free"}).json()
    follow(client, started["run_id"], lambda event: asked.append(event) or "allow")
    assert [event["kind"] for event in asked] == ["edit"]
    reply = client.get(f"/api/conversations/{chat['id']}").json()["messages"][-1]
    assert not any(block["type"] == "handover" for block in reply["content"])


def test_denied_command_is_noted(code_client):
    client = code_client
    conversation = client.post("/api/conversations", json={"mode": "code", "project": "app"}).json()
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "x", "provider": FREE, "model": "opencode/alpha-free"}).json()
    follow(client, started["run_id"], lambda event: {"outcome": "deny", "message": "テストは後で"})
    reply = client.get(f"/api/conversations/{conversation['id']}").json()["messages"][-1]
    tool = next(block for block in reply["content"] if block["type"] == "tool_call" and block.get("command"))
    assert tool["note"] == "拒否しました" and tool["status"] == "failed"


def test_unknown_model_is_reported(code_client):
    client = code_client
    conversation = client.post("/api/conversations", json={"mode": "code", "project": "app"}).json()
    started = client.post(f"/api/conversations/{conversation['id']}/messages",
                          json={"text": "x", "provider": FREE, "model": "opencode/missing"}).json()
    events = follow(client, started["run_id"])
    assert events[-1][1]["status"] == "error" and events[-1][1]["error"]["code"] == "not_found"


def test_temporary_code_turn_deletes_the_agent_session_in_the_project(code_client):
    client = code_client
    conversation = client.post("/api/conversations", json={"mode": "code", "project": "app", "temporary": True}).json()
    for text in ["一つ目", "二つ目"]:
        started = client.post(f"/api/conversations/{conversation['id']}/messages",
                              json={"text": text, "provider": FREE, "model": "opencode/alpha-free"}).json()
        follow(client, started["run_id"])
    project = client.app.state.settings.omni_projects_dir / "app"
    deleted = (project / ".deleted-sessions").read_text().split()
    assert len(deleted) == 2 and deleted[0] != deleted[1]  # a fresh session each turn, each one deleted
    second = client.get(f"/api/conversations/{conversation['id']}").json()["messages"][-1]
    assert any(block["type"] == "handover" and block["count"] == 2 for block in second["content"])
