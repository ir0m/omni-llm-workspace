import base64
import hashlib
import json
import os
from pathlib import Path

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from omniws.auth import create_enroll_token, token_hash
from omniws.models import EnrollToken
from omniws.main import create_app

from .conftest import PASSWORD, make_settings

ORIGIN = "http://localhost:8300"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftAuthenticator:
    """A platform authenticator in software: P-256 keys, "none" attestation, user verified."""

    def __init__(self) -> None:
        self.credentials: dict[bytes, tuple[ec.EllipticCurvePrivateKey, str]] = {}
        self.count = 0

    def create(self, options: dict, origin: str = ORIGIN) -> dict:
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        credential_id = os.urandom(16)
        rp_id = options["rp"]["id"]
        self.credentials[credential_id] = (key, rp_id)
        auth_data = (hashlib.sha256(rp_id.encode()).digest() + bytes([0x45]) + (0).to_bytes(4, "big") + bytes(16)
                     + len(credential_id).to_bytes(2, "big") + credential_id + cose)
        client = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": origin,
                             "crossOrigin": False}).encode()
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64(credential_id), "rawId": b64(credential_id), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(client), "attestationObject": b64(attestation), "transports": ["internal"]}}

    def get(self, options: dict, origin: str = ORIGIN, credential_id: bytes | None = None) -> dict:
        credential_id = credential_id or next(iter(self.credentials))
        key, rp_id = self.credentials[credential_id]
        self.count += 1
        auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([0x05]) + self.count.to_bytes(4, "big")
        client = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin,
                             "crossOrigin": False}).encode()
        signature = key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(credential_id), "rawId": b64(credential_id), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(client), "authenticatorData": b64(auth_data),
                             "signature": b64(signature), "userHandle": None}}


def app_client(tmp_path, **values) -> TestClient:
    values = {"omni_public_urls": ORIGIN, **values}
    return TestClient(create_app(make_settings(tmp_path, **values), web_dist=Path("/nonexistent")))


def post(client, path, body=None, origin=ORIGIN):
    return client.post(path, json=body or {}, headers={"origin": origin})


def register(client, device: SoftAuthenticator, token: str | None = None, name: str = "iPhone"):
    started = post(client, "/api/auth/passkey/register/options", {"token": token})
    assert started.status_code == 200, started.text
    options = started.json()["options"]
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    return post(client, "/api/auth/passkey/register/verify",
                {"challenge_id": started.json()["challenge_id"], "credential": device.create(options), "name": name})


def sign_in(client, device: SoftAuthenticator, credential_id: bytes | None = None):
    started = post(client, "/api/auth/passkey/login/options")
    assert started.status_code == 200, started.text
    credential = device.get(started.json()["options"], credential_id=credential_id)
    return post(client, "/api/auth/passkey/login/verify", {"challenge_id": started.json()["challenge_id"], "credential": credential})


def test_closed_until_the_first_passkey(tmp_path):
    with app_client(tmp_path, omni_password=None) as client:
        assert client.get("/api/auth/me").json()["registered"] is False
        assert client.get("/api/conversations").status_code == 503
        assert post(client, "/api/auth/login", {"password": "x"}).status_code == 404  # no password sign-in
        assert post(client, "/api/auth/passkey/register/options", {"token": "made-up"}).status_code == 403


def test_register_with_a_one_time_link_then_sign_in(tmp_path):
    device = SoftAuthenticator()
    with app_client(tmp_path, omni_password=None) as client:
        token = create_enroll_token(client.app.state.sessions)
        assert register(client, device, token).status_code == 200
        assert client.get("/api/conversations").status_code == 200  # registering signs in
        assert post(client, "/api/auth/passkey/register/options", {"token": token}, origin=ORIGIN).status_code == 200  # signed in now
        client.post("/api/auth/logout")
        assert post(client, "/api/auth/passkey/register/options", {"token": token}).status_code == 403  # used once
        assert client.get("/api/conversations").status_code == 401
        assert sign_in(client, device).status_code == 200
        me = client.get("/api/auth/me").json()
        assert me["signed_in"] and me["passkey_id"] == 1
        assert sign_in(client, device).status_code == 200  # the sign count goes up each time


def test_forged_or_replayed_assertions_fail(tmp_path):
    device, stranger = SoftAuthenticator(), SoftAuthenticator()
    with app_client(tmp_path, omni_password=None) as client:
        register(client, device, create_enroll_token(client.app.state.sessions))
        client.post("/api/auth/logout")
        stranger.create({"rp": {"id": "localhost"}, "challenge": b64(b"x")})
        assert sign_in(client, stranger).status_code == 400  # unknown credential
        started = post(client, "/api/auth/passkey/login/options").json()
        assertion = device.get(started["options"])
        assertion["response"]["signature"] = b64(b"\x30\x06\x02\x01\x01\x02\x01\x01")
        assert post(client, "/api/auth/passkey/login/verify", {"challenge_id": started["challenge_id"],
                                                                "credential": assertion}).status_code == 400
        assert post(client, "/api/auth/passkey/login/verify", {"challenge_id": started["challenge_id"],
                                                                "credential": assertion}).status_code == 400  # challenge spent
        assert client.get("/api/conversations").status_code == 401


