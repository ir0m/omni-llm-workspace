import asyncio
from pathlib import Path
from types import SimpleNamespace

import claude_agent_sdk as sdk
import pytest
from fastapi.testclient import TestClient

from omniws.agents.base import Decision, Turn
from omniws.agents.claude import ClaudeCodeAgent, tool_fields
from omniws.config import Settings
from omniws.main import create_app
from omniws.providers.registry import Registry
from omniws.providers.base import ProviderError

from .conftest import PASSWORD, make_settings


def agent(tmp_path, billing="subscription", **values) -> ClaudeCodeAgent:
    settings = Settings(_env_file=None, omni_data_dir=tmp_path / "data", **values)
    return ClaudeCodeAgent(settings, billing)


def turn(tmp_path, mode="code", temporary=False, ask=None, sessions=None) -> Turn:
    async def allow(request):
        return Decision("allow")
    return Turn(conversation_id="c", mode=mode, cwd=tmp_path, model="haiku", text="q", prompt="q", fresh_prompt="all q",
                unseen=[], earlier=[], native_session=None, temporary=temporary, effort=None,
                ask_permission=ask or allow, on_session=(sessions.append if sessions is not None else lambda _id: None))


def test_environment_never_leaks_keys_or_proxies(tmp_path, monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE"):
        monkeypatch.setenv(key, "from-the-server")
    env = agent(tmp_path, omni_allow_claude_subscription=True).env()
    assert env["ANTHROPIC_API_KEY"] == "" and env["ANTHROPIC_BASE_URL"] == "" and env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    api = agent(tmp_path, "api", anthropic_api_key="sk-test").env(temporary=True)
    assert api["ANTHROPIC_API_KEY"] == "sk-test" and api["ANTHROPIC_BASE_URL"] == ""
    assert api["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_which_executors_exist(tmp_path):
    assert not agent(tmp_path).installed()  # the subscription needs OMNI_ALLOW_CLAUDE_SUBSCRIPTION
    assert agent(tmp_path, omni_allow_claude_subscription=True).installed()
    assert not agent(tmp_path, "api").installed() and agent(tmp_path, "api", anthropic_api_key="k").installed()


def test_tool_fields(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    edit = tool_fields("Edit", {"file_path": str(tmp_path / "a.py"), "old_string": "x = 1", "new_string": "x = 2"}, tmp_path)
    assert edit["kind"] == "edit" and edit["path"] == "a.py" and "-x = 1" in edit["diff"] and "+x = 2" in edit["diff"]
    write = tool_fields("Write", {"file_path": str(tmp_path / "a.py"), "content": "x = 3\n"}, tmp_path)
    assert "-x = 1" in write["diff"] and "+x = 3" in write["diff"]
    bash = tool_fields("Bash", {"command": "npm test", "description": "テストを実行"}, tmp_path)
    assert bash == {"kind": "execute", "title": "テストを実行", "command": "npm test"}
    assert tool_fields("Grep", {"pattern": "TODO"}, tmp_path)["kind"] == "search"


def test_options_for_chat_and_code(tmp_path):
    claude = agent(tmp_path, omni_allow_claude_subscription=True)
    chat = claude.turn_options(turn(tmp_path, mode="chat"), None)
    # Only web search, which runs on Anthropic's side and needs no approval; nothing runs on this machine
    assert chat.tools == ["WebSearch"] and chat.allowed_tools == ["WebSearch"]
    assert chat.can_use_tool is None and chat.setting_sources == [] and chat.strict_mcp_config
    off = agent(tmp_path, omni_allow_claude_subscription=True, omni_chat_web_search=False)
    assert off.turn_options(turn(tmp_path, mode="chat"), None).tools == []
    (tmp_path / "CLAUDE.md").write_text("日本語で答える")
    code = claude.turn_options(turn(tmp_path, temporary=True), None)
    assert code.system_prompt["preset"] == "claude_code" and "日本語で答える" in code.system_prompt["append"]
    assert code.setting_sources == [] and code.can_use_tool is not None and code.resume is None
    assert code.extra_args == {"no-session-persistence": None}
    assert claude.turn_options(turn(tmp_path), "ses-1").resume == "ses-1"
    # `@AGENTS.md` in CLAUDE.md brings that file in; files outside the project stay out
    (tmp_path / "AGENTS.md").write_text("作業ルール")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n@../outside.md")
    (tmp_path.parent / "outside.md").write_text("秘密")
    append = claude.turn_options(turn(tmp_path), None).system_prompt["append"]
    assert "作業ルール" in append and "秘密" not in append and "@../outside.md" in append


def test_messages_become_updates(tmp_path):
    claude = agent(tmp_path, omni_allow_claude_subscription=True)
    sessions = []
    current = turn(tmp_path, sessions=sessions)
    stream = lambda delta: sdk.StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta", "delta": delta},
                                           parent_tool_use_id=None)  # noqa: E731
    assert claude.updates(stream({"type": "text_delta", "text": "は"}), current)[0]["content"]["text"] == "は"
    assert claude.updates(stream({"type": "thinking_delta", "thinking": "考"}), current)[0]["sessionUpdate"] == "agent_thought_chunk"
    claude.updates(sdk.SystemMessage(subtype="init", data={"session_id": "ses-9"}), current)
    assert sessions == ["ses-9"]
    assistant = sdk.AssistantMessage(content=[
        sdk.ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
        sdk.ToolUseBlock(id="t2", name="TodoWrite", input={"todos": [{"content": "調べる", "status": "in_progress"}]})],
        model="haiku", parent_tool_use_id=None)
    updates = claude.updates(assistant, current)
    assert updates[0]["sessionUpdate"] == "tool_call" and updates[0]["command"] == "ls"
    assert updates[1] == {"sessionUpdate": "plan", "entries": [{"content": "調べる", "status": "in_progress", "priority": "medium"}]}
    result = sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id="t1", content="a.py", is_error=False)], parent_tool_use_id=None)
    assert claude.updates(result, current) == [{"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
                                                "output": "a.py"}]


