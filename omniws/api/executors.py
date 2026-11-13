from __future__ import annotations

import time

from fastapi import APIRouter, Request

from .. import app_settings
from ..agents.jsonrpc import RpcError
from ..agents.opencode import FREE, PROVIDER_IDS
from ..providers.base import ProviderError

FREE_CACHE_SECONDS = 600
router = APIRouter()


def restart_required(state) -> bool:
    """The screen was rebuilt after this server started, so it may call APIs this process doesn't have."""
    try:
        return state.web_index.stat().st_mtime > state.started_at
    except (AttributeError, OSError):
        return False


@router.get("/api/executors")
async def list_executors(request: Request, refresh: bool = False):
    """What can take a turn. Providers answer Chat directly; in Code, OpenCode runs their models.
    OpenCode's free models are listed only when OMNI_ALLOW_OPENCODE_FREE is set."""
    state = request.app.state
    overview = await state.registry.overview(refresh)
    opencode = state.agents.get("opencode")
    code = bool(opencode and opencode.installed())
    for item in overview:
        item["modes"] = ["chat", "code"] if code and item["id"] in PROVIDER_IDS else ["chat"]
        item["code_via"] = "opencode"
    if code and state.settings.omni_allow_opencode_free:
        cache = state.free_models
        if refresh or time.monotonic() - cache.get("at", 0) > FREE_CACHE_SECONDS:
            try:
                cache.update(models=[model.to_dict() for model in await opencode.list_models()], error=None)
            except ProviderError as exc:
                cache.update(models=[], error=exc.to_dict())
            cache["at"] = time.monotonic()
        overview.append({"id": FREE, "label": "OpenCode 無料モデル", "kind": "api", "configured": True,
                         "setting": "OMNI_ALLOW_OPENCODE_FREE", "error": cache.get("error"), "modes": ["code"],
                         "code_via": None, "note": "外部サービス", "models": cache.get("models", [])})
    codex = state.agents.get("codex")
    if codex and codex.installed():
        overview.insert(0, await codex_entry(codex, refresh))
    for position, agent_id in enumerate(("claude_code", "claude_code_api")):
        agent = state.agents.get(agent_id)
        if agent is not None and (agent.installed() or agent_id == "claude_code"):
            overview.insert(position, await claude_entry(agent, refresh))
    # The model and effort a new conversation starts with, chosen in the settings screen
    with state.sessions() as session:
        defaults = app_settings.read(session, app_settings.MODEL_DEFAULTS, {})
    for item in overview:
        item["default"] = defaults.get(item["id"])
    return overview


async def claude_entry(agent, refresh: bool) -> dict:
    """Claude Code with the CLI's login (only when OMNI_ALLOW_CLAUDE_SUBSCRIPTION is set) or with the API key."""
    subscription = agent.billing == "subscription"
    entry = {"id": agent.id, "label": "Claude Code（サブスク）" if subscription else "Claude Code（API課金）",
             "kind": "subscription" if subscription else "api", "configured": False, "error": None,
             "setting": "OMNI_ALLOW_CLAUDE_SUBSCRIPTION=true" if subscription else "ANTHROPIC_API_KEY",
             "modes": ["chat", "code"], "code_via": None, "models": [], "usage": None,
             "note": "個人利用・自己責任" if subscription else None}
    if not agent.installed():
        return entry
    try:
        if subscription:
            await agent.server_info(refresh)
            plan = await agent.plan()
            if not plan:
                entry["setting"] = "claude auth login"
                return entry
            entry.update(label=f"Claude Code（{plan.removeprefix('Claude ')}）", usage=agent.usage())
        entry.update(configured=True, models=[model.to_dict() for model in await agent.list_models()])
    except ProviderError as exc:
        entry["error"] = exc.to_dict()
    return entry


async def codex_entry(codex, refresh: bool) -> dict:
    """Codex with the ChatGPT login: its plan, models and usage windows. Not configured until `codex login`."""
    entry = {"id": "codex", "label": "Codex", "kind": "subscription", "configured": False, "setting": "codex login",
             "error": None, "modes": ["chat", "code"], "code_via": None, "models": [], "usage": None}
    try:
        await codex.refresh_account(force=refresh)
        if codex.account:
            plan = codex.account.get("planType")
            entry.update(configured=True, label=f"Codex（{str(plan).capitalize()}）" if plan else "Codex",
                         models=[model.to_dict() for model in await codex.list_models()], usage=codex.usage())
    except ProviderError as exc:
        entry["error"] = exc.to_dict()
    except (RpcError, OSError) as exc:
        entry["error"] = {"code": "unknown", "message": str(exc)[:200]}
    return entry


@router.get("/api/config")
def config(request: Request):
    """The limits from .env that the screen needs."""
    state = request.app.state
    settings = state.settings
    return {"approval_default": settings.omni_approval_default, "approval_max": settings.omni_approval_max,
            "capabilities": ["native_sessions", "project_files", "settings"], "restart_required": restart_required(state),
            "allow_shell": settings.omni_allow_shell, "allow_network": settings.omni_allow_network, "shell_deny": settings.shell_deny(),
            "allow_opencode_free": settings.omni_allow_opencode_free,
            "allow_claude_subscription": settings.omni_allow_claude_subscription}