def test_only_configured_origins(tmp_path):
    with app_client(tmp_path, omni_password=None) as client:
        token = create_enroll_token(client.app.state.sessions)
        assert post(client, "/api/auth/passkey/register/options", {"token": token}, origin="http://testserver").status_code == 400


def test_devices_can_be_added_and_deleted(tmp_path):
    phone, laptop = SoftAuthenticator(), SoftAuthenticator()
    with app_client(tmp_path, omni_password=None) as client:
        register(client, phone, create_enroll_token(client.app.state.sessions), name="iPhone")
        assert register(client, laptop, name="Mac").status_code == 200  # signed in: no link needed
        listed = client.get("/api/auth/passkeys").json()
        assert [(item["name"], item["current"]) for item in listed] == [("iPhone", True), ("Mac", False)]
        assert client.delete(f"/api/auth/passkeys/{listed[0]['id']}", headers={"origin": ORIGIN}).status_code == 200
        assert client.get("/api/conversations").status_code == 401  # this session came from the deleted passkey
        assert sign_in(client, laptop).status_code == 200
        assert client.delete(f"/api/auth/passkeys/{listed[1]['id']}", headers={"origin": ORIGIN}).status_code == 409  # last one


def test_password_sign_in_for_development(tmp_path):
    with app_client(tmp_path) as client:
        assert client.get("/api/auth/me").json()["password"] is True
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
        assert client.get("/api/conversations").status_code == 200
        client.post("/api/auth/logout")
        assert client.get("/api/conversations").status_code == 401


def test_forged_cookie_is_refused(tmp_path):
    with app_client(tmp_path) as client:
        client.cookies.set("omni_session", "forged.value.here")
        assert client.get("/api/conversations").status_code == 401


def test_cross_origin_posts_are_refused(tmp_path):
    with app_client(tmp_path) as client:
        response = client.post("/api/auth/login", json={"password": PASSWORD}, headers={"origin": "https://evil.example"})
        assert response.status_code == 403
        same_site = client.post("/api/auth/login", json={"password": PASSWORD}, headers={"origin": "http://testserver"})
        assert same_site.status_code == 200


def test_generated_secret_is_private_and_stable(tmp_path):
    settings = make_settings(tmp_path)
    create_app(settings, web_dist=Path("/nonexistent"))
    key = settings.omni_data_dir / "secret.key"
    assert key.stat().st_mode & 0o777 == 0o600
    first = key.read_text()
    create_app(settings, web_dist=Path("/nonexistent"))
    assert key.read_text() == first


@pytest.mark.parametrize("claims", [{"v": 1}, {"v": 2}, {"v": 2, "pw": True}, {"v": 2, "pk": 999}])
def test_old_or_inactive_sessions_are_refused(tmp_path, claims):
    with app_client(tmp_path, omni_password=None) as client:
        assert register(client, SoftAuthenticator(), create_enroll_token(client.app.state.sessions)).status_code == 200
        client.cookies.clear()
        client.cookies.set("omni_session", client.app.state.auth.serializer.dumps(claims))
        assert client.get("/api/auth/me").json()["signed_in"] is False
        assert client.get("/api/conversations").status_code == 401


def test_enrollment_link_expires_during_registration(tmp_path):
    with app_client(tmp_path, omni_password=None) as client:
        token = create_enroll_token(client.app.state.sessions)
        started = post(client, "/api/auth/passkey/register/options", {"token": token}).json()
        with client.app.state.sessions.begin() as session:
            session.get(EnrollToken, token_hash(token)).expires_at = 0
        result = post(client, "/api/auth/passkey/register/verify", {
            "challenge_id": started["challenge_id"], "credential": SoftAuthenticator().create(started["options"])})
        assert result.status_code == 403
        assert result.json()["detail"] == "link_invalid"
        assert client.get("/api/auth/me").json()["registered"] is False


def test_one_link_cannot_complete_two_registrations(tmp_path):
    with app_client(tmp_path, omni_password=None) as client:
        token = create_enroll_token(client.app.state.sessions)
        starts = [post(client, "/api/auth/passkey/register/options", {"token": token}).json() for _ in range(2)]
        statuses = []
        for started in starts:
            result = post(client, "/api/auth/passkey/register/verify", {
                "challenge_id": started["challenge_id"], "credential": SoftAuthenticator().create(started["options"])})
            statuses.append(result.status_code)
        assert statuses == [200, 403]
        assert len(client.get("/api/auth/passkeys").json()) == 1


def test_malformed_registration_is_rejected_without_server_error(tmp_path):
    with app_client(tmp_path, omni_password=None) as client:
        token = create_enroll_token(client.app.state.sessions)
        started = post(client, "/api/auth/passkey/register/options", {"token": token}).json()
        result = post(client, "/api/auth/passkey/register/verify", {
            "challenge_id": started["challenge_id"], "credential": {}})
        assert result.status_code == 400
        assert result.json()["detail"] == "registration_failed"
        assert register(client, SoftAuthenticator(), token).status_code == 200
