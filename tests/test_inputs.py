import asyncio
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniws.agents.base import PermissionRequest
from omniws.agents.handover import build_prompt, turn_items
from omniws.agents.session_history import codex_history
from omniws.db import mark_interrupted
from omniws.events import message_chunk
from omniws.history import chat_history
from omniws.main import create_app
from omniws.models import Conversation, Message
from omniws.providers.base import ProviderError
from omniws.providers.registry import Registry

from .conftest import PASSWORD, make_settings, read_events


class InputAgent:
    id, label = "codex", "Codex"

    def __init__(self):
        self.accept = True
        self.fail = False
        self.transport_error = False
        self.calls, self.received = [], []

    def installed(self):
        return True

    async def run(self, turn):
        self.calls.append(turn)
        turn.on_session("native-test")
        if turn.on_turn_id:
            turn.on_turn_id(f"turn-{len(self.calls)}")

        async def steer(text):
            self.received.append(text)
            if self.transport_error:
                raise OSError("connection lost")
            return self.accept

        turn.on_input_ready(steer)
        try:
            yield message_chunk("作業しています")
            await turn.ask_permission(PermissionRequest("command", "execute", "確認", command="echo check"))
            if self.fail:
                raise ProviderError("unknown", "テスト用エラー")
            yield message_chunk("完了")
        finally:
            turn.on_input_ready(None)


@pytest.fixture
def input_client(tmp_path):
    settings = make_settings(tmp_path)
    agent = InputAgent()
    app = create_app(settings, web_dist=Path('/nonexistent'), registry=Registry(settings, {}), agents={"codex": agent})
    with TestClient(app) as client:
        client.post('/api/auth/login', json={"password": PASSWORD})
        client.post('/api/projects', json={"name": "app", "git_init": False})
        yield client, agent


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(.01)
    raise AssertionError("timed out")


def start(client):
    cid = client.post('/api/conversations', json={"mode": "code", "project": "app"}).json()['id']
    response = client.post(f'/api/conversations/{cid}/messages', json={
        "text": "初めの指示", "provider": "codex", "model": "m1", "effort": "low"})
    assert response.status_code == 202, response.text
    run = client.app.state.runs.runs[response.json()['run_id']]
    wait_for(lambda: run.pending)
    return cid, run


def submit(client, run, text, input_id=None):
    return client.post(f'/api/runs/{run.id}/input', json={"id": input_id or str(uuid.uuid4()), "text": text})


def allow(client, run):
    request_id = next(iter(wait_for(lambda: run.pending)))
    assert client.post(f'/api/runs/{run.id}/permissions/{request_id}', json={"outcome": "allow"}).status_code == 200


def detail(client, cid):
    return client.get(f'/api/conversations/{cid}').json()


def test_steer_is_saved_in_order_and_request_retries_are_idempotent(input_client):
    client, agent = input_client
    cid, run = start(client)
    input_id = str(uuid.uuid4())
    first = submit(client, run, 'テストも追加して', input_id)
    assert first.json()['status'] == 'delivered'
    assert submit(client, run, 'テストも追加して', input_id).json() == first.json()
    assert agent.received == ['テストも追加して']
    assert detail(client, cid)['pending_inputs'] == []
    allow(client, run)
    events = read_events(client, run.id)
    assert events[-1][2]['status'] == 'complete'
    content = detail(client, cid)['messages'][-1]['content']
    assert content == [
        {'type': 'text', 'text': '作業しています'},
        {'type': 'user_input', 'text': 'テストも追加して'},
        {'type': 'text', 'text': '完了'},
    ]
    assert submit(client, run, '遅い指示').status_code == 409


def test_queued_input_runs_without_browser_with_same_target(input_client):
    client, agent = input_client
    agent.accept = False
    cid, run = start(client)
    for text in ['続き1', '続き2']:
        assert submit(client, run, text).json()['status'] == 'queued'
    assert len(detail(client, cid)['pending_inputs']) == 2
    # No SSE connection: the server starts the next turn on its own.
    allow(client, run)
    wait_for(lambda: len(agent.calls) == 2)
    next_run = client.app.state.runs.active_for(cid)
    assert next_run and next_run.id != run.id
    assert (agent.calls[-1].text, agent.calls[-1].model, agent.calls[-1].effort) == ('続き1\n\n続き2', 'm1', 'low')
    assert detail(client, cid)['pending_inputs'] == []
    allow(client, next_run)
    assert read_events(client, next_run.id)[-1][2]['status'] == 'complete'


def test_later_input_cannot_overtake_an_earlier_queued_instruction(input_client):
    client, agent = input_client
    agent.accept = False
    cid, run = start(client)
    assert submit(client, run, '先の指示').json()['status'] == 'queued'
    agent.accept = True
    assert submit(client, run, '後の指示').json()['status'] == 'queued'
    assert agent.received == ['先の指示']
    client.post(f'/api/runs/{run.id}/cancel')
    read_events(client, run.id)


