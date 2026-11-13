"""A small ACP (Agent Client Protocol) client on top of JsonRpcProcess.

The agent sends `session/update` notifications while a prompt runs and may call back into the client:
`session/request_permission`, `fs/read_text_file`, `fs/write_text_file`. Each session registers a handler
for those; file access is limited to the session's folder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .jsonrpc import JsonRpcProcess, MethodNotFound, RpcError

PROTOCOL_VERSION = 1
AcpError = RpcError

__all__ = ["AcpConnection", "AcpError", "MethodNotFound"]


class AcpConnection(JsonRpcProcess):
    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path) -> None:
        super().__init__(argv, env, cwd, route=lambda params: params.get("sessionId"))
        self.info: dict[str, Any] = {}

    async def start(self, client_name: str = "omni-llm-workspace", version: str = "0") -> dict[str, Any]:
        await self.spawn()
        self.info = await self.call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": False},
            "clientInfo": {"name": client_name, "version": version}}, timeout=60)
        return self.info
