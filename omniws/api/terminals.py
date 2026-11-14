"""Human-operated PTYs. Explicit terminal permission is independent of agent tool permissions."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
from pathlib import Path

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from ..projects import ProjectError, is_project, project_path

router = APIRouter(prefix="/api/projects")
ws_router = APIRouter()


@router.get("/{name:path}/terminal")
def status(name: str, request: Request):
    return {"enabled": request.app.state.settings.omni_allow_terminal}


@ws_router.websocket("/api/projects/{name:path}/terminal/ws")
async def terminal(ws: WebSocket, name: str):
    state = ws.app.state
    # Cookies alone do not prevent cross-site WebSocket hijacking. Require an explicit approved origin.
    origin = ws.headers.get("origin", "")
    own_origin = f"{'https' if ws.url.scheme == 'wss' else 'http'}://{ws.headers.get('host', '')}"
    if origin not in [own_origin, *state.settings.public_origins()] or not state.auth.is_signed_in(ws):
        await ws.close(code=1008)
        return
    if not state.settings.omni_allow_terminal:
        await ws.close(code=1008)
        return
    try:
        cwd = project_path(state.settings.omni_projects_dir, name).resolve(strict=True)
        if not cwd.is_dir() or not is_project(state.settings.omni_projects_dir, name):
            raise ValueError()
    except (ProjectError, OSError, ValueError):
        await ws.close(code=1008)
        return
    if len(state.terminals) >= 8:
        await ws.close(code=1013)
        return
    reservation = object()
    state.terminals.add(reservation)
    master, slave = -1, -1
    process = None
    tasks = []
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    try:
        await ws.accept()
        master, slave = pty.openpty()
        # Do not pass application keys, tokens, BASH_ENV or PYTHONPATH into the interactive shell.
        env = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL") if key in os.environ}
        env.update(TERM="xterm-256color", COLORTERM="truecolor")
        process = subprocess.Popen([sys.executable, str(Path(__file__).parents[1] / "pty_child.py")],
                                   cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True)
        state.terminals.add(process.pid)
        state.terminals.discard(reservation)
        os.close(slave)
        slave = -1
        os.set_blocking(master, False)
        loop.add_reader(master, ready.set)

        async def output():
            while True:
                await ready.wait()
                ready.clear()
                try:
                    data = os.read(master, 16384)
                except BlockingIOError:
                    continue
                except OSError:
                    return
                if not data:
                    return
                await ws.send_bytes(data)

        async def input_():
            while True:
                raw = await ws.receive_text()
                if len(raw) > 65536:
                    return
                item = json.loads(raw)
                if not isinstance(item, dict):
                    return
                if item.get("type") == "resize":
                    cols, rows = item.get("cols"), item.get("rows")
                    if type(cols) is not int or type(rows) is not int or not (2 <= cols <= 500 and 1 <= rows <= 200):
                        return
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                elif item.get("type") == "input" and isinstance(item.get("data"), str):
                    data = item["data"].encode()
                    while data:
                        try:
                            count = os.write(master, data)
                            data = data[count:]
                        except BlockingIOError:
                            await asyncio.sleep(0.01)

        async def permission_watch():
            while state.settings.omni_allow_terminal and state.auth.is_signed_in(ws):
                await asyncio.sleep(0.5)

        tasks = [asyncio.create_task(fn()) for fn in (output, input_, permission_watch)]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        foreground = None
        if master >= 0:
            try:
                foreground = os.tcgetpgrp(master)
            except OSError:
                pass
            loop.remove_reader(master)
            os.close(master)
        if slave >= 0:
            os.close(slave)
        if process is not None:
            # Foreground jobs can have their own process group; terminate the entire PTY session.
            await asyncio.to_thread(stop_session, process, foreground)
            state.terminals.discard(process.pid)
        state.terminals.discard(reservation)
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


def stop_session(process: subprocess.Popen, foreground: int | None):
    pids = [process.pid]
    proc = Path("/proc")
    for entry in proc.iterdir() if proc.is_dir() else []:
        if entry.name.isdigit():
            try:
                if os.getsid(int(entry.name)) == process.pid:
                    pids.append(int(entry.name))
            except (ProcessLookupError, PermissionError):
                pass
    for group in {process.pid, foreground} - {None, 0}:
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for pid in set(pids):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    process.wait(timeout=5)
