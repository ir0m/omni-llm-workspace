from omniws import notes


def make_project(client):
    assert client.post('/api/projects', json={'name': 'vault', 'git_init': False}).status_code == 201
    return client.app.state.settings.omni_projects_dir / 'vault'


def test_index_search_links_aliases_and_code_exclusions(client, tmp_path):
    root = make_project(client)
    (root / 'notes').mkdir()
    (root / 'notes/Target.md').write_text('---\naliases: [別名]\ntags: [研究]\n---\n# Target\n本文 telescope\n')
    (root / 'Home.md').write_text('# Home\n[[別名#Target|説明]] ![[notes/Target]] #実験/run\n')
    (root / 'Code.md').write_text('`[[Target]]`\n\n```md\n[[Target]]\n```\n')
    (root / 'Relative.md').write_text('[note](notes/Target.md#Target)')
    (root / '.env.md').write_text('telescope secret')
    (root / '.git').mkdir()
    (root / '.git/hidden.md').write_text('telescope secret')
    (root / 'link.md').symlink_to(root / 'notes/Target.md')
    data = client.get('/api/projects/vault/notes', params={'path': 'notes/Target.md', 'q': 'telescope'}).json()
    assert {x['path'] for x in data['backlinks']} == {'Home.md', 'Relative.md'}
    assert [x['path'] for x in data['results']] == ['notes/Target.md']
    assert data['results'][0]['line'] == 6
    assert not {'.env.md', '.git/hidden.md', 'link.md'} & set(data['paths'])
    target = next(x for x in data['notes'] if x['path'] == 'notes/Target.md')
    assert target['aliases'] == ['別名'] and target['tags'] == ['研究']
    assert 'text' not in target
    assert not data['truncated']


def test_resolution_is_deterministic_and_ambiguous_names_are_not_guessed():
    paths = {'a/Note.md', 'b/Note.md', 'a/Start.md', 'other.md'}
    assert notes.resolve('a/Start.md', 'Note', paths, {}) == 'a/Note.md'
    assert notes.resolve('other.md', 'Note', paths, {}) is None
    assert notes.resolve('other.md', 'a/Note#Heading', paths, {}) == 'a/Note.md'
    assert notes.resolve('a/Start.md', '../other.md', paths, {}, False) == 'other.md'
    assert notes.resolve('a/Start.md', '../../other.md', paths, {}, False) is None
    assert notes.resolve('other.md', 'https://example.com', paths, {}) is None


def test_index_is_bounded_and_reflects_external_edits(client, monkeypatch):
    root = make_project(client)
    (root / 'one.md').write_text('before')
    assert client.get('/api/projects/vault/notes', params={'q': 'before'}).json()['results']
    (root / 'one.md').write_text('after')
    assert not client.get('/api/projects/vault/notes', params={'q': 'before'}).json()['results']
    (root / 'two.md').write_text('two')
    monkeypatch.setattr(notes, 'MAX_NOTES', 1)
    result = client.get('/api/projects/vault/notes').json()
    assert len(result['notes']) == 1 and result['truncated']


def test_invalid_frontmatter_does_not_break_other_notes(client):
    root = make_project(client)
    (root / 'bad.md').write_text('---\naliases: [\n---\n# Body\n')
    assert client.get('/api/projects/vault/notes').json()['notes'][0]['title'] == 'Body'
    client.post('/api/auth/logout')
    assert client.get('/api/projects/vault/notes').status_code == 401
