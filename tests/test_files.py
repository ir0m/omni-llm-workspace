import base64
import os
import subprocess

import pytest

from omniws.files import IMAGE_LIMIT, TEXT_LIMIT


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def project(client):
    assert client.post("/api/projects", json={"name": "app", "git_init": True}).status_code == 201
    root = client.app.state.settings.omni_projects_dir / "app"
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('before')\n")
    (root / "delete.txt").write_text("old\n")
    (root / "rename.txt").write_text("rename me\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Fixture")
    return root


def test_tree_is_lazy_sorted_and_paginated(client, project):
    for name in ("b.txt", "a.txt"):
        (project / name).write_text(name)
    response = client.get("/api/projects/app/files", params={"limit": 2}).json()
    assert [entry["name"] for entry in response["entries"]] == ["src", "a.txt"]
    assert response["next_offset"] == 2
    second = client.get("/api/projects/app/files", params={"offset": 2, "limit": 2}).json()
    assert second["entries"][0]["name"] == "b.txt"
    child = client.get("/api/projects/app/files", params={"path": "src"}).json()
    assert child["entries"][0]["path"] == "src/main.py"
    assert "main.py" not in [entry["name"] for entry in response["entries"]]


@pytest.mark.parametrize("path", ["../outside", "src/../../outside", ".env", ".env.local", "data/db.sqlite",
                                      ".git/config", ".aws/credentials", "id_ed25519", "private.pem"])
def test_private_paths_and_traversal_are_denied(client, project, path):
    for endpoint in ("files", "file", "image", "diff"):
        response = client.get(f"/api/projects/app/{endpoint}", params={"path": path})
        assert response.status_code == 403


def test_secret_entries_and_git_diffs_are_hidden(client, project):
    for name in (".env", ".env.production", "private.key", "auth.json"):
        (project / name).write_text("placeholder\n")
    (project / ".env.example").write_text("EXAMPLE=\n")
    (project / "data").mkdir()
    (project / "data" / "entry.txt").write_text("placeholder\n")
    git(project, "add", ".")
    names = {entry["name"] for entry in client.get("/api/projects/app/files").json()["entries"]}
    assert ".env.example" in names
    assert not names & {".env", ".env.production", "private.key", "auth.json", "data", ".git"}
    changes = client.get("/api/projects/app/changes").json()
    assert [item["path"] for item in changes["staged"]] == [".env.example"]


