"""A stand-in for `opencode acp`: speaks just enough ACP over stdio for the adapter tests.

On session/prompt it streams a thought, a todo plan, an edit (asks permission, then writes through the
client's fs/write_text_file), a command (asks permission), a message, and finishes with usage.
"""

import json
import sys

pending: dict[int, dict] = {}
next_id = 1000
sessions: dict[str, str] = {}


def send(message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def update(session_id, payload):
    send({"method": "session/update", "params": {"sessionId": session_id, "update": payload}})


def ask(method, params):
    """Calls the client and waits for its answer (answering client calls are not needed here)."""
    global next_id
    next_id += 1
    send({"id": next_id, "method": method, "params": params})
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("id") == next_id and "method" not in message:
            return message
    sys.exit(0)


def prompt(request_id, params):
    sid = params["sessionId"]
    cwd = sessions[sid]
    text = params["prompt"][0]["text"]
    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "考え中"}})
    update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t0", "title": "todowrite", "kind": "other", "status": "pending",
                 "rawInput": {"todos": [{"content": "書く", "status": "in_progress", "priority": "high"}]}})
    path = f"{cwd}/hello.txt"
    update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "write", "kind": "edit", "status": "pending",
                 "locations": [{"path": path}], "rawInput": {"filePath": path, "content": "hi\n"}})
    answer = ask("session/request_permission", {"sessionId": sid, "toolCall": {
        "toolCallId": "t1", "title": path, "kind": "edit", "locations": [{"path": path}],
        "rawInput": {"filepath": path, "diff": "--- a/hello.txt\n+++ b/hello.txt\n@@ -0,0 +1 @@\n+hi\n"}},
        "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow"},
                    {"optionId": "reject", "kind": "reject_once", "name": "Reject"}]})
    if answer["result"]["outcome"].get("optionId") == "once":
        ask("fs/write_text_file", {"sessionId": sid, "path": path, "content": "hi\n"})
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
    escape = ask("fs/write_text_file", {"sessionId": sid, "path": "/tmp/outside-omni-test.txt", "content": "x"})
    update(sid, {"sessionUpdate": "agent_message_chunk",
                 "content": {"type": "text", "text": f"outside write: {'error' if 'error' in escape else 'ok'}. "}})
    update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t2", "title": "bash", "kind": "execute", "status": "pending",
                 "rawInput": {"command": "npm test -- --watch=false"}})
    answer = ask("session/request_permission", {"sessionId": sid, "toolCall": {
        "toolCallId": "t2", "title": "npm test -- --watch=false", "kind": "execute",
        "rawInput": {"command": "npm test -- --watch=false"}},
        "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow"},
                    {"optionId": "reject", "kind": "reject_once", "name": "Reject"}]})
    ran = answer["result"]["outcome"].get("optionId") == "once"
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t2", "status": "completed" if ran else "failed",
                 "rawOutput": {"output": "PASS 3 tests" if ran else "rejected"}})
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"受け取った: {text[-20:]}"}})
    update(sid, {"sessionUpdate": "usage_update", "used": 1234, "size": 200000, "cost": {"amount": 0, "currency": "USD"}})
    send({"id": request_id, "result": {"stopReason": "end_turn", "usage": {"inputTokens": 50, "outputTokens": 7,
                                                                          "cachedReadTokens": 1000, "thoughtTokens": 3}}})


def main():
    if sys.argv[1:2] == ["models"]:
        print("opencode/alpha-free\nopencode/paid-model\nopencode/beta-free")
        return
    if sys.argv[1:2] == ["session"]:  # `session delete <id>`: record where it ran
        import os
        with open(os.path.join(os.getcwd(), ".deleted-sessions"), "a") as log:
            log.write(sys.argv[3] + "\n")
        return
    for line in sys.stdin:
        message = json.loads(line)
        method, params, request_id = message.get("method"), message.get("params") or {}, message.get("id")
        if method == "initialize":
            send({"id": request_id, "result": {"protocolVersion": 1, "agentCapabilities": {
                "loadSession": True, "sessionCapabilities": {"resume": {}}}}})
        elif method == "session/new":
            sid = f"ses_{len(sessions) + 1}"
            sessions[sid] = params["cwd"]
            send({"id": request_id, "result": {"sessionId": sid, "configOptions": [
                {"id": "model", "type": "select", "currentValue": "opencode/alpha-free", "options": []}]}})
        elif method == "session/resume":
            sessions[params["sessionId"]] = params["cwd"]
            send({"id": request_id, "result": {}})
        elif method == "session/set_config_option":
            ok = params["value"] != "opencode/missing"
            send({"id": request_id, **({"result": {}} if ok else {"error": {"code": -32602, "message": "unknown model"}})})
        elif method == "session/prompt":
            prompt(request_id, params)


main()
