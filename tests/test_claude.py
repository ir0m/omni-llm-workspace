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
    assert chat.tools == [] and chat.can_use_tool is None and chat.setting_sources == [] and chat.strict_mcp_config
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
        async def receive_response(self):
            yield sdk.AssistantMessage(content=[], model='haiku', error='rate_limit')
            yield sdk.ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
                                    num_turns=1, session_id='test-limit', result='Usage limit')
    monkeypatch.setattr(sdk, 'ClaudeSDKClient', LimitedClient)
    async def check():
        return [update async for update in agent(tmp_path).run(turn(tmp_path))]
    with pytest.raises(ProviderError) as caught:
        asyncio.run(check())
    assert caught.value.code == 'rate_limit'
