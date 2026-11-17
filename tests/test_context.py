import asyncio
from types import SimpleNamespace
import pytest
from omniws import context
from omniws.models import Message
from omniws.providers.base import ChatRequest, ProviderError
from omniws.events import message_chunk, usage_update
from .conftest import read_events
from .test_runs import new_conversation, start


def fill(client, count=5):
    cid = new_conversation(client)
    for i in range(count):
        read_events(client, start(client, cid, text=f"question {i}")["run_id"])
    return cid


def test_summary_preserves_archive_and_recent_turns(client, providers):
    cid = fill(client)
    before = client.get(f"/api/conversations/{cid}").json()["messages"]
    response = client.post(f"/api/conversations/{cid}/context", json={"provider": "fake", "model": "m1"})
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["through_seq"] == 4 and summary["usage"]["inputTokens"] == 10
    assert client.get(f"/api/conversations/{cid}").json()["messages"] == before
    read_events(client, start(client, cid, provider="other")["run_id"])
    sent = providers["other"].requests[-1].messages
    assert "handover" in sent[0]["content"] and "question 0" not in str(sent)
    assert all(f"question {i}" in str(sent) for i in (2, 3, 4))
    assert client.delete(f"/api/conversations/{cid}/context").status_code == 200
    read_events(client, start(client, cid)["run_id"])
    assert "question 0" in str(providers["fake"].requests[-1].messages)


def test_summary_failure_keeps_original(client, providers):
    cid = fill(client, 4)
    providers["fake"].fail = ProviderError("rate_limit")
    assert client.post(f"/api/conversations/{cid}/context", json={"provider": "fake", "model": "m1"}).status_code == 502
    detail = client.get(f"/api/conversations/{cid}").json()
    assert detail["context"] is None and len(detail["messages"]) == 8


def test_summary_requires_old_turns_and_refuses_live_run(client, providers):
    cid = fill(client, 1)
    body = {"provider": "fake", "model": "m1"}
    assert client.post(f"/api/conversations/{cid}/context", json=body).status_code == 400
    providers["fake"].delay = 1
    run = start(client, cid)
    assert client.post(f"/api/conversations/{cid}/context", json=body).status_code == 409
    client.post(f"/api/runs/{run['run_id']}/cancel")


def test_summary_invalidates_when_imported_prefix_changes():
    messages = [Message(id=str(i), seq=i, role="user" if i % 2 else "assistant", content=[{"type": "text", "text": str(i)}], status="complete") for i in range(1, 5)]
    summary = {"through_seq": 2, "text": "saved decisions", "fingerprint": context.fingerprint(messages[:2])}
    assert [m.seq for m in context.selected(messages, summary)] == [2, 3, 4]
    assert [m.seq for m in context.selected(messages, summary, 2)] == [3, 4]
    messages[0].content = [{"type": "text", "text": "changed"}]
    assert context.selected(messages, summary) == messages


def test_native_replay_is_private_to_provider_model_system_and_transport(client, providers):
    provider = providers["fake"]
    async def stream(request, model=None):
        provider.requests.append(request)
        yield message_chunk("answer")
        request.replay_messages = [*request.messages, {"role": "assistant", "content": "signed-reasoning"}]
    provider.stream = stream
    cid = fill(client, 1)
    read_events(client, start(client, cid)["run_id"])
    assert "signed-reasoning" in str(provider.requests[-1].messages)
    read_events(client, start(client, cid, provider="other")["run_id"])
    assert "signed-reasoning" not in str(providers["other"].requests[-1].messages)
    read_events(client, start(client, cid, model="m2")["run_id"])
    assert "signed-reasoning" not in str(provider.requests[-1].messages)
    detail = client.get(f"/api/conversations/{cid}").json()
    assert "signed-reasoning" not in str(detail)


