import copy
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from omniws.agents.session_history import HistoryTurn, SessionInfo
from omniws.main import create_app
from omniws.models import AgentSession, ExternalSession, Message
from omniws.providers.registry import Registry
from .conftest import PASSWORD, make_settings
from .test_external_sessions import blocks


class Native:
    label = 'テストCLI'

    def __init__(self, agent_id, cwd):
        self.id, self.cwd = agent_id, cwd
        self.data = {'original': [HistoryTurn('first', blocks('質問1'), blocks('回答1'), source_ids=['user1', 'answer1']),
                                  HistoryTurn('second', blocks('質問2'), blocks('回答2'), source_ids=['user2', 'answer2'])]}
        self.names = {'original': '元の会話'}
        self.calls = []
        self.fail = False

    def installed(self):
        return True

    async def list_sessions(self, cwd):
        return [SessionInfo(id, self.names[id], str(self.cwd), 1000, 2000, 'model') for id in self.data]

    async def session_history(self, id, cwd):
        assert cwd.resolve() == self.cwd.resolve()
        return self.data[id]

    async def rename_session(self, id, cwd, title):
        if self.fail:
            raise RuntimeError('do not expose private stderr')
        self.names[id] = title

    async def fork_session(self, id, cwd, title, cutoff):
        self.calls.append(cutoff)
        data = self.data[id]
        index = next(i for i, turn in enumerate(data) if cutoff in [turn.id, *turn.source_ids])
        new_id = str(uuid.uuid4())
        self.data[new_id] = copy.deepcopy(data[:index + 1])
        # Claude forks remap UUIDs. Synchronization must still use the copied app messages.
        for turn in self.data[new_id]:
            turn.id = str(uuid.uuid4())
            turn.source_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.names[new_id] = title
        return new_id

    async def delete_session(self, id, cwd):
        if self.fail:
            raise RuntimeError('do not expose private stderr')
        del self.data[id]


@pytest.fixture(params=['codex', 'claude_code'])
def native(request, tmp_path):
    settings = make_settings(tmp_path)
    cwd = settings.omni_projects_dir / 'app'
    cwd.mkdir(parents=True)
    agent = Native(request.param, cwd)
    app = create_app(settings, web_dist=Path('/none'), registry=Registry(settings, {}), agents={agent.id: agent})
    with TestClient(app) as client:
        client.post('/api/auth/login', json={'password': PASSWORD})
        client.post('/api/sessions/discover')
        cid = client.get('/api/conversations').json()[0]['id']
        yield client, agent, cid


def test_native_rename_fork_boundaries_sync_and_deletion(native):
    client, agent, cid = native
    assert client.patch(f'/api/conversations/{cid}', json={'title': '変更した名前'}).status_code == 200
    assert agent.names['original'] == '変更した名前'
    original = client.get(f'/api/conversations/{cid}').json()
    response = client.post(f'/api/conversations/{cid}/fork', json={'message_id': original['messages'][1]['id']})
    assert response.status_code == 201, response.text
    new_id = response.json()['id']
    assert agent.calls == ['first' if agent.id == 'codex' else 'answer1']
    for _ in range(2):
        forked = client.get(f'/api/conversations/{new_id}').json()
        assert len(forked['messages']) == 2
        assert forked['messages'][1]['content'] == blocks('回答1')
    assert len(client.get(f'/api/conversations/{cid}').json()['messages']) == 4
    with client.app.state.sessions() as session:
        link = session.scalar(select(AgentSession).where(AgentSession.conversation_id == new_id))
        native_id = link.native_id
        assert native_id != 'original'
        assert link.synced_seq == 2
    agent.data[native_id].append(HistoryTurn('new-cli', blocks('続き'), blocks('追加の返答')))
    assert len(client.get(f'/api/conversations/{new_id}').json()['messages']) == 4
    assert client.delete(f'/api/conversations/{new_id}').status_code == 200
    assert native_id in agent.data
    client.post('/api/sessions/discover')
    assert len(client.get('/api/conversations').json()) == 1
    assert client.delete(f'/api/conversations/{cid}?native=true').status_code == 200
    assert 'original' not in agent.data


def test_native_errors_and_limits_preserve_app_data(native):
    client, agent, cid = native
    agent.fail = True
    response = client.patch(f'/api/conversations/{cid}', json={'title': '失敗する変更'})
    assert response.status_code == 502
    assert 'private stderr' not in response.text
    assert client.get(f'/api/conversations/{cid}').json()['title'] == '元の会話'
    assert client.delete(f'/api/conversations/{cid}?native=true').status_code == 502
    assert client.get(f'/api/conversations/{cid}').status_code == 200
    agent.fail = False
    client.app.state.settings.omni_allow_native_session_changes = False
    assert client.patch(f'/api/conversations/{cid}', json={'title': '拒否'}).status_code == 403
    assert client.post(f'/api/conversations/{cid}/fork', json={}).status_code == 403
    assert len(agent.data) == 1
    assert client.delete(f'/api/conversations/{cid}?native=true').status_code == 403
    assert client.delete(f'/api/conversations/{cid}').status_code == 200


