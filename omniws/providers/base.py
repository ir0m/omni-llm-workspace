"""Chat providers: an inference endpoint plus its credentials (not an agent; agents come in P2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ..events import Update


@dataclass(frozen=True)
class ModelInfo:
    """What a model can do. None means the provider didn't say."""

    id: str
    name: str
    context_window: int | None = None
    max_output: int | None = None
    vision: bool | None = None
    reasoning: bool | None = None
    efforts: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["efforts"] = list(self.efforts)
        data.pop("extra")
        return data


@dataclass
class ChatRequest:
    model: str
    # Alternating turns starting with the user: {"role": "user" | "assistant", "content": str}
    # (with tools, also the provider's own tool-call and tool-result turns)
    messages: list[dict[str, Any]]
    system: str | None = None
    effort: str | None = None
    # MCP tools offered to the model: {"name", "description", "schema"}. None: no tools.
    tools: list[dict[str, Any]] | None = None
    # Filled by the provider when the model stopped to call tools: {"id", "name", "arguments"},
    # and the assistant turn to send back with their results
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_turn: dict[str, Any] | None = None


class ProviderError(Exception):
    """`code`: auth, not_found, rate_limit, overloaded, bad_request, network, refused, unknown."""

    def __init__(self, code: str, message: str = "", *, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message[:500]}


class ChatProvider(Protocol):
    id: str
    label: str
    kind: str  # "api" | "local"

    async def list_models(self) -> list[ModelInfo]: ...

    def stream(self, request: ChatRequest, model: ModelInfo | None = None) -> AsyncIterator[Update]:
        """Yields agent_message_chunk / agent_thought_chunk updates, then one usage_update."""
        ...


def error_for_status(status: int, message: str = "") -> ProviderError:
    if status in (401, 403):
        return ProviderError("auth", message, status=status)
    if status == 404:
        return ProviderError("not_found", message, status=status)
    if status == 429:
        return ProviderError("rate_limit", message, status=status)
    if status >= 500:
        return ProviderError("overloaded", message, status=status)
    return ProviderError("bad_request", message, status=status)
