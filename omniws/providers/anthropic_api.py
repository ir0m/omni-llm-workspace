"""Claude API through the official anthropic SDK (streaming, adaptive thinking, effort)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from ..attachments import expand
from ..events import Update, message_chunk, thought_chunk, usage_update
from .base import ChatRequest, ModelInfo, ProviderError

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 64000
# Server-side fallback on refusals (beta). The API accepts it only for these model lines.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_PREFIXES = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
# On-demand compaction (beta): the returned signed block replaces the summarized messages.
COMPACT_BETA = "compact-2026-09-04"
COMPACT_MAX_TOKENS = 8192
# Server-side web tools. The dynamic-filtering variants need these model lines; others get basic search only.
WEB_DYNAMIC_PREFIXES = ("claude-fable", "claude-mythos", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
                        "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6")
WEB_SEARCH_USES = 5
# A long server-tool turn comes back as pause_turn; it is resent as is to continue, at most this often
PAUSE_LIMIT = 5
WEB_TITLES = {"web_search": "Web検索", "web_fetch": "Webページ取得"}


def web_tools(model: str) -> list[dict[str, Any]]:
    if model.startswith(WEB_DYNAMIC_PREFIXES):
        return [{"type": "web_search_20260209", "name": "web_search", "max_uses": WEB_SEARCH_USES},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": WEB_SEARCH_USES}]
    return [{"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_SEARCH_USES}]
ATTACHMENT_KINDS = frozenset({"image", "pdf", "text"})


def supported(capability: Any) -> bool:
    return bool(capability is not None and getattr(capability, "supported", False))


def model_info(model: Any) -> ModelInfo:
    caps = getattr(model, "capabilities", None)
    efforts: tuple[str, ...] = ()
    thinking_modes: list[str] = []
    vision = reasoning = None
    if caps is not None:
        vision = supported(caps.image_input)
        reasoning = supported(caps.thinking)
        if supported(caps.effort):
            efforts = tuple(level for level in EFFORT_LEVELS if supported(getattr(caps.effort, level, None)))
        types = getattr(caps.thinking, "types", None)
        thinking_modes = [name for name in ("adaptive", "enabled") if supported(getattr(types, name, None))]
    return ModelInfo(id=model.id, name=getattr(model, "display_name", None) or model.id,
                     context_window=getattr(model, "max_input_tokens", None), max_output=getattr(model, "max_tokens", None),
                     vision=vision, reasoning=reasoning, efforts=efforts, extra={"thinking_modes": thinking_modes})


def request_options(request: ChatRequest, model: ModelInfo | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "model": request.model,
        "max_tokens": min(model.max_output or MAX_TOKENS, MAX_TOKENS) if model else MAX_TOKENS,
        "messages": expand(request.messages, request.data_dir, "anthropic", ATTACHMENT_KINDS),
    }
    if request.system:
        options["system"] = request.system
    # Adaptive thinking with readable summaries, where the model offers it
    if model is not None and "adaptive" in model.extra.get("thinking_modes", []):
        options["thinking"] = {"type": "adaptive", "display": "summarized"}
    if request.effort and (model is None or request.effort in model.efforts):
        options["output_config"] = {"effort": request.effort}
    tools = [{"name": tool["name"], "description": tool["description"], "input_schema": tool["schema"]}
             for tool in request.tools or []]
    if request.web_search:
        tools += web_tools(request.model)
    if tools:
        options["tools"] = tools
    if request.model.startswith(FALLBACK_PREFIXES):
        options["betas"] = [FALLBACK_BETA]
        options["fallbacks"] = "default"
    if any(isinstance(m.get("content"), list) and any(b.get("type") == "compaction" for b in m["content"]) for m in request.messages):
        options["betas"] = [*options.get("betas", []), COMPACT_BETA]
    return options


def provider_error(exc: Exception) -> ProviderError:
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ProviderError("auth", "Claude API のキーを確認してください", status=exc.status_code)
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError("not_found", "モデルが見つかりません", status=404)
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderError("rate_limit", "利用上限に達しました。時間をおいて再試行してください", status=429)
    if isinstance(exc, anthropic.BadRequestError):
        return ProviderError("bad_request", exc.message, status=400)
    if isinstance(exc, anthropic.APIStatusError):
        return ProviderError("overloaded" if exc.status_code >= 500 else "bad_request", exc.message,
                             status=exc.status_code)
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError("network", "Claude API に接続できません")
    return ProviderError("unknown", type(exc).__name__)


def turn_block(block: Any) -> dict[str, Any]:
    """A block of the model's turn, as it goes back to the API with the tool results."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return block.model_dump(exclude_none=True)


