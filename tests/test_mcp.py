import json
import sys
from pathlib import Path

import httpx2 as httpx
from fastapi.testclient import TestClient

from omniws.events import message_chunk, usage_update
from omniws.main import create_app
from omniws.models import AppSetting
from omniws.providers.base import ChatRequest, ModelInfo
from omniws.providers.registry import Registry

from .conftest import PASSWORD, make_settings
from .test_agents import follow
from .test_providers import collect, openai_provider, sse_body

SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


class ToolProvider:
    """Calls the first tool it is offered with the given arguments, then answers with what the tool said."""

    id, label, kind = "toolful", "Tool model", "api"

    def __init__(self, tool_suffix="add", arguments=None):
        self.tool_suffix = tool_suffix
        self.arguments = arguments if arguments is not None else {"a": 2, "b": 3}
        self.requests = []

    async def list_models(self):
        return [ModelInfo(id="m", name="m")]

    @staticmethod
    def tool_results(results):
        return [{"role": "tool", "results": results}]

    async def stream(self, request, model=None):
        self.requests.append([dict(item) for item in request.messages])
        last = request.messages[-1]
        if last.get("role") == "tool":
            yield message_chunk("結果: " + last["results"][0]["text"])
        else:
            tool = next(item for item in request.tools or [] if item["name"].endswith(self.tool_suffix))
            request.tool_calls = [{"id": "c1", "name": tool["name"], "arguments": self.arguments}]
            request.assistant_turn = {"role": "assistant", "content": "呼びます"}
        yield usage_update(input_tokens=1, output_tokens=1)


def app_for(tmp_path, provider, **values):
    config = make_settings(tmp_path, **values)
    app = create_app(config, web_dist=Path("/nonexistent"), registry=Registry(config, {provider.id: provider}), agents={})
    return app


def stdio_server(**extra):
    return {"name": "デモ", "transport": "stdio", "command": sys.executable, "args": [SERVER], **extra}


def test_env_limits_mcp_and_local_servers(tmp_path):
    with TestClient(app_for(tmp_path, ToolProvider())) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        assert client.post("/api/settings/mcp", json=stdio_server()).status_code == 403
        assert client.put("/api/settings/permissions", json={"omni_allow_mcp": True}).status_code == 400
    with TestClient(app_for(tmp_path / "2", ToolProvider(), omni_allow_mcp=True)) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        assert client.post("/api/settings/mcp", json=stdio_server()).status_code == 403  # stdio needs its own switch
        remote = {"name": "remote", "transport": "http", "url": "http://example.com/mcp"}
        assert client.post("/api/settings/mcp", json=remote).status_code == 400  # plain http only to this machine
        assert client.post("/api/settings/mcp", json={**remote, "url": "https://example.com/mcp"}).status_code == 201


def test_secrets_are_sealed_and_tools_listed(tmp_path):
    app = app_for(tmp_path, ToolProvider(), omni_allow_mcp=True, omni_allow_mcp_stdio=True)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        view = client.post("/api/settings/mcp", json=stdio_server(env={"DEMO_TOKEN": "s3cret"})).json()
        server = view["servers"][0]
        assert server["env_keys"] == ["DEMO_TOKEN"] and "s3cret" not in json.dumps(view)
        with app.state.sessions() as session:
            stored = json.dumps(session.get(AppSetting, "mcp_servers").value)
        assert "s3cret" not in stored
        inspected = client.post(f"/api/settings/mcp/{server['id']}/inspect").json()
        assert inspected["ok"] and {tool["name"]: tool["read_only"] for tool in inspected["tools"]} == {"add": True, "secret_hint": False}
        assert client.get("/api/settings/mcp").json()["servers"][0]["tools"][0]["name"] == "add"
        # Keeping a secret: leaving it out keeps it, null removes it
        kept = client.put(f"/api/settings/mcp/{server['id']}", json={"name": "改名"}).json()["servers"][0]
        assert kept["env_keys"] == ["DEMO_TOKEN"] and kept["name"] == "改名"
        gone = client.put(f"/api/settings/mcp/{server['id']}", json={"env": {"DEMO_TOKEN": None}}).json()["servers"][0]
        assert gone["env_keys"] == []