def test_rate_limits_are_kept_with_their_time(tmp_path):
    claude = agent(tmp_path, omni_allow_claude_subscription=True)
    claude.info = {"account": {"subscriptionType": "Claude Pro"}}
    info = SimpleNamespace(status="allowed_warning", rate_limit_type="five_hour", utilization=0.96, resets_at=1800000000)
    claude.updates(sdk.RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s"), turn(tmp_path))
    usage = ClaudeCodeAgent(claude.settings, "subscription").usage()  # read back from the file
    assert usage["plan"] == "Claude Pro"
    assert usage["windows"][0]["label"] == "5時間" and usage["windows"][0]["usedPercent"] == 96 and usage["windows"][0]["at"]
    # Without a percentage (usage is low) the status is what's shown
    calm = SimpleNamespace(status="allowed", rate_limit_type="five_hour", utilization=None, resets_at=1800000000)
    claude.updates(sdk.RateLimitEvent(rate_limit_info=calm, uuid="u", session_id="s"), turn(tmp_path))
    window = claude.usage()["windows"][0]
    assert window["usedPercent"] is None and window["status"] == "allowed"


def test_errors_and_context(tmp_path):
    claude = agent(tmp_path, omni_allow_claude_subscription=True)
    limit = SimpleNamespace(api_error_status=None, result="Claude AI usage limit reached", errors=None)
    assert claude.error_from(limit).code == "rate_limit"
    login = SimpleNamespace(api_error_status=401, result="", errors=None)
    assert claude.error_from(login).code == "auth" and "claude auth login" in claude.error_from(login).message
    context = ClaudeCodeAgent.context_from({"totalTokens": 3000, "maxTokens": 1_000_000,
                                            "categories": [{"name": "System prompt", "tokens": 2000},
                                                           {"name": "Free space", "tokens": 997000}]})
    assert context == {"contextUsed": 3000, "contextWindow": 1_000_000, "contextBreakdown": [{"name": "System prompt", "tokens": 2000}]}


def test_permissions_go_through_the_app(tmp_path):
    asked = []

    async def deny(request):
        asked.append(request)
        return Decision("deny", message="テストはまだ")

    claude = agent(tmp_path, omni_allow_claude_subscription=True)
    handler = claude.permission_handler(turn(tmp_path, ask=deny))
    context = SimpleNamespace(tool_use_id="t9", blocked_path=None)
    result = asyncio.run(handler("Bash", {"command": "pytest"}, context))
    assert isinstance(result, sdk.PermissionResultDeny) and result.message == "テストはまだ"
    assert asked[0].kind == "execute" and asked[0].command == "pytest" and asked[0].tool_call_id == "t9"
    outside = asyncio.run(handler("Edit", {"file_path": "/etc/hosts", "old_string": "a", "new_string": "b"}, context))
    assert isinstance(outside, sdk.PermissionResultDeny) and asked[1].outside_project
    assert isinstance(asyncio.run(handler("TodoWrite", {"todos": []}, context)), sdk.PermissionResultAllow)


def test_executor_entries(tmp_path):
    settings = make_settings(tmp_path, omni_allow_claude_subscription=True)
    subscription = ClaudeCodeAgent(settings, "subscription")
    info = {"account": {"subscriptionType": "Claude Pro"}, "models": [
        {"value": "default", "displayName": "Default"},
        {"value": "opus", "displayName": "Opus 5.5", "supportedEffortLevels": ["low", "high"], "resolvedModel": "claude-opus-5-5"},
        {"value": "haiku", "displayName": "Haiku 5.5", "supportedEffortLevels": []}]}

    async def server_info(refresh=False):
        subscription.info = info
        return info

    subscription.server_info = server_info
    app = create_app(settings, web_dist=Path("/nonexistent"), registry=Registry(settings, {}),
                     agents={"claude_code": subscription, "claude_code_api": ClaudeCodeAgent(settings, "api")})
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        entries = {item["id"]: item for item in client.get("/api/executors").json()}
    assert entries["claude_code"]["label"] == "Claude Code（Pro）" and entries["claude_code"]["configured"]
    assert [model["id"] for model in entries["claude_code"]["models"]] == ["opus", "haiku"]
    assert entries["claude_code"]["models"][0]["efforts"] == ["low", "high"]
    assert "claude_code_api" not in entries  # no ANTHROPIC_API_KEY


def test_subscription_is_off_by_default(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings, web_dist=Path("/nonexistent"), registry=Registry(settings, {}),
                     agents={"claude_code": ClaudeCodeAgent(settings, "subscription")})
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        entry = next(item for item in client.get("/api/executors").json() if item["id"] == "claude_code")
        conversation = client.post("/api/conversations", json={}).json()
        refused = client.post(f"/api/conversations/{conversation['id']}/messages",
                              json={"text": "x", "provider": "claude_code", "model": "haiku"})
    assert entry["configured"] is False and entry["setting"] == "OMNI_ALLOW_CLAUDE_SUBSCRIPTION=true"
    assert refused.status_code == 400 and refused.json()["detail"] == "agent_not_installed"


def test_usage_limit_is_not_success_even_if_cli_result_says_success(tmp_path, monkeypatch):
    class LimitedClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def query(self, prompt):
            pass
        async def receive_messages(self):
            yield sdk.AssistantMessage(content=[], model='haiku', error='rate_limit')
            yield sdk.ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
                                    num_turns=1, session_id='test-limit', result='Usage limit')
    monkeypatch.setattr(sdk, 'ClaudeSDKClient', LimitedClient)
    async def check():
        return [update async for update in agent(tmp_path).run(turn(tmp_path))]
    with pytest.raises(ProviderError) as caught:
        asyncio.run(check())
    assert caught.value.code == 'rate_limit'