def web_update(block: Any, final: bool = False) -> Update | None:
    """A card for a server-side web search or fetch, and its result."""
    kind = getattr(block, "type", None)
    if kind == "server_tool_use" and block.name in WEB_TITLES:
        data = block.input if isinstance(block.input, dict) else {}
        target = data.get("query") or data.get("url") or ""
        update: Update = {"sessionUpdate": "tool_call" if not final else "tool_call_update", "toolCallId": block.id,
                          "kind": "fetch", "title": f"{WEB_TITLES[block.name]}: {target}" if target else WEB_TITLES[block.name]}
        if not final:
            update["status"] = "in_progress"
        return update
    if kind in ("web_search_tool_result", "web_fetch_tool_result"):
        content = getattr(block, "content", None)
        failed = getattr(content, "type", "").endswith("_error") if content is not None and not isinstance(content, list) else False
        output = ""
        if isinstance(content, list):
            output = "\n".join(f"{getattr(item, 'title', '') or ''} {getattr(item, 'url', '')}".strip() for item in content[:10])
        elif failed:
            output = str(getattr(content, "error_code", "") or "")
        elif content is not None:
            output = str(getattr(content, "url", "") or "")
        update = {"sessionUpdate": "tool_call_update", "toolCallId": block.tool_use_id,
                  "status": "failed" if failed else "completed"}
        if output:
            update["output"] = output
        return update
    return None


class AnthropicProvider:
    context_format = "anthropic-v1"
    id = "anthropic"
    label = "Claude API"
    web_search = True
    kind = "api"
    attachment_kinds = ATTACHMENT_KINDS

    def __init__(self, api_key: str, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)

    @staticmethod
    def tool_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": result["id"],
                                              "content": result["text"] or "(結果なし)", "is_error": result["is_error"]}
                                             for result in results]}]

    async def list_models(self) -> list[ModelInfo]:
        try:
            return [model_info(model) async for model in self.client.models.list(limit=100)]
        except anthropic.APIError as exc:
            raise provider_error(exc) from exc

    async def stream(self, request: ChatRequest, model: ModelInfo | None = None) -> AsyncIterator[Update]:
        for _ in range(PAUSE_LIMIT + 1):
            final = None
            async for update in self._stream_once(request, model):
                if isinstance(update, dict):
                    yield update
                else:
                    final = update
            if final is None or final.stop_reason != "pause_turn":
                return
            # The server paused a long web-tool turn: send the partial turn back unchanged to let it continue.
            request.messages = [*request.messages, {"role": "assistant", "content": [turn_block(b) for b in final.content]}]
        raise ProviderError("unknown", "Web検索の処理が終わりませんでした")

    async def _stream_once(self, request: ChatRequest, model: ModelInfo | None) -> AsyncIterator[Any]:
        """Updates for one request, then the final message itself."""
        options = request_options(request, model)
        api = self.client.beta.messages if "betas" in options else self.client.messages
        try:
            async with api.stream(**options) as stream:
                async for event in stream:
                    if event.type == "text" and event.text:
                        yield message_chunk(event.text)
                    elif event.type == "thinking" and event.thinking:
                        yield thought_chunk(event.thinking)
                    elif event.type == "content_block_start":
                        update = web_update(event.content_block)
                        if update:
                            yield update
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise provider_error(exc) from exc
        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            raise ProviderError("refused", getattr(details, "explanation", None) or "応答が拒否されました")
        # The query or URL is only complete once the block has finished streaming
        for block in final.content:
            if getattr(block, "type", None) == "server_tool_use" and block.name in WEB_TITLES:
                yield web_update(block, final=True)
        if final.stop_reason == "tool_use":
            request.tool_calls = [{"id": block.id, "name": block.name, "arguments": block.input}
                                  for block in final.content if block.type == "tool_use"]
            request.assistant_turn = {"role": "assistant", "content": [turn_block(block) for block in final.content]}
        if final.stop_reason == "end_turn":
            request.replay_messages = [*request.messages, {"role": "assistant", "content": [turn_block(b) for b in final.content]}]
        usage = final.usage
        yield usage_update(input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
                           cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
                           cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
                           context_window=model.context_window if model else None,
                           stop_reason=final.stop_reason)
        yield final

    async def compact(self, request: ChatRequest, model: ModelInfo | None = None) -> list[dict[str, Any]]:
        """On-demand compaction: same model, system, tools and thinking settings as the conversation."""
        options = request_options(request, model)
        options.pop("fallbacks", None)
        options["betas"] = [COMPACT_BETA]
        options["compaction"] = {"type": "summarize"}
        # Non-streaming call; the summary is short, and a large cap would trip the SDK's long-request guard.
        options["max_tokens"] = COMPACT_MAX_TOKENS
        try:
            result = await self.client.beta.messages.create(**options)
        except anthropic.APIError as exc:
            raise provider_error(exc) from exc
        if result.stop_reason != "compaction":
            raise ProviderError("bad_request", "このモデルでは圧縮できませんでした")
        return [{"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in result.content]}]
