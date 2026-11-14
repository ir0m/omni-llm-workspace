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
        # A ChatGPT sign-in in progress: {"id", "code", "url", "error", "done"}
        self.login: dict[str, Any] | None = None
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
        elif method == "account/login/completed":
            if self.login and params.get("loginId") in (None, self.login.get("id")):
                self.login.update(done=True, error=None if params.get("success") else (params.get("error") or "ログインできませんでした"))
            self.account_at = 0.0
        elif method == "account/updated":
            self.account_at = 0.0

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

    async def start_login(self) -> dict[str, Any]:
        """Signs in with a device code: the user opens the URL on any device and enters the code there,
        so it works from a phone. The app never sees the ChatGPT credentials."""
        await self.cancel_login()
        connection = await self.connect()
        result = await connection.call("account/login/start", {"type": "chatgptDeviceCode"}, timeout=60) or {}
        self.login = {"id": result.get("loginId"), "code": result.get("userCode"), "url": result.get("verificationUrl"),
                      "error": None, "done": False}
        return self.login

    async def cancel_login(self) -> None:
        login, self.login = self.login, None
        if login and login.get("id") and not login.get("done") and self.connection and self.connection.alive:
            with contextlib.suppress(RpcError, asyncio.TimeoutError):
                await self.connection.call("account/login/cancel", {"loginId": login["id"]}, timeout=30)

    async def logout(self) -> None:
        connection = await self.connect()
        await connection.call("account/logout", None, timeout=30)
        self.account, self.limits, self.account_at, self.login = None, None, 0.0, None

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

    async def list_sessions(self, cwd: Path):
        from .session_history import SessionInfo

        connection = await self.connect()
        found = {}
        # The registered project can be a symlink, while the CLI records the real path.
        for directory in dict.fromkeys([str(cwd), str(cwd.resolve())]):
            cursor = None
            while True:
                result = await connection.call("thread/list", {
                    "cwd": directory, "limit": 100, "cursor": cursor, "sortKey": "updated_at",
                    "sourceKinds": ["cli", "vscode", "exec", "appServer"], "modelProviders": [],
                }, timeout=30)
                for item in result.get("data") or []:
                    if item.get("ephemeral") or Path(item.get("cwd", "/")).resolve() != cwd.resolve():
                        continue
                    found[item["id"]] = SessionInfo(item["id"], item.get("name") or item.get("preview") or "無題",
                        item["cwd"], item.get("createdAt", 0) * 1000, item.get("updatedAt", 0) * 1000,
                        item.get("model"))
                next_cursor = result.get("nextCursor")
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
        return list(found.values())

    async def session_history(self, native_id: str, cwd: Path):
        from .session_history import codex_history

        connection = await self.connect()
        result = await connection.call("thread/read", {"threadId": native_id}, timeout=30)
        thread = result["thread"]
        if Path(thread["cwd"]).resolve() != cwd.resolve():
            raise ProviderError("bad_request", "セッションの作業フォルダがプロジェクトと一致しません")
        turns, cursor = [], None
        while True:
            try:
                page = await connection.call("thread/turns/list", {"threadId": native_id, "limit": 100,
                    "cursor": cursor, "sortDirection": "asc", "itemsView": "full"}, timeout=30)
            except RpcError as exc:
                if exc.code != -32601:
                    raise
                legacy = await connection.call("thread/read", {"threadId": native_id, "includeTurns": True}, timeout=30)
                turns = legacy["thread"].get("turns") or []
                break
            turns.extend(page.get("data") or [])
            next_cursor = page.get("nextCursor")
            if not next_cursor:
                break
            if next_cursor == cursor:
                raise ProviderError("bad_request", "履歴の続きを取得できませんでした")
            cursor = next_cursor
        thread["turns"] = turns
        if any(turn.get("itemsView", "full") != "full" for turn in turns):
            raise ProviderError("bad_request", "このセッションの履歴を完全に取得できませんでした")
        return codex_history(thread, cwd)

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
        if turn.require_resume:
            try:
                await connection.call("thread/resume", {"threadId": turn.native_session, **settings}, timeout=60)
            except RpcError as exc:
                raise ProviderError("not_found", "元のセッションを再開できませんでした") from exc
            self.open_threads[turn.native_session] = setup
            return turn.native_session, turn.unseen
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
        if turn.require_resume:
            # A fresh process resumes from disk, including work done in the CLI since the last app turn.
            # Closing it also avoids keeping a second, stale in-memory copy of the original session.
            dedicated = CodexAgent(self.settings, executable=self.executable)
            try:
                async for update in dedicated._run(turn):
                    yield update
            finally:
                if dedicated.account:
                    self.account, self.limits, self.account_at = dedicated.account, dedicated.limits, dedicated.account_at
                await dedicated.close()
        else:
            async for update in self._run(turn):
                yield update

    async def _run(self, turn: Turn) -> AsyncIterator[Update]:
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
            if turn_id and turn.on_turn_id:
                turn.on_turn_id(turn_id)
            async def steer(text: str) -> bool:
                try:
                    await connection.call("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                        "input": [{"type": "text", "text": text}]}, timeout=30)
                    return True
                except RpcError as exc:
                    if exc.code in (-32600, -32601, -32602):  # explicitly rejected, not an uncertain transport failure
                        return False
                    raise

            if turn_id and turn.on_input_ready:
                turn.on_input_ready(steer)
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
                    if finished.get("status") == "interrupted":
                        raise asyncio.CancelledError
                    break
            if usage:
                yield usage
        except asyncio.CancelledError:
            if turn_id:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(connection.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}), 10)
            raise
        finally:
            if turn.on_input_ready:
                turn.on_input_ready(None)
            connection.unlisten(thread_id)
            if turn.temporary:
                self.open_threads.pop(thread_id, None)
            with contextlib.suppress(Exception):
                await self.refresh_account(force=True)

    async def rename_session(self, native_id: str, cwd: Path, title: str) -> None:
        connection = await self.connect()
        await connection.call("thread/name/set", {"threadId": native_id, "name": title}, timeout=30)

    async def fork_session(self, native_id: str, cwd: Path, title: str, last_turn: str | None) -> str:
        connection = await self.connect()
        params = {"threadId": native_id, "cwd": str(cwd.resolve()), "approvalPolicy": "untrusted",
                  "sandbox": "workspace-write", "excludeTurns": True}
        if last_turn:
            params["lastTurnId"] = last_turn
        result = await connection.call("thread/fork", params, timeout=30)
        new_id = result["thread"]["id"]
        try:
            await self.rename_session(new_id, cwd, title)
        except Exception:
            await self.delete_session(new_id, cwd)
            raise
        return new_id

    async def delete_session(self, native_id: str, cwd: Path) -> None:
        connection = await self.connect()
        await connection.call("thread/delete", {"threadId": native_id}, timeout=30)
        self.open_threads.pop(native_id, None)

    async def compact(self, native_id: str, cwd: Path) -> None:
        dedicated = CodexAgent(self.settings, executable=self.executable)
        try:
            connection = await dedicated.connect()
            await connection.call("thread/resume", {"threadId": native_id}, timeout=60)
            async def deny(method, params):
                return {"decision": "decline"}
            queue = connection.listen(native_id, deny)
            try:
                async with asyncio.timeout(120):
                    await connection.call("thread/compact/start", {"threadId": native_id}, timeout=30)
                    compacted = False
                    while True:
                        event = await queue.get()
                        if event is None:
                            raise ProviderError("network", "Codex との接続が切れました")
                        if event["method"] == "item/completed" and event["params"].get("item", {}).get("type") == "contextCompaction":
                            compacted = True
                        if event["method"] == "turn/completed":
                            if not compacted or event["params"].get("turn", {}).get("status") != "completed":
                                raise ProviderError("unknown", "圧縮が完了しませんでした")
                            break
            finally:
                connection.unlisten(native_id)
            # The persistent thread has changed; the next turn must reload it.
            self.open_threads.pop(native_id, None)
        finally:
            await dedicated.close()

    async def forget(self, native_session: str, cwd: Path) -> None:
        with contextlib.suppress(Exception):
            connection = await self.connect()
            await connection.call("thread/delete", {"threadId": native_session}, timeout=30)

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
