import asyncio
import subprocess

import pytest

from omniws import projects


def test_names_are_checked(tmp_path):
    for bad in ["", " ", ".hidden", "..", "x\\y", "a\nb", "a//b", "a/../b", "a/.git", "a/b/c/d/e/f", "a/" + "x" * 101]:
        with pytest.raises(projects.ProjectError):
            projects.project_path(tmp_path, bad)
    assert projects.project_path(tmp_path, " 日本語のアプリ ").name == "日本語のアプリ"
    assert projects.check_name("/仕事/ 顧客A /app/") == "仕事/顧客A/app"
    assert projects.project_path(tmp_path, "a/b/c/d/e") == tmp_path / "a" / "b" / "c" / "d" / "e"


def test_nested_projects_live_in_groups(tmp_path):
    root = tmp_path / "projects"
    projects.create_project(root, "仕事/顧客A/app", git_init=False, readme=True, gitignore=False)
    projects.create_project(root, "仕事/顧客A/site", git_init=True, readme=False, gitignore=False)
    projects.create_project(root, "top", git_init=False, readme=False, gitignore=False)
    # A folder someone made by hand inside a group is neither Git nor made by the app: not a project
    (root / "仕事" / "顧客A" / "scratch").mkdir()
    listed = [item["name"] for item in asyncio.run(projects.list_projects(root))]
    assert listed == ["top", "仕事/顧客A/app", "仕事/顧客A/site"]
    assert projects.list_groups(root) == ["仕事", "仕事/顧客A"]
    assert projects.exists(root, "仕事/顧客A/app") and not projects.exists(root, "仕事") \
        and not projects.exists(root, "仕事/顧客A/scratch")
    # Groups never go inside a project, and a group can't be made a project
    for name in ("top/sub", "仕事/顧客A/app/inner", "仕事"):
        with pytest.raises(projects.ProjectError):
            projects.create_project(root, name, git_init=False, readme=False, gitignore=False)


def test_groups_cannot_be_links_out_of_the_root(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "app").mkdir(parents=True)
    (outside / "app" / ".git").mkdir()
    (outside / projects.GROUP_MARKER).write_text("app\n")
    (root / "linked").symlink_to(outside)
    with pytest.raises(projects.ProjectError):
        projects.project_path(root, "linked/app")
    assert not projects.exists(root, "linked/app")
    with pytest.raises(projects.ProjectError):
        projects.create_project(root, "linked/new", git_init=False, readme=False, gitignore=False)


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
    # A path in the name: the repository goes exactly there, in new groups
    nested = projects.clone_project(tmp_path, "https://example.com/a/site.git", name="仕事/顧客A/site")
    assert nested == tmp_path / "仕事" / "顧客A" / "site" and calls[-1][-1] == str(nested)
    assert projects.is_group(tmp_path / "仕事" / "顧客A") and "site" in projects.group_members(tmp_path / "仕事" / "顧客A")


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


def test_nested_projects_through_the_api(client):
    created = client.post("/api/projects", json={"name": "仕事/顧客A/app", "git_init": True, "readme": True})
    assert created.status_code == 201 and created.json() == {"name": "仕事/顧客A/app"}
    listing = client.get("/api/projects").json()
    assert [item["name"] for item in listing["projects"]] == ["仕事/顧客A/app"]
    assert listing["groups"] == ["仕事", "仕事/顧客A"] and listing["max_depth"] == 5
    # Both a raw and an encoded slash reach the project
    for name in ("仕事/顧客A/app", "仕事%2F顧客A%2Fapp"):
        files = client.get(f"/api/projects/{name}/files")
        assert files.status_code == 200, name
        assert [entry["name"] for entry in files.json()["entries"]] == ["README.md"]
    assert client.get("/api/projects/仕事/顧客A/app/changes").json()["is_git"] is True
    # A group is not a project
    assert client.get("/api/projects/仕事/files").status_code == 404
    assert client.post("/api/conversations", json={"mode": "code", "project": "仕事"}).status_code == 400
    conversation = client.post("/api/conversations", json={"mode": "code", "project": "仕事/顧客A/app"})
    assert conversation.status_code == 201 and conversation.json()["project"] == "仕事/顧客A/app"
    assert client.post("/api/projects", json={"name": "仕事/顧客A/app/inner"}).status_code == 400