def test_native_compaction_replays_result(client, providers):
    provider = providers["fake"]
    async def stream(request, model=None):
        provider.requests.append(request)
        yield message_chunk("answer")
        request.replay_messages = [*request.messages, {"role": "assistant", "content": "native"}]
    async def compact(request, model=None):
        return [{"role": "assistant", "content": "compacted"}]
    provider.stream, provider.compact = stream, compact
    cid = fill(client, 1)
    assert client.post(f"/api/conversations/{cid}/context/native", json={"provider": "fake", "model": "m1"}).status_code == 200
    read_events(client, start(client, cid)["run_id"])
    assert provider.requests[-1].messages[0]["content"] == "compacted"


def test_responses_preserves_reasoning_and_rejects_truncated_stream():
    import httpx2 as httpx
    from omniws.providers.openai_responses import OpenAIResponsesProvider
    from .test_providers import collect, sse_body
    output = [{"type": "reasoning", "id": "r", "encrypted_content": "opaque", "summary": []},
              {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}]
    def handler(request):
        return httpx.Response(200, content=sse_body({"type": "response.output_text.delta", "delta": "answer"},
            {"type": "response.completed", "response": {"output": output, "usage": {"input_tokens": 5}}}))
    provider = OpenAIResponsesProvider("openai", "OpenAI", "api", "https://example.com/v1", transport=httpx.MockTransport(handler))
    request = ChatRequest(model="test", messages=[{"role": "user", "content": "q"}])
    collect(provider, request)
    assert request.replay_messages[-1]["items"] == output
    assert provider.inputs(request.replay_messages)[-2:] == output
    provider.transport = httpx.MockTransport(lambda _: httpx.Response(200, content=sse_body({"type": "response.output_text.delta", "delta": "partial"})))
    with pytest.raises(ProviderError):
        collect(provider, ChatRequest(model="test", messages=[]))


def test_anthropic_signed_blocks_survive_and_compaction_beta_is_sent():
    from omniws.providers.anthropic_api import AnthropicProvider, request_options
    from .test_providers import collect, FakeStream
    signed = SimpleNamespace(type="thinking", thinking="summary", signature="opaque-signature")
    final = SimpleNamespace(stop_reason="end_turn", content=[signed, SimpleNamespace(type="text", text="answer")],
        usage=SimpleNamespace(input_tokens=2, output_tokens=3))
    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream([], final)))
    provider = AnthropicProvider("test", client=client)
    request = ChatRequest(model="test", messages=[{"role": "user", "content": "q"}])
    collect(provider, request)
    assert request.replay_messages[-1]["content"][0] == {"type": "thinking", "thinking": "summary", "signature": "opaque-signature"}
    request.messages = [{"role": "assistant", "content": [{"type": "compaction", "content": "summary", "signature": "s"}]}]
    assert "compact-2026-09-04" in request_options(request, None)["betas"]


def test_replay_invalidated_by_system_and_format():
    messages = [Message(id="m", seq=1, role="user", content=[{"type": "text", "text": "q"}], status="complete")]
    checkpoint = {"provider": "p", "model": "m", "system": "s", "format": "v1", "through_seq": 1,
        "fingerprint": context.fingerprint(messages), "messages": [{"role": "assistant", "content": "secret"}]}
    assert "secret" in str(context.replay(messages, None, checkpoint, "p", "m", "s", "v1"))
    for system, format in [("different", "v1"), ("s", "v2")]:
        assert "secret" not in str(context.replay(messages, None, checkpoint, "p", "m", system, format))


