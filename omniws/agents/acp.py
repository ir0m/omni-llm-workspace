"""A small ACP (Agent Client Protocol) client: JSON-RPC 2.0 over the agent's stdin/stdout, one JSON per line.

The agent sends `session/update` notifications while a prompt runs and may call back into the client:
`session/request_permission`, `fs/read_text_file`, `fs/write_text_file`. Each session registers a handler
for those; file access is limited to the session's folder.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
LINE_LIMIT = 64 * 1024 * 1024
PROTOCOL_VERSION = 1


class AcpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class MethodNotFound(AcpError):
    def __init__(self, method: str) -> None:
        super().__init__(-32601, f"method not found: {method}")


Handler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class AcpConnection:
    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path) -> None:
        self.argv, self.env, self.cwd = argv, env, cwd
        self.process: asyncio.subprocess.Process | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.listeners: dict[str, asyncio.Queue] = {}
        self.handlers: dict[str, Handler] = {}
        self.reader: asyncio.Task | None = None
        self.info: dict[str, Any] = {}

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self, client_name: str = "omni-llm-workspace", version: str = "0") -> dict[str, Any]:
        self.process = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=LINE_LIMIT)
        self.reader = asyncio.create_task(self._read())
        self.info = await self.call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": False},
            "clientInfo": {"name": client_name, "version": version}}, timeout=60)
        return self.info

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self.process.wait(), 5)
            if self.process.returncode is None:
                self.process.kill()
        if self.reader:
            self.reader.cancel()

    def _write(self, message: dict[str, Any]) -> None:
        if not self.alive or self.process.stdin is None:
            raise AcpError(-32000, "agent is not running")
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n").encode())

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self._write({"id": request_id, "method": method, "params": params})
        await self.process.stdin.drain()
        try:
            return await (asyncio.wait_for(future, timeout) if timeout else future)
        finally:
            self.pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        with contextlib.suppress(AcpError):
            self._write({"method": method, "params": params})

    def listen(self, session_id: str, handler: Handler) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.listeners[session_id] = queue
        self.handlers[session_id] = handler
        return queue

    def unlisten(self, session_id: str) -> None:
        self.listeners.pop(session_id, None)
        self.handlers.pop(session_id, None)

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        try:
            async for line in self.process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self._dispatch(message)
        finally:
            error = AcpError(-32000, "agent exited")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            for queue in self.listeners.values():
                queue.put_nowait(None)  # wakes a waiting turn

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:  # a response to one of our calls
            future = self.pending.get(message.get("id"))
            if future and not future.done():
                if "error" in message:
                    error = message["error"] or {}
                    future.set_exception(AcpError(error.get("code", -32000), error.get("message", ""), error.get("data")))
                else:
                    future.set_result(message.get("result"))
            return
        params = message.get("params") or {}
        if "id" not in message:  # a notification
            if method == "session/update":
                queue = self.listeners.get(params.get("sessionId"))
                if queue is not None:
                    queue.put_nowait(params.get("update") or {})
            return
        asyncio.create_task(self._serve(message["id"], method, params))

    async def _serve(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        handler = self.handlers.get(params.get("sessionId"))
        try:
            if handler is None:
                raise MethodNotFound(method)
            result = await handler(method, params)
            self._write({"id": request_id, "result": result})
        except AcpError as exc:
            self._write({"id": request_id, "error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:  # never leave the agent waiting
            log.exception("ACP request %s failed", method)
            self._write({"id": request_id, "error": {"code": -32603, "message": type(exc).__name__}})
