import time

from omniws.providers.base import ProviderError

from .conftest import read_events


def new_conversation(client):
    return client.post("/api/conversations", json={}).json()["id"]


def start(client, conversation_id, text="質問", provider="fake", model="m1", **extra):
    response = client.post(f"/api/conversations/{conversation_id}/messages",
                           json={"text": text, "provider": provider, "model": model, **extra})
    assert response.status_code == 202, response.text
    return response.json()


def test_reply_streams_and_is_saved(client, providers):
    conversation_id = new_conversation(client)
    started = start(client, conversation_id, "最初の質問\n2行目")
    events = read_events(client, started["run_id"])
    assert providers["fake"].requests[-1].messages == [{"role": "user", "content": "最初の質問\n2行目"}]
    kinds = [(name, data.get("sessionUpdate") or data.get("status")) for _, name, data in events]
    assert kinds == [("status", "running"), ("update", "agent_message_chunk"), ("update", "agent_message_chunk"),
                     ("update", "usage_update"), ("status", "complete")]
    assert [event_id for event_id, _, _ in events] == [1, 2, 3, 4, 5]
    saved = client.get(f"/api/conversations/{conversation_id}").json()
    assert saved["title"] == "最初の質問"
    assert saved["target"] == {"provider": "fake", "model": "m1", "effort": None}
    reply = saved["messages"][1]
    assert reply["status"] == "complete"
    assert reply["content"] == [{"type": "text", "text": "こんにちは、世界"}]
    assert reply["usage"]["inputTokens"] == 10 and reply["usage"]["contextWindow"] == 1000


def test_each_message_can_use_another_provider_with_the_whole_history(client, providers):
    conversation_id = new_conversation(client)
    read_events(client, start(client, conversation_id, "一つ目")["run_id"])
    read_events(client, start(client, conversation_id, "二つ目", provider="other")["run_id"])
    request = providers["other"].requests[-1]
    assert request.messages == [{"role": "user", "content": "一つ目"},
                                {"role": "assistant", "content": "こんにちは、世界"},
                                {"role": "user", "content": "二つ目"}]
    messages = client.get(f"/api/conversations/{conversation_id}").json()["messages"]
    assert [m["provider"] for m in messages] == [None, "fake", None, "other"]


def test_thinking_is_kept_but_not_sent_to_the_next_model(client, providers):
    providers["fake"].thinking = "考え中"
    conversation_id = new_conversation(client)
    read_events(client, start(client, conversation_id, "一つ目")["run_id"])
    reply = client.get(f"/api/conversations/{conversation_id}").json()["messages"][1]
    assert reply["content"][0] == {"type": "thinking", "text": "考え中"}
    read_events(client, start(client, conversation_id, "二つ目", provider="other")["run_id"])
    assert all("考え中" not in m["content"] for m in providers["other"].requests[-1].messages)


def test_reconnect_gets_only_missed_events(client):
    conversation_id = new_conversation(client)
    run_id = start(client, conversation_id)["run_id"]
    everything = read_events(client, run_id)
    later = read_events(client, run_id, headers={"Last-Event-ID": "3"})
    assert later == everything[3:]


def test_provider_error_keeps_partial_text(client, providers):
    providers["fake"].fail = ProviderError("rate_limit", "上限")
    conversation_id = new_conversation(client)
    events = read_events(client, start(client, conversation_id)["run_id"])
    assert events[-1][2]["status"] == "error"
    reply = client.get(f"/api/conversations/{conversation_id}").json()["messages"][1]
    assert reply["status"] == "error"
    assert reply["error"] == {"code": "rate_limit", "message": "上限"}
    assert reply["content"] == [{"type": "text", "text": "こんにちは、世界"}]


def test_cancel(client, providers):
    providers["fake"].chunks = ["a"] * 200
    providers["fake"].delay = 0.02
    conversation_id = new_conversation(client)
    run_id = start(client, conversation_id)["run_id"]
    assert client.post(f"/api/conversations/{conversation_id}/messages",
                       json={"text": "x", "provider": "fake", "model": "m1"}).status_code == 409
    assert client.get(f"/api/conversations/{conversation_id}").json()["active_run"] == run_id
    time.sleep(0.1)
    assert client.post(f"/api/runs/{run_id}/cancel").json() == {"cancelled": True}
    events = read_events(client, run_id)
    assert events[-1][2]["status"] == "cancelled"
    reply = client.get(f"/api/conversations/{conversation_id}").json()["messages"][1]
    assert reply["status"] == "cancelled"
    assert 0 < len(reply["content"][0]["text"]) < 200


def test_unknown_provider_and_run(client):
    conversation_id = new_conversation(client)
    response = client.post(f"/api/conversations/{conversation_id}/messages",
                           json={"text": "x", "provider": "missing", "model": "m"})
    assert response.status_code == 400
    assert client.get("/api/runs/nope/events").status_code == 404


def test_executor_overview(client):
    overview = {item["id"]: item for item in client.get("/api/executors").json()}
    assert overview["anthropic"]["configured"] is False
    assert overview["anthropic"]["setting"] == "ANTHROPIC_API_KEY"
    assert overview["fake"]["models"][0]["id"] == "m1"
    assert overview["fake"]["modes"] == ["chat"]


def test_events_added_while_reading_are_not_skipped():
    import asyncio

    from omniws.runs import Run, RunManager

    async def scenario():
        manager = RunManager()
        run = Run(id="r", conversation_id="c", message_id="m")
        run.emit("status", {"status": "running"})
        seen = []
        async for event_id, _name, data in manager.subscribe(run):
            seen.append(event_id)
            if event_id == 1:  # more events arrive while the reader holds the first one
                run.emit("update", {"n": 2})
                run.emit("status", {"status": "complete"})
                run.finished_at = 1.0
        return seen

    assert asyncio.run(scenario()) == [1, 2, 3]
