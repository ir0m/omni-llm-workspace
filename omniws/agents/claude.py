"""Claude Code through the Claude Agent SDK.

Two executors share this adapter:
- `claude_code`: the CLI's own login (a Claude subscription). Only when OMNI_ALLOW_CLAUDE_SUBSCRIPTION is set:
  Anthropic does not allow apps to collect or intermediate Claude.ai credentials, so the app never sees them,
  and ANTHROPIC_API_KEY is blanked so the turn can't fall back to API billing.
- `claude_code_api`: ANTHROPIC_API_KEY (pay as you go).

Code turns use Claude Code's own system prompt and tools in the project, plus the project's CLAUDE.md; its
settings files (allow rules, hooks) are not loaded, so every permission goes through the app's policy.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import claude_agent_sdk as sdk

from ..config import Settings
from ..events import Update, message_chunk, thought_chunk, usage_update
from ..providers.base import ModelInfo, ProviderError
from .base import PermissionRequest, Turn, inside

OUTPUT_LIMIT = 12_000
INFO_SECONDS = 600
CLAUDE_MD_LIMIT = 40_000
KINDS = {"Read": "read", "Glob": "search", "Grep": "search", "LS": "read", "Edit": "edit", "MultiEdit": "edit",
         "Write": "edit", "NotebookEdit": "edit", "Bash": "execute", "BashOutput": "read", "KillShell": "execute",
         "WebFetch": "fetch", "WebSearch": "fetch"}
WINDOWS = {"five_hour": "5時間", "seven_day": "7日", "seven_day_opus": "7日（Opus）", "seven_day_sonnet": "7日（Sonnet）"}
# `claude -p /usage` (the CLI's own report, no model call): "Current session: 71% used · resets Nov 14, 4pm (Asia/Tokyo)"
USAGE_LINE = re.compile(r"^Current (session|week \(([^)]+)\)):\s*(\d+)% used(?:\s*·\s*resets (.+))?$")
RESET = re.compile(r"^(?:([A-Z][a-z]{2}) (\d{1,2}), )?(\d{1,2})(?::(\d{2}))?\s*(am|pm) \(([^)]+)\)$")
USAGE_SECONDS = 45
ERRORS = {401: "auth", 403: "auth", 404: "not_found", 429: "rate_limit", 529: "overloaded", 500: "overloaded"}


# One JSON message from the CLI can carry a whole PDF or image as base64: the attachments of a message
# (up to 50 MiB, a third more as base64) or a PDF the Read tool loaded. The SDK's default of 1 MiB stops
# such a turn with "JSON message exceeded maximum buffer size".
MAX_MESSAGE_BYTES = 128 * 1024 * 1024

# Background work the turn waits for. A turn's process ends with the turn, and ending it kills what still runs:
# background agents (the model said it would wait for their results) are waited for, as the SDK itself does;
# background shells (dev servers, watchers) may never end, so they are not.
WAITED_TASK_TYPES = frozenset({"local_agent", "local_workflow"})
BACKGROUND_WAIT = 30 * 60  # seconds, after the model's reply, before giving up on them
TASK_DONE = {"completed": "completed", "failed": "failed", "stopped": "failed", "killed": "failed"}

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


def tool_fields(name: str, data: dict[str, Any], cwd: Path) -> dict[str, Any]:
    """What a tool call shows: kind, title, command, path and a diff for edits."""
    kind = KINDS.get(name, "other")
    path = relative(data.get("file_path") or data.get("notebook_path") or data.get("path"), cwd)
    fields: dict[str, Any] = {"kind": kind, "title": name, "path": path}
    if name == "Bash":
        fields.update(command=data.get("command"), title=data.get("description") or data.get("command") or name)
    elif name in ("Glob", "Grep"):
        fields["title"] = f"{name}: {data.get('pattern', '')}"
    elif name == "Edit":
        fields["diff"] = unified(path or "file", str(data.get("old_string", "")), str(data.get("new_string", "")))
    elif name == "MultiEdit":
        fields["diff"] = "".join(unified(path or "file", str(edit.get("old_string", "")), str(edit.get("new_string", "")))
                                 for edit in data.get("edits") or [])
    elif name == "Write":
        old = ""
        with contextlib.suppress(OSError):
            target = Path(data.get("file_path", ""))
            if target.is_file() and target.stat().st_size < 2_000_000:
                old = target.read_text(encoding="utf-8", errors="replace")
        fields["diff"] = unified(path or "file", old, str(data.get("content", "")))
    elif name in ("WebFetch", "WebSearch"):
        fields["title"] = f"{name}: {data.get('url') or data.get('query') or ''}"
    elif name == "EnterPlanMode":
        fields["title"] = "計画モードに切り替え"
    elif name == "ExitPlanMode":
        fields["title"] = "計画を提示"
    elif name in ("Task", "Agent"):
        fields["title"] = f"サブエージェント: {data.get('description', '')}"
    if "diff" in fields:
        fields["diff"] = clip(fields["diff"], 40_000)
    return {key: value for key, value in fields.items() if value}


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")
    return ""


def plan_update(data: dict[str, Any]) -> Update:
    return {"sessionUpdate": "plan", "entries": [{"content": todo.get("content", ""), "status": todo.get("status", "pending"),
                                                  "priority": "medium"} for todo in data.get("todos") or [] if isinstance(todo, dict)]}


def expand_imports(text: str, cwd: Path) -> str:
    """Lines that are just `@path` (CLAUDE.md imports) are replaced by that file, if it is inside the project."""
    lines = []
    for line in text.splitlines():
        target = line.strip()
        if target.startswith("@") and " " not in target and len(target) > 1:
            path = (cwd / target[1:]).resolve()
            if inside(cwd, path) and path.is_file():
                lines.append(path.read_text(encoding="utf-8", errors="replace"))
                continue
        lines.append(line)
    return "\n".join(lines)


def claude_md(cwd: Path) -> str | None:
    """The project's CLAUDE.md (and CLAUDE.local.md), the only part of the project's Claude settings that is used."""
    parts = []
    for name in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
        path = cwd / name
        if path.is_file():
            content = expand_imports(path.read_text(encoding="utf-8", errors="replace"), cwd)
            parts.append(f"Contents of {name} (project instructions):\n\n{content}")
    text = "\n\n".join(parts)
    return text[:CLAUDE_MD_LIMIT] if text else None


def parse_reset(text: str, now: float) -> int | None:
    match = RESET.match(text.strip())
    if not match:
        return None
    month, day, hour, minute, half, zone = match.groups()
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    current = datetime.fromtimestamp(now, tz)
    if not 1 <= int(hour) <= 12 or not 0 <= int(minute or 0) <= 59:
        return None
    hour24 = int(hour) % 12 + (12 if half == "pm" else 0)
    if month:
        try:
            when = datetime.strptime(f"{current.year} {month} {day}", "%Y %b %d").replace(
                hour=hour24, minute=int(minute or 0), tzinfo=tz)
        except ValueError:
            return None
        if when < current - timedelta(days=1):  # "Jan 2" seen in late December
            try:
                when = when.replace(year=current.year + 1)
            except ValueError:  # February 29 in a non-leap year
                return None
    else:
        when = current.replace(hour=hour24, minute=int(minute or 0), second=0, microsecond=0)
        if when < current:
            when += timedelta(days=1)
    return int(when.timestamp())


def parse_usage(text: str, now: float) -> dict[str, dict[str, Any]]:
    """{"five_hour": {...}, "seven_day": {...}, "seven_day_fable": {...}} from the CLI's /usage report."""
    windows: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        match = USAGE_LINE.match(line.strip())
        if not match:
            continue
        kind, scope, percent, reset = match.groups()
        if kind == "session":
            key, label = "five_hour", WINDOWS["five_hour"]
        elif scope == "all models":
            key, label = "seven_day", WINDOWS["seven_day"]
        else:
            key, label = f"seven_day_{re.sub(r'[^a-z0-9]+', '_', scope.lower())}", f"7日（{scope}）"
        windows[key] = {"usedPercent": int(percent), "resetsAt": parse_reset(reset, now) if reset else None,
                        "status": None, "at": int(now), "label": label}
    return windows


