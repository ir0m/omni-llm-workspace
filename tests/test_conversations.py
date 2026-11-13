from .conftest import read_events


def create(client, **body):
    response = client.post("/api/conversations", json=body)
    assert response.status_code == 201
    return response.json()


def titles(client, **params):
    return [item["title"] for item in client.get("/api/conversations", params=params).json()]


def test_new_conversations_come_first_and_pins_stay_on_top(client):
    a, b = create(client, title="A"), create(client, title="B")
    assert titles(client) == ["B", "A"]
    client.patch(f"/api/conversations/{a['id']}", json={"pinned": True})
    assert titles(client) == ["A", "B"]


def test_rename_hide_and_show(client):
    a = create(client, title="A")
    create(client, title="B")
    client.patch(f"/api/conversations/{a['id']}", json={"title": "  新しい名前 ", "hidden": True})
    assert titles(client) == ["B"]
    assert titles(client, include_hidden=True) == ["B", "新しい名前"]
    client.patch(f"/api/conversations/{a['id']}", json={"hidden": False})
    assert "新しい名前" in titles(client)


def test_manual_order(client):
    a, b, c = (create(client, title=name) for name in "ABC")
    client.put("/api/conversations/order", json={"ids": [a["id"], c["id"], b["id"]]})
    assert titles(client) == ["A", "C", "B"]


def test_temporary_flag(client):
    temp = create(client, temporary=True)
    assert temp["temporary"] is True
    assert client.get(f"/api/conversations/{temp['id']}").json()["temporary"] is True


def send(client, conversation_id, text, provider="fake", model="m1"):
    response = client.post(f"/api/conversations/{conversation_id}/messages",
                           json={"text": text, "provider": provider, "model": model})
    assert response.status_code == 202, response.text
    read_events(client, response.json()["run_id"])
    return response.json()


def test_fork_copies_up_to_a_message(client):
    conversation = create(client)
    first = send(client, conversation["id"], "一つ目")
    send(client, conversation["id"], "二つ目")
    fork = client.post(f"/api/conversations/{conversation['id']}/fork",
                       json={"message_id": first["assistant_message"]["id"]}).json()
    copied = client.get(f"/api/conversations/{fork['id']}").json()
    assert fork["forked_from"] == conversation["id"]
    assert [m["content"][0]["text"] for m in copied["messages"]] == ["一つ目", "こんにちは、世界"]
    assert copied["title"].endswith("（分岐）")


def test_delete(client):
    conversation = create(client)
    send(client, conversation["id"], "消す")
    assert client.delete(f"/api/conversations/{conversation['id']}").status_code == 200
    assert client.get(f"/api/conversations/{conversation['id']}").status_code == 404


def test_unknown_conversation(client):
    assert client.get("/api/conversations/nope").status_code == 404
    assert client.patch("/api/conversations/nope", json={"title": "x"}).status_code == 404
