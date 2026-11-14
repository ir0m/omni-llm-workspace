import base64
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniws import attachments as files
from omniws.events import message_chunk
from omniws.history import chat_history
from omniws.main import create_app
from omniws.models import Message
from omniws.providers.anthropic_api import request_options
from omniws.providers.base import ChatRequest, ModelInfo
from omniws.providers.openai_compat import OpenAICompatProvider
from omniws.providers.registry import Registry

from .conftest import PASSWORD, FakeProvider, make_settings, read_events
from .test_codex import executable as codex_executable
from .test_codex import requests as codex_requests

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7l8AAAAASUVORK5CYII=")
PDF = b"%PDF-1.4\n%test\n"


def upload(client, name, data):
    return client.post("/api/attachments", files={"file": (name, data, "application/octet-stream")})


def send(client, conversation, **body):
    return client.post(f"/api/conversations/{conversation}/messages", json={"provider": "fake", "model": "m1", **body})


@pytest.fixture
def seeing(providers):
    providers["fake"].attachment_kinds = frozenset({"image", "pdf", "text"})
    return providers["fake"]


def test_uploads_are_classified_by_content_and_bounded(client):
    image = upload(client, "../写真.png", PNG).json()
    assert image["kind"] == "image" and image["media_type"] == "image/png" and image["name"] == "写真.png"
    assert upload(client, "fake.png", b"<svg onload=alert(1)>").json()["kind"] == "text"
    assert upload(client, "doc.pdf", PDF).json()["kind"] == "pdf"
    assert upload(client, "data.bin", b"\0\1\2").json() | {"id": None, "created_at": None} == {
        "id": None, "created_at": None, "name": "data.bin", "media_type": "application/octet-stream", "size": 3, "kind": "file"}
    assert upload(client, ".hidden.md", b"# hi").json()["name"] == "hidden.md"
    assert files.clean_name("a\nb\x00c.md") == "abc.md" and files.clean_name("C:\\dir\\x.txt") == "x.txt"
    assert files.clean_name("") == "file" and len(files.clean_name("あ" * 300 + ".txt").encode()) <= 200
    assert upload(client, "empty.txt", b"").status_code == 400
    assert upload(client, "large.png", PNG + b"\0" * files.IMAGE_LIMIT).status_code == 413
    assert upload(client, "huge.bin", b"\0" * (files.FILE_LIMIT + 1)).status_code == 413
    client.post("/api/auth/logout")
    assert upload(client, "a.txt", b"a").status_code == 401


def test_downloads_render_only_images(client):
    image = upload(client, "a.png", PNG).json()
    shown = client.get(f"/api/attachments/{image['id']}")
    assert shown.content == PNG and shown.headers["content-type"] == "image/png"
    assert shown.headers["content-disposition"].startswith("inline") and "sandbox" in shown.headers["content-security-policy"]
    page = upload(client, "page.html", b"<script>alert(1)</script>").json()
    served = client.get(f"/api/attachments/{page['id']}")
    assert served.headers["content-type"] == "application/octet-stream"
    assert served.headers["content-disposition"].startswith("attachment") and served.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/attachments/../../secret.key").status_code == 404
    assert client.get("/api/attachments/" + "0" * 32).status_code == 404


def test_chat_message_keeps_references_and_the_provider_gets_them(client, seeing):
    conversation = client.post("/api/conversations", json={}).json()["id"]
    image = upload(client, "screen.png", PNG).json()
    note = upload(client, "notes.md", "# メモ".encode()).json()
    started = send(client, conversation, text="", attachments=[image["id"], note["id"]])
    assert started.status_code == 202
    read_events(client, started.json()["run_id"])
    detail = client.get(f"/api/conversations/{conversation}").json()
    assert detail["title"] == "screen.png"
    user = detail["messages"][0]["content"]
    assert [block["type"] for block in user] == ["attachment", "attachment"]
    assert user[0] == {"type": "attachment", "id": image["id"], "name": "screen.png", "media_type": "image/png",
                       "size": len(PNG), "kind": "image"}
    sent = seeing.requests[-1]
    assert sent.data_dir == client.app.state.settings.omni_data_dir
    assert sent.messages[-1]["content"][0]["id"] == image["id"]  # references, not bytes
    assert "base64" not in str(sent.messages)


