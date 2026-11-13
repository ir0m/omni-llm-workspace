from pathlib import Path

from fastapi.testclient import TestClient

from omniws.main import create_app

from .conftest import PASSWORD, make_settings


def app_client(tmp_path, **values) -> TestClient:
    return TestClient(create_app(make_settings(tmp_path, **values), web_dist=Path("/nonexistent")))


def test_sign_in_and_out(tmp_path):
    with app_client(tmp_path) as client:
        assert client.get("/api/auth/me").json() == {"password_set": True, "signed_in": False}
        assert client.get("/api/conversations").status_code == 401
        assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
        assert client.get("/api/auth/me").json()["signed_in"] is True
        assert client.get("/api/conversations").status_code == 200
        client.post("/api/auth/logout")
        assert client.get("/api/conversations").status_code == 401


def test_wrong_password(tmp_path):
    with app_client(tmp_path) as client:
        response = client.post("/api/auth/login", json={"password": "nope"})
        assert response.status_code == 401
        assert "set-cookie" not in response.headers


def test_forged_cookie_is_refused(tmp_path):
    with app_client(tmp_path) as client:
        client.cookies.set("omni_session", "forged.value.here")
        assert client.get("/api/conversations").status_code == 401


def test_without_password_the_api_stays_closed(tmp_path):
    with app_client(tmp_path, omni_password=None) as client:
        assert client.get("/api/auth/me").json()["password_set"] is False
        assert client.post("/api/auth/login", json={"password": ""}).status_code == 503
        assert client.get("/api/conversations").status_code == 503


def test_cross_origin_posts_are_refused(tmp_path):
    with app_client(tmp_path) as client:
        response = client.post("/api/auth/login", json={"password": PASSWORD},
                               headers={"origin": "https://evil.example"})
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