def test_native_running_history_and_mismatched_fork_rejected(native):
    client, agent, cid = native
    agent.data['original'][-1].status = 'streaming'
    assert client.delete(f'/api/conversations/{cid}?native=true').status_code == 409
    assert client.post(f'/api/conversations/{cid}/fork', json={}).status_code == 409
    agent.data['original'][-1].status = 'complete'
    original_fork = agent.fork_session
    async def broken(*args):
        new_id = await original_fork(*args)
        agent.data[new_id].append(HistoryTurn('unexpected', blocks('extra'), blocks('extra')))
        return new_id
    agent.fork_session = broken
    assert client.post(f'/api/conversations/{cid}/fork', json={}).status_code == 409
    assert list(agent.data) == ['original']
    assert len(client.get('/api/conversations').json()) == 1


def test_app_owned_repeated_answers_fork_using_native_turn_id(native):
    client, agent, cid = native
    for turn in agent.data['original']:
        turn.assistant = blocks('同じ回答')
    detail = client.get(f'/api/conversations/{cid}').json()
    with client.app.state.sessions.begin() as session:
        external = session.scalar(select(ExternalSession).where(ExternalSession.conversation_id == cid))
        session.delete(external)
        for index, turn in enumerate(agent.data['original']):
            message = session.get(Message, detail['messages'][index * 2 + 1]['id'])
            message.native_origin = {'agent': 'codex' if agent.id == 'codex' else 'claude', 'session': 'original',
                                     'turn': turn.id if agent.id == 'codex' else turn.source_ids[-1]}
    response = client.post(f'/api/conversations/{cid}/fork', json={'message_id': detail['messages'][1]['id']})
    assert response.status_code == 201, response.text
    new_id = response.json()['id']
    assert len(client.get(f'/api/conversations/{new_id}').json()['messages']) == 2
    # UUID remapping after a fork must also support another fork from the copy.
    assert client.post(f'/api/conversations/{new_id}/fork', json={}).status_code == 201


def test_partial_native_delete_can_be_retried(native):
    client, agent, cid = native
    other_id = 'claude_code' if agent.id == 'codex' else 'codex'
    other = Native(other_id, agent.cwd)
    other.fail = True
    client.app.state.agents[other_id] = other
    with client.app.state.sessions.begin() as session:
        session.add(AgentSession(conversation_id=cid, agent=other_id, native_id='original'))
    assert client.delete(f'/api/conversations/{cid}?native=true').status_code == 502
    assert 'original' not in agent.data
    assert client.get(f'/api/conversations/{cid}').status_code == 200
    other.fail = False
    assert client.delete(f'/api/conversations/{cid}?native=true').status_code == 200
    assert 'original' not in other.data


def test_codex_native_protocol(tmp_path):
    import asyncio
    from omniws.agents.codex import CodexAgent
    agent = CodexAgent(make_settings(tmp_path))
    calls = []
    class Connection:
        async def call(self, method, params, **kwargs):
            calls.append((method, params))
            return {'thread': {'id': 'forked'}}
    async def connect():
        return Connection()
    agent.connect = connect
    async def run():
        assert await agent.fork_session('original', tmp_path, '分岐', 'turn-first') == 'forked'
        await agent.delete_session('forked', tmp_path)
    asyncio.run(run())
    assert calls[0][0] == 'thread/fork'
    assert calls[0][1]['lastTurnId'] == 'turn-first'
    assert calls[0][1]['approvalPolicy'] == 'untrusted'
    assert calls[1] == ('thread/name/set', {'threadId': 'forked', 'name': '分岐'})
    assert calls[2] == ('thread/delete', {'threadId': 'forked'})


def test_real_claude_sdk_mutations_use_isolated_transcript(tmp_path, monkeypatch):
    import asyncio
    import json
    import re
    import claude_agent_sdk as sdk
    from omniws.agents.claude import ClaudeCodeAgent
    config = tmp_path / 'claude-config'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
    project = tmp_path / 'project'
    project.mkdir()
    storage = config / 'projects' / re.sub(r'[^a-zA-Z0-9]', '-', str(project.resolve()))
    storage.mkdir(parents=True)
    native_id = str(uuid.uuid4())
    ids = [str(uuid.uuid4()) for _ in range(4)]
    rows = []
    for i, (role, text) in enumerate([('user', '質問1'), ('assistant', '回答1'), ('user', '質問2'), ('assistant', '回答2')]):
        rows.append({'type': role, 'uuid': ids[i], 'parentUuid': ids[i-1] if i else None,
                     'sessionId': native_id, 'cwd': str(project), 'isSidechain': False,
                     'timestamp': '2026-11-14T00:00:00Z',
                     'message': {'role': role, 'model': 'test', 'content': blocks(text)}})
    source = storage / f'{native_id}.jsonl'
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    agent = ClaudeCodeAgent(make_settings(tmp_path, omni_allow_claude_subscription=True), 'subscription')
    async def run():
        await agent.rename_session(native_id, project, '実SDKで改名')
        assert sdk.list_sessions(directory=str(project))[0].custom_title == '実SDKで改名'
        forked = await agent.fork_session(native_id, project, '実SDKで分岐', ids[1])
        turns = await agent.session_history(forked, project)
        assert len(turns) == 1 and turns[0].assistant == blocks('回答1')
        assert turns[0].id != ids[0]
        await agent.delete_session(forked, project)
        assert source.exists() and not (storage / f'{forked}.jsonl').exists()
    asyncio.run(run())
