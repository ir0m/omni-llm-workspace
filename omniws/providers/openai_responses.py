"""Opt-in Responses transport; retain opaque reasoning items without interpreting them."""
import json
import httpx2 as httpx
from .openai_compat import OpenAICompatProvider, error_message
from .base import ProviderError, error_for_status
from ..events import message_chunk, thought_chunk, usage_update


class OpenAIResponsesProvider(OpenAICompatProvider):
    context_format = "responses-v1"
    @staticmethod
    def inputs(messages):
        result = []
        for message in messages:
            if message.get("role") == "assistant" and "items" in message:
                result.extend(message["items"])
            elif message.get("role") == "tool":
                result.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
            else:
                result.append(message)
        return result

    async def compact(self, request, model=None):
        body = {"model": request.model, "input": self.inputs(request.messages)}
        if request.system:
            body["instructions"] = request.system
        async with self.client() as client:
            try:
                response = await client.post(f"{self.base_url}/responses/compact", json=body)
                if response.status_code >= 400:
                    raise error_for_status(response.status_code, error_message(response.content))
                return [{"role": "assistant", "items": response.json()["output"]}]
            except httpx.HTTPError as exc:
                raise ProviderError("network", "圧縮サービスに接続できません") from exc

    async def stream(self, request, model=None):
        body = {"model": request.model, "input": self.inputs(request.messages), "stream": True,
                "store": False, "include": ["reasoning.encrypted_content"]}
        if request.system:
            body["instructions"] = request.system
        if request.effort:
            body["reasoning"] = {"effort": request.effort}
        if request.tools:
            body["tools"] = [{"type": "function", "name": t["name"], "description": t["description"],
                              "parameters": t["schema"], "strict": False} for t in request.tools]
        completed = False
        async with self.client() as client:
            try:
                async with client.stream("POST", f"{self.base_url}/responses", json=body) as response:
                    if response.status_code >= 400:
                        raise error_for_status(response.status_code, error_message(await response.aread()))
                    async for line in response.aiter_lines():
                        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                            continue
                        event = json.loads(line[5:])
                        kind = event.get("type")
                        if kind == "response.output_text.delta":
                            yield message_chunk(event["delta"])
                        elif kind == "response.reasoning_summary_text.delta":
                            yield thought_chunk(event["delta"])
                        elif kind in ("error", "response.failed", "response.incomplete"):
                            raise ProviderError("unknown", "応答が正常に完了しませんでした")
                        elif kind == "response.completed":
                            completed = True
                            result = event["response"]
                            output = result.get("output", [])
                            if any(b.get("type") == "refusal" for item in output for b in item.get("content", []) if isinstance(b, dict)):
                                raise ProviderError("refused", "応答が拒否されました")
                            turn = {"role": "assistant", "items": output}
                            calls = [item for item in output if item.get("type") == "function_call"]
                            if calls:
                                request.tool_calls = [{"id": c["call_id"], "name": c["name"], "arguments": json.loads(c["arguments"])} for c in calls]
                                request.assistant_turn = turn
                            else:
                                request.replay_messages = [*request.messages, turn]
                            usage = result.get("usage") or {}
                            yield usage_update(input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                                reasoning_tokens=(usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
                                cache_read_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens", 0))
            except httpx.HTTPError as exc:
                raise ProviderError("network", "OpenAI に接続できません") from exc
        if not completed:
            raise ProviderError("network", "応答の途中で接続が切れました")