def test_links_and_special_files_cannot_escape_or_block_the_server(client, project, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside marker")
    (project / "escape.txt").symlink_to(outside)
    (project / "escape-dir").symlink_to(tmp_path, target_is_directory=True)
    (project / "inside-link").symlink_to(project / "src" / "main.py")
    os.mkfifo(project / "pipe")
    for path in ("escape.txt", "escape-dir/outside.txt", "inside-link", "pipe"):
        response = client.get("/api/projects/app/file", params={"path": path})
        assert response.status_code in (403, 415)
        assert "outside marker" not in response.text
    links = {item["name"]: item["kind"] for item in client.get("/api/projects/app/files").json()["entries"]}
    assert links["escape.txt"] == "link" and links["pipe"] == "other"


def test_registered_project_symlinks_work_but_data_aliases_do_not(client, project):
    root = client.app.state.settings.omni_projects_dir
    (root / "alias").symlink_to(project, target_is_directory=True)
    assert client.get("/api/projects/alias/file", params={"path": "src/main.py"}).json()["kind"] == "text"
    (root / "private").symlink_to(client.app.state.settings.omni_data_dir, target_is_directory=True)
    assert client.get("/api/projects/private/files").status_code == 403
    assert client.get("/api/projects/private/changes").status_code == 403


def test_file_previews_are_bounded_and_html_is_only_text(client, project):
    (project / "page.html").write_text("<script>alert('test')</script>")
    preview = client.get("/api/projects/app/file", params={"path": "page.html"}).json()
    assert preview["kind"] == "text" and "<script>" in preview["text"]
    (project / "large.txt").write_bytes(b"a" * (TEXT_LIMIT - 1) + "あ".encode() + b"x")
    preview = client.get("/api/projects/app/file", params={"path": "large.txt"}).json()
    assert preview["truncated"] and len(preview["text"]) == TEXT_LIMIT - 1
    (project / "binary.bin").write_bytes(b"\xff" + b"a" * TEXT_LIMIT)
    assert client.get("/api/projects/app/file", params={"path": "binary.bin"}).json()["kind"] == "binary"
    assert client.get("/api/projects/app/file", params={"path": "missing"}).status_code == 404


def test_images_are_authenticated_bounded_and_do_not_serve_svg_as_active_content(client, project):
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7l8AAAAASUVORK5CYII=")
    (project / "image.png").write_bytes(png)
    assert client.get("/api/projects/app/file", params={"path": "image.png"}).json()["kind"] == "image"
    response = client.get("/api/projects/app/image", params={"path": "image.png"})
    assert response.content == png and response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff" and response.headers["cache-control"] == "no-store"
    (project / "image.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>test()</script></svg>')
    assert client.get("/api/projects/app/image", params={"path": "image.svg"}).status_code == 415
    assert client.get("/api/projects/app/file", params={"path": "image.svg"}).json()["kind"] == "text"
    with (project / "huge.png").open("wb") as file:
        file.write(png)
        file.truncate(IMAGE_LIMIT + 1)
    assert client.get("/api/projects/app/file", params={"path": "huge.png"}).json()["too_large"]
    assert client.get("/api/projects/app/image", params={"path": "huge.png"}).status_code == 413
    client.post("/api/auth/logout")
    assert client.get("/api/projects/app/image", params={"path": "image.png"}).status_code == 401


def test_git_stages_are_separate_and_diffs_are_read_only(client, project):
    file = project / "src" / "main.py"
    file.write_text("print('staged')\n")
    git(project, "add", "src/main.py")
    file.write_text("print('working')\n")
    (project / "delete.txt").unlink()
    (project / "日本語 space.txt").write_text("new file\n")
    before = git(project, "status", "--porcelain=v1", "-z")
    changes = client.get("/api/projects/app/changes").json()
    assert {item["path"] for item in changes["staged"]} == {"src/main.py"}
    assert {item["path"] for item in changes["unstaged"]} == {"src/main.py", "delete.txt"}
    assert {item["path"] for item in changes["untracked"]} == {"日本語 space.txt"}
    staged = client.get("/api/projects/app/diff", params={"path": "src/main.py", "stage": "staged"}).json()["patch"]
    unstaged = client.get("/api/projects/app/diff", params={"path": "src/main.py", "stage": "unstaged"}).json()["patch"]
    assert "-print('before')" in staged and "+print('staged')" in staged and "working" not in staged
    assert "-print('staged')" in unstaged and "+print('working')" in unstaged
    deleted = client.get("/api/projects/app/diff", params={"path": "delete.txt"}).json()["patch"]
    assert "-old" in deleted
    new = client.get("/api/projects/app/diff", params={"path": "日本語 space.txt", "stage": "untracked"}).json()["patch"]
    assert "+new file" in new
    assert git(project, "status", "--porcelain=v1", "-z") == before


def test_renames_and_literal_pathspecs(client, project):
    git(project, "mv", "rename.txt", "new name.txt")
    special = ":(glob)*.txt"
    (project / special).write_text("literal\n")
    git(project, "--literal-pathspecs", "add", "--", special)
    changes = client.get("/api/projects/app/changes").json()
    rename = next(item for item in changes["staged"] if item["path"] == "new name.txt")
    assert rename["old_path"] == "rename.txt" and rename["status"] == "R"
    patch = client.get("/api/projects/app/diff", params={"path": "new name.txt", "stage": "staged"}).json()["patch"]
    assert "rename me" in patch
    patch = client.get("/api/projects/app/diff", params={"path": special, "stage": "staged"}).json()["patch"]
    assert "+literal" in patch and "rename me" not in patch


def test_git_does_not_run_configured_helpers(client, project):
    marker = project / "helper-ran"
    helper = project / "helper.sh"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    helper.chmod(0o700)
    (project / ".gitattributes").write_text("*.txt diff=custom\n")
    git(project, "config", "core.fsmonitor", str(helper))
    git(project, "config", "diff.custom.command", str(helper))
    git(project, "config", "diff.custom.textconv", str(helper))
    (project / "delete.txt").write_text("changed\n")
    assert client.get("/api/projects/app/changes").status_code == 200
    response = client.get("/api/projects/app/diff", params={"path": "delete.txt"})
    assert response.status_code == 200 and "+changed" in response.json()["patch"]
    assert not marker.exists()


def test_no_git_parent_repository_or_stale_change(client, project):
    root = client.app.state.settings.omni_projects_dir
    (root / "nested").symlink_to(project / "src", target_is_directory=True)
    assert client.get("/api/projects/nested/changes").json()["is_git"] is False
    assert client.get("/api/projects/app/diff", params={"path": "src/main.py"}).status_code == 404
    assert client.get("/api/projects/app/diff", params={"path": "src/main.py", "stage": "invalid"}).status_code == 422
    assert client.get("/api/projects/missing/files").status_code == 404


def test_unborn_repository_can_show_staged_additions(client):
    client.post("/api/projects", json={"name": "new", "git_init": True})
    root = client.app.state.settings.omni_projects_dir / "new"
    (root / "file.txt").write_text("initial\n")
    git(root, "add", ".")
    response = client.get("/api/projects/new/diff", params={"path": "file.txt", "stage": "staged"})
    assert response.status_code == 200 and "+initial" in response.json()["patch"]


def test_large_diff_is_bounded_for_browser_rendering(client, project):
    (project / "delete.txt").write_text("new line\n" * 6000)
    response = client.get("/api/projects/app/diff", params={"path": "delete.txt"})
    assert response.status_code == 200 and response.json()["truncated"]
    assert len(response.json()["patch"].splitlines()) <= 5000
