"""Codex with the user's ChatGPT login, through `codex app-server` (JSON-RPC over stdio, one process).

Chat turns run in an empty folder with a read-only sandbox and no tools. Code turns run in the project with
the `workspace-write` sandbox and the `untrusted` approval policy, so Codex asks before every action and the
app's policy decides. Earlier turns another model wrote are handed over with `thread/inject_items`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import Settings
from ..events import Update, message_chunk, thought_chunk, usage_update
from ..providers.base import ModelInfo, ProviderError
from .base import PermissionRequest, Turn, inside
from .jsonrpc import JsonRpcProcess, RpcError

OUTPUT_LIMIT = 12_000
LIVE_OUTPUT = 4_000
ACCOUNT_SECONDS = 60
# Chat turns: no shell, apps or plugins; no web search
CHAT_FEATURES_OFF = ("shell_tool", "unified_exec", "plugins", "apps", "hooks", "browser_use", "computer_use", "view_image")
STATUS = {"inProgress": "in_progress", "completed": "completed", "failed": "failed", "declined": "failed"}
PLAN_STATUS = {"pending": "pending", "inProgress": "in_progress", "completed": "completed"}
WINDOW_LABEL = {300: "5時間", 10080: "週"}
ERRORS = {"usageLimitExceeded": "rate_limit", "rateLimitExceeded": "rate_limit", "unauthorized": "auth",
          "modelNotFound": "not_found", "serverOverloaded": "overloaded", "internalServerError": "overloaded",
          "contextWindowExceeded": "bad_request", "badRequest": "bad_request", "cyberPolicy": "refused",
          "misalignmentPolicyViolation": "refused"}


def find_executable() -> str | None:
    found = shutil.which("codex")
    local = Path.home() / ".local" / "bin" / "codex"
    return found or (str(local) if local.exists() else None)


def clip_tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def unwrap(command: str | None) -> str:
    """`/bin/bash -lc "npm test"` -> `npm test`."""
    if not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if len(parts) == 3 and parts[0].endswith(("bash", "sh", "zsh")) and parts[1] in ("-lc", "-c"):
        return parts[2]
    return command


def command_kind(actions: list[dict[str, Any]] | None) -> str:
    """Codex parses each command; reading and listing are "read", searching is "search", anything else runs."""
    kinds = {action.get("type") for action in actions or []}
    if not kinds or kinds - {"read", "listFiles", "search"}:
        return "execute"
    return "search" if kinds & {"search"} else "read"


def relative(path: str | None, cwd: Path) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(cwd.resolve()))
    except (ValueError, OSError):
        return path


def change_diff(changes: list[dict[str, Any]], cwd: Path) -> str:
    parts = []
    for change in changes:
        path = relative(change.get("path"), cwd) or "file"
        kind = (change.get("kind") or {}).get("type")
        old = "/dev/null" if kind == "add" else f"a/{path}"
        new = "/dev/null" if kind == "delete" else f"b/{path}"
        parts.append(f"--- {old}\n+++ {new}\n{change.get('diff', '').rstrip()}\n")
    return "".join(parts)


def item_update(kind: str, item: dict[str, Any], cwd: Path, output: str | None = None) -> Update | None:
    """A Codex thread item as an ACP-shaped tool_call / tool_call_update."""
    item_type = item.get("type")
    base: Update = {"sessionUpdate": kind, "toolCallId": item.get("id"), "status": STATUS.get(item.get("status"), "in_progress")}
    if item_type == "commandExecution":
        actions = item.get("commandActions") or []
        command = unwrap(item.get("command"))
        tool_kind = command_kind(actions)
        paths = [relative(action.get("path"), cwd) for action in actions if action.get("path")]
        base.update(kind=tool_kind, title=command, command=command if tool_kind == "execute" else None,
                    path=paths[0] if paths and tool_kind != "execute" else None)
        text = item.get("aggregatedOutput") if item.get("aggregatedOutput") is not None else output
        if text:
            base["output"] = clip_tail(text, OUTPUT_LIMIT)
        if item.get("exitCode") not in (None, 0) and base["status"] == "completed":
            base["status"] = "failed"
    elif item_type == "fileChange":
        changes = item.get("changes") or []
        kinds = {(change.get("kind") or {}).get("type") for change in changes}
        path = relative(changes[0].get("path"), cwd) if changes else None
        base.update(kind="delete" if kinds == {"delete"} else "edit", title=path or "files", path=path,
                    diff=change_diff(changes, cwd)[:40_000])
    elif item_type == "mcpToolCall":
        base.update(kind="other", title=f"{item.get('server')}.{item.get('tool')}")
    elif item_type == "webSearch":
        base.update(kind="fetch", title=f"Web検索: {item.get('query') or ''}")
    else:
        return None
    return {key: value for key, value in base.items() if value is not None}


def response_item(item: dict[str, str]) -> dict[str, Any]:
    kind = "input_text" if item["role"] == "user" else "output_text"
    return {"type": "message", "role": item["role"], "content": [{"type": kind, "text": item["text"]}]}


def turn_error(error: dict[str, Any] | None) -> ProviderError:
    error = error or {}
    info = error.get("codexErrorInfo")
    code = ERRORS.get(info, "unknown") if isinstance(info, str) else "network" if info else "unknown"
    messages = {"rate_limit": "ChatGPT プランの利用上限に達しました", "auth": "Codex にログインしてください（codex login）"}
    return ProviderError(code, messages.get(code) or error.get("message") or "Codex でエラーが起きました")


class CodexAgent:
    id = "codex"
    label = "Codex"

    def __init__(self, settings: Settings, executable: str | None = None) -> None:
        self.settings = settings
        self.executable = executable or find_executable()
        self.connection: JsonRpcProcess | None = None
        self.lock = asyncio.Lock()
        # Loaded threads and the (mode, folder) they were set up for
        self.open_threads: dict[str, tuple[str, str]] = {}
        self.account: dict[str, Any] | None = None
        self.limits: dict[str, Any] | None = None
        self.account_at = 0.0
        self.models_cache: list[ModelInfo] | None = None

    def installed(self) -> bool:
        return self.executable is not None

    def env(self) -> dict[str, str]:
        directory = str(Path(self.executable).parent) if self.executable else ""
        return {"HOME": os.environ.get("HOME", str(Path.home())), "LANG": "C.UTF-8",
                "PATH": ":".join(dict.fromkeys([directory, "/usr/local/bin", "/usr/bin", "/bin"]))}

    def _notice(self, method: str, params: dict[str, Any]) -> None:
        if method == "account/rateLimits/updated":
            self.limits = params.get("rateLimits") or self.limits

    async def connect(self) -> JsonRpcProcess:
        async with self.lock:
            if self.connection and self.connection.alive:
                return self.connection
            if not self.executable:
                raise ProviderError("not_found", "Codex がインストールされていません")
            connection = JsonRpcProcess([self.executable, "app-server"], self.env(), Path.home(),
                                        route=lambda params: params.get("threadId"), on_notification=self._notice)
            try:
                await connection.spawn()
                await connection.call("initialize", {"clientInfo": {"name": "omni_llm_workspace", "title": "OmniLLMWorkspace",
                                                                    "version": __version__}}, timeout=60)
                connection.notify("initialized", {})
            except (RpcError, OSError) as exc:
                await connection.close()
                raise ProviderError("unknown", f"Codex を起動できません: {exc}") from exc
            self.connection, self.open_threads = connection, {}
            return connection

    async def refresh_account(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.account_at < ACCOUNT_SECONDS:
            return
        connection = await self.connect()
        account = await connection.call("account/read", {}, timeout=30)
        self.account = (account or {}).get("account")
        if self.account:
            with contextlib.suppress(RpcError):
                self.limits = ((await connection.call("account/rateLimits/read", {}, timeout=30)) or {}).get("rateLimits")
        self.account_at = time.monotonic()

    def usage(self) -> dict[str, Any] | None:
        """The plan's windows for the picker and the meter: [{"label", "usedPercent", "resetsAt"}]."""
        if not self.limits:
            return None
        windows = []
        for key in ("primary", "secondary"):
            window = self.limits.get(key)
            if window:
                minutes = window.get("windowDurationMins")
                windows.append({"label": WINDOW_LABEL.get(minutes, f"{minutes}分" if minutes else key),
                                "usedPercent": window.get("usedPercent"), "resetsAt": window.get("resetsAt")})
        return {"plan": (self.account or {}).get("planType"), "windows": windows}

    async def list_models(self) -> list[ModelInfo]:
        if self.models_cache is not None:
            return self.models_cache
        connection = await self.connect()
        result = await connection.call("model/list", {}, timeout=60)
        models = []
        for item in (result or {}).get("data", []):
            if item.get("hidden"):
                continue
            efforts = tuple(option.get("reasoningEffort") for option in item.get("supportedReasoningEfforts") or []
                            if option.get("reasoningEffort"))
            info = ModelInfo(id=item.get("model") or item["id"], name=item.get("displayName") or item["id"],
                             reasoning=bool(efforts), efforts=efforts)
            models.insert(0, info) if item.get("isDefault") else models.append(info)
        self.models_cache = models
        return models

    def thread_settings(self, turn: Turn) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if turn.mode == "chat":
            settings = {"sandbox": "read-only", "approvalPolicy": "never"}
            config["features"] = {name: False for name in CHAT_FEATURES_OFF}
            config["web_search"] = "disabled"
        else:
            settings = {"sandbox": "workspace-write", "approvalPolicy": "untrusted"}
            config["sandbox_workspace_write"] = {"network_access": self.settings.omni_allow_network}
        if turn.temporary:
            config.setdefault("features", {})["memories"] = False
            config["history"] = {"persistence": "none"}
        return {**settings, "cwd": str(turn.cwd), "config": config}

    async def thread(self, connection: JsonRpcProcess, turn: Turn) -> tuple[str, list[dict[str, str]]]:
        """The thread id and the earlier turns to inject into it."""
        settings = self.thread_settings(turn)
        setup = (turn.mode, str(turn.cwd))
        loaded = self.open_threads.get(turn.native_session or "")
        if loaded == setup:
            return turn.native_session, turn.unseen
        # A thread set up for another mode or folder (Chat -> Code) is not reused: its sandbox would be wrong
        if turn.native_session and loaded is None and not turn.temporary:
            with contextlib.suppress(RpcError):
                await connection.call("thread/resume", {"threadId": turn.native_session, **settings}, timeout=60)
                self.open_threads[turn.native_session] = setup
                return turn.native_session, turn.unseen
        result = await connection.call("thread/start", {**settings, "model": turn.model, "ephemeral": turn.temporary},
                                       timeout=60)
        thread_id = result["thread"]["id"]
        self.open_threads[thread_id] = setup
        if not turn.temporary:
            turn.on_session(thread_id)
        return thread_id, turn.earlier

    async def handle(self, turn: Turn, items: dict[str, dict[str, Any]], method: str, params: dict[str, Any]) -> Any:
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            item = items.get(params.get("itemId", ""), {})
            if method.startswith("item/commandExecution"):
                actions = params.get("commandActions") or item.get("commandActions")
                command = unwrap(params.get("command") or item.get("command"))
                cwd = params.get("cwd") or str(turn.cwd)
                kind = command_kind(actions)
                paths = [action["path"] for action in actions or [] if action.get("path")]
                request = PermissionRequest(tool_call_id=params.get("itemId", ""), kind=kind, title=command,
                                            command=command, paths=[relative(path, turn.cwd) or path for path in paths],
                                            outside_project=not inside(turn.cwd, cwd)
                                            or any(not inside(turn.cwd, Path(cwd) / path) for path in paths))
            else:
                changes = item.get("changes") or []
                paths = [change.get("path", "") for change in changes]
                root = params.get("grantRoot")
                request = PermissionRequest(tool_call_id=params.get("itemId", ""), kind="edit",
                                            title=", ".join(relative(path, turn.cwd) or path for path in paths) or "files",
                                            paths=[relative(path, turn.cwd) or path for path in paths],
                                            diff=change_diff(changes, turn.cwd)[:40_000] or None,
                                            outside_project=any(not inside(turn.cwd, path) for path in paths)
                                            or bool(root and not inside(turn.cwd, root)))
            try:
                decision = await turn.ask_permission(request)
            except asyncio.CancelledError:
                return {"decision": "cancel"}
            return {"decision": "decline" if decision.outcome == "deny" else "accept"}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}}  # no extra sandbox permissions; .env decides those
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline"}
        if method in ("execCommandApproval", "applyPatchApproval"):
            return {"decision": "denied"}
        raise RpcError(-32601, f"not supported: {method}")

    async def run(self, turn: Turn) -> AsyncIterator[Update]:
        connection = await self.connect()
        thread_id, inject = await self.thread(connection, turn)
        items: dict[str, dict[str, Any]] = {}
        queue = connection.listen(thread_id, lambda method, params: self.handle(turn, items, method, params))
        turn_id = None
        try:
            if inject:
                await connection.call("thread/inject_items", {"threadId": thread_id,
                                                              "items": [response_item(item) for item in inject]}, timeout=60)
            params: dict[str, Any] = {"threadId": thread_id, "input": [{"type": "text", "text": turn.text}],
                                      "model": turn.model}
            if turn.effort:
                params["effort"] = turn.effort
            try:
                started = await connection.call("turn/start", params, timeout=60)
            except RpcError as exc:
                raise ProviderError("bad_request", exc.message) from exc
            turn_id = (started or {}).get("turn", {}).get("id")
            outputs: dict[str, str] = {}
            streamed: set[str] = set()
            failure: dict[str, Any] | None = None
            usage: Update | None = None
            while True:
                message = await queue.get()
                if message is None:
                    raise ProviderError("unknown", "Codex が終了しました")
                method, data = message["method"], message["params"]
                if method == "item/agentMessage/delta":
                    streamed.add(data.get("itemId", ""))
                    yield message_chunk(data.get("delta", ""))
                elif method == "item/reasoning/summaryTextDelta":
                    yield thought_chunk(data.get("delta", ""))
                elif method == "item/reasoning/summaryPartAdded":
                    yield thought_chunk("\n\n")
                elif method in ("item/started", "item/completed"):
                    item = data.get("item") or {}
                    items[item.get("id", "")] = item
                    if item.get("type") == "agentMessage" and method == "item/completed" and item.get("id") not in streamed:
                        if item.get("text"):
                            yield message_chunk(item["text"])
                    update = item_update("tool_call" if method == "item/started" else "tool_call_update", item,
                                         turn.cwd, outputs.get(item.get("id", "")))
                    if update:
                        yield update
                elif method == "item/commandExecution/outputDelta":
                    item_id = data.get("itemId", "")
                    outputs[item_id] = clip_tail(outputs.get(item_id, "") + data.get("delta", ""), LIVE_OUTPUT)
                    yield {"sessionUpdate": "tool_call_update", "toolCallId": item_id, "output": outputs[item_id]}
                elif method == "turn/plan/updated":
                    entries = [{"content": step.get("step", ""), "status": PLAN_STATUS.get(step.get("status"), "pending")}
                               for step in data.get("plan") or []]
                    yield {"sessionUpdate": "plan", "entries": entries}
                elif method == "thread/tokenUsage/updated":
                    token_usage = data.get("tokenUsage") or {}
                    last = token_usage.get("last") or {}
                    usage = usage_update(input_tokens=int(last.get("inputTokens") or 0),
                                         output_tokens=int(last.get("outputTokens") or 0),
                                         cache_read_tokens=int(last.get("cachedInputTokens") or 0),
                                         reasoning_tokens=int(last.get("reasoningOutputTokens") or 0),
                                         context_window=token_usage.get("modelContextWindow"))
                    usage["contextUsed"] = last.get("totalTokens")
                elif method == "error" and not data.get("willRetry"):
                    failure = data.get("error")
                elif method == "turn/completed":
                    finished = data.get("turn") or {}
                    if finished.get("status") == "failed":
                        raise turn_error(finished.get("error") or failure)
                    break
            if usage:
                yield usage
        except asyncio.CancelledError:
            if turn_id:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(connection.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}), 10)
            raise
        finally:
            connection.unlisten(thread_id)
            if turn.temporary:
                self.open_threads.pop(thread_id, None)
            with contextlib.suppress(Exception):
                await self.refresh_account(force=True)

    async def forget(self, native_session: str, cwd: Path) -> None:
        with contextlib.suppress(Exception):
            connection = await self.connect()
            await connection.call("thread/delete", {"threadId": native_session}, timeout=30)

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
