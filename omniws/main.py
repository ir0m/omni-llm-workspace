from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import ROOT, Settings, get_settings

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


def create_app(settings: Settings | None = None, web_dist: Path = WEB_DIST) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="OmniLLMWorkspace", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    # Guards against DNS rebinding: the app can run shell commands, so only known host names are served.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts())
    app.include_router(health)
    mount_web(app, web_dist)
    return app


app = create_app()