def test_chat_provider_without_steering_uses_the_server_queue(client, providers):
    providers['fake'].delay = .15
    cid = client.post('/api/conversations', json={}).json()['id']
    rid = client.post(f'/api/conversations/{cid}/messages', json={
        'text': '最初', 'provider': 'fake', 'model': 'm1'}).json()['run_id']
    run = client.app.state.runs.runs[rid]
    assert submit(client, run, 'APIにも追加').json()['status'] == 'queued'
    read_events(client, rid)
    next_run = client.app.state.runs.active_for(cid)
    assert next_run
    read_events(client, next_run.id)
    assert providers['fake'].requests[-1].messages[-1] == {'role': 'user', 'content': 'APIにも追加'}


@pytest.mark.parametrize('ending', ['cancelled', 'error', 'changed_project'])
def test_unsent_inputs_are_returned_instead_of_sent(input_client, ending):
    client, agent = input_client
    agent.accept = False
    cid, run = start(client)
    submit(client, run, '戻す指示')
    if ending == 'cancelled':
        client.post(f'/api/runs/{run.id}/cancel')
    else:
        if ending == 'error':
            agent.fail = True
        else:
            client.patch(f'/api/conversations/{cid}', json={"mode": "chat"})
        allow(client, run)
    read_events(client, run.id)
    assert len(agent.calls) == 1
    pending = detail(client, cid)['pending_inputs']
    assert len(pending) == 1 and pending[0]['status'] == 'returned' and pending[0]['text'] == '戻す指示'
    client.delete(f'/api/conversations/{cid}/inputs/{pending[0]["id"]}')
    assert detail(client, cid)['pending_inputs'] == []


def test_removing_queue_entry_prevents_delivery_and_other_conversations_are_isolated(input_client):
    client, agent = input_client
    agent.accept = False
    cid, run = start(client)
    other = client.post('/api/conversations', json={}).json()['id']
    queued = submit(client, run, '取り消す').json()
    client.delete(f'/api/conversations/{other}/inputs/{queued["id"]}')
    assert len(detail(client, cid)['pending_inputs']) == 1
    client.delete(f'/api/conversations/{cid}/inputs/{queued["id"]}')
    allow(client, run)
    read_events(client, run.id)
    assert len(agent.calls) == 1
    assert detail(client, cid)['pending_inputs'] == []


def test_uncertain_delivery_is_returned_and_never_automatically_retried(input_client):
    client, agent = input_client
    agent.transport_error = True
    cid, run = start(client)
    assert submit(client, run, '送信結果不明').json()['status'] == 'returned'
    allow(client, run)
    read_events(client, run.id)
    assert len(agent.calls) == 1
    assert detail(client, cid)['pending_inputs'][0]['status'] == 'returned'


def test_restart_preserves_pending_text_as_drafts(input_client):
    client, agent = input_client
    cid, run = start(client)
    agent.accept = False
    submit(client, run, '再起動後も残す')
    mark_interrupted(client.app.state.sessions)
    assert detail(client, cid)['pending_inputs'][0]['status'] == 'returned'
    client.post(f'/api/runs/{run.id}/cancel')
    read_events(client, run.id)


def test_finishing_during_delivery_keeps_the_accepted_instruction(input_client):
    client, agent = input_client
    cid, run = start(client)

    async def finishing_steer(text):
        from omniws.agents.base import Decision
        client.app.state.runs.resolve(run.id, next(iter(run.pending)), Decision('allow'))
        await asyncio.sleep(.05)
        return True

    run.steer = finishing_steer
    assert submit(client, run, '最後の指示').json()['status'] == 'delivered'
    assert read_events(client, run.id)[-1][2]['status'] == 'complete'
    assert {'type': 'user_input', 'text': '最後の指示'} in detail(client, cid)['messages'][-1]['content']


def test_instruction_roles_are_preserved_for_chat_and_agent_handover(tmp_path):
    history = codex_history({'turns': [{'id': 't', 'items': [
        {'type': 'userMessage', 'content': [{'type': 'text', 'text': '質問'}]},
        {'type': 'agentMessage', 'text': '調べます'},
        {'type': 'userMessage', 'content': [{'type': 'text', 'text': '追加指示'}]},
        {'type': 'agentMessage', 'text': '対応します'},
    ]}]}, tmp_path)
    messages = [Message(role='user', content=history[0].user, status='complete'),
                Message(role='assistant', content=history[0].assistant, status='complete')]
    assert [item['role'] for item in chat_history(messages)] == ['user', 'assistant', 'user', 'assistant']
    assert [item['role'] for item in turn_items(messages)] == ['user', 'assistant', 'user', 'assistant']
    assert '[user]\n追加指示' in build_prompt(messages, '次へ')
