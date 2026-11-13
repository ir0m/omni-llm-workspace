"""OpenCode over ACP (`opencode acp`). It runs Code turns for API and local models, and OpenCode's own
free models when OMNI_ALLOW_OPENCODE_FREE is set.

One OpenCode process serves every conversation. Its permission settings make it ask before editing,
running commands or fetching, so every action goes through the app's policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..config import Settings
from ..events import Update, usage_update
from ..providers.base import ModelInfo, ProviderError
from ..providers.registry import Registry
from .acp import AcpConnection, AcpError, MethodNotFound
from .base import PermissionRequest, Turn, inside

OUTPUT_LIMIT = 12_000
# The app's provider ids -> OpenCode's provider ids
PROVIDER_IDS = {"anthropic": "anthropic", "openai": "openai", "gemini": "google", "openrouter": "openrouter",
                "ollama": "omni-ollama", "local": "omni-local"}
KEY_VARIABLES = {"anthropic_api_key": "ANTHROPIC_API_KEY", "openai_api_key": "OPENAI_API_KEY",
                 "gemini_api_key": "GOOGLE_GENERATIVE_AI_API_KEY", "openrouter_api_key": "OPENROUTER_API_KEY"}
FREE = "opencode_free"


def find_executable() -> str | None:
    found = shutil.which("opencode")
    local = Path.home() / ".local" / "bin" / "opencode"
    return found or (str(local) if local.exists() else None)


def clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n… （{len(text) - limit} 文字省略）…\n{text[-half:]}"


def relative(path: str | None, cwd: Path) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(cwd.resolve()))
    except (ValueError, OSError):
        return path


def unified(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="")
    return "\n".join(lines) + "\n"


def short_paths(diff: str, cwd: Path) -> str:
    """Diff headers relative to the project instead of absolute paths."""
    for prefix in {str(cwd.resolve()) + "/", str(cwd) + "/"}:
        diff = diff.replace(prefix, "")
    return diff


def tool_update(update: dict[str, Any], cwd: Path) -> Update | None:
    """An ACP tool_call(_update) in the app's shape, or a plan for OpenCode's todo tool."""
    raw = update.get("rawInput") or {}
    if isinstance(raw, dict) and isinstance(raw.get("todos"), list):
        entries = [{"content": str(todo.get("content", "")), "status": todo.get("status", "pending"),
                    "priority": todo.get("priority", "medium")} for todo in raw["todos"] if isinstance(todo, dict)]
        return {"sessionUpdate": "plan", "entries": entries}
    if update.get("title") in ("todowrite", "todoread") or str(update.get("title", "")).endswith(" todos"):
        return None
    locations = [{"path": relative(item.get("path"), cwd)} for item in update.get("locations") or [] if item.get("path")]
    path = relative(raw.get("filePath") or raw.get("filepath") or raw.get("path"), cwd) if isinstance(raw, dict) else None
    result: Update = {"sessionUpdate": update["sessionUpdate"], "toolCallId": update["toolCallId"],
                      "title": update.get("title"), "kind": update.get("kind"), "status": update.get("status"),
                      "locations": locations or None, "command": raw.get("command") if isinstance(raw, dict) else None,
                      "path": path or (locations[0]["path"] if locations else None)}
    texts, diffs = [], []
    for item in update.get("content") or []:
        if item.get("type") == "content" and (item.get("content") or {}).get("type") == "text":
            texts.append(item["content"]["text"])
        elif item.get("type") == "diff":
            diffs.append(unified(relative(item.get("path"), cwd) or "file", item.get("oldText") or "", item.get("newText") or ""))
    if isinstance(raw, dict) and isinstance(raw.get("diff"), str):
        diffs.append(short_paths(raw["diff"], cwd))
    elif isinstance(raw, dict) and isinstance(raw.get("content"), str) and update.get("kind") == "edit":
        diffs.append(unified(path or "file", "", raw["content"]))
    raw_output = update.get("rawOutput")
    if not texts and isinstance(raw_output, dict) and isinstance(raw_output.get("output"), str):
        texts.append(raw_output["output"])
    if diffs:
        result["diff"] = clip("\n".join(diffs), 40_000)
    if texts and update.get("kind") != "edit":
        result["output"] = clip("\n".join(texts))
    return {key: value for key, value in result.items() if value is not None or key in ("sessionUpdate", "toolCallId")}


def normalize(update: dict[str, Any], cwd: Path) -> Update | None:
    kind = update.get("sessionUpdate")
    if kind in ("agent_message_chunk", "agent_thought_chunk"):
        content = update.get("content") or {}
        return update if content.get("type") == "text" and content.get("text") else None
    if kind in ("tool_call", "tool_call_update"):
        return tool_update(update, cwd)
    if kind == "plan":
        return {"sessionUpdate": "plan", "entries": update.get("entries") or []}
    if kind == "usage_update":
        cost = update.get("cost") or {}
        result: Update = {"sessionUpdate": "usage_update", "contextUsed": update.get("used"), "contextWindow": update.get("size")}
        if cost.get("currency", "USD") == "USD" and isinstance(cost.get("amount"), (int, float)):
            result["costUsd"] = cost["amount"]
        return result
    return None  # commands, config options, replayed user messages, session info


def session_update(message: dict[str, Any], cwd: Path) -> Update | None:
    if message.get("method") != "session/update":
        return None
    return normalize((message.get("params") or {}).get("update") or {}, cwd)


def permission_request(params: dict[str, Any], cwd: Path) -> PermissionRequest:
    call = params.get("toolCall") or {}
    raw = call.get("rawInput") or {}
    paths = [item["path"] for item in call.get("locations") or [] if item.get("path")]
    for key in ("filePath", "filepath", "path"):
        if isinstance(raw.get(key), str):
            paths.append(raw[key])
    kind = call.get("kind") or "other"
    diff = short_paths(raw["diff"], cwd) if isinstance(raw.get("diff"), str) else None
    return PermissionRequest(tool_call_id=call.get("toolCallId", ""), kind=kind,
                             title=str(call.get("title") or kind), command=raw.get("command"),
                             paths=[relative(path, cwd) or path for path in dict.fromkeys(paths)],
                             diff=clip(diff, 40_000) if diff else None,
                             outside_project=any(not inside(cwd, path) for path in paths))


def pick_option(options: list[dict[str, Any]], kinds: tuple[str, ...]) -> str | None:
    for kind in kinds:
        for option in options:
            if option.get("kind") == kind:
                return option.get("optionId")
    return None


class OpenCodeAgent:
    id = "opencode"
    label = "OpenCode"

    def __init__(self, settings: Settings, registry: Registry, executable: str | None = None) -> None:
        self.settings = settings
        self.registry = registry
        self.executable = executable or find_executable()
        self.connection: AcpConnection | None = None
        self.config_key = ""
        self.open_sessions: dict[str, str | None] = {}  # session id -> current model
        self.active = 0
        self.lock = asyncio.Lock()

    def installed(self) -> bool:
        return self.executable is not None

    async def list_models(self) -> list[ModelInfo]:
        """OpenCode's own free models (they run on OpenCode's service)."""
        if not self.executable:
            return []
        process = await asyncio.create_subprocess_exec(self.executable, "models", "opencode", env=self.base_env(),
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 60)
        except TimeoutError as exc:
            process.kill()
            raise ProviderError("network", "OpenCode のモデル一覧を取得できません") from exc
        names = [line.strip() for line in out.decode().splitlines() if line.strip().endswith("-free")]
        return [ModelInfo(id=name, name=name.removeprefix("opencode/")) for name in names]

    @staticmethod
    def model_ref(executor: str, model: str) -> str:
        return model if executor == FREE else f"{PROVIDER_IDS[executor]}/{model}"

    def base_env(self) -> dict[str, str]:
        directory = str(Path(self.executable).parent) if self.executable else ""
        return {"HOME": os.environ.get("HOME", str(Path.home())), "LANG": "C.UTF-8",
                "PATH": ":".join(dict.fromkeys([directory, "/usr/local/bin", "/usr/bin", "/bin"]))}

    async def build_config(self) -> tuple[dict[str, str], str]:
        providers: dict[str, Any] = {}
        for provider_id, name in (("ollama", "Ollama"), ("local", "ローカル（OpenAI互換）")):
            provider = self.registry.get(provider_id)
            if provider is None:
                continue
            listing = await self.registry.models(provider_id)
            options: dict[str, Any] = {"baseURL": provider.base_url}
            key = self.settings.local_openai_api_key if provider_id == "local" else None
            if key:
                options["apiKey"] = key.get_secret_value()
            providers[PROVIDER_IDS[provider_id]] = {"npm": "@ai-sdk/openai-compatible", "name": name, "options": options,
                                                    "models": {model.id: {"name": model.name} for model in listing.models}}
        config = {"autoupdate": False, "share": "disabled",
                  "permission": {"edit": "ask", "bash": "ask", "webfetch": "ask"}, "provider": providers}
        env = self.base_env()
        for field, variable in KEY_VARIABLES.items():
            value = getattr(self.settings, field)
            if value is not None:
                env[variable] = value.get_secret_value()
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
        return env, hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()

    async def connect(self) -> AcpConnection:
        async with self.lock:
            env, key = await self.build_config()
            if self.connection and self.connection.alive and (key == self.config_key or self.active):
                return self.connection
            if self.connection:
                await self.connection.close()
            if not self.executable:
                raise ProviderError("not_found", "OpenCode がインストールされていません")
            connection = AcpConnection([self.executable, "acp", "--pure"], env, Path.home())
            try:
                await connection.start()
            except (AcpError, OSError) as exc:
                await connection.close()
                raise ProviderError("unknown", f"OpenCode を起動できません: {exc}") from exc
            self.connection, self.config_key, self.open_sessions = connection, key, {}
            return connection

    async def session(self, connection: AcpConnection, turn: Turn) -> tuple[str, str]:
        """The session id and the prompt to send: a new session gets the whole conversation."""
        if turn.native_session and turn.native_session in self.open_sessions:
            return turn.native_session, turn.prompt
        params = {"cwd": str(turn.cwd), "mcpServers": []}
        if turn.native_session:
            capabilities = (connection.info.get("agentCapabilities") or {})
            for method, ok in (("session/resume", "resume" in (capabilities.get("sessionCapabilities") or {})),
                               ("session/load", capabilities.get("loadSession"))):
                if not ok:
                    continue
                with contextlib.suppress(AcpError):
                    await connection.call(method, {"sessionId": turn.native_session, **params}, timeout=60)
                    self.open_sessions[turn.native_session] = None
                    return turn.native_session, turn.prompt
        result = await connection.call("session/new", params, timeout=60)
        session_id = result["sessionId"]
        current = next((option.get("currentValue") for option in result.get("configOptions") or []
                        if option.get("id") == "model"), None)
        self.open_sessions[session_id] = current
        turn.on_session(session_id)
        return session_id, turn.fresh_prompt

    async def handle(self, turn: Turn, method: str, params: dict[str, Any]) -> Any:
        if method == "session/request_permission":
            request = permission_request(params, turn.cwd)
            try:
                decision = await turn.ask_permission(request)
            except asyncio.CancelledError:  # the turn was stopped while waiting
                return {"outcome": {"outcome": "cancelled"}}
            options = params.get("options") or []
            kinds = ("allow_once", "allow_always") if decision.outcome != "deny" else ("reject_once", "reject_always")
            option = pick_option(options, kinds)
            if option is None:
                return {"outcome": {"outcome": "cancelled"}}
            return {"outcome": {"outcome": "selected", "optionId": option}}
        if method == "fs/read_text_file":
            path = Path(params.get("path", ""))
            if not inside(turn.cwd, path):
                raise AcpError(-32602, "outside the project")
            text = path.read_text(encoding="utf-8", errors="replace")
            line, limit = params.get("line"), params.get("limit")
            if line or limit:
                lines = text.splitlines(keepends=True)
                start = max(0, (line or 1) - 1)
                text = "".join(lines[start:start + limit] if limit else lines[start:])
            return {"content": text}
        if method == "fs/write_text_file":
            path = Path(params.get("path", ""))
            if not inside(turn.cwd, path):
                raise AcpError(-32602, "outside the project")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params.get("content", ""), encoding="utf-8")
            return None
        raise MethodNotFound(method)

    async def run(self, turn: Turn) -> AsyncIterator[Update]:
        connection = await self.connect()
        self.active += 1
        session_id = None
        try:
            session_id, prompt = await self.session(connection, turn)
            if self.open_sessions.get(session_id) != turn.model:
                try:
                    await connection.call("session/set_config_option",
                                          {"sessionId": session_id, "configId": "model", "value": turn.model}, timeout=30)
                except AcpError as exc:
                    raise ProviderError("not_found", f"OpenCode でこのモデルを使えません: {turn.model}") from exc
                self.open_sessions[session_id] = turn.model
            queue = connection.listen(session_id, lambda method, params: self.handle(turn, method, params))
            prompting = asyncio.create_task(connection.call(
                "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]}))
            try:
                while True:
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait({getter, prompting}, return_when=asyncio.FIRST_COMPLETED)
                    if getter not in done:
                        getter.cancel()
                        break
                    message = getter.result()
                    if message is None:
                        break
                    if (update := session_update(message, turn.cwd)) is not None:
                        yield update
                while not queue.empty():
                    message = queue.get_nowait()
                    if message and (update := session_update(message, turn.cwd)) is not None:
                        yield update
                try:
                    result = await prompting
                except AcpError as exc:
                    raise ProviderError("unknown", exc.message or "OpenCode でエラーが起きました") from exc
            except asyncio.CancelledError:
                connection.notify("session/cancel", {"sessionId": session_id})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(prompting, 10)
                raise
            finally:
                connection.unlisten(session_id)
            usage = (result or {}).get("usage") or {}
            if (result or {}).get("stopReason") == "refusal":
                raise ProviderError("refused", "応答が拒否されました")
            yield usage_update(input_tokens=int(usage.get("inputTokens") or 0),
                               output_tokens=int(usage.get("outputTokens") or 0),
                               cache_read_tokens=int(usage.get("cachedReadTokens") or 0),
                               cache_write_tokens=int(usage.get("cachedWriteTokens") or 0),
                               reasoning_tokens=int(usage.get("thoughtTokens") or 0),
                               stop_reason=(result or {}).get("stopReason"))
        finally:
            self.active -= 1
            if turn.temporary and session_id:
                self.open_sessions.pop(session_id, None)
                await self.forget(session_id, turn.cwd)

    async def forget(self, native_session: str, cwd: Path) -> None:
        """OpenCode keeps sessions per project, so the deletion runs in the project's folder."""
        if not self.executable:
            return
        with contextlib.suppress(Exception):
            process = await asyncio.create_subprocess_exec(self.executable, "session", "delete", native_session,
                                                           cwd=cwd, env=self.base_env(),
                                                           stdout=asyncio.subprocess.DEVNULL,
                                                           stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(process.wait(), 30)

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
