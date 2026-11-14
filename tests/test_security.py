import asyncio
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from omniws import projects
from omniws.agents import policy
from omniws.agents.base import PermissionRequest
from omniws.agents.claude import ClaudeCodeAgent
from omniws.main import create_app
from omniws.providers.registry import Registry

from .conftest import PASSWORD, make_settings


def app_for(tmp_path, **values):
    config = make_settings(tmp_path, omni_public_urls="https://app.example.ts.net", **values)
    return create_app(config, web_dist=Path("/nonexistent"), registry=Registry(config, {}), agents={})


def test_other_ports_and_sites_cannot_post(tmp_path):
    with TestClient(app_for(tmp_path, omni_allowed_hosts="testserver,app.example.ts.net")) as client:
        login = {"password": PASSWORD}
        # Another service on the same host name (a different port) is the same "site" for cookies: refused
        for origin in ("http://testserver:8000", "https://app.example.ts.net:8443", "https://evil.example"):
            assert client.post("/api/auth/login", json=login, headers={"Origin": origin}).status_code == 403, origin
        assert client.post("/api/auth/login", json=login, headers={"Origin": "http://testserver"}).status_code == 200
        assert client.post("/api/auth/login", json=login,
                           headers={"Origin": "https://app.example.ts.net"}).status_code == 200


def test_pages_cannot_be_framed_or_load_outside_images(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<div id=root></div>")
    config = make_settings(tmp_path)
    app = create_app(config, web_dist=web, registry=Registry(config, {}), agents={})
    with TestClient(app) as client:
        response = client.get("/")
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "img-src 'self' data: blob:" in csp
        assert response.headers["x-frame-options"] == "DENY"
        assert client.get("/api/health").headers["x-content-type-options"] == "nosniff"


def request(kind, path):
    return PermissionRequest(tool_call_id="t", kind=kind, title=path, paths=[path])


def test_secrets_git_and_app_data_always_ask_below_auto(tmp_path):
    config = make_settings(tmp_path)
    cwd = tmp_path / "projects" / "app"
    for path in (".env", "config/.env.local", ".git/hooks/pre-commit", ".git/config", "deploy/server.pem",
                 str(config.omni_data_dir / "omni.db")):
        assert policy.decide(request("edit", path), "auto_edit", [{"kind": "edit", "outside": True}], config, cwd)[0] == "ask", path
        assert policy.decide(request("read", path), "ask", [], config, cwd)[0] == "ask", path
        assert policy.decide(request("edit", path), "auto", [], config, cwd)[0] == "allow", path
    for path in ("src/main.py", ".env.example", "data/fixtures.json"):
        assert policy.decide(request("edit", path), "auto_edit", [], config, cwd)[0] == "allow", path


def test_project_listing_never_runs_programs_from_git_config(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    marker = tmp_path / "ran"
    # What an agent could write into .git/config: git status would run it as a filesystem monitor
    subprocess.run(["git", "-C", str(project), "config", "core.fsmonitor", f"touch {marker}; false"], check=True)
    state = asyncio.run(projects.git_state(project))
    assert state is not None and not marker.exists()


def test_claude_code_is_denied_reading_secrets(tmp_path):
    config = make_settings(tmp_path)
    rules = ClaudeCodeAgent(config, "api").secret_reads()
    assert "Read(**/.env)" in rules
    assert f"Read(/{config.omni_data_dir.resolve()}/**)" in rules
