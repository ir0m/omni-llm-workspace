"""Child-process entry point: install kernel restrictions, then exec the compiler.

Run as a separate Python process, never through preexec_fn in the threaded server.
Failure to establish restrictions must never fall back to an unrestricted compiler.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import resource
import sys


def deny_sockets() -> None:
    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError('seccomp_init failed')
    try:
        for name in (b'socket', b'socketpair'):
            syscall = lib.seccomp_syscall_resolve_name(name)
            if syscall < 0 or lib.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, syscall, 0) != 0:
                raise RuntimeError('seccomp rule failed')
        if lib.seccomp_load(ctx) != 0:
            raise RuntimeError('seccomp_load failed')
    finally:
        lib.seccomp_release(ctx)


def main() -> None:
    from py_landlock import AccessFs, Landlock

    policy = json.loads(sys.argv[1])
    command = sys.argv[2:]
    rules = Landlock()
    if rules.abi_version < 3:
        raise RuntimeError('Landlock ABI 3 or newer is required')
    rules.allow_all_network()  # offline mode is enforced for all sockets by seccomp below
    for value in policy['read']:
        path = Path(value).resolve()
        if path.exists():
            rules.add_path_rule(path, access=AccessFs.READ_FILE | (AccessFs.READ_DIR if path.is_dir() else AccessFs(0)))
    for value in policy['write']:
        rules.allow_read_write(value)
    for value in policy['execute']:
        if Path(value).exists():
            rules.allow_execute(value)
    for name in ('/dev/null', '/dev/urandom', '/dev/random'):
        if Path(name).exists():
            rules.add_path_rule(name, access=AccessFs.READ_FILE | AccessFs.WRITE_FILE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (policy['file_limit'], policy['file_limit']))
    resource.setrlimit(resource.RLIMIT_CPU, (policy['timeout'], policy['timeout'] + 1))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    rules.apply()
    if not policy['network']:
        deny_sockets()
    os.execv(command[0], command)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'コンパイルの隔離処理を開始できません: {exc}', file=sys.stderr)
        sys.exit(125)
