"""Sign-in: passkeys (WebAuthn), with a signed HttpOnly session cookie for 30 days.

The first passkey is registered through a one-time link made on the server (`python -m omniws passkey-link`);
more can be added while signed in. A session remembers which passkey opened it, so deleting a lost device's
passkey ends its sessions. OMNI_PASSWORD still works for development and tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import webauthn
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor, ResidentKeyRequirement, UserVerificationRequirement,
)

from .config import Settings
from .db import now_ms
from .models import EnrollToken, Passkey

COOKIE = "omni_session"
MAX_AGE = 30 * 24 * 3600
FAILURE_WINDOW = 15 * 60
MAX_FAILURES = 10
CHALLENGE_SECONDS = 300
LINK_MINUTES = 15
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
RP_NAME = "OmniLLMWorkspace"


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


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_enroll_token(sessions: sessionmaker[Session], minutes: int = LINK_MINUTES) -> str:
    """A one-time token for registering a passkey without being signed in."""
    token = secrets.token_urlsafe(32)
    with sessions.begin() as session:
        session.add(EnrollToken(token_hash=token_hash(token), expires_at=now_ms() + minutes * 60_000))
    return token


@dataclass
class Challenge:
    value: bytes
    kind: str          # login | register
    rp_id: str
    origin: str
    expires: float
    enroll: str | None = None  # the token hash a registration was opened with


class Auth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.password = settings.omni_password.get_secret_value() if settings.omni_password else None
        secret = load_secret(settings)
        self.serializer = URLSafeTimedSerializer(secret, salt="omni-session")
        self.user_id = hashlib.sha256(f"{secret}:user".encode()).digest()[:16]
        self.failures: dict[str, deque[float]] = defaultdict(deque)
        self.challenges: dict[str, Challenge] = {}
        self.passkeys: set[int] = set()  # ids of registered passkeys; sessions of others are void

    def load(self, sessions: sessionmaker[Session]) -> None:
        with sessions() as session:
            self.passkeys = set(session.scalars(select(Passkey.id)))

    def session(self, request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        try:
            data = self.serializer.loads(token, max_age=MAX_AGE)
        except BadSignature:
            return None
        if not isinstance(data, dict) or data.get("v") != 2:
            return None
        if type(data.get("pk")) is int and data["pk"] in self.passkeys:
            return data
        if data.get("pw") is True and self.password is not None:
            return data
        return None

    def is_signed_in(self, request: Request) -> bool:
        return self.session(request) is not None

    def sign_in(self, request: Request, response: Response, **claims: Any) -> None:
        response.set_cookie(COOKIE, self.serializer.dumps({"v": 2, **claims}), max_age=MAX_AGE, httponly=True,
                            samesite="strict", secure=request.url.scheme == "https", path="/")

    def recent_failures(self, client: str) -> deque[float]:
        times = self.failures[client]
        while times and times[0] < time.monotonic() - FAILURE_WINDOW:
            times.popleft()
        return times

    def origin(self, request: Request) -> tuple[str, str]:
        """The page's origin, if passkeys may be used there, and its RP ID (the host name)."""
        origin = (request.headers.get("origin") or "").rstrip("/")
        if origin not in self.settings.public_origins():
            raise HTTPException(status_code=400, detail="origin_not_allowed")
        return origin, urlsplit(origin).hostname or ""

    def new_challenge(self, value: bytes, kind: str, rp_id: str, origin: str, enroll: str | None = None) -> str:
        now = time.monotonic()
        for key in [key for key, item in self.challenges.items() if item.expires < now]:
            del self.challenges[key]
        key = secrets.token_urlsafe(16)
        self.challenges[key] = Challenge(value, kind, rp_id, origin, now + CHALLENGE_SECONDS, enroll)
        return key

    def take_challenge(self, key: str, kind: str) -> Challenge:
        challenge = self.challenges.pop(key, None)
        if challenge is None or challenge.kind != kind or challenge.expires < time.monotonic():
            raise HTTPException(status_code=400, detail="challenge_expired")
        return challenge


def require_user(request: Request) -> None:
    auth: Auth = request.app.state.auth
    if not auth.passkeys and auth.password is None:
        raise HTTPException(status_code=503, detail="setup_required")
    if not auth.is_signed_in(request):
        raise HTTPException(status_code=401, detail="not_signed_in")


