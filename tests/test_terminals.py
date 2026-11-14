from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omniws.main import create_app
from omniws.providers.registry import Registry
from .conftest import PASSWORD, make_settings


@pytest.fixture
def terminal_client(tmp_path):
    settings = make_settings(tmp_path, omni_allow_terminal=True)
    (settings.omni_projects_dir / "app").mkdir(parents=True)
    app = create_app(settings, web_dist=Path('/none'), registry=Registry(settings, {}), agents={})
    with TestClient(app) as client:
        client.post('/api/auth/login', json={'password': PASSWORD})
        yield client


def test_terminal_requires_login_origin_project_and_env(terminal_client):
    client = terminal_client
    url = '/api/projects/app/terminal/ws'
    for headers in ({}, {'origin': 'https://evil.invalid'}, {'origin': 'http://testserver:1234'}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=headers):
                pass
    client.app.state.settings.omni_allow_terminal = False
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers={'origin': 'http://testserver'}):
            pass
    client.app.state.settings.omni_allow_terminal = True
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/api/projects/missing/terminal/ws', headers={'origin': 'http://testserver'}):
            pass
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers={'origin': 'http://testserver'}):
            pass


def test_terminal_real_pty_resize_cwd_and_no_app_secret(terminal_client, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-secret-never-in-terminal')
    client = terminal_client
    data = b''
    with client.websocket_connect('/api/projects/app/terminal/ws', headers={'origin': 'http://testserver'}) as ws:
        ws.send_json({'type': 'resize', 'cols': 93, 'rows': 27})
        ws.send_json({'type': 'input', 'data': "printf 'PWD=%s\\n' \"${PWD##*/}\"; stty size; test -z \"$ANTHROPIC_API_KEY\" && printf 'NO_SECRET\\n'; exit\n"})
        try:
            while True:
                data += ws.receive_bytes()
        except WebSocketDisconnect:
            pass
    assert b'PWD=app\r\n' in data
    assert b'27 93\r\n' in data
    assert b'NO_SECRET\r\n' in data
    assert b'test-secret-never-in-terminal' not in data
    assert not client.app.state.terminals


def test_revoke_permission_closes_live_terminal(terminal_client):
    client = terminal_client
    with client.websocket_connect('/api/projects/app/terminal/ws', headers={'origin': 'http://testserver'}) as ws:
        ws.send_json({'type': 'input', 'data': 'sleep 30\n'})
        assert client.put('/api/settings/permissions', json={'omni_allow_terminal': False}).status_code == 200
        with pytest.raises(WebSocketDisconnect):
            while True:
                ws.receive_bytes()
    assert not client.app.state.terminals


def test_terminal_cannot_be_enabled_above_env(client):
    assert client.put('/api/settings/permissions', json={'omni_allow_terminal': True}).status_code == 400