def send(client, conversation, text="計算して"):
    response = client.post(f"/api/conversations/{conversation}/messages",
                           json={"text": text, "provider": "toolful", "model": "m"})
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_chat_calls_tools_with_approval(tmp_path):
    provider = ToolProvider()
    app = app_for(tmp_path, provider, omni_allow_mcp=True, omni_allow_mcp_stdio=True)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        server = client.post("/api/settings/mcp", json=stdio_server()).json()["servers"][0]
        conversation = client.post("/api/conversations", json={"mode": "chat"}).json()["id"]

        # Asked every time: denied, the model hears it
        events = follow(client, send(client, conversation), answer=lambda event: "deny")
        asks = [data for name, data in events if name == "permission"]
        assert asks and asks[0]["kind"] == "mcp" and asks[0]["title"] == "デモ / add" and '"a": 2' in asks[0]["command"]
        reply = client.get(f"/api/conversations/{conversation}").json()["messages"][-1]
        assert "拒否" in reply["content"][-1]["text"]

        # "Always allow" for this conversation: the next call runs without asking
        follow(client, send(client, conversation), answer=lambda event: "allow_always")
        events = follow(client, send(client, conversation))
        assert not [name for name, _ in events if name == "permission"]
        reply = client.get(f"/api/conversations/{conversation}").json()["messages"][-1]
        tool = next(block for block in reply["content"] if block["type"] == "tool_call")
        assert tool["status"] == "completed" and tool["output"] == "5" and reply["content"][-1]["text"] == "結果: 5"
        # The model got the tool turn and its result back
        assert provider.requests[-1][-2]["content"] == "呼びます"

        # Turned off for this conversation: no tools offered at all
        client.patch(f"/api/conversations/{conversation}", json={"mcp_off": [server["id"]]})
        assert client.get(f"/api/conversations/{conversation}").json()["mcp_off"] == [server["id"]]


def test_allowed_in_settings_and_secret_env_reaches_the_server(tmp_path):
    provider = ToolProvider(tool_suffix="secret_hint", arguments={})
    app = app_for(tmp_path, provider, omni_allow_mcp=True, omni_allow_mcp_stdio=True)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        server = client.post("/api/settings/mcp", json=stdio_server(env={"DEMO_TOKEN": "s3cret"})).json()["servers"][0]
        client.put(f"/api/settings/mcp/{server['id']}", json={"always_allow": ["secret_hint"]})
        conversation = client.post("/api/conversations", json={"mode": "chat"}).json()["id"]
        events = follow(client, send(client, conversation))
        assert not [name for name, _ in events if name == "permission"]
        reply = client.get(f"/api/conversations/{conversation}").json()["messages"][-1]
        assert reply["content"][-1]["text"] == "結果: token:set"


def test_unreachable_server_is_reported_and_the_turn_goes_on(tmp_path):
    provider = ToolProvider()
    app = app_for(tmp_path, provider, omni_allow_mcp=True, omni_allow_mcp_stdio=True)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        client.post("/api/settings/mcp", json={"name": "壊れた", "transport": "stdio", "command": "/nonexistent/mcp"})

        class Plain(ToolProvider):
            async def stream(self, request, model=None):
                assert request.tools is None
                yield message_chunk("ツールなしで回答")
                yield usage_update()

        app.state.registry.providers["toolful"] = Plain()
        conversation = client.post("/api/conversations", json={"mode": "chat"}).json()["id"]
        follow(client, send(client, conversation))
        reply = client.get(f"/api/conversations/{conversation}").json()["messages"][-1]
        failed = next(block for block in reply["content"] if block["type"] == "tool_call")
        assert failed["status"] == "failed" and failed["title"] == "MCP: 壊れた"
        assert reply["content"][-1]["text"] == "ツールなしで回答"


def test_openai_compatible_tool_calls_arrive_in_pieces():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        body = sse_body(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "s__add", "arguments": "{\"a\": 2"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ", \"b\": 3}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    provider = openai_provider(handler)
    request = ChatRequest(model="gpt-x", messages=[{"role": "user", "content": "q"}],
                          tools=[{"name": "s__add", "description": "Add", "schema": {"type": "object"}}])
    collect(provider, request)
    assert seen["body"]["tools"][0]["function"]["name"] == "s__add"
    assert request.tool_calls == [{"id": "call_a", "name": "s__add", "arguments": {"a": 2, "b": 3}}]
    assert request.assistant_turn["tool_calls"][0]["function"]["arguments"] == "{\"a\": 2, \"b\": 3}"
    assert provider.tool_results([{"id": "call_a", "text": "5", "is_error": False}]) == \
        [{"role": "tool", "tool_call_id": "call_a", "content": "5"}]


def test_anthropic_tool_use_turn_goes_back_with_results():
    from types import SimpleNamespace
    from omniws.providers import anthropic_api
    from .test_providers import FakeStream

    content = [SimpleNamespace(type="thinking", thinking="考え", signature="sig"),
               SimpleNamespace(type="tool_use", id="toolu_1", name="s__add", input={"a": 1})]
    final = SimpleNamespace(stop_reason="tool_use", content=content, usage=SimpleNamespace(
        input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    calls = {}

    def stream(**options):
        calls.update(options)
        return FakeStream([], final)

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream), beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    provider = anthropic_api.AnthropicProvider("key", client=client)
    request = ChatRequest(model="claude-x", messages=[{"role": "user", "content": "q"}],
                          tools=[{"name": "s__add", "description": "Add", "schema": {"type": "object"}}])
    collect(provider, request)
    assert calls["tools"][0] == {"name": "s__add", "description": "Add", "input_schema": {"type": "object"}}
    assert request.tool_calls == [{"id": "toolu_1", "name": "s__add", "arguments": {"a": 1}}]
    assert request.assistant_turn["content"][0] == {"type": "thinking", "thinking": "考え", "signature": "sig"}
    results = provider.tool_results([{"id": "toolu_1", "text": "2", "is_error": False}])
    assert results[0]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "2", "is_error": False}