def test_refuses_what_the_model_cannot_take(client, providers):
    conversation = client.post("/api/conversations", json={}).json()["id"]
    image = upload(client, "a.png", PNG).json()["id"]
    pdf = upload(client, "a.pdf", PDF).json()["id"]
    binary = upload(client, "a.zip", b"PK\3\4\0").json()["id"]
    assert send(client, conversation, text="").status_code == 422
    default = send(client, conversation, text="x", attachments=[image])
    assert default.status_code == 400 and default.json()["detail"].startswith("a.png:")  # a provider without kinds: text only
    providers["fake"].attachment_kinds = frozenset({"image", "text"})
    assert "PDF" in send(client, conversation, text="x", attachments=[pdf]).json()["detail"]
    assert send(client, conversation, text="x", attachments=[binary]).status_code == 400
    assert send(client, conversation, text="x", attachments=["0" * 32]).status_code == 404
    assert send(client, conversation, text="x", attachments=[image] * 11).status_code == 422

    async def blind():
        return [ModelInfo(id="m1", name="m1", vision=False)]
    providers["fake"].list_models = blind
    client.app.state.registry.cache.clear()
    refused = send(client, conversation, text="x", attachments=[image])
    assert refused.status_code == 400 and "画像に対応していません" in refused.json()["detail"]
    assert client.get(f"/api/conversations/{conversation}").json()["messages"] == []  # nothing stored

def test_history_and_provider_parts(tmp_path):
    data_dir = tmp_path / "data"
    image = files.save(data_dir, PNG, "a.png")
    pdf = files.save(data_dir, PDF, "a.pdf")
    text = files.save(data_dir, "本文".encode(), "a.txt")
    messages = [Message(id="1", seq=1, role="user", status="complete",
                        content=[{"type": "text", "text": "見て"}, *map(files.block, (image, pdf, text))]),
                Message(id="2", seq=2, role="user", status="complete", content=[{"type": "text", "text": "続き"}])]
    history = chat_history(messages)
    assert len(history) == 1 and [part["type"] for part in history[0]["content"]] == [
        "text", "attachment", "attachment", "attachment", "text"]
    original = repr(history)

    anthropic = request_options(ChatRequest(model="claude", messages=history, data_dir=data_dir), None)["messages"][0]["content"]
    assert [part["type"] for part in anthropic] == ["text", "image", "document", "text", "text"]
    assert anthropic[1]["source"] == {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}
    assert anthropic[3]["text"] == '<attachment name="a.txt">\n本文\n</attachment>'
    assert repr(history) == original  # the stored references are never replaced

    ollama = OpenAICompatProvider("ollama", "Ollama", "local", "http://127.0.0.1:1/v1")
    chat = ollama.body(ChatRequest(model="m", messages=history, data_dir=data_dir))["messages"][0]["content"]
    assert chat[1]["type"] == "image_url" and chat[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert chat[2] == {"type": "text", "text": "[添付ファイル a.pdf（PDF）: このモデルには送れない形式のため省略]"}
    openai = OpenAICompatProvider("openai", "OpenAI", "api", "http://127.0.0.1:1/v1")
    assert openai.body(ChatRequest(model="m", messages=history, data_dir=data_dir))["messages"][0]["content"][2]["type"] == "file"
    responses = files.expand(history, data_dir, "responses", frozenset({"image", "pdf", "text"}))[0]["content"]
    assert [part["type"] for part in responses] == ["input_text", "input_image", "input_file", "input_text", "input_text"]

    files.path(data_dir, image["id"]).unlink()
    gone = files.expand(history, data_dir, "anthropic", frozenset({"image", "pdf", "text"}))[0]["content"][1]
    assert gone == {"type": "text", "text": "[添付ファイル a.png（画像）: ファイルが見つからないため省略]"}


class ChatAgent:
    def __init__(self, agent_id="codex"):
        self.id, self.label = agent_id, agent_id
        self.turns = []

    def installed(self):
        return True

    async def run(self, turn):
        self.turns.append(turn)
        yield message_chunk("ok")


@pytest.fixture
def agent_client(tmp_path):
    settings = make_settings(tmp_path)
    agents = {"codex": ChatAgent("codex"), "claude_code": ChatAgent("claude_code")}
    app = create_app(settings, web_dist=Path("/nonexistent"), registry=Registry(settings, {}), agents=agents)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        yield client, agents


def test_chat_agents_get_images_natively_and_text_inline(agent_client):
    client, agents = agent_client
    conversation = client.post("/api/conversations", json={}).json()["id"]
    image = upload(client, "a.png", PNG).json()
    note = upload(client, "a.txt", b"hello").json()
    pdf = upload(client, "a.pdf", PDF).json()
    body = {"text": "見て", "provider": "codex", "model": "m", "attachments": [image["id"], note["id"]]}
    read_events(client, client.post(f"/api/conversations/{conversation}/messages", json=body).json()["run_id"])
    turn = agents["codex"].turns[-1]
    assert turn.text == '見て\n\n<attachment name="a.txt">\nhello\n</attachment>'
    assert [(item["kind"], Path(item["path"]).read_bytes()) for item in turn.attachments] == [("image", PNG)]
    refused = client.post(f"/api/conversations/{conversation}/messages", json={**body, "attachments": [pdf["id"]]})
    assert refused.status_code == 400 and "Claude Code" in refused.json()["detail"]
    other = client.post("/api/conversations", json={}).json()["id"]
    accepted = client.post(f"/api/conversations/{other}/messages",
                           json={**body, "provider": "claude_code", "attachments": [pdf["id"]]})
    read_events(client, accepted.json()["run_id"])
    assert agents["claude_code"].turns[-1].attachments[0]["kind"] == "pdf"


def test_code_agents_get_copies_in_the_project(agent_client, tmp_path):
    client, agents = agent_client
    client.post("/api/projects", json={"name": "app", "git_init": True})
    root = client.app.state.settings.omni_projects_dir / "app"
    conversation = client.post("/api/conversations", json={"mode": "code", "project": "app"}).json()["id"]
    archive = upload(client, "data.zip", b"PK\3\4\0").json()
    body = {"text": "展開して", "provider": "codex", "model": "m", "attachments": [archive["id"]]}
    read_events(client, client.post(f"/api/conversations/{conversation}/messages", json=body).json()["run_id"])
    copy = root / ".omni-attachments" / archive["id"][:12] / "data.zip"
    assert copy.read_bytes() == b"PK\3\4\0"
    turn = agents["codex"].turns[-1]
    assert turn.attachments == [] and f".omni-attachments/{archive['id'][:12]}/data.zip" in turn.text
    assert "/.omni-attachments/" in (root / ".git" / "info" / "exclude").read_text()
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert ".omni-attachments" not in status

    # A link an agent left in place of the folder is never followed
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".omni-attachments").rename(tmp_path / "moved")
    (root / ".omni-attachments").symlink_to(outside, target_is_directory=True)
    again = client.post(f"/api/conversations/{conversation}/messages", json=body)
    assert again.status_code == 409 and list(outside.iterdir()) == []


