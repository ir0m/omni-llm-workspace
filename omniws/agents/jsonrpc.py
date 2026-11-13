"""A child process that speaks JSON-RPC 2.0, one JSON per line on stdin/stdout (ACP agents, Codex app-server).

Messages from the process are routed by a key taken from their params (an ACP sessionId, a Codex threadId):
notifications go to that key's queue, requests to that key's handler. Notifications without a listener go to
`on_notification`.
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


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class MethodNotFound(RpcError):
    def __init__(self, method: str) -> None:
        super().__init__(-32601, f"method not found: {method}")


Handler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class JsonRpcProcess:
    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path, route: Callable[[dict[str, Any]], str | None],
                 on_notification: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.argv, self.env, self.cwd, self.route = argv, env, cwd, route
        self.on_notification = on_notification
        self.process: asyncio.subprocess.Process | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.listeners: dict[str, asyncio.Queue] = {}
        self.handlers: dict[str, Handler] = {}
        self.reader: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def spawn(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=LINE_LIMIT)
        self.reader = asyncio.create_task(self._read())

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
            raise RpcError(-32000, "the agent is not running")
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
        with contextlib.suppress(RpcError):
            self._write({"method": method, "params": params})

    def listen(self, key: str, handler: Handler) -> asyncio.Queue:
        """Notifications for `key` arrive on the queue as {"method", "params"}; None means the process exited."""
        queue: asyncio.Queue = asyncio.Queue()
        self.listeners[key] = queue
        self.handlers[key] = handler
        return queue

    def unlisten(self, key: str) -> None:
        self.listeners.pop(key, None)
        self.handlers.pop(key, None)

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
            error = RpcError(-32000, "the agent exited")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            for queue in self.listeners.values():
                queue.put_nowait(None)

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:  # a response to one of our calls
            future = self.pending.get(message.get("id"))
            if future and not future.done():
                if message.get("error"):
                    error = message["error"]
                    future.set_exception(RpcError(error.get("code", -32000), error.get("message", ""), error.get("data")))
                else:
                    future.set_result(message.get("result"))
            return
        params = message.get("params") or {}
        if "id" not in message:  # a notification
            queue = self.listeners.get(self.route(params) or "")
            if queue is not None:
                queue.put_nowait({"method": method, "params": params})
            elif self.on_notification:
                self.on_notification(method, params)
            return
        asyncio.create_task(self._serve(message["id"], method, params))

    async def _serve(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        handler = self.handlers.get(self.route(params) or "")
        try:
            if handler is None:
                raise MethodNotFound(method)
            result = await handler(method, params)
            self._write({"id": request_id, "result": result})
        except RpcError as exc:
            self._write({"id": request_id, "error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:  # never leave the agent waiting
            log.exception("request %s from the agent failed", method)
            with contextlib.suppress(RpcError):
                self._write({"id": request_id, "error": {"code": -32603, "message": type(exc).__name__}})
