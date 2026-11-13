"""The providers configured in `.env`, and their model lists (cached)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from .anthropic_api import AnthropicProvider
from .base import ChatProvider, ModelInfo, ProviderError
from .openai_compat import OpenAICompatProvider

MODEL_CACHE_SECONDS = 600
GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Every provider the app knows, in the order the model picker shows them
KNOWN = {
    "anthropic": ("Claude API", "api", "ANTHROPIC_API_KEY"),
    "openai": ("OpenAI API", "api", "OPENAI_API_KEY"),
    "gemini": ("Gemini API", "api", "GEMINI_API_KEY"),
    "openrouter": ("OpenRouter", "api", "OPENROUTER_API_KEY"),
    "ollama": ("Ollama", "local", "OLLAMA_BASE_URL"),
    "local": ("ローカル（OpenAI互換）", "local", "LOCAL_OPENAI_BASE_URL"),
}


def secret(value: Any) -> str | None:
    return value.get_secret_value() if value is not None else None


def build_providers(settings: Settings) -> dict[str, ChatProvider]:
    found: dict[str, ChatProvider] = {}
    if settings.anthropic_api_key:
        found["anthropic"] = AnthropicProvider(secret(settings.anthropic_api_key))
    if settings.openai_api_key:
        found["openai"] = OpenAICompatProvider("openai", KNOWN["openai"][0], "api", settings.openai_base_url,
                                               secret(settings.openai_api_key))
    if settings.gemini_api_key:
        found["gemini"] = OpenAICompatProvider("gemini", KNOWN["gemini"][0], "api", GEMINI_OPENAI_URL,
                                               secret(settings.gemini_api_key))
    if settings.openrouter_api_key:
        found["openrouter"] = OpenAICompatProvider("openrouter", KNOWN["openrouter"][0], "api", OPENROUTER_URL,
                                                   secret(settings.openrouter_api_key),
                                                   headers={"X-Title": "OmniLLMWorkspace"})
    if settings.ollama_base_url:
        root = settings.ollama_base_url.rstrip("/").removesuffix("/v1")
        found["ollama"] = OpenAICompatProvider("ollama", KNOWN["ollama"][0], "local", f"{root}/v1", ollama_root=root)
    if settings.local_openai_base_url:
        found["local"] = OpenAICompatProvider("local", KNOWN["local"][0], "local", settings.local_openai_base_url,
                                              secret(settings.local_openai_api_key))
    enabled = settings.enabled_providers()
    return {key: value for key, value in found.items() if enabled is None or key in enabled}


@dataclass
class ModelList:
    models: list[ModelInfo] = field(default_factory=list)
    error: dict[str, Any] | None = None
    fetched_at: float = 0.0


class Registry:
    def __init__(self, settings: Settings, providers: dict[str, ChatProvider] | None = None) -> None:
        self.settings = settings
        self.providers = build_providers(settings) if providers is None else providers
        self.cache: dict[str, ModelList] = {}

    def get(self, provider_id: str) -> ChatProvider | None:
        return self.providers.get(provider_id)

    async def models(self, provider_id: str, refresh: bool = False) -> ModelList:
        cached = self.cache.get(provider_id)
        if cached and not refresh and time.monotonic() - cached.fetched_at < MODEL_CACHE_SECONDS:
            return cached
        provider = self.providers[provider_id]
        fixed = self.settings.fixed_models(provider_id)
        try:
            models = await provider.list_models()
            error = None
        except ProviderError as exc:
            models, error = [], exc.to_dict()
        if fixed:  # OMNI_MODELS_* decides the list; known details are kept
            known = {model.id: model for model in models}
            models, error = [known.get(name) or ModelInfo(id=name, name=name) for name in fixed], None
        result = ModelList(models=models, error=error, fetched_at=time.monotonic())
        self.cache[provider_id] = result
        return result

    async def model(self, provider_id: str, model_id: str) -> ModelInfo | None:
        listing = await self.models(provider_id)
        return next((model for model in listing.models if model.id == model_id), None)

    async def overview(self, refresh: bool = False) -> list[dict[str, Any]]:
        ids = list(self.providers)
        listings = await asyncio.gather(*(self.models(provider_id, refresh) for provider_id in ids))
        by_id = dict(zip(ids, listings))
        enabled = self.settings.enabled_providers()
        overview = []
        for provider_id, (label, kind, setting) in KNOWN.items():
            if enabled is not None and provider_id not in enabled:
                continue
            listing = by_id.get(provider_id)
            overview.append({"id": provider_id, "label": label, "kind": kind, "configured": listing is not None,
                             "setting": setting, "error": listing.error if listing else None,
                             "models": [model.to_dict() for model in listing.models] if listing else []})
        for provider_id in ids:  # providers added in tests or later
            if provider_id not in KNOWN:
                provider, listing = self.providers[provider_id], by_id[provider_id]
                overview.append({"id": provider_id, "label": provider.label, "kind": provider.kind, "configured": True,
                                 "setting": None, "error": listing.error,
                                 "models": [model.to_dict() for model in listing.models]})
        return overview
