"""Any OpenAI-compatible Chat Completions endpoint: OpenAI, Gemini, OpenRouter, Ollama, LM Studio,
llama.cpp, vLLM. Streams with Server-Sent Events and asks for usage in the last chunk."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx

from ..events import Update, message_chunk, thought_chunk, usage_update
from .base import ChatRequest, ModelInfo, ProviderError, error_for_status

TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
# /v1/models lists more than chat models on some services. These are skipped unless OMNI_MODELS_* names them.
NOT_CHAT = ("embedding", "embed", "tts", "whisper", "transcribe", "dall-e", "image", "moderation", "audio",
            "realtime", "sora", "rerank")


def error_message(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace")[:300]
    error = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    if isinstance(error, list) and error and isinstance(error[0], dict):  # Gemini wraps errors in a list
        return str(error[0].get("error", {}).get("message") or error[0])[:300]
    return str(error)[:300]


class OpenAICompatProvider:
    def __init__(self, id: str, label: str, kind: str, base_url: str, api_key: str | None = None, *,
                 ollama_root: str | None = None, headers: dict[str, str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.id = id
        self.label = label
        self.kind = kind
        self.base_url = base_url.rstrip("/")
        self.ollama_root = ollama_root.rstrip("/") if ollama_root else None
        self.headers = {**(headers or {}), **({"Authorization": f"Bearer {api_key}"} if api_key else {})}
        self.transport = transport

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, headers=self.headers, transport=self.transport)

    async def list_models(self) -> list[ModelInfo]:
        async with self.client() as client:
            try:
                if self.ollama_root:
                    response = await client.get(f"{self.ollama_root}/api/tags")
                else:
                    response = await client.get(f"{self.base_url}/models")
            except httpx.HTTPError as exc:
                raise ProviderError("network", f"{self.label} に接続できません") from exc
        if response.status_code >= 400:
            raise error_for_status(response.status_code, error_message(response.content))
        data = response.json()
        if self.ollama_root:
            return [ModelInfo(id=item["name"], name=item["name"]) for item in data.get("models", [])]
        models = []
        for item in data.get("data", []):
            model_id = str(item.get("id", ""))
            if not model_id or any(word in model_id.lower() for word in NOT_CHAT):
                continue
            model_id = model_id.removeprefix("models/")  # Gemini
            models.append(ModelInfo(id=model_id, name=item.get("name") or item.get("display_name") or model_id,
                                    context_window=item.get("context_length") or item.get("context_window")))
        return sorted(models, key=lambda model: model.id)

    def body(self, request: ChatRequest) -> dict[str, Any]:
        messages = ([{"role": "system", "content": request.system}] if request.system else []) + request.messages
        body: dict[str, Any] = {"model": request.model, "messages": messages, "stream": True,
                                "stream_options": {"include_usage": True}}
        if request.effort:
            body["reasoning_effort"] = request.effort
        if self.id == "openrouter":
            body["usage"] = {"include": True}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                                                                "parameters": tool["schema"]}} for tool in request.tools]
        return body

    @staticmethod
    def tool_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": "tool", "tool_call_id": result["id"], "content": result["text"] or "(結果なし)"}
                for result in results]

    async def stream(self, request: ChatRequest, model: ModelInfo | None = None) -> AsyncIterator[Update]:
        usage: dict[str, Any] | None = None
        finish: str | None = None
        text: list[str] = []
        calls: dict[int, dict[str, Any]] = {}  # tool calls arrive in pieces, keyed by index
        async with self.client() as client:
            try:
                async with client.stream("POST", f"{self.base_url}/chat/completions", json=self.body(request)) as response:
                    if response.status_code >= 400:
                        raise error_for_status(response.status_code, error_message(await response.aread()))
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            continue
                        if chunk.get("error"):
                            raise ProviderError("bad_request", error_message(payload.encode()))
                        usage = chunk.get("usage") or usage
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if isinstance(reasoning, str) and reasoning:
                                yield thought_chunk(reasoning)
                            if isinstance(delta.get("content"), str) and delta["content"]:
                                text.append(delta["content"])
                                yield message_chunk(delta["content"])
                            for piece in delta.get("tool_calls") or []:
                                call = calls.setdefault(int(piece.get("index") or 0), {"id": None, "name": "", "arguments": ""})
                                call["id"] = piece.get("id") or call["id"]
                                function = piece.get("function") or {}
                                call["name"] += function.get("name") or ""
                                call["arguments"] += function.get("arguments") or ""
                            finish = choice.get("finish_reason") or finish
            except httpx.HTTPError as exc:
                raise ProviderError("network", f"{self.label} との通信が切れました") from exc
        if finish == "content_filter":
            raise ProviderError("refused", "応答がフィルタされました")
        if calls and request.tools:
            ordered = [calls[index] for index in sorted(calls)]
            for number, call in enumerate(ordered):
                call["id"] = call["id"] or f"call_{number}"  # some services leave the id out
            request.assistant_turn = {"role": "assistant", "content": "".join(text) or None, "tool_calls": [
                {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"] or "{}"}}
                for call in ordered]}
            request.tool_calls = []
            for call in ordered:
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                except ValueError:
                    arguments = {}
                request.tool_calls.append({"id": call["id"], "name": call["name"],
                                           "arguments": arguments if isinstance(arguments, dict) else {}})
        usage = usage or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        cost = usage.get("cost")
        yield usage_update(input_tokens=int(usage.get("prompt_tokens") or 0),
                           output_tokens=int(usage.get("completion_tokens") or 0),
                           cache_read_tokens=int(prompt_details.get("cached_tokens") or 0),
                           reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
                           cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
                           context_window=model.context_window if model else None, stop_reason=finish)
