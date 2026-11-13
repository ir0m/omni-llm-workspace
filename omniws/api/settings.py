"""The settings screen: accounts and CLIs, permissions (never looser than `.env`), the server, sessions."""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__, app_settings, projects
from ..agents.jsonrpc import RpcError
from ..external_sessions import remember_deleted
from ..models import Conversation, ExternalSession
from ..providers.base import ProviderError
from .executors import restart_required

router = APIRouter(prefix="/api/settings")
CONFIRM_WORD = "削除"


def db(request: Request):
    yield from request.app.state.get_session()


def active_runs(request: Request) -> int:
    return sum(1 for run in request.app.state.runs.runs.values() if not run.done)


def failure(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# --- Permissions ------------------------------------------------------------------------------------------

@router.get("/permissions")
def get_permissions(request: Request, session: Session = Depends(db)):
    state = request.app.state
    return app_settings.permissions(state.settings, state.env_settings, app_settings.read(session, app_settings.PERMISSIONS, {}))


@router.put("/permissions")
def put_permissions(body: dict[str, Any], request: Request, session: Session = Depends(db)):
    """Merges the changes; null puts a field back to the `.env` value."""
    state = request.app.state
    saved = app_settings.read(session, app_settings.PERMISSIONS, {})
    merged = {**saved, **body}
    merged = {field: value for field, value in merged.items() if value is not None}
    try:
        clean = app_settings.check(state.env_settings, merged)
    except app_settings.SettingError as exc:
        raise failure(400, "looser_than_env", exc.message) from exc
    app_settings.write(session, app_settings.PERMISSIONS, clean)
    session.commit()
    app_settings.apply(state.settings, state.env_settings, clean)
    return app_settings.permissions(state.settings, state.env_settings, clean)


# --- Accounts and CLIs ------------------------------------------------------------------------------------

async def codex_account(codex, refresh: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {"installed": bool(codex and codex.installed()), "signed_in": False, "error": None,
                             "login": None}
    if not entry["installed"]:
        return entry
    try:
        await codex.refresh_account(force=refresh or bool(codex.login))
    except (ProviderError, RpcError, OSError, asyncio.TimeoutError) as exc:
        entry["error"] = getattr(exc, "message", None) or "Codex の状態を読み込めませんでした"
        return entry
    account = codex.account or {}
    entry.update(signed_in=bool(account), kind=account.get("type"), email=account.get("email"),
                 plan=account.get("planType"), workspace=account.get("workspaceName"), usage=codex.usage(),
                 login=codex.login)
    if codex.login and account:
        # Signing in is offered only while signed out, so an account means it finished
        codex.login = None
        entry["login"] = None
    return entry


async def claude_account(agent, settings, refresh: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {"allowed": settings.omni_allow_claude_subscription, "signed_in": False, "error": None}
    if not agent or not agent.installed():
        return entry
    try:
        info = await agent.server_info(refresh)
    except ProviderError as exc:
        entry["error"] = exc.message
        return entry
    account = info.get("account") or {}
    plan = account.get("subscriptionType")
    entry.update(signed_in=bool(plan), plan=plan, email=account.get("email"), usage=agent.usage())
    return entry


@router.get("/accounts")
async def accounts(request: Request, refresh: bool = False):
    state = request.app.state
    codex, claude = await asyncio.gather(
        codex_account(state.agents.get("codex"), refresh),
        claude_account(state.agents.get("claude_code"), state.settings, refresh))
    with state.sessions() as session:
        defaults = app_settings.read(session, app_settings.MODEL_DEFAULTS, {})
    return {"codex": codex, "claude": claude, "model_defaults": defaults}


def codex_agent(request: Request):
    codex = request.app.state.agents.get("codex")
    if not codex or not codex.installed():
        raise failure(400, "agent_not_installed", "Codex がインストールされていません")
    return codex


@router.post("/accounts/codex/login")
async def codex_login(request: Request):
    codex = codex_agent(request)
    try:
        return await codex.start_login()
    except (ProviderError, RpcError, OSError, asyncio.TimeoutError) as exc:
        raise failure(502, "login_failed", getattr(exc, "message", None) or "ログインを始められませんでした") from exc


@router.delete("/accounts/codex/login")
async def codex_cancel_login(request: Request):
    await codex_agent(request).cancel_login()
    return {"ok": True}


@router.post("/accounts/codex/logout")
async def codex_logout(request: Request):
    if active_runs(request):
        raise failure(409, "runs_active", "返答の実行中はログアウトできません")
    try:
        await codex_agent(request).logout()
    except (ProviderError, RpcError, OSError, asyncio.TimeoutError) as exc:
        raise failure(502, "logout_failed", "ログアウトできませんでした") from exc
    return {"ok": True}


class ModelDefault(BaseModel):
    executor: str
    model: str | None = None
    effort: str | None = None


@router.put("/model-defaults")
def put_model_default(body: ModelDefault, session: Session = Depends(db)):
    """The model and effort a new conversation starts with for this executor. CLI settings files are not touched."""
    defaults = dict(app_settings.read(session, app_settings.MODEL_DEFAULTS, {}))
    if body.model:
        defaults[body.executor] = {"model": body.model, "effort": body.effort or None}
    else:
        defaults.pop(body.executor, None)
    app_settings.write(session, app_settings.MODEL_DEFAULTS, defaults)
    session.commit()
    return defaults


# --- Server -----------------------------------------------------------------------------------------------

async def version_of(executable: str | None) -> str | None:
    if not executable:
        return None
    try:
        process = await asyncio.create_subprocess_exec(executable, "--version", stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.DEVNULL)
        output, _ = await asyncio.wait_for(process.communicate(), 10)
    except (OSError, asyncio.TimeoutError):
        return None
    return output.decode(errors="replace").strip().splitlines()[0][:80] if output else None


def launched_by_systemd() -> bool:
    """Started directly by a systemd manager. INVOCATION_ID alone is inherited by anything a service starts."""
    try:
        return bool(os.environ.get("INVOCATION_ID")) and Path(f"/proc/{os.getppid()}/comm").read_text().strip() == "systemd"
    except OSError:
        return False


def folder_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        with contextlib.suppress(OSError):
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
    return total


@router.get("/server")
async def server(request: Request):
    state = request.app.state
    settings = state.settings
    agents = []
    for key, label in (("codex", "Codex"), ("opencode", "OpenCode")):
        agent = state.agents.get(key)
        agents.append({"id": key, "label": label, "installed": bool(agent and agent.installed()),
                       "version": await version_of(getattr(agent, "executable", None)) if agent else None})
    try:
        import claude_agent_sdk
        claude_version = f"Agent SDK {claude_agent_sdk.__version__}"
    except (ImportError, AttributeError):
        claude_version = None
    agents.append({"id": "claude_code", "label": "Claude Code", "installed": claude_version is not None,
                   "version": claude_version})
    folders = await projects.list_projects(settings.omni_projects_dir)
    size = await asyncio.to_thread(folder_size, settings.omni_data_dir)
    return {"version": __version__, "python": platform.python_version(), "pid": os.getpid(),
            "started_at": int(state.started_at * 1000), "uptime_seconds": int(time.time() - state.started_at),
            "systemd": launched_by_systemd(), "can_restart": bool(getattr(state, "can_restart", False)),
            "restart_required": restart_required(state),
            "active_runs": active_runs(request), "listen": f"{settings.omni_host}:{settings.omni_port}",
            "public_urls": settings.public_origins(), "data_dir": str(settings.omni_data_dir), "data_bytes": size,
            "projects_dir": str(settings.omni_projects_dir), "projects": len(folders), "agents": agents}


@router.post("/server/restart")
async def restart(request: Request):
    """Stops the server gracefully and starts it again in the same process (see __main__)."""
    if active_runs(request):
        raise failure(409, "runs_active", "返答の実行中は再起動できません。終わってから再起動してください")
    state = request.app.state
    if not getattr(state, "can_restart", False):
        raise failure(400, "restart_unavailable", "この起動方法では再起動できません")
    state.restart_requested = True
    # Answer first, then stop gracefully; __main__ starts the process again
    asyncio.get_running_loop().call_later(0.5, setattr, state.server, "should_exit", True)
    return {"ok": True, "started_at": int(state.started_at * 1000)}


# --- Sessions ---------------------------------------------------------------------------------------------

@router.get("/sessions")
def sessions(request: Request, session: Session = Depends(db)):
    counts = dict(session.execute(select(Conversation.mode, func.count()).group_by(Conversation.mode)).all())
    hidden = session.scalar(select(func.count()).select_from(Conversation).where(Conversation.hidden.is_(True)))
    imported = dict(session.execute(select(ExternalSession.family, func.count())
                                    .where(ExternalSession.conversation_id.is_not(None))
                                    .group_by(ExternalSession.family)).all())
    skipped = session.scalar(select(func.count()).select_from(ExternalSession)
                             .where(ExternalSession.conversation_id.is_(None)))
    return {"conversations": sum(counts.values()), "chat": counts.get("chat", 0), "code": counts.get("code", 0),
            "hidden": hidden or 0, "imported": {"codex": imported.get("codex", 0), "claude": imported.get("claude", 0)},
            "skipped": skipped or 0, "active_runs": active_runs(request), "confirm_word": CONFIRM_WORD}


class DeleteAll(BaseModel):
    confirm: str


@router.post("/conversations/delete-all")
def delete_all(body: DeleteAll, request: Request, session: Session = Depends(db)):
    """Deletes every conversation in the app. The CLIs' own sessions stay, and the deleted ones are not
    imported again."""
    if body.confirm != CONFIRM_WORD:
        raise failure(400, "confirm_mismatch", f"確認のため「{CONFIRM_WORD}」と入力してください")
    if active_runs(request):
        raise failure(409, "runs_active", "返答の実行中は削除できません")
    root = request.app.state.settings.omni_projects_dir
    deleted = 0
    for conversation in session.scalars(select(Conversation)).all():
        remember_deleted(session, conversation, root)
        session.delete(conversation)
        deleted += 1
    session.commit()
    return {"deleted": deleted}
