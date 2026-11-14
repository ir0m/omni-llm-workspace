"""Code-mode projects under OMNI_PROJECTS_DIR.

Folders may be nested up to MAX_DEPTH levels ("work/client/app"). A folder in the path is either a project or a
group: groups only hold other folders and carry a GROUP_MARKER file, which also lists the projects the app made in
them. Directly under the root, every folder that isn't a group is a project (as before nesting). Inside a group,
a folder is a project if it is a Git repository or the app made it.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Any

GIT_TIMEOUT = 5
CLONE_TIMEOUT = 600
MAX_DEPTH = 5
NAME_LIMIT = 200  # the database keeps project names in 200 characters
SEGMENT_LIMIT = 100
GROUP_MARKER = ".omni-group"
GITIGNORE = ".env\n.env.*\n!.env.example\nnode_modules/\n.venv/\n__pycache__/\n*.pyc\ndist/\n.DS_Store\n"
CLONE_URL = re.compile(r"^(https://[^\s]+|ssh://[^\s]+|[\w.-]+@[\w.-]+:[^\s]+)$")


class ProjectError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def segments(name: str) -> list[str]:
    """ "work/client/app" -> ["work", "client", "app"]: each part a plain folder name."""
    name = name.strip().strip("/")
    parts = [part.strip() for part in name.split("/")]
    invalid = (not name or len(name) > NAME_LIMIT or len(parts) > MAX_DEPTH
               or any(not part or len(part) > SEGMENT_LIMIT or part.startswith(".") or "\\" in part or "\0" in part
                      or any(ord(char) < 32 for char in part) for part in parts))
    if invalid:
        raise ProjectError("invalid_name", f"フォルダ名は / 区切りで{MAX_DEPTH}階層まで。各名前の先頭に . は使えません")
    return parts


def check_name(name: str) -> str:
    return "/".join(segments(name))


def project_path(root: Path, name: str) -> Path:
    """The folder for a project name. The project folder itself may be a symlink the user put there on purpose,
    but the groups on the way must be real folders, so a name can never lead outside the root."""
    parts = segments(name)
    for depth in range(1, len(parts)):
        group = root.joinpath(*parts[:depth])
        if group.is_symlink():
            raise ProjectError("invalid_name", f"「{'/'.join(parts[:depth])}」はリンクなので、その中は使えません")
    return root.joinpath(*parts)


def is_group(path: Path) -> bool:
    return not path.is_symlink() and (path / GROUP_MARKER).is_file()


def group_members(group: Path) -> set[str]:
    try:
        return {line.strip() for line in (group / GROUP_MARKER).read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def is_project(root: Path, name: str) -> bool:
    try:
        path = project_path(root, name)
    except ProjectError:
        return False
    if not path.is_dir() or is_group(path):
        return False
    parent = path.parent
    if parent == root:
        return True
    # Inside a group: every group on the way must be a group, and the folder a repository or made by the app
    for depth in range(1, len(segments(name))):
        if not is_group(root.joinpath(*segments(name)[:depth])):
            return False
    return (path / ".git").exists() or path.name in group_members(parent)


def exists(root: Path, name: str) -> bool:
    return is_project(root, name)


async def git_state(path: Path) -> dict[str, Any] | None:
    if not (path / ".git").exists():
        return None
    try:
        # The project's .git/config is writable by agents: never let it run a program (fsmonitor, hooks)
        # behind a plain project listing.
        process = await asyncio.create_subprocess_exec(
            "git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
            "-C", str(path), "status", "--porcelain=v1", "--branch",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
        out, _ = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT)
    except (OSError, TimeoutError):
        return {"branch": None, "changes": None}
    lines = out.decode("utf-8", "replace").splitlines()
    branch = None
    if lines and lines[0].startswith("## "):
        head = lines[0][3:]
        branch = head.removeprefix("No commits yet on ").split("...")[0].strip() or None
        lines = lines[1:]
    return {"branch": branch, "changes": len(lines)}


def find_projects(root: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """(projects, groups) as names relative to the root, walking groups up to MAX_DEPTH."""
    found: list[tuple[str, Path]] = []
    groups: list[str] = []

    def walk(folder: Path, prefix: str, depth: int) -> None:
        members = group_members(folder) if prefix else set()
        try:
            entries = sorted((entry for entry in folder.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
                             key=lambda entry: entry.name.lower())
        except OSError:
            return
        for entry in entries:
            name = f"{prefix}{entry.name}"
            if is_group(entry):
                groups.append(name)
                if depth < MAX_DEPTH:
                    walk(entry, name + "/", depth + 1)
            elif not prefix or (entry / ".git").exists() or entry.name in members:
                found.append((name, entry))

    if root.is_dir():
        walk(root, "", 1)
    return found, groups


async def list_projects(root: Path) -> list[dict[str, Any]]:
    folders, _ = find_projects(root)
    states = await asyncio.gather(*(git_state(folder) for _, folder in folders))
    return [{"name": name, "path": str(folder), "git": state, "modified_at": int(folder.stat().st_mtime * 1000)}
            for (name, folder), state in zip(folders, states)]


def list_groups(root: Path) -> list[str]:
    return find_projects(root)[1]


def run_git(args: list[str], cwd: Path | None = None, timeout: int = 60) -> None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
    try:
        subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as exc:
        last = (exc.stderr or "").strip().splitlines()
        raise ProjectError("git_failed", last[-1] if last else "git が失敗しました") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectError("git_timeout", "時間内に終わりませんでした") from exc


def prepare_parent(root: Path, name: str) -> Path:
    """Makes the groups a new project goes into and returns its folder (which must not exist yet)."""
    parts = segments(name)
    path = project_path(root, name)
    if path.exists() or path.is_symlink():
        raise ProjectError("exists", "同じ名前のフォルダがあります")
    root.mkdir(parents=True, exist_ok=True)
    for depth in range(1, len(parts)):
        group = root.joinpath(*parts[:depth])
        label = "/".join(parts[:depth])
        if group.exists():
            if not is_group(group):
                raise ProjectError("not_group", f"「{label}」はプロジェクトなので、その中には作れません")
            continue
        group.mkdir()
        (group / GROUP_MARKER).write_text("", encoding="utf-8")
    return path


def register(root: Path, name: str) -> None:
    """Remembers a project the app made inside a group, so it is listed even without Git."""
    parts = segments(name)
    if len(parts) < 2:
        return
    group = root.joinpath(*parts[:-1])
    members = group_members(group) | {parts[-1]}
    (group / GROUP_MARKER).write_text("".join(f"{member}\n" for member in sorted(members)), encoding="utf-8")


def create_project(root: Path, name: str, git_init: bool, readme: bool, gitignore: bool) -> Path:
    path = prepare_parent(root, name)
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise ProjectError("exists", "同じ名前のフォルダがあります") from exc
    register(root, name)
    if readme:
        (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    if gitignore:
        (path / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    if git_init:
        run_git(["init", "-b", "main"], cwd=path)
    return path


def clone_name(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail.removesuffix(".git") or "repository"


def clone_project(root: Path, url: str, name: str | None = None) -> Path:
    """Clones into the given path ("work/client/site" becomes the repository), or the repository's name."""
    url = url.strip()
    if not CLONE_URL.match(url) or url.startswith("-"):
        raise ProjectError("invalid_url", "https://… か git@host:… の形で入力してください")
    name = check_name(name or clone_name(url))
    path = prepare_parent(root, name)
    run_git(["clone", "--", url, str(path)], timeout=CLONE_TIMEOUT)
    register(root, name)
    return path
