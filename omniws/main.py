from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .api import conversations, executors, projects, runs
from .auth import Auth, OriginCheck, require_user
from .auth import router as auth_router
from .config import ROOT, Settings, get_settings
from .db import make_engine, mark_interrupted, migrate, session_dependency
from .providers.registry import Registry
from .runs import RunManager

WEB_DIST = ROOT / "web" / "dist"

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


def create_app(settings: Settings | None = None, web_dist: Path = WEB_DIST, registry: Registry | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.omni_data_dir)
        migrate(engine)
        app.state.sessions = sessionmaker(engine, expire_on_commit=False)
        app.state.get_session = session_dependency(app.state.sessions)
        mark_interrupted(app.state.sessions)
        yield
        for run in list(app.state.runs.runs.values()):
            if run.task and not run.done:
                run.task.cancel()
        engine.dispose()

    app = FastAPI(title="OmniLLMWorkspace", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.auth = Auth(settings)
    app.state.registry = registry or Registry(settings)
    app.state.runs = RunManager()
    # Guards against DNS rebinding and cross-site requests: the app will run agents on this machine.
    app.add_middleware(OriginCheck, allowed_hosts=settings.allowed_hosts())
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts())
    app.include_router(health)
    app.include_router(auth_router)
    signed_in = [Depends(require_user)]
    for router in (conversations.router, runs.router, executors.router, projects.router):
        app.include_router(router, dependencies=signed_in)
    mount_web(app, web_dist)
    return app


app = create_app()