class ClaudeCodeAgent:
    label = "Claude Code"

    def __init__(self, settings: Settings, billing: str, cli_path: str | None = None) -> None:
        self.settings = settings
        self.billing = billing  # "subscription" | "api"
        self.id = "claude_code" if billing == "subscription" else "claude_code_api"
        self.cli_path = cli_path
        self.info: dict[str, Any] | None = None
        self.info_at = 0.0
        self.usage_file = settings.omni_data_dir / "claude-usage.json"
        self.refreshing: asyncio.Task | None = None
        self.refresh_pending = False

    def installed(self) -> bool:
        if self.billing == "subscription":
            return self.settings.omni_allow_claude_subscription
        return self.settings.anthropic_api_key is not None

    def env(self, temporary: bool = False) -> dict[str, str]:
        """Blanks every inherited Anthropic/Claude variable so nothing from the server's environment (a key, a proxy,
        a parent Claude Code session) decides how the turn is billed or routed."""
        env = {key: "" for key in os.environ if key.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE"))}
        if self.billing == "api" and self.settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key.get_secret_value()
        if temporary:
            env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", CLAUDE_CODE_SKIP_PROMPT_HISTORY="1")
        return env

    def options(self, **values: Any) -> sdk.ClaudeAgentOptions:
        base: dict[str, Any] = {"setting_sources": [], "strict_mcp_config": True, "include_partial_messages": True,
                                "max_buffer_size": MAX_MESSAGE_BYTES}
        if self.cli_path:
            base["cli_path"] = self.cli_path
        return sdk.ClaudeAgentOptions(**{**base, **values})

    async def server_info(self, refresh: bool = False) -> dict[str, Any]:
        if self.info is not None and not refresh and time.monotonic() - self.info_at < INFO_SECONDS:
            return self.info
        options = self.options(cwd=str(Path.home()), tools=[], env=self.env(temporary=True),
                               extra_args={"no-session-persistence": None})
        try:
            async with sdk.ClaudeSDKClient(options=options) as client:
                self.info = await client.get_server_info() or {}
        except (sdk.ClaudeSDKError, OSError) as exc:
            raise ProviderError("unknown", f"Claude Code を起動できません: {exc}") from exc
        self.info_at = time.monotonic()
        return self.info

    async def plan(self) -> str | None:
        """The subscription's name (e.g. "Claude Pro"), or None when the CLI isn't signed in."""
        account = (await self.server_info()).get("account") or {}
        return account.get("subscriptionType")

    async def list_models(self) -> list[ModelInfo]:
        models = []
        for item in (await self.server_info()).get("models") or []:
            value = item.get("value")
            if not value or value == "default":
                continue
            models.append(ModelInfo(id=value, name=item.get("displayName") or value,
                                    efforts=tuple(item.get("supportedEffortLevels") or ()),
                                    reasoning=bool(item.get("supportsAdaptiveThinking")),
                                    extra={"resolved": item.get("resolvedModel")}))
        return models

    async def list_sessions(self, cwd: Path):
        from .session_history import SessionInfo

        items = await asyncio.to_thread(sdk.list_sessions, directory=str(cwd.resolve()), include_worktrees=False)
        return [SessionInfo(item.session_id, item.custom_title or item.summary or "無題", item.cwd or str(cwd),
                            item.created_at or item.last_modified, item.last_modified)
                for item in items if not item.cwd or Path(item.cwd).resolve() == cwd.resolve()]

    async def session_history(self, native_id: str, cwd: Path):
        from .session_history import claude_history

        sessions = await self.list_sessions(cwd)
        if not any(item.id == native_id for item in sessions):
            raise ProviderError("not_found", "元のセッションが見つかりません")
        messages = await asyncio.to_thread(sdk.get_session_messages, native_id, directory=str(cwd.resolve()))
        return claude_history(messages, cwd)

    def usage(self) -> dict[str, Any] | None:
        """The last rate-limit windows Claude Code reported, and when (they only arrive during turns)."""
        try:
            data = json.loads(self.usage_file.read_text())
        except (OSError, ValueError):
            return None
        # Claude sends a percentage only when usage is high; otherwise just a status
        windows = [{"label": value.get("label") or WINDOWS.get(key, key), "usedPercent": value.get("usedPercent"), "resetsAt": value.get("resetsAt"),
                    "status": value.get("status"), "at": value.get("at")} for key, value in data.get("windows", {}).items()]
        return {"plan": data.get("plan"), "windows": windows} if windows else None

    def cli(self) -> str | None:
        bundled = Path(sdk.__file__).parent / "_bundled" / "claude"
        return self.cli_path or (str(bundled) if bundled.is_file() else shutil.which("claude"))

    async def refresh_usage(self) -> None:
        """Asks the CLI itself for the plan's usage (`/usage` makes no model call), so the meter is exact after
        every turn. Only the CLI touches the login; the app reads the text it prints."""
        if self.billing != "subscription" or not self.installed() or not (cli := self.cli()):
            return
        try:
            process = await asyncio.create_subprocess_exec(
                cli, "-p", "/usage", "--output-format", "json", "--no-session-persistence",
                cwd=str(Path.home()), env={**os.environ, **self.env(temporary=True)},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(process.communicate(), USAGE_SECONDS)
            except TimeoutError:
                return
            finally:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            if process.returncode != 0:
                return
            text = json.loads(out).get("result")
            if not isinstance(text, str):
                return
        except (OSError, ValueError, AttributeError):
            return
        windows = parse_usage(text, time.time())
        if not windows:
            return
        try:
            data = json.loads(self.usage_file.read_text())
        except (OSError, ValueError):
            data = {}
        data["windows"] = windows  # the full report replaces what turns sent piecemeal
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(json.dumps(data, ensure_ascii=False))

    def remember_limit(self, info: Any) -> None:
        if getattr(info, "rate_limit_type", None) not in WINDOWS:
            return
        try:
            data = json.loads(self.usage_file.read_text())
        except (OSError, ValueError):
            data = {}
        utilization = getattr(info, "utilization", None)
        data.setdefault("windows", {})[info.rate_limit_type] = {
            "usedPercent": round(utilization * 100) if isinstance(utilization, (int, float)) else None,
            "resetsAt": getattr(info, "resets_at", None), "status": getattr(info, "status", None), "at": int(time.time())}
        if self.info and (self.info.get("account") or {}).get("subscriptionType"):
            data["plan"] = self.info["account"]["subscriptionType"]
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(json.dumps(data))

    def secret_reads(self) -> list[str]:
        """Claude Code reads files inside the project without asking, so the app can't show these to the user first.
        Deny reading secrets and the app's data outright (the rules also cover Grep and Glob)."""
        data = self.settings.omni_data_dir.resolve()
        return ["Read(**/.env)", "Read(**/.env.local)", "Read(**/.env.*.local)", "Read(**/.env.production)",
                "Read(**/*.pem)", "Read(**/*.key)", f"Read(/{data}/**)"]

    def permission_handler(self, turn: Turn):
        async def can_use_tool(name: str, data: dict[str, Any], context: Any):
            if name in ("TodoWrite", "EnterPlanMode"):  # entering plan mode only narrows what the agent may do
                return sdk.PermissionResultAllow()
            if name == "ExitPlanMode":
                # The plan itself, for the user to approve (always asked, whatever the approval mode)
                request = PermissionRequest(tool_call_id=getattr(context, "tool_use_id", None) or "", kind="plan",
                                            title="計画", plan=str(data.get("plan") or "")[:100_000] or None)
                try:
                    decision = await turn.ask_permission(request)
                except asyncio.CancelledError:
                    return sdk.PermissionResultDeny(message="The user stopped this turn.", interrupt=True)
                if decision.outcome == "deny":
                    # Said plainly, so the agent takes the user's words as theirs and not as text from a tool
                    request_text = f" The user asked for these changes: {decision.message}" if decision.message else ""
                    return sdk.PermissionResultDeny(message="The user reviewed the plan and did not approve it yet."
                                                    + request_text + " Revise the plan and present it again with ExitPlanMode.")
                return sdk.PermissionResultAllow()
            fields = tool_fields(name, data, turn.cwd)
            paths = [path for path in [data.get("file_path"), data.get("notebook_path"), data.get("path")] if path]
            blocked = getattr(context, "blocked_path", None)
            request = PermissionRequest(
                tool_call_id=getattr(context, "tool_use_id", None) or "", kind=fields["kind"],
                title=fields.get("title", name), command=fields.get("command"),
                paths=[relative(path, turn.cwd) or path for path in paths], diff=fields.get("diff"),
                outside_project=any(not inside(turn.cwd, path) for path in paths) or bool(blocked and not inside(turn.cwd, blocked)))
            try:
                decision = await turn.ask_permission(request)
            except asyncio.CancelledError:
                return sdk.PermissionResultDeny(message="The user stopped this turn.", interrupt=True)
            if decision.outcome == "deny":
                return sdk.PermissionResultDeny(message=decision.message or "The user declined this action.")
            return sdk.PermissionResultAllow()
        return can_use_tool

    def turn_options(self, turn: Turn, resume: str | None) -> sdk.ClaudeAgentOptions:
        values: dict[str, Any] = {"cwd": str(turn.cwd), "model": turn.model, "env": self.env(turn.temporary)}
        if turn.effort:
            values["effort"] = turn.effort
        if resume:
            values["resume"] = resume
        if turn.temporary:
            values["extra_args"] = {"no-session-persistence": None}
        if turn.mode == "chat":
            # Chat: no tools on this machine. Web search runs on Anthropic's side (WebFetch would fetch from here).
            web = ["WebSearch"] if self.settings.omni_chat_web_search else []
            values.update(tools=web, allowed_tools=web)
        else:
            prompt: dict[str, Any] = {"type": "preset", "preset": "claude_code"}
            if (instructions := claude_md(turn.cwd)):
                prompt["append"] = instructions
            # Plan mode carries over between turns: resuming in "default" would tell the agent it may now edit,
            # although nobody approved its plan
            values.update(system_prompt=prompt, can_use_tool=self.permission_handler(turn),
                          permission_mode="plan" if turn.plan else "default",
                          disallowed_tools=self.secret_reads())
        return self.options(**values)

    async def run(self, turn: Turn) -> AsyncIterator[Update]:
        resume = None if turn.temporary else turn.native_session
        try:
            try:
                async for update in self._run(turn, resume):
                    yield update
            except _ResumeFailed:
                if turn.require_resume:
                    raise ProviderError("not_found", "元のセッションを再開できませんでした")
                async for update in self._run(turn, None):  # the session is gone: start over with the whole conversation
                    yield update
        finally:
            # In the background: the turn ends at once, and the executor list the screen loads after it
            # waits for this (see usage_ready), so the meter shows the usage including this turn.
            self.schedule_usage_refresh()

    def schedule_usage_refresh(self) -> None:
        if self.billing != "subscription" or not self.installed():
            return
        self.refresh_pending = True
        if self.refreshing is None or self.refreshing.done():
            self.refreshing = asyncio.create_task(self.quiet_refresh())

    async def quiet_refresh(self) -> None:
        # Concurrent conversations share one CLI probe. If another turn ends during a probe,
        # refresh once more so an older report cannot overwrite a newer one.
        while self.refresh_pending:
            self.refresh_pending = False
            with contextlib.suppress(Exception):
                await self.refresh_usage()

    async def usage_ready(self, timeout: float = 15) -> None:
        task = self.refreshing
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout)

    async def _run(self, turn: Turn, resume: str | None) -> AsyncIterator[Update]:
        prompt = turn.prompt if resume else turn.fresh_prompt
        started = False
        try:
            async with sdk.ClaudeSDKClient(options=self.turn_options(turn, resume)) as client:
                await client.query(user_message(prompt, turn.attachments) if turn.attachments else prompt)
                result = None
                results: list[Any] = []
                failure = None
                background: dict[str, str] = {}  # running background agents: task id -> description
                plan_tools: dict[str, str] = {}  # EnterPlanMode / ExitPlanMode calls waiting for their result
                notified = False  # a background task reported its end since the last result
                answered = False  # the model wrote something since the last result
                deadline: float | None = None
                follow_up = False  # the next text starts a follow-up turn: a new paragraph
                loop = asyncio.get_running_loop()
                messages = client.receive_messages().__aiter__()
                while True:
                    try:
                        if deadline is None:
                            message = await messages.__anext__()
                        else:
                            message = await asyncio.wait_for(messages.__anext__(), max(0.1, deadline - loop.time()))
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        yield message_chunk("\n\n（バックグラウンドのエージェントが30分以内に終わらなかったため、待つのをやめました）")
                        break
                    started = True
                    if isinstance(message, sdk.AssistantMessage) and message.error == "rate_limit":
                        # Some CLI versions end a usage-limit reply with a success ResultMessage.
                        # It must still return queued instructions instead of starting another request.
                        failure = ProviderError("rate_limit", "Claude の利用上限に達しました")
                    if isinstance(message, sdk.AssistantMessage) and not message.parent_tool_use_id:
                        answered = True
                        if message.uuid and turn.on_turn_id:
                            turn.on_turn_id(message.uuid)
                    for update in self.updates(message, turn):
                        if follow_up and update.get("sessionUpdate") == "agent_message_chunk":
                            follow_up = False
                            yield message_chunk("\n\n")
                        yield update
                    for update in self.task_updates(message, background):
                        yield update
                    self.plan_changes(message, plan_tools, turn)
                    if isinstance(message, (sdk.TaskNotificationMessage, sdk.TaskUpdatedMessage)) \
                            and getattr(message, "status", None) in TASK_DONE:
                        notified = True
                    if not isinstance(message, sdk.ResultMessage):
                        continue
                    if not results and not message.is_error and message.num_turns == 0 and notified and not answered:
                        # On resume, the CLI first reports background work the previous process left unfinished
                        # and closes that report with an empty result of its own. The answer comes after it.
                        notified = False
                        continue
                    result = message
                    results.append(message)
                    notified = answered = False
                    if message.is_error or failure or not background:
                        break
                    # The model is waiting for background agents: their results start a follow-up turn
                    follow_up = True
                    if deadline is None:
                        deadline = loop.time() + BACKGROUND_WAIT
                usage = self.usage_from(result, results)
                with contextlib.suppress(Exception):
                    context = await client.get_context_usage()
                    usage.update(self.context_from(context))
                if failure:
                    raise failure
                if result is not None and result.is_error:
                    raise self.error_from(result)
                yield usage
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except (sdk.ClaudeSDKError, OSError) as exc:
            if resume and not started:
                raise _ResumeFailed from exc
            raise ProviderError("unknown", f"Claude Code でエラーが起きました: {str(exc)[:200]}") from exc

    def updates(self, message: Any, turn: Turn) -> list[Update]:
        if isinstance(message, sdk.StreamEvent):
            if message.parent_tool_use_id:  # a subagent's own stream
                return []
            delta = (message.event or {}).get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [message_chunk(delta["text"])]
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                return [thought_chunk(delta["thinking"])]
            return []
        if isinstance(message, sdk.SystemMessage):
            session_id = (message.data or {}).get("session_id")
            if message.subtype == "init" and session_id and not turn.temporary:
                turn.on_session(session_id)
            return []
        if isinstance(message, sdk.RateLimitEvent):
            self.remember_limit(message.rate_limit_info)
            return []
        out: list[Update] = []
        if isinstance(message, sdk.AssistantMessage) and not message.parent_tool_use_id:
            for block in message.content:
                if isinstance(block, sdk.ToolUseBlock):
                    if block.name == "TodoWrite":
                        out.append(plan_update(block.input or {}))
                        continue
                    out.append({"sessionUpdate": "tool_call", "toolCallId": block.id, "status": "in_progress",
                                **tool_fields(block.name, block.input or {}, turn.cwd)})
        elif isinstance(message, sdk.UserMessage) and not message.parent_tool_use_id and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, sdk.ToolResultBlock):
                    update: Update = {"sessionUpdate": "tool_call_update", "toolCallId": block.tool_use_id,
                                      "status": "failed" if block.is_error else "completed"}
                    text = result_text(block.content)
                    if text:
                        update["output"] = clip(text)
                    out.append(update)
        return out

    def usage_from(self, result: Any, results: list[Any] | None = None) -> Update:
        """The turn's usage. A turn that waited for background agents has a result for each follow-up."""
        parts = results or ([result] if result else [])
        total = lambda key: sum(int(((getattr(item, "usage", None) or {}).get(key)) or 0) for item in parts)
        costs = [item.total_cost_usd for item in parts if getattr(item, "total_cost_usd", None) is not None]
        update = usage_update(input_tokens=total("input_tokens"), output_tokens=total("output_tokens"),
                              cache_read_tokens=total("cache_read_input_tokens"),
                              cache_write_tokens=total("cache_creation_input_tokens"),
                              cost_usd=sum(costs) if costs and self.billing == "api" else None,
                              stop_reason=getattr(result, "stop_reason", None) if result else None)
        return update

    @staticmethod
    def plan_changes(message: Any, calls: dict[str, str], turn: Turn) -> None:
        """Report entering plan mode, or leaving it once the plan was approved, when the tool call succeeds."""
        if turn.on_plan is None or getattr(message, "parent_tool_use_id", None):
            return
        if isinstance(message, sdk.AssistantMessage):
            for block in message.content:
                if isinstance(block, sdk.ToolUseBlock) and block.name in ("EnterPlanMode", "ExitPlanMode"):
                    calls[block.id] = block.name
        elif isinstance(message, sdk.UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, sdk.ToolResultBlock) and block.tool_use_id in calls:
                    name = calls.pop(block.tool_use_id)
                    if not block.is_error:
                        turn.on_plan(name == "EnterPlanMode")

    @staticmethod
    def task_updates(message: Any, background: dict[str, str]) -> list[Update]:
        """Background agents as cards: running while the turn waits for them, then done or failed."""
        if isinstance(message, sdk.TaskStartedMessage) and message.task_type in WAITED_TASK_TYPES:
            background[message.task_id] = message.description or "エージェント"
            return [{"sessionUpdate": "tool_call", "toolCallId": f"task-{message.task_id}", "kind": "other",
                     "status": "in_progress", "title": f"バックグラウンド: {background[message.task_id]}"}]
        if isinstance(message, (sdk.TaskNotificationMessage, sdk.TaskUpdatedMessage)):
            status = TASK_DONE.get(getattr(message, "status", None) or "")
            if status and message.task_id in background:
                background.pop(message.task_id)
                update: Update = {"sessionUpdate": "tool_call_update", "toolCallId": f"task-{message.task_id}", "status": status}
                summary = getattr(message, "summary", None)
                if summary:
                    update["output"] = clip(summary)
                return [update]
        return []

    @staticmethod
    def context_from(context: Any) -> dict[str, Any]:
        get = (lambda key: context.get(key)) if isinstance(context, dict) else (lambda key: getattr(context, key, None))
        categories = []
        for item in get("categories") or []:
            name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
            tokens = item.get("tokens") if isinstance(item, dict) else getattr(item, "tokens", 0)
            if name and name not in ("Free space",) and tokens:
                categories.append({"name": name, "tokens": tokens})
        return {"contextUsed": get("totalTokens"), "contextWindow": get("maxTokens"), "contextBreakdown": categories}

    def error_from(self, result: Any) -> ProviderError:
        status = getattr(result, "api_error_status", None)
        text = (getattr(result, "result", None) or "") + " ".join(getattr(result, "errors", None) or [])
        lowered = text.lower()
        if status == 429 or "usage limit" in lowered or "limit reached" in lowered:
            return ProviderError("rate_limit", "Claude の利用上限に達しました")
        if status in (401, 403) or "login" in lowered or "authenticat" in lowered:
            hint = "claude auth login を実行してください" if self.billing == "subscription" else "ANTHROPIC_API_KEY を確認してください"
            return ProviderError("auth", hint)
        return ProviderError(ERRORS.get(status, "unknown"), text[:300] or "Claude Code でエラーが起きました")

    async def rename_session(self, native_id: str, cwd: Path, title: str) -> None:
        await asyncio.to_thread(sdk.rename_session, native_id, title, directory=str(cwd.resolve()))

    async def fork_session(self, native_id: str, cwd: Path, title: str, last_turn: str | None) -> str:
        result = await asyncio.to_thread(sdk.fork_session, native_id, directory=str(cwd.resolve()),
                                        title=title, up_to_message_id=last_turn)
        return result.session_id

    async def delete_session(self, native_id: str, cwd: Path) -> None:
        await asyncio.to_thread(sdk.delete_session, native_id, directory=str(cwd.resolve()))

    async def compact(self, native_id: str, cwd: Path) -> None:
        async with asyncio.timeout(120):
            async with sdk.ClaudeSDKClient(options=self.options(cwd=str(cwd), resume=native_id,
                    tools=[], env=self.env())) as client:
                await client.query("/compact")
                completed = False
                boundary = False
                async for message in client.receive_response():
                    if isinstance(message, sdk.SystemMessage) and message.subtype == "compact_boundary":
                        boundary = True
                    if isinstance(message, sdk.ResultMessage):
                        if message.is_error:
                            raise ProviderError("unknown", "Claude Code の圧縮に失敗しました")
                        completed = True
                if not completed or not boundary:
                    raise ProviderError("unknown", "圧縮結果を確認できませんでした")

    async def forget(self, native_session: str, cwd: Path) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(sdk.delete_session, native_session, str(cwd))

    async def close(self) -> None:
        self.refresh_pending = False
        if self.refreshing is not None:
            if not self.refreshing.done():
                self.refreshing.cancel()
            await asyncio.gather(self.refreshing, return_exceptions=True)


def attachment_block(item: dict[str, Any]) -> dict[str, Any]:
    data = base64.b64encode(Path(item["path"]).read_bytes()).decode()
    if item["kind"] == "image":
        return {"type": "image", "source": {"type": "base64", "media_type": item["media_type"], "data": data}}
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            "title": item["name"]}


async def user_message(prompt: str, attachments: list[dict[str, Any]]):
    """The prompt with images and PDFs, as the one user message of the SDK's streaming input."""
    try:
        blocks = [attachment_block(item) for item in attachments]
    except OSError as exc:
        raise ProviderError("bad_request", "添付ファイルを読み込めませんでした。もう一度添付してください") from exc
    yield {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}, *blocks]},
           "parent_tool_use_id": None}


class _ResumeFailed(Exception):
    pass
