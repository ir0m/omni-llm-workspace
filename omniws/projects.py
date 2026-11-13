"""Code-mode projects: the folders directly under OMNI_PROJECTS_DIR."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Any

GIT_TIMEOUT = 5
CLONE_TIMEOUT = 600
GITIGNORE = ".env\n.env.*\n!.env.example\nnode_modules/\n.venv/\n__pycache__/\n*.pyc\ndist/\n.DS_Store\n"
CLONE_URL = re.compile(r"^(https://[^\s]+|ssh://[^\s]+|[\w.-]+@[\w.-]+:[^\s]+)$")


class ProjectError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def check_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 100 or name.startswith(".") or any(char in name for char in "/\\\0") \
            or any(ord(char) < 32 for char in name):
        raise ProjectError("invalid_name", "名前に / や先頭の . は使えません")
    return name


def project_path(root: Path, name: str) -> Path:
    """The folder for a project name. Symlinked folders the user put there are allowed on purpose."""
    path = root / check_name(name)
    if path.parent != root:
        raise ProjectError("invalid_name")
    return path


def exists(root: Path, name: str) -> bool:
    try:
        return project_path(root, name).is_dir()
    except ProjectError:
        return False


async def git_state(path: Path) -> dict[str, Any] | None:
    if not (path / ".git").exists():
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(path), "status", "--porcelain=v1", "--branch",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
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


async def list_projects(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    folders = sorted((entry for entry in root.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
                     key=lambda entry: entry.name.lower())
    states = await asyncio.gather(*(git_state(folder) for folder in folders))
    return [{"name": folder.name, "path": str(folder), "git": state, "modified_at": int(folder.stat().st_mtime * 1000)}
            for folder, state in zip(folders, states)]


def run_git(args: list[str], cwd: Path | None = None, timeout: int = 60) -> None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
    try:
        subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as exc:
        last = (exc.stderr or "").strip().splitlines()
        raise ProjectError("git_failed", last[-1] if last else "git が失敗しました") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectError("git_timeout", "時間内に終わりませんでした") from exc


def create_project(root: Path, name: str, git_init: bool, readme: bool, gitignore: bool) -> Path:
    path = project_path(root, name)
    root.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise ProjectError("exists", "同じ名前のフォルダがあります") from exc
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
    url = url.strip()
    if not CLONE_URL.match(url) or url.startswith("-"):
        raise ProjectError("invalid_url", "https://… か git@host:… の形で入力してください")
    path = project_path(root, name or clone_name(url))
    if path.exists():
        raise ProjectError("exists", "同じ名前のフォルダがあります")
    root.mkdir(parents=True, exist_ok=True)
    run_git(["clone", "--", url, str(path)], timeout=CLONE_TIMEOUT)
    return path