router = APIRouter(prefix="/api/auth")


def db(request: Request):
    yield from request.app.state.get_session()


def client_of(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def slow_down(auth: Auth, request: Request) -> None:
    failures = auth.recent_failures(client_of(request))
    failures.append(time.monotonic())
    await asyncio.sleep(min(len(failures), 5) * 0.5)


def guard(auth: Auth, request: Request) -> None:
    if len(auth.recent_failures(client_of(request))) >= MAX_FAILURES:
        raise HTTPException(status_code=429, detail="too_many_attempts")


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    auth: Auth = request.app.state.auth
    claims = auth.session(request)
    return {"signed_in": claims is not None, "registered": bool(auth.passkeys), "password": auth.password is not None,
            "passkey_origins": auth.settings.public_origins(), "passkey_id": (claims or {}).get("pk")}


class Login(BaseModel):
    password: str


@router.post("/login")
async def login(body: Login, request: Request, response: Response) -> dict[str, bool]:
    """Password sign-in, for development only (OMNI_PASSWORD)."""
    auth: Auth = request.app.state.auth
    if auth.password is None:
        raise HTTPException(status_code=404, detail="password_sign_in_off")
    guard(auth, request)
    if not hmac.compare_digest(body.password.encode(), auth.password.encode()):
        await slow_down(auth, request)
        raise HTTPException(status_code=401, detail="wrong_password")
    auth.failures[client_of(request)].clear()
    auth.sign_in(request, response, pw=True)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


class RegisterStart(BaseModel):
    token: str | None = Field(default=None, max_length=200)


class Finish(BaseModel):
    challenge_id: str = Field(max_length=100)
    credential: dict[str, Any]
    name: str = Field(default="", max_length=100)


def options_json(options: Any) -> dict[str, Any]:
    return json.loads(webauthn.options_to_json(options))


def descriptor(passkey: Passkey) -> PublicKeyCredentialDescriptor:
    return PublicKeyCredentialDescriptor(id=base64url_to_bytes(passkey.credential_id))


@router.post("/passkey/register/options")
def register_options(body: RegisterStart, request: Request, session: Session = Depends(db)) -> dict[str, Any]:
    """Registration is open to a signed-in user, or with a valid one-time link."""
    auth: Auth = request.app.state.auth
    origin, rp_id = auth.origin(request)
    enroll = None
    if not auth.is_signed_in(request):
        guard(auth, request)
        record = session.get(EnrollToken, token_hash(body.token or ""))
        if record is None or record.used_at is not None or record.expires_at < now_ms():
            raise HTTPException(status_code=403, detail="link_invalid")
        enroll = record.token_hash
    existing = session.scalars(select(Passkey).where(Passkey.rp_id == rp_id)).all()
    options = webauthn.generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_id=auth.user_id, user_name="owner", user_display_name=RP_NAME,
        exclude_credentials=[descriptor(passkey) for passkey in existing],
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                               user_verification=UserVerificationRequirement.REQUIRED))
    key = auth.new_challenge(options.challenge, "register", rp_id, origin, enroll)
    return {"challenge_id": key, "options": options_json(options)}


@router.post("/passkey/register/verify")
async def register_verify(body: Finish, request: Request, response: Response) -> dict[str, Any]:
    auth: Auth = request.app.state.auth
    challenge = auth.take_challenge(body.challenge_id, "register")
    if challenge.enroll is None and not auth.is_signed_in(request):
        raise HTTPException(status_code=401, detail="not_signed_in")
    try:
        verified = webauthn.verify_registration_response(
            credential=body.credential, expected_challenge=challenge.value, expected_rp_id=challenge.rp_id,
            expected_origin=challenge.origin, require_user_verification=True)
    except (WebAuthnException, ValueError, TypeError) as exc:
        await slow_down(auth, request)
        raise HTTPException(status_code=400, detail="registration_failed") from exc
    transports = (body.credential.get("response") or {}).get("transports")

    def store() -> int:
        with request.app.state.sessions.begin() as session:
            if challenge.enroll:
                consumed = session.execute(update(EnrollToken).where(
                    EnrollToken.token_hash == challenge.enroll,
                    EnrollToken.used_at.is_(None),
                    EnrollToken.expires_at > now_ms(),
                ).values(used_at=now_ms()))
                if consumed.rowcount != 1:
                    raise HTTPException(status_code=403, detail="link_invalid")
            passkey = Passkey(credential_id=bytes_to_base64url(verified.credential_id),
                              public_key=verified.credential_public_key, sign_count=verified.sign_count,
                              rp_id=challenge.rp_id, name=body.name.strip() or "この端末",
                              transports=transports if isinstance(transports, list) else None, last_used_at=now_ms())
            session.add(passkey)
            session.flush()
            return passkey.id

    try:
        passkey_id = await run_in_threadpool(store)
    except IntegrityError as exc:
        raise HTTPException(status_code=400, detail="registration_failed") from exc
    auth.passkeys.add(passkey_id)
    if challenge.enroll or not auth.is_signed_in(request):
        auth.sign_in(request, response, pk=passkey_id)
    return {"ok": True, "id": passkey_id}