def test_usage_report_from_the_cli_is_parsed_and_stored(tmp_path):
    import asyncio
    import datetime as dt
    import json as json_
    import stat
    import sys as sys_
    from omniws.agents.claude import ClaudeCodeAgent, parse_usage
    from .conftest import make_settings

    now = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp()
    report = ("You are currently using your subscription\n\nCurrent session: 71% used · resets Nov 14, 4pm (Asia/Tokyo)\n"
              "Current week (all models): 89% used · resets Nov 17, 2:30pm (Asia/Tokyo)\n"
              "Current week (Fable): 0% used\n\nWhat's contributing to your usage?\n  91%  Start fresh")
    windows = parse_usage(report, now)
    assert windows["five_hour"]["usedPercent"] == 71 and windows["seven_day"]["usedPercent"] == 89
    assert dt.datetime.fromtimestamp(windows["five_hour"]["resetsAt"], dt.timezone(dt.timedelta(hours=9))).hour == 16
    assert dt.datetime.fromtimestamp(windows["seven_day"]["resetsAt"], dt.timezone(dt.timedelta(hours=9))).minute == 30
    assert windows["seven_day_fable"] == {"usedPercent": 0, "resetsAt": None, "status": None, "at": int(now), "label": "7日（Fable）"}

    # The CLI is asked with /usage and no saved session; nothing else is read
    cli = tmp_path / "fake-claude"
    cli.write_text(f"#!{sys_.executable}\nimport json, sys\nassert sys.argv[1:] == ['-p', '/usage', '--output-format', 'json', "
                   f"'--no-session-persistence'], sys.argv\nprint(json.dumps({{'result': {report!r}}}))\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    config = make_settings(tmp_path, omni_allow_claude_subscription=True)
    agent = ClaudeCodeAgent(config, "subscription", cli_path=str(cli))
    asyncio.run(agent.refresh_usage())
    labels = {window["label"]: window["usedPercent"] for window in agent.usage()["windows"]}
    assert labels == {"5時間": 71, "7日": 89, "7日（Fable）": 0}
    assert json_.loads(config.omni_data_dir.joinpath("claude-usage.json").read_text())["windows"]["five_hour"]["usedPercent"] == 71
    # API billing has no plan usage to ask for
    api = ClaudeCodeAgent(make_settings(tmp_path / "api"), "api", cli_path=str(cli))
    asyncio.run(api.refresh_usage())
    assert api.usage() is None


@pytest.mark.parametrize("cancel", [False, True])
def test_usage_probe_is_killed_and_reaped_on_timeout_or_cancel(tmp_path, monkeypatch, cancel):
    from omniws.agents import claude as module

    async def scenario():
        started = asyncio.Event()

        class Process:
            returncode = None
            killed = False
            waited = False
            async def communicate(self):
                started.set()
                await asyncio.Event().wait()
            def kill(self):
                self.killed = True
            async def wait(self):
                self.waited = True
                self.returncode = -9

        process = Process()
        async def spawn(*args, **kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(module, "USAGE_SECONDS", 10 if cancel else 0.01)
        claude = agent(tmp_path, omni_allow_claude_subscription=True)
        claude.cli_path = "fake-cli"
        task = asyncio.create_task(claude.refresh_usage())
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert process.killed and process.waited
        assert not claude.usage_file.exists()

    asyncio.run(scenario())


def test_usage_refreshes_are_serialized_and_closed(tmp_path, monkeypatch):
    async def scenario():
        claude = agent(tmp_path, omni_allow_claude_subscription=True)
        started = asyncio.Queue()
        release = asyncio.Event()
        calls = []
        active = 0
        async def refresh():
            nonlocal active
            active += 1
            assert active == 1
            calls.append(len(calls) + 1)
            started.put_nowait(len(calls))
            try:
                await release.wait()
            finally:
                active -= 1

        monkeypatch.setattr(claude, "refresh_usage", refresh)
        claude.schedule_usage_refresh()
        assert await started.get() == 1
        for _ in range(5):
            claude.schedule_usage_refresh()
        release.set()
        await claude.usage_ready(timeout=1)
        assert calls == [1, 2]
        release.clear()
        claude.schedule_usage_refresh()
        assert await started.get() == 2
        assert await started.get() == 3
        await claude.close()
        assert active == 0 and claude.refreshing.done()

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["4:99pm (Asia/Tokyo)", "0am (UTC)", "13pm (UTC)", "Feb 30, 4pm (UTC)",
                                  "4pm (Invalid/Zone)"])
def test_malformed_reset_times_do_not_break_usage(text):
    from omniws.agents.claude import parse_reset
    assert parse_reset(text, 1_800_000_000) is None


def test_messages_carrying_whole_pdfs_fit_the_sdk_buffer(tmp_path):
    from omniws.attachments import MESSAGE_LIMIT
    claude = agent(tmp_path, omni_allow_claude_subscription=True)
    for options in (claude.turn_options(turn(tmp_path, mode="chat"), None), claude.turn_options(turn(tmp_path), None)):
        # The largest message's attachments as base64, plus room for the JSON around them
        assert options.max_buffer_size >= MESSAGE_LIMIT * 4 // 3 + 1024 * 1024



# --- Background agents and the CLI's own result frames ------------------------------------------------------

def scripted(monkeypatch, messages, hang=False):
    """A ClaudeSDKClient that streams `messages` (then stays open when `hang`), like the CLI's stdout."""
    class Client:
        def __init__(self, **kwargs):
            self.closed = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            self.closed = True
        async def query(self, prompt):
            pass
        async def get_context_usage(self):
            return {}
        async def receive_messages(self):
            for message in messages:
                for item in (message if isinstance(message, Said) else [message]):
                    yield item
            if hang:
                await asyncio.sleep(3600)
    monkeypatch.setattr(sdk, 'ClaudeSDKClient', Client)


class Said(list):
    """The streamed text delta and the complete message, both of which the CLI sends for a reply."""


def text(value):
    delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": value}}
    return Said([sdk.StreamEvent(uuid='e', session_id='s', event=delta),
                 sdk.AssistantMessage(content=[sdk.TextBlock(text=value)], model='haiku')])


def result(turns=1, value='', tokens=0):
    return sdk.ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False, num_turns=turns,
                             session_id='s', result=value, usage={'input_tokens': tokens, 'output_tokens': tokens})


