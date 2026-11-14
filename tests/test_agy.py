import json
from pathlib import Path

from fastapi.testclient import TestClient

from omniws.agents.agy import AgyAgent, model_options
from omniws.agents.acp import AcpError
from omniws.main import create_app
from omniws.providers.registry import Registry

from .conftest import PASSWORD, FakeProvider, make_settings
from .test_agents import fake_executable, follow


def client_for(tmp_path, **values):
    config = make_settings(tmp_path, **values)
    agent = AgyAgent(config, executable=fake_executable(tmp_path))
    app = create_app(config, web_dist=Path("/nonexistent"), registry=Registry(config, {"fake": FakeProvider("fake")}),
                     agents={"agy": agent})
    return app, agent


def test_agy_is_off_until_env_allows_it(tmp_path):
    app, agent = client_for(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        entry = next(item for item in client.get("/api/executors").json() if item["id"] == "agy")
        assert entry["configured"] is False and entry["setting"] == "OMNI_ALLOW_AGY=true" and entry["models"] == []
        conversation = client.post("/api/conversations", json={"mode": "chat"}).json()
        response = client.post(f"/api/conversations/{conversation['id']}/messages",
                               json={"text": "hi", "provider": "agy", "model": "default"})
        assert response.status_code == 400
        # The settings screen can't turn it on either
        assert client.put("/api/settings/permissions", json={"omni_allow_agy": True}).status_code == 400


def test_agy_runs_code_turns_over_acp_with_approvals(tmp_path):
    app, agent = client_for(tmp_path, omni_allow_agy=True)
    assert agent.acp_argv()[1:] == ["acp"]
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        client.post("/api/projects", json={"name": "app", "git_init": False})
        entry = next(item for item in client.get("/api/executors").json() if item["id"] == "agy")
        assert entry["configured"] and entry["models"][0]["id"] == "default" and entry["modes"] == ["chat", "code"]
        conversation = client.post("/api/conversations", json={"mode": "code", "project": "app"}).json()
        started = client.post(f"/api/conversations/{conversation['id']}/messages",
                              json={"text": "書いて", "provider": "agy", "model": "default"}).json()
        events = follow(client, started["run_id"])
        assert any(event[0] == "permission" for event in events)
        assert (tmp_path / "projects" / "app" / "hello.txt").read_text() == "hi\n"
        detail = client.get(f"/api/conversations/{conversation['id']}").json()
        assert detail["messages"][-1]["status"] == "complete" and detail["messages"][-1]["agent"] == "agy"


def test_agy_learns_models_from_a_session_and_reports_logins(tmp_path):
    config = make_settings(tmp_path, omni_allow_agy=True)
    agent = AgyAgent(config, executable="/bin/true")
    agent.remember_options({"configOptions": [{"id": "model", "options": [
        {"group": "Google", "options": [{"value": "gemini-x", "name": "Gemini X"}]},
        {"value": "claude-y", "name": "Claude Y"}]}]})
    assert model_options({"configOptions": []}) == []
    import asyncio
    models = asyncio.run(agent.list_models())
    assert [model.id for model in models] == ["default", "gemini-x", "claude-y"]
    assert json.loads((config.omni_data_dir / "agy-models.json").read_text())[0]["name"] == "Gemini X"
    error = agent.session_error(AcpError(-32000, "Authentication required"))
    assert error.code == "auth" and "ログイン" in error.message