@router.post("/passkey/login/options")
def login_options(request: Request, session: Session = Depends(db)) -> dict[str, Any]:
    auth: Auth = request.app.state.auth
    guard(auth, request)
    origin, rp_id = auth.origin(request)
    if not session.scalar(select(func.count()).select_from(Passkey).where(Passkey.rp_id == rp_id)):
        raise HTTPException(status_code=400, detail="no_passkey")
    # No allow list: passkeys are discoverable, so the device offers its own
    options = webauthn.generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    key = auth.new_challenge(options.challenge, "login", rp_id, origin)
    return {"challenge_id": key, "options": options_json(options)}


@router.post("/passkey/login/verify")
async def login_verify(body: Finish, request: Request, response: Response) -> dict[str, bool]:
    auth: Auth = request.app.state.auth
    guard(auth, request)
    challenge = auth.take_challenge(body.challenge_id, "login")
    try:
        credential = parse_authentication_credential_json(body.credential)
    except Exception as exc:  # malformed input
        await slow_down(auth, request)
        raise HTTPException(status_code=400, detail="sign_in_failed") from exc
    credential_id = bytes_to_base64url(credential.raw_id)

    def find() -> Passkey | None:
        with request.app.state.sessions() as session:
            return session.scalar(select(Passkey).where(Passkey.credential_id == credential_id,
                                                        Passkey.rp_id == challenge.rp_id))

    passkey = await run_in_threadpool(find)
    if passkey is None:
        await slow_down(auth, request)
        raise HTTPException(status_code=400, detail="sign_in_failed")
    try:
        verified = webauthn.verify_authentication_response(
            credential=credential, expected_challenge=challenge.value, expected_rp_id=challenge.rp_id,
            expected_origin=challenge.origin, credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count, require_user_verification=True)
    except (WebAuthnException, ValueError, TypeError) as exc:
        await slow_down(auth, request)
        raise HTTPException(status_code=400, detail="sign_in_failed") from exc

    def touch() -> None:
        with request.app.state.sessions.begin() as session:
            stored = session.get(Passkey, passkey.id)
            if stored is not None:
                stored.sign_count, stored.last_used_at = verified.new_sign_count, now_ms()

    await run_in_threadpool(touch)
    auth.failures[client_of(request)].clear()
    auth.sign_in(request, response, pk=passkey.id)
    return {"ok": True}


@router.get("/passkeys", dependencies=[Depends(require_user)])
def list_passkeys(request: Request, session: Session = Depends(db)) -> list[dict[str, Any]]:
    current = (request.app.state.auth.session(request) or {}).get("pk")
    return [{"id": item.id, "name": item.name, "rp_id": item.rp_id, "created_at": item.created_at,
             "last_used_at": item.last_used_at, "current": item.id == current}
            for item in session.scalars(select(Passkey).order_by(Passkey.created_at))]


@router.delete("/passkeys/{passkey_id}", dependencies=[Depends(require_user)])
def delete_passkey(passkey_id: int, request: Request, session: Session = Depends(db)) -> dict[str, bool]:
    """Deleting a passkey also ends the sessions it opened. The last one can't be deleted."""
    auth: Auth = request.app.state.auth
    passkey = session.get(Passkey, passkey_id)
    if passkey is None:
        raise HTTPException(status_code=404, detail="passkey_not_found")
    if session.scalar(select(func.count()).select_from(Passkey)) <= 1:
        raise HTTPException(status_code=409, detail="last_passkey")
    session.delete(passkey)
    session.commit()
    auth.passkeys.discard(passkey_id)
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