def started(task_id, kind='local_agent', description='調査'):
    return sdk.TaskStartedMessage(subtype='task_started', data={}, task_id=task_id, description=description, uuid='u',
                                  session_id='s', task_type=kind)


def ended(task_id, status='completed', summary='結果'):
    return sdk.TaskNotificationMessage(subtype='task_notification', data={}, task_id=task_id, status=status,
                                       output_file='', summary=summary, uuid='u', session_id='s')


def run_turn(tmp_path):
    async def collect():
        return [update async for update in agent(tmp_path).run(turn(tmp_path))]
    return asyncio.run(collect())


def said(updates):
    return "".join(u["content"]["text"] for u in updates if u["sessionUpdate"] == "agent_message_chunk")


def test_resumed_session_answers_after_the_report_of_stopped_background_work(tmp_path, monkeypatch):
    # The CLI reports agents the previous process left behind and closes that report with an empty result.
    # Stopping there left the user's message unanswered (an empty reply).
    scripted(monkeypatch, [ended('old', 'stopped'), sdk.SystemMessage(subtype='init', data={}), result(0),
                           sdk.SystemMessage(subtype='init', data={}), text('計画書はできています'), result(1, tokens=5)])
    updates = run_turn(tmp_path)
    assert said(updates) == '計画書はできています'
    assert updates[-1]["inputTokens"] == 5


