import datetime

import pytest

from omniws import templates


@pytest.fixture
def editing(client):
    client.app.state.settings.omni_allow_file_edit = True
    assert client.post("/api/projects", json={"name": "app", "git_init": False}).status_code == 201
    return client.app.state.settings.omni_projects_dir


def names(client):
    return {item["name"]: item for item in client.get("/api/templates").json()["templates"]}


def test_bundled_templates_are_listed_with_their_description(client):
    listed = names(client)
    assert {"project-basic", "research-notes", "document", "agent-skill"} <= set(listed)
    basic = listed["project-basic"]
    assert basic["source"] == "builtin" and basic["label"] == "プロジェクトの基本" and basic["description"]
    assert "AGENTS.md" in basic["files"] and ".gitignore" in basic["files"] and "TEMPLATE.md" not in basic["files"]
    assert ".agents/skills/review-changes/SKILL.md" in listed["agent-skill"]["files"]


def test_new_project_from_a_template(client):
    body = {"name": "paper", "git_init": False, "readme": True, "template": "research-notes"}
    assert client.post("/api/projects", json=body).status_code == 201
    root = client.app.state.settings.omni_projects_dir / "paper"
    assert (root / "README.md").read_text() == "# paper\n"  # the option's file was there first and is kept
    assert "# paper の作業ルール" in (root / "AGENTS.md").read_text()
    assert datetime.date.today().isoformat() in (root / "notes" / "experiments.md").read_text()
    assert not (root / "TEMPLATE.md").exists()
    missing = client.post("/api/projects", json={"name": "other", "git_init": False, "template": "nope"})
    assert missing.status_code == 404 and not (root.parent / "other").exists()


def test_plan_shows_each_file_and_apply_writes_only_what_was_chosen(client, editing):
    root = editing / "app"
    (root / "README.md").write_text("# mine\n")
    (root / ".gitignore").write_text((templates.BUILTIN_DIR / "project-basic" / ".gitignore").read_text())
    plan = client.get("/api/templates/project-basic/plan", params={"project": "app"}).json()
    status = {entry["path"]: entry for entry in plan["files"]}
    assert status["AGENTS.md"]["status"] == "new" and status[".gitignore"]["status"] == "same"
    assert status["README.md"]["status"] == "changed" and "-# mine" in status["README.md"]["diff"]
    assert not (root / "AGENTS.md").exists()  # a plan writes nothing

    applied = client.post("/api/templates/project-basic/apply", json={"project": "app", "files": [{"path": "AGENTS.md"}]}).json()
    assert applied["files"] == [{"path": "AGENTS.md", "ok": True, "message": None}]
    assert (root / "README.md").read_text() == "# mine\n"

    # Replacing a file needs the hash the plan showed: a file changed since then is left alone
    (root / "README.md").write_text("# changed again\n")
    stale = [{"path": "README.md", "base_hash": status["README.md"]["hash"]}, {"path": "README.md"}, {"path": "nope.md"}]
    result = client.post("/api/templates/project-basic/apply", json={"project": "app", "files": stale}).json()
    assert [item["ok"] for item in result["files"]] == [False, False, False]
    assert (root / "README.md").read_text() == "# changed again\n"
    fresh = client.get("/api/templates/project-basic/plan", params={"project": "app"}).json()["files"]
    readme = next(entry for entry in fresh if entry["path"] == "README.md")
    body = {"project": "app", "files": [{"path": "README.md", "base_hash": readme["hash"]}]}
    assert client.post("/api/templates/project-basic/apply", json=body).json()["files"][0]["ok"]
    assert (root / "README.md").read_text().startswith("# app\n")


def test_nested_files_are_added_and_links_are_not_followed(client, editing, tmp_path):
    root = editing / "app"
    (root / "docs").symlink_to(tmp_path, target_is_directory=True)
    plan = client.get("/api/templates/document/plan", params={"project": "app"}).json()
    assert {entry["status"] for entry in plan["files"]} == {"blocked"}
    body = {"project": "app", "files": [{"path": "docs/report.md"}]}
    assert not client.post("/api/templates/document/apply", json=body).json()["files"][0]["ok"]
    assert not (tmp_path / "report.md").exists()
    (root / "docs").unlink()
    assert client.post("/api/templates/document/apply", json=body).json()["files"][0]["ok"]
    assert (root / "docs" / "report.md").is_file()


def test_templates_need_the_edit_permission_to_write(client):
    assert client.post("/api/projects", json={"name": "app", "git_init": False}).status_code == 201
    assert client.get("/api/templates/project-basic/plan", params={"project": "app"}).status_code == 200
    body = {"project": "app", "files": [{"path": "AGENTS.md"}]}
    assert client.post("/api/templates/project-basic/apply", json=body).status_code == 403
    assert client.post("/api/templates", json={"name": "mine"}).status_code == 403


def test_own_templates_are_projects_that_can_be_edited_and_replace_bundled_ones(client, editing):
    created = client.post("/api/templates", json={"name": "memo"})
    assert created.status_code == 201 and created.json()["project"] == "_templates/memo"
    assert client.post("/api/templates", json={"name": "memo"}).status_code == 409
    assert "_templates/memo" in [item["name"] for item in client.get("/api/projects").json()["projects"]]
    # Written in Docs like any other project file
    save = {"path": "notes/{{x}}.md", "text": "# {{name}} のメモ\n", "create": True}
    assert client.post("/api/projects/_templates/memo/folder", json={"path": "notes"}).status_code == 201
    assert client.put("/api/projects/_templates/memo/file", json={**save, "path": "notes/memo.md"}).status_code == 200
    info = client.get("/api/projects/_templates/memo/file", params={"path": "TEMPLATE.md", "full": True}).json()
    text = "# 自分のメモ\n\n説明です。\n"
    assert client.put("/api/projects/_templates/memo/file", json={"path": "TEMPLATE.md", "text": text, "base_hash": info["hash"]}).status_code == 200
    memo = names(client)["memo"]
    assert memo["source"] == "user" and memo["label"] == "自分のメモ" and memo["description"] == "説明です。"
    assert memo["files"] == ["notes/memo.md"]
    assert client.post("/api/templates/memo/apply", json={"project": "app", "files": [{"path": "notes/memo.md"}]}).json()["files"][0]["ok"]
    assert (editing / "app" / "notes" / "memo.md").read_text() == "# app のメモ\n"

    # A copy of a bundled template under the same name takes its place and can then be changed
    copy = client.post("/api/templates", json={"name": "project-basic", "source": "template:project-basic"})
    assert copy.status_code == 201 and copy.json()["source"] == "user"
    assert copy.json()["label"] == "プロジェクトの基本" and "AGENTS.md" in copy.json()["files"]
    assert (editing / "_templates" / "project-basic" / "AGENTS.md").read_text().startswith("# {{name}}")


def test_a_project_can_become_a_template_without_its_secrets(client, editing):
    root = editing / "app"
    (root / "guide.md").write_text("guide\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / "binary.bin").write_bytes(b"\0\1")
    (root / "outside").symlink_to(editing.parent)
    made = client.post("/api/templates", json={"name": "from-app", "source": "project:app"})
    assert made.status_code == 201 and made.json()["files"] == ["guide.md"]
    assert sorted(path.name for path in (editing / "_templates" / "from-app").iterdir()) == ["TEMPLATE.md", "guide.md"]


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", ""])
def test_template_names_cannot_leave_the_template_folder(client, editing, name):
    assert client.post("/api/templates", json={"name": name}).status_code == 400
    assert client.post("/api/templates", json={"name": "ok", "source": f"template:{name}"}).status_code in (400, 404)
