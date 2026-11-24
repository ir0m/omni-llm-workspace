"""Managed worktrees are normal registered projects, each with its own conversation."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from ..db import now_ms
from ..projects import check_name
from ..files import FileError, ProjectFiles, parts_of, PATCH_LIMIT, PATCH_LINES
from ..history import text_blocks
from ..models import AppSetting, Conversation
from ..agents.base import PermissionRequest
from ..agents.policy import denied_by_env
from .conversations import conversation_dict

router = APIRouter(prefix="/api/projects")
PREFIX = "worktree:"


class NewWorktree(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    base: str = Field(default="HEAD", min_length=1, max_length=200)


def read_record(state, name):
    with state.sessions() as session:
        row = session.get(AppSetting, PREFIX + name)
        if not row:
            raise HTTPException(404, "この作業フォルダはアプリで作成されていません")
        return dict(row.value)


def check_idle(state, *names):
    with state.sessions() as session:
        ids = session.scalars(select(Conversation.id).where(Conversation.project.in_(names)))
        if any(state.runs.active_for(id) for id in ids) or state.terminals:
            raise HTTPException(409, "関連する返答と端末を終了してから操作してください")


def check_write(state, command):
    if not state.settings.omni_allow_project_create or not state.settings.omni_allow_shell:
        raise HTTPException(403, "フォルダ作成とコマンド実行の許可が必要です")
    reason = denied_by_env(PermissionRequest("worktree", "execute", "作業フォルダの操作", command=command,
                                            outside_project=True), state.settings)
    if reason:
        raise HTTPException(403, f"権限の上限により操作できません: {reason}")


def managed(state, name, record):
    source, child = (ProjectFiles(state.settings, n) for n in (record["source"], name))
    if str(source.root) != record["source_path"] or str(child.root) != record["path"]:
        raise HTTPException(409, "作業フォルダの参照先が変わっています")
    common = lambda f: Path(f.git("rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()
    if common(source) != common(child) or child.git("branch", "--show-current").decode().strip() != record["branch"]:
        raise HTTPException(409, "作業フォルダのブランチ・リポジトリが変わっています")
    return source, child


async def operation(fn):
    try:
        return await run_in_threadpool(fn)
    except FileError as exc:
        raise HTTPException(exc.status, exc.message) from exc


@router.get("/{name:path}/worktrees")
async def list_worktrees(name: str, request: Request):
    state = request.app.state
    def read():
        with state.sessions() as session:
            items = []
            for row in session.scalars(select(AppSetting).where(AppSetting.key.startswith(PREFIX))):
                if row.value["source"] != name:
                    continue
                item = dict(row.value)
                conversation = session.get(Conversation, item["conversation_id"])
                item["conversation"] = conversation_dict(conversation, state.runs) if conversation else None
                items.append(item)
            return {"items": items, "can_create": state.settings.omni_allow_project_create and state.settings.omni_allow_shell}
    return await operation(read)


@router.get("/{name:path}/branches")
async def branches(name: str, request: Request):
    def read():
        files = ProjectFiles(request.app.state.settings, name)
        if not files.changes()["is_git"]:
            raise HTTPException(400, "Gitリポジトリで選択してください")
        raw = files.git("for-each-ref", "--sort=refname", "--format=%(refname)%00%(symref)%00%(HEAD)",
                        "refs/heads/", "refs/remotes/")
        items, current = [], None
        for line in raw.decode("utf-8", "replace").splitlines():
            ref, symbolic, checked = line.split("\0")
            if symbolic:
                continue  # origin/HEAD is an alias, not another branch.
            remote = ref.startswith("refs/remotes/")
            items.append({"ref": ref, "name": ref.removeprefix("refs/remotes/" if remote else "refs/heads/"),
                          "remote": remote})
            if checked == "*":
                current = ref
        try:
            head = files.git("rev-parse", "--verify", "HEAD").decode().strip()
        except FileError as exc:
            if exc.status != 409:
                raise
            head = None  # A repository without its first commit has no starting point yet.
        return {"branches": items, "current_ref": current, "head": head}
    return await operation(read)


@router.post("/{name:path}/worktrees", status_code=201)
async def create_worktree(name: str, body: NewWorktree, request: Request):
    state = request.app.state
    async with state.worktree_lock:
        def create():
            check_write(state, "git worktree add")
            files = ProjectFiles(state.settings, name)
            if not files.changes()["is_git"]:
                raise HTTPException(400, "Gitリポジトリで作成してください")
            if body.base.startswith("-"):
                raise HTTPException(400, "開始地点が正しくありません")
            base = files.git("rev-parse", "--verify", "--end-of-options", body.base + "^{commit}").decode().strip()
            suffix = uuid.uuid4().hex[:12]
            # Next to the source, in the same group: "work/client/app" -> "work/client/app-task-…"
            parent, _, leaf = check_name(name).rpartition("/")
            child = f"{parent + '/' if parent else ''}{leaf[:60]}-task-{suffix}"
            branch = "omni/" + suffix
            path = state.settings.omni_projects_dir / child
            # Checkout filters are programs too. Do not run configured helpers behind the GUI action.
            configs = files.git("config", "--null", "--list", "--includes").split(b"\0")
            drivers = {entry.split(b"\n", 1)[0].decode("utf-8", "replace").rsplit(".", 1)[0]
                       for entry in configs if entry.startswith(b"filter.") and b"\n" in entry}
            overrides = []
            for driver in drivers:
                for suffix_, value in (("smudge", ""), ("process", ""), ("required", "false")):
                    overrides += ["-c", f"{driver}.{suffix_}={value}"]
            try:
                files.git(*overrides, "worktree", "add", "-b", branch, "--", str(path), base)
            except FileError:
                if path.exists():
                    try:
                        files.git("worktree", "remove", "--", str(path))
                    except FileError:
                        raise HTTPException(409, f"作成に失敗しました。残ったフォルダ {child} を確認してください")
                raise
            now = now_ms()
            conversation = Conversation(id=str(uuid.uuid4()), title=body.title.strip(), mode="code", project=child, agent_sessions=[],
                                        sort_key=float(now), created_at=now, updated_at=now)
            record = {"name": child, "source": name, "branch": branch, "base": base,
                      "title": body.title.strip(), "conversation_id": conversation.id,
                      "path": str(path.resolve()), "source_path": str(files.root)}
            try:
                with state.sessions.begin() as session:
                    session.add(conversation)
                    session.add(AppSetting(key=PREFIX + child, value=record))
            except Exception:
                files.git("worktree", "remove", "--", str(path))
                raise
            return {**record, "conversation": conversation_dict(conversation, state.runs)}
        return await operation(create)


@router.get("/{name:path}/worktree-result")
async def result(name: str, request: Request):
    state = request.app.state
    def read():
        record = read_record(state, name)
        files = ProjectFiles(state.settings, name)
        changes = files.changes()
        paths = files.git("diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "--no-renames", record["base"], "--").split(b"\0")
        committed = [path.decode("utf-8", "replace") for path in paths if path]
        committed = [path for path in committed if files.allowed(parts_of(path))]
        with state.sessions() as session:
            conversation = session.get(Conversation, record["conversation_id"])
            last = next((m for m in reversed(conversation.messages) if m.role == "assistant"), None) if conversation else None
            answer = text_blocks(last.content) if last else ""
        return {"changes": changes, "paths": committed, "answer": answer[-20000:]}
    return await operation(read)


@router.get("/{name:path}/worktree-diff")
async def diff(name: str, request: Request, path: str):
    state = request.app.state
    def read():
        record = read_record(state, name)
        files = ProjectFiles(state.settings, name)
        if not files.allowed(parts_of(path)):
            raise HTTPException(403, "このパスは表示できません")
        try:
            with files.opened(path):
                pass
        except FileError as exc:
            if exc.status != 404:
                raise
        patch = files.git("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                          record["base"], "--", path).decode("utf-8", "replace")
        lines = patch[:PATCH_LIMIT].splitlines(keepends=True)
        return {"path": path, "patch": "".join(lines[:PATCH_LINES]),
                "truncated": len(patch) > PATCH_LIMIT or len(lines) > PATCH_LINES}
    return await operation(read)


@router.post("/{name:path}/worktree-merge")
async def merge(name: str, request: Request):
    state = request.app.state
    async with state.worktree_lock, state.external_sessions.lock:
        def integrate():
            check_write(state, "git merge --ff-only")
            record = read_record(state, name)
            check_idle(state, name, record["source"])
            source, child = managed(state, name, record)
            for files in (source, child):
                if files.git("status", "--porcelain=v1", "--untracked-files=all").strip():
                    raise HTTPException(409, "元フォルダと作業フォルダの変更をコミットまたは整理してから統合してください")
            head = child.git("rev-parse", "HEAD").decode().strip()
            try:
                source.git("merge", "--ff-only", "--", head)
            except FileError as exc:
                raise HTTPException(409, "自動で直線的に統合できません。端末でブランチの分岐・競合を確認してください") from exc
            return {"ok": True}
        return await operation(integrate)


@router.delete("/{name:path}/worktree")
async def remove(name: str, request: Request):
    state = request.app.state
    async with state.worktree_lock, state.external_sessions.lock:
        def cleanup():
            check_write(state, "git worktree remove")
            record = read_record(state, name)
            check_idle(state, name, record["source"])
            source, child = managed(state, name, record)
            if child.git("status", "--porcelain=v1", "--untracked-files=all", "--ignored").strip():
                raise HTTPException(409, "未コミットまたは無視されたファイルがあります。端末で整理してから削除してください")
            source.git("worktree", "remove", "--", str(child.root))
            with state.sessions.begin() as session:
                session.delete(session.get(AppSetting, PREFIX + name))
            return {"ok": True}
        return await operation(cleanup)
