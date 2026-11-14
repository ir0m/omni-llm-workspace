import subprocess
from pathlib import Path

import pytest


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def repo(client):
    client.post('/api/projects', json={'name': 'app', 'git_init': True, 'readme': True, 'gitignore': True})
    path = client.app.state.settings.omni_projects_dir / 'app'
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'Initial')
    return path


def create(client):
    response = client.post('/api/projects/app/worktrees', json={'title': '比較作業'})
    assert response.status_code == 201, response.text
    return response.json()


def test_worktree_isolated_base_diff_merge_and_cleanup(client, repo):
    (repo / 'README.md').write_text('uncommitted original')
    (repo / '.env').write_text('private')
    item = create(client)
    child = repo.parent / item['name']
    assert (child / 'README.md').read_text() == '# app\n'
    assert not (child / '.env').exists()
    assert git(child, 'branch', '--show-current') == item['branch']
    assert client.get(f"/api/conversations/{item['conversation_id']}").json()['project'] == item['name']
    (child / 'README.md').write_text('new worktree content\n')
    (child / 'fresh.txt').write_text('new untracked\n')
    result = client.get(f"/api/projects/{item['name']}/worktree-result").json()
    assert result['paths'] == ['README.md']
    assert result['changes']['untracked'][0]['path'] == 'fresh.txt'
    diff = client.get(f"/api/projects/{item['name']}/worktree-diff", params={'path': 'README.md'}).json()
    assert '+new worktree content' in diff['patch']
    assert client.post(f"/api/projects/{item['name']}/worktree-merge").status_code == 409
    assert client.delete(f"/api/projects/{item['name']}/worktree").status_code == 409
    (repo / 'README.md').write_text('# app\n')
    git(child, 'add', '.')
    git(child, 'commit', '-m', 'Worktree result')
    assert client.post(f"/api/projects/{item['name']}/worktree-merge").status_code == 200
    assert (repo / 'fresh.txt').read_text() == 'new untracked\n'
    assert client.delete(f"/api/projects/{item['name']}/worktree").status_code == 200
    assert not child.exists()
    assert git(repo, 'rev-parse', item['branch'])
    assert client.get(f"/api/conversations/{item['conversation_id']}").status_code == 200


def test_worktree_limits_private_paths_and_divergence(client, repo):
    state = client.app.state
    state.settings.omni_shell_deny = 'git worktree'
    assert client.post('/api/projects/app/worktrees', json={'title': 'denied'}).status_code == 403
    state.settings.omni_shell_deny = ''
    state.settings.omni_outside_write = 'deny'
    assert client.post('/api/projects/app/worktrees', json={'title': 'denied'}).status_code == 403
    state.settings.omni_outside_write = 'mode'
    assert client.post('/api/projects/app/worktrees', json={'title': 'bad', 'base': '--help'}).status_code == 400
    item = create(client)
    child = repo.parent / item['name']
    (child / '.env').write_text('private')
    (child / 'test.txt').write_text('child')
    git(child, 'add', 'test.txt'); git(child, 'commit', '-m', 'Child')
    (repo / 'other.txt').write_text('source')
    git(repo, 'add', 'other.txt'); git(repo, 'commit', '-m', 'Source')
    assert client.post(f"/api/projects/{item['name']}/worktree-merge").status_code == 409
    assert client.get(f"/api/projects/{item['name']}/worktree-diff", params={'path': '.env'}).status_code == 403
    assert client.delete(f"/api/projects/{item['name']}/worktree").status_code == 409
    assert not (repo / 'test.txt').exists()


def test_worktree_detects_replaced_folder(client, repo):
    item = create(client)
    child = repo.parent / item['name']
    child.rename(child.with_name(child.name + '-moved'))
    child.symlink_to(repo, target_is_directory=True)
    assert client.delete(f"/api/projects/{item['name']}/worktree").status_code == 409
    assert (repo / 'README.md').exists()


def test_worktree_checkout_does_not_execute_filters_or_hooks(client, repo):
    marker = repo.parent / 'must-not-exist'
    (repo / '.gitattributes').write_text('README.md filter=unsafe\n')
    git(repo, 'add', '.gitattributes'); git(repo, 'commit', '-m', 'Attributes')
    git(repo, 'config', 'filter.unsafe.smudge', f'touch {marker}')
    hook = repo / '.git' / 'hooks' / 'post-checkout'
    hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
    hook.chmod(0o700)
    item = create(client)
    assert not marker.exists()
    assert (repo.parent / item['name'] / 'README.md').read_text() == '# app\n'


def test_branch_list_and_selected_starting_commit(client, repo):
    git(repo, 'checkout', '-b', 'feature/example')
    (repo / 'feature.txt').write_text('selected branch\n')
    git(repo, 'add', 'feature.txt'); git(repo, 'commit', '-m', 'Feature')
    tip = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', 'main')
    git(repo, 'update-ref', 'refs/remotes/origin/feature/example', tip)
    git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/feature/example')
    response = client.get('/api/projects/app/branches')
    assert response.status_code == 200
    data = response.json()
    assert data['current_ref'] == 'refs/heads/main'
    assert data['head'] == git(repo, 'rev-parse', 'HEAD')
    assert {row['ref'] for row in data['branches']} == {
        'refs/heads/main', 'refs/heads/feature/example', 'refs/remotes/origin/feature/example'}
    assert next(row for row in data['branches'] if row['remote'])['name'] == 'origin/feature/example'
    for ref in ('refs/heads/feature/example', 'refs/remotes/origin/feature/example'):
        result = client.post('/api/projects/app/worktrees', json={'title': 'Branch choice', 'base': ref})
        assert result.status_code == 201, result.text
        child = repo.parent / result.json()['name']
        assert result.json()['base'] == git(child, 'rev-parse', 'HEAD') == tip
        assert (child / 'feature.txt').read_text() == 'selected branch\n'
    assert not (repo / 'feature.txt').exists()


def test_branch_list_detached_empty_and_non_git(client, repo):
    head = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', '--detach', head)
    data = client.get('/api/projects/app/branches').json()
    assert data['current_ref'] is None and data['head'] == head
    client.post('/api/projects', json={'name': 'empty', 'git_init': True})
    assert client.get('/api/projects/empty/branches').json() == {'branches': [], 'current_ref': None, 'head': None}
    client.post('/api/projects', json={'name': 'plain', 'git_init': False})
    assert client.get('/api/projects/plain/branches').status_code == 400
