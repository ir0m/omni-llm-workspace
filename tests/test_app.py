from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniws.config import Settings
from omniws.main import create_app


def settings(**values) -> Settings:
    return Settings(_env_file=None, **{"omni_allowed_hosts": "testserver", **values})


def test_health():
    client = TestClient(create_app(settings(), web_dist=Path("/nonexistent")))
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unknown_host_is_rejected():
    client = TestClient(create_app(Settings(_env_file=None), web_dist=Path("/nonexistent")))
    assert client.get("/api/health").status_code == 400
    assert client.get("/api/health", headers={"host": "127.0.0.1:8300"}).status_code == 200


def test_wildcard_hosts_are_refused():
    with pytest.raises(ValueError):
        settings(omni_allowed_hosts="*.ts.net").allowed_hosts()


def test_empty_env_values_count_as_unset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OMNI_PORT", "")
    loaded = Settings(_env_file=None)
    assert loaded.anthropic_api_key is None
    assert loaded.omni_port == 8300


def test_project_dirs_expand_home(monkeypatch):
    monkeypatch.setenv("OMNI_PROJECTS_DIR", "~/src")
    assert Settings(_env_file=None).omni_projects_dir == Path.home() / "src"


def test_web_app_is_served_with_fallback(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "index.html").write_text("<div id=root></div>")
    (tmp_path.parent / "secret.txt").write_text("no")
    client = TestClient(create_app(settings(), web_dist=tmp_path))
    assert client.get("/").text == "<div id=root></div>"
    assert client.get("/c/123").text == "<div id=root></div>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/../secret.txt").text != "no"
    assert client.get("/%2e%2e/secret.txt").text != "no"
    assert client.get("/api/unknown").status_code == 404