def test_turn_waits_for_background_agents_and_their_follow_up(tmp_path, monkeypatch):
    scripted(monkeypatch, [started('a1', description='実装を調べる'), text('結果を待っています。'), result(1, tokens=2),
                           ended('a1', summary='見つかった'), text('まとめ'), result(1, tokens=3),
                           text('閉じた後の出力')], hang=True)
    updates = run_turn(tmp_path)
    assert said(updates) == '結果を待っています。\n\nまとめ'
    cards = [u for u in updates if u.get("toolCallId") == "task-a1"]
    assert cards[0]["title"] == "バックグラウンド: 実装を調べる" and cards[0]["status"] == "in_progress"
    assert cards[-1]["status"] == "completed" and cards[-1]["output"] == "見つかった"
    assert updates[-1]["inputTokens"] == 5  # both results


def test_background_shells_are_not_waited_for(tmp_path, monkeypatch):
    scripted(monkeypatch, [started('b1', kind='local_bash', description='npm run dev'), text('起動しました'), result(1),
                           text('来ないはずの出力')], hang=True)
    updates = run_turn(tmp_path)
    assert said(updates) == '起動しました'
    assert not [u for u in updates if u.get("toolCallId") == "task-b1"]


def test_waiting_for_background_agents_is_bounded(tmp_path, monkeypatch):
    from omniws.agents import claude as claude_module
    monkeypatch.setattr(claude_module, 'BACKGROUND_WAIT', 0.2)
    scripted(monkeypatch, [started('a1'), text('待っています'), result(1)], hang=True)
    updates = run_turn(tmp_path)
    assert '待つのをやめました' in said(updates)


