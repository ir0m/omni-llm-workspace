import asyncio
import json
from types import SimpleNamespace

import httpx2 as httpx
import pytest

from omniws.config import Settings
from omniws.history import chat_history
from omniws.models import Message
from omniws.providers import anthropic_api
from omniws.providers.base import ChatRequest, ModelInfo, ProviderError
from omniws.providers.openai_compat import OpenAICompatProvider
from omniws.providers.registry import Registry, build_providers


def collect(provider, request, model=None):
    async def run():
        return [update async for update in provider.stream(request, model)]
    return asyncio.run(run())


def sse_body(*chunks) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def openai_provider(handler, **kwargs):
    return OpenAICompatProvider("openai", "OpenAI API", "api", "https://api.example/v1", "sk-test",
                                transport=httpx.MockTransport(handler), **kwargs)


def test_openai_compatible_stream():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        body = sse_body({"choices": [{"delta": {"reasoning_content": "考える"}}]},
                        {"choices": [{"delta": {"content": "答え"}}]},
                        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3,
                                                  "prompt_tokens_details": {"cached_tokens": 4}}})
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    updates = collect(openai_provider(handler), ChatRequest(model="gpt-x", messages=[{"role": "user", "content": "q"}],
                                                            system="sys", effort="low"))
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["reasoning_effort"] == "low" and seen["body"]["stream_options"] == {"include_usage": True}
    assert [u["sessionUpdate"] for u in updates] == ["agent_thought_chunk", "agent_message_chunk", "usage_update"]
    assert updates[-1]["inputTokens"] == 12 and updates[-1]["cacheReadTokens"] == 4
    assert updates[-1]["stopReason"] == "stop"


@pytest.mark.parametrize("status,code", [(401, "auth"), (404, "not_found"), (429, "rate_limit"), (503, "overloaded"),
                                         (400, "bad_request")])
def test_openai_compatible_errors(status, code):
    provider = openai_provider(lambda request: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(ProviderError) as error:
        collect(provider, ChatRequest(model="m", messages=[]))
    assert error.value.code == code and error.value.message == "nope"


def test_openai_compatible_network_error():
    def handler(request):
        raise httpx.ConnectError("down")
    with pytest.raises(ProviderError) as error:
        collect(openai_provider(handler), ChatRequest(model="m", messages=[]))
    assert error.value.code == "network"


def test_model_lists():
    listing = {"data": [{"id": "gpt-x"}, {"id": "text-embedding-3"}, {"id": "models/gemini-pro"}, {"id": "whisper-1"}]}
    provider = openai_provider(lambda request: httpx.Response(200, json=listing))
    assert [m.id for m in asyncio.run(provider.list_models())] == ["gemini-pro", "gpt-x"]
    tags = OpenAICompatProvider("ollama", "Ollama", "local", "http://h:11434/v1", ollama_root="http://h:11434",
                                transport=httpx.MockTransport(
                                    lambda request: httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})))
    assert [m.id for m in asyncio.run(tags.list_models())] == ["qwen3:8b"]


def test_fixed_models_and_enabled_providers(tmp_path):
    settings = Settings(_env_file=None, openai_api_key="sk", ollama_base_url="http://h:11434/v1/",
                        omni_providers="openai", omni_models_openai="a, b")
    assert list(build_providers(settings)) == ["openai"]
    registry = Registry(settings, {"openai": openai_provider(lambda r: httpx.Response(500, json={}))})
    listing = asyncio.run(registry.models("openai"))
    assert [m.id for m in listing.models] == ["a", "b"] and listing.error is None
    ollama = build_providers(Settings(_env_file=None, ollama_base_url="http://h:11434/v1/"))["ollama"]
    assert ollama.base_url == "http://h:11434/v1" and ollama.ollama_root == "http://h:11434"


def fake_model(adaptive=True, efforts=("low", "high")):
    support = lambda value: SimpleNamespace(supported=value)  # noqa: E731
    effort = SimpleNamespace(supported=bool(efforts), **{lvl: support(lvl in efforts) for lvl in anthropic_api.EFFORT_LEVELS})
    caps = SimpleNamespace(image_input=support(True), thinking=SimpleNamespace(
        supported=True, types=SimpleNamespace(adaptive=support(adaptive), enabled=support(not adaptive))), effort=effort)
    return SimpleNamespace(id="claude-opus-5-5", display_name="Claude Opus 5.5", max_input_tokens=1_000_000,
                           max_tokens=128_000, capabilities=caps)