def test_codex_receives_local_images(tmp_path):
    from omniws.agents.codex import CodexAgent
    settings = make_settings(tmp_path)
    agent = CodexAgent(settings, executable=codex_executable(tmp_path))
    app = create_app(settings, web_dist=Path("/nonexistent"), registry=Registry(settings, {}), agents={"codex": agent})
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        conversation = client.post("/api/conversations", json={}).json()["id"]
        image = upload(client, "a.png", PNG).json()
        started = client.post(f"/api/conversations/{conversation}/messages",
                              json={"text": "これは?", "provider": "codex", "model": "gpt-b", "attachments": [image["id"]]})
        read_events(client, started.json()["run_id"])
        log = codex_requests(settings.omni_data_dir / "agent-chat")
        sent = [item["params"] for item in log if item["method"] == "turn/start"][-1]["input"]
        assert sent[0] == {"type": "text", "text": "これは?"}
        assert sent[1] == {"type": "localImage", "path": str(files.path(settings.omni_data_dir, image["id"]))}


def test_unreferenced_files_are_collected(client, seeing):
    data_dir = client.app.state.settings.omni_data_dir
    conversation = client.post("/api/conversations", json={}).json()["id"]
    used = upload(client, "used.png", PNG).json()["id"]
    unsent = upload(client, "unsent.png", PNG).json()["id"]
    fresh = upload(client, "fresh.png", PNG).json()["id"]
    read_events(client, send(client, conversation, text="x", attachments=[used]).json()["run_id"])
    old = time.time() - files.UNSENT_TTL - 10
    for attachment in (used, unsent):
        os.utime(files.folder(data_dir, attachment), (old, old))
    from omniws.api.attachments import collect
    assert collect(client.app.state) == 1
    assert files.folder(data_dir, used).exists() and files.folder(data_dir, fresh).exists()
    assert not files.folder(data_dir, unsent).exists()
    client.delete(f"/api/conversations/{conversation}")
    assert not files.folder(data_dir, used).exists()


def test_claude_and_acp_inputs(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from omniws.agents.claude import user_message
    from omniws.agents.opencode import OpenCodeAgent
    from omniws.providers.base import ProviderError

    image = tmp_path / "a.png"
    image.write_bytes(PNG)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(PDF)
    items = [{"kind": "image", "media_type": "image/png", "name": "a.png", "path": str(image)},
             {"kind": "pdf", "media_type": "application/pdf", "name": "a.pdf", "path": str(pdf)}]

    async def collect():
        return [message async for message in user_message("見て", items)]
    [message] = asyncio.run(collect())
    content = message["message"]["content"]
    assert message["type"] == "user" and content[0] == {"type": "text", "text": "見て"}
    assert content[1]["source"]["media_type"] == "image/png" and content[2]["type"] == "document"

    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    turn = SimpleNamespace(attachments=items[:1])
    able = SimpleNamespace(info={"agentCapabilities": {"promptCapabilities": {"image": True}}})
    blocks = agent.prompt_blocks(able, "見て", turn)
    assert blocks[1] == {"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()}
    with pytest.raises(ProviderError):
        agent.prompt_blocks(SimpleNamespace(info={}), "見て", turn)
    with pytest.raises(ProviderError):
        agent.prompt_blocks(able, "見て", SimpleNamespace(attachments=items))
