"""Agents: external programs with their own loop and tools (OpenCode, Codex, Claude Code).

Unlike a chat provider, an agent works inside a folder, asks for permission before acting, and keeps
its own session. A turn streams ACP-shaped updates (see events.py and blocks.py).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ..events import Update
from ..providers.base import ModelInfo

# ACP tool kinds
ToolKind = Literal["read", "edit", "delete", "move", "search", "execute", "think", "fetch", "other"]


@dataclass
class PermissionRequest:
    tool_call_id: str
    kind: str
    title: str
    command: str | None = None
    paths: list[str] = field(default_factory=list)
    diff: str | None = None
    outside_project: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    outcome: Literal["allow", "allow_always", "deny"]
    message: str | None = None   # why it was denied, for agents that can take it
    automatic: bool = False       # decided by the policy, not the user


AskPermission = Callable[[PermissionRequest], Awaitable[Decision]]


@dataclass
class Turn:
    conversation_id: str
    mode: str                       # chat | code
    cwd: Path                       # the project folder (code) or an empty folder (chat)
    model: str                      # the agent's own model id
    prompt: str                     # turns the agent hasn't seen + the user's message
    fresh_prompt: str               # the whole conversation + the user's message, for a new agent session
    native_session: str | None      # the agent's session to continue, if any
    temporary: bool                 # leave no session or memory behind
    effort: str | None
    ask_permission: AskPermission
    on_session: Callable[[str], None]  # called with the agent's session id once known


class Agent(Protocol):
    id: str
    label: str

    def installed(self) -> bool: ...

    async def list_models(self) -> list[ModelInfo]: ...

    def run(self, turn: Turn) -> AsyncIterator[Update]: ...

    async def forget(self, native_session: str, cwd: Path) -> None:
        """Delete the agent's own record of a session (temporary conversations)."""
        ...


def inside(root: Path, path: str | Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False
