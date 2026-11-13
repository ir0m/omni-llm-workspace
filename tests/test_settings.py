from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniws.agents import policy
from omniws.agents.base import PermissionRequest
from omniws.agents.codex import CodexAgent
from omniws.main import create_app
from omniws.models import ExternalSession
from omniws.providers.registry import Registry

from .conftest import PASSWORD, FakeProvider, make_settings
from .test_codex import executable


def make_client(tmp_path, agents=None, **env):
    config = make_settings(tmp_path, **env)
    app = create_app(config, web_dist=Path("/nonexistent"), registry=Registry(config, {"fake": FakeProvider("fake")}),
                     agents=agents or {})
    return app, config


def signed_in(app):
    client = TestClient(app)
    client.__enter__()
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    return client


def test_permissions_can_only_be_stricter_than_env(tmp_path):
    app, config = make_client(tmp_path, omni_approval_max="auto_edit", omni_allow_network=False)
    client = signed_in(app)
    try:
        data = client.get("/api/settings/permissions").json()
        assert data["limits"]["omni_approval_max"] == "auto_edit"
        assert data["values"]["omni_allow_shell"] is True

        # Looser than .env is refused, also through the API
        for body in ({"omni_approval_max": "auto"}, {"omni_allow_network": True}, {"omni_outside_write": "bogus"},
                     {"omni_unknown": 1}):
            response = client.put("/api/settings/permissions", json=body)
            assert response.status_code == 400, body
        assert config.omni_approval_max == "auto_edit"

        data = client.put("/api/settings/permissions", json={"omni_allow_shell": False, "omni_approval_max": "ask",
                                                              "omni_outside_write": "deny",
                                                              "omni_shell_deny_extra": ["npm publish", " ", "a,b"]}).json()
        assert data["values"]["omni_allow_shell"] is False
        assert data["shell_deny_extra"] == ["npm publish"]
        # Applied to the shared settings at once, and the default follows the stricter maximum
        assert config.omni_allow_shell is False and config.omni_approval_default == "ask"
        assert "npm publish" in config.shell_deny() and "sudo" in config.shell_deny()
        assert client.get("/api/config").json()["approval_max"] == "ask"

        # null puts a field back to the .env value
        client.put("/api/settings/permissions", json={"omni_allow_shell": None})
        assert config.omni_allow_shell is True
    finally:
        client.__exit__(None, None, None)

    # Saved overrides are applied again after a restart
    app2, config2 = make_client(tmp_path, omni_approval_max="auto_edit", omni_allow_network=False)
    with TestClient(app2):
        assert config2.omni_approval_max == "ask" and config2.omni_outside_write == "deny"

    # If .env becomes stricter than what was saved, .env wins
    app3, config3 = make_client(tmp_path, omni_approval_max="ask", omni_allow_network=False, omni_outside_write="deny",
                                omni_allow_shell=False)
    with TestClient(app3):
        assert config3.omni_allow_shell is False


def request(kind, outside, command=None):
    return PermissionRequest(tool_call_id="t", kind=kind, title="x", command=command, outside_project=outside)


def test_outside_project_limits(tmp_path):
    config = make_settings(tmp_path)
    read_out, edit_out, edit_in = request("read", True), request("edit", True), request("edit", False)
    assert policy.decide(edit_out, "auto", [], config)[0] == "allow"
    config.omni_outside_write = "ask"
    # Always confirmed: neither auto nor a remembered "always allow" lets it through
    assert policy.decide(edit_out, "auto", [{"kind": "edit", "outside": True}], config)[0] == "ask"
    assert policy.decide(edit_in, "auto", [], config)[0] == "allow"
    config.omni_outside_write = "deny"
    assert policy.decide(edit_out, "auto", [], config) == ("deny", "OMNI_OUTSIDE_WRITE=deny")
    assert policy.decide(request("execute", True, "ls"), "auto", [], config)[0] == "deny"
    assert policy.decide(read_out, "auto", [], config)[0] == "allow"
    config.omni_outside_read = "deny"
    assert policy.decide(read_out, "auto", [], config) == ("deny", "OMNI_OUTSIDE_READ=deny")
    assert policy.decide(request("read", False), "ask", [], config)[0] == "allow"


def test_model_defaults_reach_the_executor_list(tmp_path):
    app, _ = make_client(tmp_path)
    client = signed_in(app)
    try:
        client.put("/api/settings/model-defaults", json={"executor": "fake", "model": "m1", "effort": "high"})
        fake = next(item for item in client.get("/api/executors").json() if item["id"] == "fake")
        assert fake["default"] == {"model": "m1", "effort": "high"}
        client.put("/api/settings/model-defaults", json={"executor": "fake", "model": None})
        fake = next(item for item in client.get("/api/executors").json() if item["id"] == "fake")
        assert fake["default"] is None
    finally:
        client.__exit__(None, None, None)


def test_codex_device_code_login_and_logout(tmp_path):
    config = make_settings(tmp_path)
    agent = CodexAgent(config, executable=executable(tmp_path))
    app, _ = make_client(tmp_path, agents={"codex": agent})
    client = signed_in(app)
    try:
        codex = client.get("/api/settings/accounts").json()["codex"]
        assert codex["signed_in"] and codex["plan"] == "plus" and codex["email"] == "me@example.invalid"
        assert client.post("/api/settings/accounts/codex/logout").status_code == 200
        assert client.get("/api/settings/accounts").json()["codex"]["signed_in"] is False
        login = client.post("/api/settings/accounts/codex/login").json()
        assert login["code"] == "ABCD-1234" and login["url"].startswith("https://")
        codex = client.get("/api/settings/accounts").json()["codex"]
        assert codex["signed_in"] is True and codex["login"] is None
    finally:
        client.__exit__(None, None, None)


def test_server_status_and_restart_guard(tmp_path):
    app, _ = make_client(tmp_path)
    client = signed_in(app)
    try:
        server = client.get("/api/settings/server").json()
        assert server["active_runs"] == 0 and server["version"]
        assert {agent["id"] for agent in server["agents"]} == {"codex", "opencode", "claude_code"}
        # Only `python -m omniws` can start itself again
        assert client.post("/api/settings/server/restart").status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_delete_all_conversations_keeps_them_from_being_imported_again(tmp_path):
    app, _ = make_client(tmp_path)
    client = signed_in(app)
    try:
        client.post("/api/projects", json={"name": "app", "git_init": False})
        chat = client.post("/api/conversations", json={"mode": "chat"}).json()
        code = client.post("/api/conversations", json={"mode": "code", "project": "app"}).json()
        with app.state.sessions() as session:
            session.add(ExternalSession(family="codex", agent="codex", native_id="thread-9", conversation_id=code["id"],
                                        project="app", cwd=str(tmp_path / "projects" / "app"), turns={}, pending=[]))
            session.commit()
        counts = client.get("/api/settings/sessions").json()
        assert counts["conversations"] == 2 and counts["imported"]["codex"] == 1

        assert client.post("/api/settings/conversations/delete-all", json={"confirm": "はい"}).status_code == 400
        assert client.post("/api/settings/conversations/delete-all", json={"confirm": "削除"}).json() == {"deleted": 2}
        assert client.get("/api/conversations").json() == []
        assert client.get(f"/api/conversations/{chat['id']}").status_code == 404
        counts = client.get("/api/settings/sessions").json()
        assert counts["conversations"] == 0 and counts["skipped"] == 1
    finally:
        client.__exit__(None, None, None)