def test_anthropic_model_info_and_options():
    info = anthropic_api.model_info(fake_model())
    assert info.context_window == 1_000_000 and info.vision and info.efforts == ("low", "high")
    options = anthropic_api.request_options(ChatRequest(model=info.id, messages=[], effort="high"), info)
    assert options["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert options["output_config"] == {"effort": "high"} and options["max_tokens"] == 64000
    assert options["fallbacks"] == "default" and options["betas"] == [anthropic_api.FALLBACK_BETA]
    other = anthropic_api.model_info(fake_model(adaptive=False, efforts=()))
    plain = anthropic_api.request_options(ChatRequest(model="claude-haiku-4-5", messages=[], effort="max"), other)
    assert "thinking" not in plain and "output_config" not in plain and "fallbacks" not in plain


class FakeStream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for event in self.events:
                yield event
        return gen()

    async def get_final_message(self):
        return self.final


def test_anthropic_stream_maps_events():
    final = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="はい")], usage=SimpleNamespace(
        input_tokens=7, output_tokens=2, cache_read_input_tokens=100, cache_creation_input_tokens=5))
    events = [SimpleNamespace(type="thinking", thinking="ふむ"), SimpleNamespace(type="text", text="はい"),
              SimpleNamespace(type="content_block_stop")]
    calls = {}

    def stream(**options):
        calls.update(options)
        return FakeStream(events, final)

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream), beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    provider = anthropic_api.AnthropicProvider("key", client=client)
    info = ModelInfo(id="claude-x", name="x", context_window=200_000)
    updates = collect(provider, ChatRequest(model="claude-x", messages=[{"role": "user", "content": "q"}]), info)
    assert [u["sessionUpdate"] for u in updates] == ["agent_thought_chunk", "agent_message_chunk", "usage_update"]
    assert updates[-1]["cacheReadTokens"] == 100 and updates[-1]["contextWindow"] == 200_000
    assert "betas" not in calls

    refused = SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(explanation="no"), usage=final.usage)
    client.messages.stream = lambda **options: FakeStream([], refused)
    with pytest.raises(ProviderError) as error:
        collect(provider, ChatRequest(model="claude-x", messages=[]), info)
    assert error.value.code == "refused"


def message(seq, role, text, kind="text"):
    return Message(id=str(seq), conversation_id="c", seq=seq, role=role, content=[{"type": kind, "text": text}])


def test_chat_history():
    history = chat_history([message(1, "assistant", "挨拶"), message(2, "user", "a"), message(3, "user", "b"),
                            message(4, "assistant", "考え", kind="thinking"), message(5, "assistant", "答え")])
    assert history == [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "答え"}]


def test_anthropic_web_tools_follow_model_and_flag():
    from omniws.providers.anthropic_api import request_options
    request = ChatRequest(model="claude-opus-5-5", messages=[{"role": "user", "content": "q"}], web_search=True)
    names = [tool["type"] for tool in request_options(request, None)["tools"]]
    assert names == ["web_search_20260209", "web_fetch_20260209"]
    request.model = "claude-haiku-4-5"
    assert [tool["type"] for tool in request_options(request, None)["tools"]] == ["web_search_20250305"]
    request.web_search = False
    assert "tools" not in request_options(request, None)
    request.web_search, request.tools = True, [{"name": "mcp", "description": "d", "schema": {"type": "object"}}]
    assert [tool.get("type", tool["name"]) for tool in request_options(request, None)["tools"]] == ["mcp", "web_search_20250305"]


def test_anthropic_web_search_cards_and_pause_turn_continuation():
    search = SimpleNamespace(type="server_tool_use", id="srv1", name="web_search", input={})
    done = SimpleNamespace(type="server_tool_use", id="srv1", name="web_search", input={"query": "天気"},
                           model_dump=lambda **kw: {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"query": "天気"}})
    result = SimpleNamespace(type="web_search_tool_result", tool_use_id="srv1",
                             content=[SimpleNamespace(type="web_search_result", title="気象庁", url="https://www.jma.go.jp/")],
                             model_dump=lambda **kw: {"type": "web_search_tool_result", "tool_use_id": "srv1", "content": []})
    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    paused = SimpleNamespace(stop_reason="pause_turn", content=[done, result], usage=usage)
    finished = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="晴れ")], usage=usage)
    streams = [FakeStream([SimpleNamespace(type="content_block_start", content_block=search),
                           SimpleNamespace(type="content_block_start", content_block=result)], paused),
               FakeStream([SimpleNamespace(type="text", text="晴れ")], finished)]
    sent = []
    def stream(**options):
        sent.append(list(options["messages"]))
        return streams.pop(0)
    client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    provider = anthropic_api.AnthropicProvider("test", client=client)
    request = ChatRequest(model="claude-opus-4-6", messages=[{"role": "user", "content": "q"}], web_search=True)
    updates = collect(provider, request)
    cards = [u for u in updates if u["sessionUpdate"].startswith("tool_call")]
    assert cards[0] == {"sessionUpdate": "tool_call", "toolCallId": "srv1", "kind": "fetch", "title": "Web検索", "status": "in_progress"}
    assert cards[1]["status"] == "completed" and "https://www.jma.go.jp/" in cards[1]["output"]
    assert cards[2]["title"] == "Web検索: 天気"
    # The paused turn is sent back unchanged so the server continues it
    assert len(sent) == 2 and sent[1][-1]["role"] == "assistant" and sent[1][-1]["content"][0]["type"] == "server_tool_use"
    assert request.replay_messages[-1]["content"] == [{"type": "text", "text": "晴れ"}]
    assert sum(1 for u in updates if u["sessionUpdate"] == "usage_update") == 2