def test_codex_compaction_waits_for_completion(tmp_path, monkeypatch):
    from omniws.agents.codex import CodexAgent
    from .conftest import make_settings
    async def scenario():
        calls = []
        queue = asyncio.Queue()
        class Connection:
            async def call(self, method, params, **kwargs):
                calls.append(method)
                if method == "thread/compact/start":
                    queue.put_nowait({"method": "item/completed", "params": {"item": {"type": "contextCompaction"}}})
                    queue.put_nowait({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
                return {}
            def listen(self, *args): return queue
            def unlisten(self, *args): calls.append("unlisten")
        async def connect(self): return Connection()
        async def close(self): calls.append("close")
        monkeypatch.setattr(CodexAgent, "connect", connect)
        monkeypatch.setattr(CodexAgent, "close", close)
        agent = CodexAgent(make_settings(tmp_path))
        agent.open_threads["native"] = "cached"
        await agent.compact("native", tmp_path)
        assert "native" not in agent.open_threads
        assert calls == ["thread/resume", "thread/compact/start", "unlisten", "close"]
    asyncio.run(scenario())


def test_claude_compaction_requires_actual_boundary(tmp_path, monkeypatch):
    import claude_agent_sdk as sdk
    from omniws.agents.claude import ClaudeCodeAgent
    from .conftest import make_settings
    boundary = False
    class Client:
        def __init__(self, options):
            assert options.tools == [] and options.resume == "native"
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def query(self, prompt): assert prompt == "/compact"
        async def receive_response(self):
            if boundary:
                yield sdk.SystemMessage(subtype="compact_boundary", data={})
            yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                    num_turns=1, session_id="native", result="done")
    monkeypatch.setattr(sdk, "ClaudeSDKClient", Client)
    agent = ClaudeCodeAgent(make_settings(tmp_path), "subscription")
    with pytest.raises(ProviderError):
        asyncio.run(agent.compact("native", tmp_path))
    boundary = True
    asyncio.run(agent.compact("native", tmp_path))


def test_summary_does_not_apply_to_changed_history(client, providers):
    cid = fill(client, 4)
    async def stream(request, model=None):
        with client.app.state.sessions.begin() as session:
            from sqlalchemy import select
            first = session.scalar(select(Message).where(Message.conversation_id == cid).order_by(Message.seq))
            first.content = [{"type": "text", "text": "updated by import"}]
        yield message_chunk("old summary")
    providers["fake"].stream = stream
    response = client.post(f"/api/conversations/{cid}/context", json={"provider": "fake", "model": "m1"})
    assert response.status_code == 409
    assert client.get(f"/api/conversations/{cid}").json()["context"] is None


def test_anthropic_on_demand_compaction_request_shape():
    from omniws.providers.anthropic_api import AnthropicProvider, COMPACT_BETA
    from omniws.providers.base import ModelInfo
    sent = {}
    block = SimpleNamespace(model_dump=lambda **kw: {"type": "compaction", "content": "summary", "signature": "sig"})
    async def create(**options):
        sent.update(options)
        return SimpleNamespace(stop_reason="compaction", content=[block])
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    provider = AnthropicProvider("test", client=client)
    model = ModelInfo(id="claude-opus-5-5", name="Opus", extra={"thinking_modes": ["adaptive"]})
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": [{"type": "text", "text": "a"}]}]
    request = ChatRequest(model="claude-opus-5-5", messages=history, system="s")
    result = asyncio.run(provider.compact(request, model))
    assert sent["betas"] == [COMPACT_BETA] and sent["compaction"] == {"type": "summarize"}
    assert sent["messages"] == history and sent["system"] == "s"
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert sent["max_tokens"] <= 16000 and "fallbacks" not in sent
    assert result == [{"role": "assistant", "content": [{"type": "compaction", "content": "summary", "signature": "sig"}]}]


def test_chat_web_search_flag_and_separate_replay(client, providers):
    provider = providers["fake"]
    provider.web_search, provider.context_format = True, "fake-v1"
    async def stream(request, model=None):
        provider.requests.append(request)
        yield message_chunk("answer")
        request.replay_messages = [*request.messages, {"role": "assistant", "content": "signed-with-web"}]
    provider.stream = stream
    cid = fill(client, 1)
    assert provider.requests[-1].web_search is True
    read_events(client, start(client, cid, provider="other")["run_id"])
    assert providers["other"].requests[-1].web_search is False  # no server-side web tools there
    read_events(client, start(client, cid)["run_id"])
    assert "signed-with-web" in str(provider.requests[-1].messages)
    # Turning web search off changes the tool set: replay data made with it must not be sent
    client.app.state.settings.omni_chat_web_search = False
    read_events(client, start(client, cid)["run_id"])
    assert provider.requests[-1].web_search is False
    assert "signed-with-web" not in str(provider.requests[-1].messages)
