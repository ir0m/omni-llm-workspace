"""Single-user password login with a signed, HttpOnly session cookie."""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings

COOKIE = "omni_session"
MAX_AGE = 30 * 24 * 3600
FAILURE_WINDOW = 15 * 60
MAX_FAILURES = 10
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def load_secret(settings: Settings) -> str:
    """OMNI_SECRET, or a random key kept in the data directory (readable only by the user)."""
    if settings.omni_secret:
        return settings.omni_secret.get_secret_value()
    path = settings.omni_data_dir / "secret.key"
    if path.is_file():
        return path.read_text().strip()
    settings.omni_data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(key)
    return key


class Auth:
    def __init__(self, settings: Settings) -> None:
        self.password = settings.omni_password.get_secret_value() if settings.omni_password else None
        self.serializer = URLSafeTimedSerializer(load_secret(settings), salt="omni-session")
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def is_signed_in(self, request: Request) -> bool:
        token = request.cookies.get(COOKIE)
        if not token:
            return False
        try:
            self.serializer.loads(token, max_age=MAX_AGE)
        except BadSignature:
            return False
        return True

    def recent_failures(self, client: str) -> deque[float]:
        times = self.failures[client]
        while times and times[0] < time.monotonic() - FAILURE_WINDOW:
            times.popleft()
        return times


def require_user(request: Request) -> None:
    auth: Auth = request.app.state.auth
    if auth.password is None:
        raise HTTPException(status_code=503, detail="password_not_set")
    if not auth.is_signed_in(request):
        raise HTTPException(status_code=401, detail="not_signed_in")


class Login(BaseModel):
    password: str


router = APIRouter(prefix="/api/auth")


@router.get("/me")
def me(request: Request) -> dict[str, bool]:
    auth: Auth = request.app.state.auth
    return {"password_set": auth.password is not None, "signed_in": auth.is_signed_in(request)}


@router.post("/login")
async def login(body: Login, request: Request, response: Response) -> dict[str, bool]:
    auth: Auth = request.app.state.auth
    if auth.password is None:
        raise HTTPException(status_code=503, detail="password_not_set")
    client = request.client.host if request.client else "unknown"
    failures = auth.recent_failures(client)
    if len(failures) >= MAX_FAILURES:
        raise HTTPException(status_code=429, detail="too_many_attempts")
    if not hmac.compare_digest(body.password.encode(), auth.password.encode()):
        failures.append(time.monotonic())
        await asyncio.sleep(min(len(failures), 5) * 0.5)  # slows down guessing
        raise HTTPException(status_code=401, detail="wrong_password")
    failures.clear()
    response.set_cookie(COOKIE, auth.serializer.dumps({"v": 1}), max_age=MAX_AGE, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


class OriginCheck:
    """Refuses state-changing requests sent from another site (on top of SameSite=Strict cookies)."""

    def __init__(self, app: ASGIApp, allowed_hosts: list[str]) -> None:
        self.app = app
        self.allowed = set(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            origin = dict(scope["headers"]).get(b"origin")
            if origin is not None:
                host = urlsplit(origin.decode("latin-1")).hostname or ""
                if host.lower() not in self.allowed:
                    response = Response('{"detail":"cross_origin"}', status_code=403, media_type="application/json")
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)
