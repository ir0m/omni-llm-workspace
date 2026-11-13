"""Claude API through the official anthropic SDK (streaming, adaptive thinking, effort)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from ..events import Update, message_chunk, thought_chunk, usage_update
from .base import ChatRequest, ModelInfo, ProviderError

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 64000
# Server-side fallback on refusals (beta). The API accepts it only for these model lines.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_PREFIXES = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")


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
        "messages": request.messages,
    }
    if request.system:
        options["system"] = request.system
    # Adaptive thinking with readable summaries, where the model offers it
    if model is not None and "adaptive" in model.extra.get("thinking_modes", []):
        options["thinking"] = {"type": "adaptive", "display": "summarized"}
    if request.effort and (model is None or request.effort in model.efforts):
        options["output_config"] = {"effort": request.effort}
    if request.model.startswith(FALLBACK_PREFIXES):
        options["betas"] = [FALLBACK_BETA]
        options["fallbacks"] = "default"
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


class AnthropicProvider:
    id = "anthropic"
    label = "Claude API"
    kind = "api"

    def __init__(self, api_key: str, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)

    async def list_models(self) -> list[ModelInfo]:
        try:
            return [model_info(model) async for model in self.client.models.list(limit=100)]
        except anthropic.APIError as exc:
            raise provider_error(exc) from exc

    async def stream(self, request: ChatRequest, model: ModelInfo | None = None) -> AsyncIterator[Update]:
        options = request_options(request, model)
        api = self.client.beta.messages if "betas" in options else self.client.messages
        try:
            async with api.stream(**options) as stream:
                async for event in stream:
                    if event.type == "text" and event.text:
                        yield message_chunk(event.text)
                    elif event.type == "thinking" and event.thinking:
                        yield thought_chunk(event.thinking)
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise provider_error(exc) from exc
        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            raise ProviderError("refused", getattr(details, "explanation", None) or "応答が拒否されました")
        usage = final.usage
        yield usage_update(input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
                           cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
                           cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
                           context_window=model.context_window if model else None,
                           stop_reason=final.stop_reason)
