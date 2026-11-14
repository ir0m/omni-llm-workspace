"""MCP tools for Chat with API and local models.

Servers are set up in the settings screen and kept in app_settings ("mcp_servers"). Tokens in headers and
environment variables are encrypted with a key derived from the app's secret; the screen only learns whether
each one is set. OMNI_ALLOW_MCP (and OMNI_ALLOW_MCP_STDIO for servers that run a program here) is the limit.

During a turn every enabled server the conversation hasn't turned off is connected in a task of its own
(the SDK's transports hold task groups that must open and close in one task), the model calls tools, and
each call is approved like an agent's request.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2 as httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .agents.base import PermissionRequest
from .config import Settings
from .events import Update

log = logging.getLogger(__name__)
KEY = "mcp_servers"
CONNECT_SECONDS = 30
CALL_SECONDS = 120
MAX_ROUNDS = 20
RESULT_LIMIT = 30_000   # characters of a tool result the model sees
SHOWN_LIMIT = 4_000     # characters shown in the tool card
NAME = re.compile(r"[^a-zA-Z0-9_-]")
SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


class McpError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- Secrets ------------------------------------------------------------------------------------------------

def box(secret: str) -> Fernet:
    digest = hashlib.sha256(f"{secret}:mcp-secrets".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def seal(secret: str, value: str) -> str:
    return box(secret).encrypt(value.encode()).decode()


def unseal(secret: str, value: str) -> str | None:
    try:
        return box(secret).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None  # the secret changed: the value has to be entered again


# --- Settings -----------------------------------------------------------------------------------------------

def public(server: dict[str, Any]) -> dict[str, Any]:
    """What the screen sees: names of secrets, never their values."""
    return {"id": server["id"], "name": server["name"], "transport": server["transport"],
            "command": server.get("command", ""), "args": server.get("args", []), "url": server.get("url", ""),
            "env_keys": sorted(server.get("env", {})), "header_keys": sorted(server.get("headers", {})),
            "enabled": server.get("enabled", True), "always_allow": server.get("always_allow", []),
            "tools": server.get("tools", [])}


def check_url(url: str) -> str:
    parts = urlsplit(url.strip())
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme not in ("https", "http") or not parts.hostname or (parts.scheme == "http" and not local):
        raise McpError("URL は https://…（このマシン上なら http://localhost も可）にしてください")
    return url.strip()


def updated(server: dict[str, Any] | None, body: dict[str, Any], secret: str) -> dict[str, Any]:
    """A new or changed server from the settings screen. Secrets: a string sets, null removes, absent keeps."""
    server = dict(server or {"id": uuid.uuid4().hex[:8], "enabled": True, "always_allow": [], "tools": [],
                             "env": {}, "headers": {}})
    name = str(body.get("name", server.get("name", ""))).strip()
    if not name or len(name) > 40:
        raise McpError("名前は1〜40文字にしてください")
    transport = body.get("transport", server.get("transport"))
    if transport not in ("stdio", "http"):
        raise McpError("種類は stdio か http です")
    server.update(name=name, transport=transport)
    if transport == "stdio":
        command = str(body.get("command", server.get("command", ""))).strip()
        args = body.get("args", server.get("args", []))
        if not command or not isinstance(args, list) or not all(isinstance(arg, str) for arg in args) or len(args) > 50:
            raise McpError("起動するコマンドを入力してください")
        server.update(command=command, args=args, url="")
    else:
        server.update(url=check_url(str(body.get("url", server.get("url", "")))), command="", args=[])
    for field_name in ("env", "headers"):
        values = dict(server.get(field_name, {}))
        for key, value in (body.get(field_name) or {}).items():
            if not SECRET_NAME.match(str(key)):
                raise McpError(f"{key} は名前として使えません")
            if value is None:
                values.pop(key, None)
            elif isinstance(value, str):
                values[key] = seal(secret, value)
        server[field_name] = values
    if "enabled" in body:
        server["enabled"] = bool(body["enabled"])
    if "always_allow" in body:
        server["always_allow"] = [str(item) for item in body["always_allow"] if isinstance(item, str)][:200]
    return server


def usable(server: dict[str, Any], settings: Settings) -> bool:
    return settings.omni_allow_mcp and (server["transport"] == "http" or settings.omni_allow_mcp_stdio)


# --- Connections --------------------------------------------------------------------------------------------

@dataclass
class Tool:
    server: dict[str, Any]
    name: str            # the server's own name for it
    exposed: str         # what the model calls it (unique, provider-safe)
    description: str
    schema: dict[str, Any]
    read_only: bool = False


@dataclass
class Connection:
    server: dict[str, Any]
    session: ClientSession | None = None
    tools: list[Tool] = field(default_factory=list)
    error: str | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


def server_env(server: dict[str, Any], secret: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "USER") if key in os.environ}
    for key, value in server.get("env", {}).items():
        plain = unseal(secret, value)
        if plain is not None:
            env[key] = plain
    return env


async def host(connection: Connection, secret: str) -> None:
    """Opens the transport and session, lists tools, then keeps them open until stop is set."""
    server = connection.server
    try:
        async with contextlib.AsyncExitStack() as stack:
            if server["transport"] == "stdio":
                params = StdioServerParameters(command=server["command"], args=list(server.get("args", [])),
                                               env=server_env(server, secret), cwd=str(Path.home()))
                with open(os.devnull, "w") as quiet:  # a server's stderr stays out of the app's log
                    read, write = await stack.enter_async_context(stdio_client(params, errlog=quiet))
            else:
                headers = {key: plain for key, value in server.get("headers", {}).items()
                           if (plain := unseal(secret, value)) is not None}
                client = await stack.enter_async_context(httpx.AsyncClient(
                    headers=headers, timeout=httpx.Timeout(CALL_SECONDS, connect=CONNECT_SECONDS)))
                streams = await stack.enter_async_context(streamable_http_client(server["url"], http_client=client))
                read, write = streams[0], streams[1]
            session = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=CALL_SECONDS))
            await asyncio.wait_for(session.initialize(), CONNECT_SECONDS)
            listed = await asyncio.wait_for(session.list_tools(), CONNECT_SECONDS)
            for item in listed.tools:
                exposed = f"{NAME.sub('_', server['id'])}__{NAME.sub('_', item.name)}"[:64]
                annotations = item.annotations
                connection.tools.append(Tool(server, item.name, exposed, (item.description or "")[:1000],
                                             item.input_schema or {"type": "object", "properties": {}},
                                             bool(annotations and getattr(annotations, "read_only_hint", False))))
            connection.session = session
            connection.ready.set()
            await connection.stop.wait()
    except Exception as exc:  # noqa: BLE001 - any failure means "this server is unavailable this turn"
        log.info("MCP server %s failed: %s", server.get("name"), type(exc).__name__)
        connection.error = "接続できませんでした" if not connection.ready.is_set() else "接続が切れました"
    finally:
        connection.session = None
        connection.ready.set()


class ToolSet:
    """The tools of several servers for one turn."""

    def __init__(self, servers: list[dict[str, Any]], secret: str) -> None:
        self.connections = [Connection(server) for server in servers]
        self.secret = secret

    async def __aenter__(self) -> ToolSet:
        for connection in self.connections:
            connection.task = asyncio.create_task(host(connection, self.secret))
        await asyncio.gather(*(asyncio.wait_for(connection.ready.wait(), CONNECT_SECONDS + 5)
                               for connection in self.connections), return_exceptions=True)
        for connection in self.connections:
            if not connection.ready.is_set():
                connection.error = "接続が時間内に完了しませんでした"
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for connection in self.connections:
            connection.stop.set()
        tasks = [connection.task for connection in self.connections if connection.task]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()

    @property
    def tools(self) -> list[Tool]:
        return [tool for connection in self.connections if connection.session for tool in connection.tools]

    def failures(self) -> list[tuple[str, str]]:
        return [(connection.server["name"], connection.error) for connection in self.connections if connection.error]

    def find(self, exposed: str) -> Tool | None:
        return next((tool for tool in self.tools if tool.exposed == exposed), None)

    async def call(self, tool: Tool, arguments: dict[str, Any]) -> tuple[str, bool]:
        """(text for the model, is_error)."""
        connection = next(item for item in self.connections if item.server is tool.server)
        if connection.session is None:
            return "MCP サーバーとの接続が切れています", True
        try:
            result = await asyncio.wait_for(connection.session.call_tool(tool.name, arguments), CALL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            return f"ツールの実行に失敗しました: {type(exc).__name__}", True
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            kind = getattr(item, "type", "")
            if kind == "text":
                parts.append(item.text)
            elif kind == "image":
                parts.append("[画像]")
            elif kind == "resource":
                resource = getattr(item, "resource", None)
                parts.append(getattr(resource, "text", None) or f"[リソース: {getattr(resource, 'uri', '')}]")
            else:
                parts.append(f"[{kind}]")
        structured = getattr(result, "structured_content", None)
        if not parts and structured is not None:
            parts.append(json.dumps(structured, ensure_ascii=False))
        return "\n".join(parts)[:RESULT_LIMIT], bool(getattr(result, "is_error", False))


async def inspect(server: dict[str, Any], secret: str) -> dict[str, Any]:
    """Connects once from the settings screen: the tools, or why it failed."""
    async with ToolSet([server], secret) as tools:
        failures = tools.failures()
        if failures:
            return {"ok": False, "error": failures[0][1], "tools": []}
        return {"ok": True, "error": None, "tools": [
            {"name": tool.name, "description": tool.description, "read_only": tool.read_only} for tool in tools.tools]}


# --- The turn -----------------------------------------------------------------------------------------------

def permission(tool: Tool, call_id: str, arguments: dict[str, Any]) -> PermissionRequest:
    return PermissionRequest(tool_call_id=call_id, kind="mcp", title=f"{tool.server['name']} / {tool.name}",
                             command=json.dumps(arguments, ensure_ascii=False, indent=2)[:4000],
                             tool=f"{tool.server['id']}/{tool.name}")


async def chat_with_tools(provider, request, model, tools: ToolSet, ask) -> AsyncIterator[Update]:
    """Streams a Chat turn, letting the model call tools until it answers in text."""
    for name, error in tools.failures():
        yield {"sessionUpdate": "tool_call", "toolCallId": f"mcp-{uuid.uuid4().hex[:8]}", "title": f"MCP: {name}",
               "kind": "other", "status": "failed", "note": error}
    available = tools.tools
    request.tools = [{"name": tool.exposed, "description": tool.description or tool.name, "schema": tool.schema}
                     for tool in available] or None
    for round_number in range(MAX_ROUNDS):
        request.tool_calls = []
        request.assistant_turn = None
        streamed = False
        try:
            async for update in provider.stream(request, model):
                streamed = True
                yield update
        except Exception as exc:
            code = getattr(exc, "code", "")
            if round_number == 0 and request.tools and not streamed and code == "bad_request":
                # The model can't take tools: answer without them
                request.tools = None
                yield {"sessionUpdate": "tool_call", "toolCallId": f"mcp-{uuid.uuid4().hex[:8]}", "title": "MCP",
                       "kind": "other", "status": "failed", "note": "このモデルはツールを使えないため、ツールなしで返答します"}
                async for update in provider.stream(request, model):
                    yield update
                return
            raise
        if not request.tool_calls or not request.assistant_turn:
            return
        results = []
        for call in request.tool_calls:
            tool = tools.find(call["name"])
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            call_id = f"mcp-{call['id']}"
            title = f"{tool.server['name']} / {tool.name}" if tool else call["name"]
            yield {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": title, "kind": "other",
                   "status": "pending", "command": json.dumps(arguments, ensure_ascii=False)[:SHOWN_LIMIT]}
            if tool is None:
                text, failed = "このツールはありません", True
            else:
                decision = await ask(permission(tool, call_id, arguments))
                if decision.outcome == "deny":
                    text = "ユーザーがこのツールの実行を拒否しました" + (f": {decision.message}" if decision.message else "")
                    failed = True
                else:
                    yield {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "in_progress"}
                    text, failed = await tools.call(tool, arguments)
            yield {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                   "status": "failed" if failed else "completed", "output": text[:SHOWN_LIMIT]}
            results.append({"id": call["id"], "text": text, "is_error": failed})
        request.messages = [*request.messages, request.assistant_turn, *provider.tool_results(results)]
    yield {"sessionUpdate": "agent_message_chunk",
           "content": {"type": "text", "text": "\n\n（ツールの呼び出しが上限の回数に達したため、ここで止めました）"}}
