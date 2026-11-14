from __future__ import annotations

import time
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, app_settings
from .api import conversations, executors, inputs, projects, runs, sessions, templates, terminals, worktrees
from .api import settings as settings_api
from .auth import Auth, OriginCheck, SecurityHeaders, require_user
from .auth import router as auth_router
from .config import ROOT, Settings, get_settings
from .db import make_engine, mark_interrupted, migrate, session_dependency
from .agents.claude import ClaudeCodeAgent
from .agents.agy import AgyAgent
from .agents.codex import CodexAgent
from .agents.opencode import OpenCodeAgent
from .providers.registry import Registry
from .runs import RunManager
from .external_sessions import ExternalSessions

WEB_DIST = ROOT / "web" / "dist"
log = logging.getLogger(__name__)

health = APIRouter()


@health.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


def mount_web(app: FastAPI, dist: Path) -> None:
    """Serve the built React app; unknown paths get index.html so the client router can take them."""
    if not (dist / "index.html").is_file():
        return
    root = dist.resolve()
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        file = (root / path).resolve()
        if path and file.is_file() and file.is_relative_to(root):
            return FileResponse(file)
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})


def create_app(settings: Settings | None = None, web_dist: Path = WEB_DIST, registry: Registry | None = None,
               agents: dict | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.omni_data_dir)
        migrate(engine)
        app.state.sessions = sessionmaker(engine, expire_on_commit=False)
        app.state.get_session = session_dependency(app.state.sessions)
        mark_interrupted(app.state.sessions)
        app_settings.load(app.state.sessions, settings, app.state.env_settings)
        app.state.auth.load(app.state.sessions)
        try:
            yield
        finally:
            # Runs save partial replies and return queued inputs while cancelling. Keep the
            # database and agents alive until that cleanup has finished.
            tasks = [run.task for run in app.state.runs.runs.values() if run.task and not run.task.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                agents_to_close = [(name, agent) for name, agent in app.state.agents.items() if hasattr(agent, "close")]
                results = await asyncio.gather(*(agent.close() for _, agent in agents_to_close), return_exceptions=True)
                for (name, _), result in zip(agents_to_close, results):
                    if isinstance(result, BaseException):
                        log.error("closing agent %s failed", name, exc_info=result)
            finally:
                engine.dispose()

    app = FastAPI(title="OmniLLMWorkspace", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    # What .env gave: the upper limit for what the settings screen changes in `settings`
    app.state.env_settings = settings.model_copy(deep=True)
    app.state.settings = settings
    app.state.started_at = time.time()
    app.state.web_index = web_dist / "index.html"
    app.state.auth = Auth(settings)
    app.state.registry = registry or Registry(settings)
    app.state.agents = agents if agents is not None else {
        "opencode": OpenCodeAgent(settings, app.state.registry), "codex": CodexAgent(settings),
        "claude_code": ClaudeCodeAgent(settings, "subscription"), "claude_code_api": ClaudeCodeAgent(settings, "api"),
        "agy": AgyAgent(settings)}
    app.state.free_models = {}
    app.state.runs = RunManager()
    app.state.terminals = set()
    app.state.worktree_lock = asyncio.Lock()
    app.state.external_sessions = ExternalSessions(app.state)
    # Guards against DNS rebinding and cross-site requests: the app will run agents on this machine.
    app.add_middleware(SecurityHeaders)
    app.add_middleware(OriginCheck, allowed_hosts=settings.allowed_hosts(), public_origins=settings.public_origins())
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts())
    app.include_router(health)
    app.include_router(auth_router)
    signed_in = [Depends(require_user)]
    for router in (conversations.router, runs.router, executors.router, projects.router, sessions.router,
                   settings_api.router, inputs.router, terminals.router, worktrees.router,
                   templates.router):
        app.include_router(router, dependencies=signed_in)
    app.include_router(terminals.ws_router)
    mount_web(app, web_dist)
    return app


app = create_app()
