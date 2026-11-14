"""Exercise the actual kernel boundary, not just the arguments sent to a compiler."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omniws import compile as compiler


def test_worker_blocks_private_reads_writes_and_network(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Linux-only compile worker')
    from py_landlock import get_abi_version
    try:
        if get_abi_version() < 3:
            pytest.skip('Landlock ABI 3 is required')
    except Exception:
        pytest.skip('Landlock is unavailable')
    secret = tmp_path / 'secret.txt'
    secret.write_text('private')
    work = tmp_path / 'work'
    work.mkdir()
    policy = {'read': ['/usr', '/lib', '/lib64', '/bin', '/etc/ld.so.cache'], 'write': [str(work)],
              'execute': ['/usr', '/bin'], 'network': False, 'file_limit': 1024 * 1024, 'timeout': 10}
    script = '''import pathlib, socket, sys
secret, work = map(pathlib.Path, sys.argv[1:])
for operation in (secret.read_text, lambda: secret.write_text('overwritten'), lambda: socket.socket(), lambda: socket.socket(socket.AF_UNIX)):
    try: operation()
    except PermissionError: pass
    else: raise AssertionError('restriction did not apply')
(work / 'output').write_text('ok')
'''
    result = subprocess.run([sys.executable, '-I', str(Path(compiler.__file__).with_name('compile_worker.py')),
                             json.dumps(policy), '/usr/bin/python3', '-c', script, str(secret), str(work)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert secret.read_text() == 'private' and (work / 'output').read_text() == 'ok'


def test_snapshot_skips_private_files_and_symlinks(client, tmp_path):
    from omniws.files import ProjectFiles
    client.post('/api/projects', json={'name': 'app', 'git_init': False})
    settings = client.app.state.settings
    root = settings.omni_projects_dir / 'app'
    (root / 'main.typ').write_text('hello')
    (root / '.env').write_text('secret')
    (root / 'linked.typ').symlink_to(root / '.env')
    (root / 'assets').mkdir()
    (root / 'assets/a.png').write_bytes(b'\x89PNG\0')
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    compiler.snapshot_project(ProjectFiles(settings, 'app'), snapshot)
    assert (snapshot / 'main.typ').read_text() == 'hello'
    assert (snapshot / 'assets/a.png').read_bytes() == b'\x89PNG\0'
    assert not (snapshot / '.env').exists() and not (snapshot / 'linked.typ').exists()