# --- Plan mode ------------------------------------------------------------------------------------------------

def test_plan_mode_carries_over_and_the_plan_is_always_for_the_user(tmp_path):
    from omniws.agents.base import PermissionRequest
    from omniws.agents.policy import decide
    from .conftest import make_settings
    claude = agent(tmp_path, omni_allow_claude_subscription=True)
    planning = turn(tmp_path)
    planning.plan = True
    assert claude.turn_options(planning, "ses-1").permission_mode == "plan"
    assert claude.turn_options(turn(tmp_path), "ses-1").permission_mode == "default"
    # Neither "auto" nor a remembered rule approves a plan
    plan = PermissionRequest(tool_call_id="t", kind="plan", title="計画", plan="1. 直す")
    settings = make_settings(tmp_path)
    assert decide(plan, "auto", [{"kind": "plan"}], settings)[0] == "ask"


def test_exit_plan_mode_asks_with_the_plan_and_entering_needs_no_approval(tmp_path):
    asked = []
    async def answer(request):
        asked.append(request)
        return Decision("deny", message="テストも追加して") if len(asked) == 1 else Decision("allow")
    claude = agent(tmp_path, omni_allow_claude_subscription=True)
    can_use_tool = claude.permission_handler(turn(tmp_path, ask=answer))
    context = SimpleNamespace(tool_use_id="tool-1")
    async def check():
        entered = await can_use_tool("EnterPlanMode", {}, context)
        first = await can_use_tool("ExitPlanMode", {"plan": "## 計画\n1. 直す"}, context)
        second = await can_use_tool("ExitPlanMode", {"plan": "## 計画\n1. 直す\n2. テスト"}, context)
        return entered, first, second
    entered, first, second = asyncio.run(check())
    assert isinstance(entered, sdk.PermissionResultAllow) and len(asked) == 2
    assert asked[0].kind == "plan" and asked[0].plan == "## 計画\n1. 直す"
    assert isinstance(first, sdk.PermissionResultDeny) and "The user asked for these changes: テストも追加して" in first.message
    assert isinstance(second, sdk.PermissionResultAllow)


def test_entering_and_approved_leaving_of_plan_mode_are_reported(tmp_path, monkeypatch):
    def use(tool_id, name):
        return sdk.AssistantMessage(content=[sdk.ToolUseBlock(id=tool_id, name=name, input={})], model='haiku')
    def done(tool_id, error=False):
        return sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id=tool_id, content='ok', is_error=error)])
    scripted(monkeypatch, [use('e1', 'EnterPlanMode'), done('e1'), use('x1', 'ExitPlanMode'), done('x1', error=True),
                           use('x2', 'ExitPlanMode'), done('x2'), text('実装します'), result(1)])
    reported = []
    planning = turn(tmp_path)
    planning.on_plan = reported.append
    async def collect():
        return [update async for update in agent(tmp_path).run(planning)]
    asyncio.run(collect())
    assert reported == [True, False]  # the rejected plan (an error result) changed nothing
