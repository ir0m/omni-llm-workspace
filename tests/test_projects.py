import asyncio
import subprocess

import pytest

from omniws import projects


def test_names_are_checked(tmp_path):
    for bad in ["", " ", ".hidden", "a/b", "..", "x\\y", "a\nb"]:
        with pytest.raises(projects.ProjectError):
            projects.project_path(tmp_path, bad)
    assert projects.project_path(tmp_path, " 日本語のアプリ ").name == "日本語のアプリ"


def test_create_and_list(tmp_path):
    root = tmp_path / "projects"
    projects.create_project(root, "app", git_init=True, readme=True, gitignore=True)
    projects.create_project(root, "notes", git_init=False, readme=False, gitignore=False)
    (root / ".cache").mkdir()
    (root / "file.txt").write_text("not a project")
    listed = {item["name"]: item for item in asyncio.run(projects.list_projects(root))}
    assert list(listed) == ["app", "notes"]
    assert listed["app"]["git"] == {"branch": "main", "changes": 2}  # README.md and .gitignore are untracked
    assert listed["notes"]["git"] is None
    assert (root / "app" / ".gitignore").read_text().startswith(".env")
    with pytest.raises(projects.ProjectError) as error:
        projects.create_project(root, "app", git_init=False, readme=False, gitignore=False)
    assert error.value.code == "exists"


def test_clone_checks_the_url_and_name(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(projects, "run_git", lambda args, cwd=None, timeout=60: calls.append(args))
    path = projects.clone_project(tmp_path, "git@github.com:someone/tool.git")
    assert path == tmp_path / "tool" and calls[-1][:2] == ["clone", "--"]
    assert projects.clone_project(tmp_path, "https://example.com/a/b-c", name="mine").name == "mine"
    for bad in ["file:///etc", "/local/path", "-uhttps://x", "https://a b"]:
        with pytest.raises(projects.ProjectError):
            projects.clone_project(tmp_path, bad)


def test_failed_git_reports_its_last_line(tmp_path):
    with pytest.raises(projects.ProjectError) as error:
        projects.run_git(["-C", str(tmp_path), "log"])
    assert error.value.code == "git_failed" and error.value.message


def test_projects_api_and_conversation_project(client, tmp_path):
    root = client.app.state.settings.omni_projects_dir
    assert client.get("/api/projects").json()["projects"] == []
    assert client.post("/api/projects", json={"name": "web", "git_init": False}).status_code == 201
    assert client.post("/api/projects", json={"name": "web"}).status_code == 409
    assert client.post("/api/projects", json={"name": "../escape"}).status_code == 400
    assert [item["name"] for item in client.get("/api/projects").json()["projects"]] == ["web"]
    assert (root / "web").is_dir()

    created = client.post("/api/conversations", json={"mode": "code", "project": "web"}).json()
    assert created["mode"] == "code" and created["project"] == "web"
    assert client.post("/api/conversations", json={"mode": "code", "project": "missing"}).status_code == 400
    back = client.patch(f"/api/conversations/{created['id']}", json={"mode": "chat"}).json()
    assert back["mode"] == "chat" and back["project"] == "web"  # stays in its project
    out = client.patch(f"/api/conversations/{created['id']}", json={"project": ""}).json()
    assert out["project"] is None and out["mode"] == "chat"


def test_code_turns_need_an_agent(client):
    client.post("/api/projects", json={"name": "web", "git_init": False})
    conversation = client.post("/api/conversations", json={"mode": "code", "project": "web"}).json()
    response = client.post(f"/api/conversations/{conversation['id']}/messages",
                           json={"text": "直して", "provider": "fake", "model": "m1"})
    assert response.status_code == 400 and response.json()["detail"] == "code_needs_agent"
