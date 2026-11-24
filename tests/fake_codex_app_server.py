"""A stand-in for `codex app-server` with just enough of the protocol for the adapter tests.

turn/start streams: a read command (asks for approval like `untrusted` does), a file change (asks), a
command that runs (asks), output, a plan, a message and token usage. Every request it got is appended to
`.codex-requests.jsonl` in the thread's folder so tests can check what the adapter sent.
"""

import json
import os
import sys

next_id = 1000
threads: dict[str, dict] = {}


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def note(cwd, method, params):
    with open(os.path.join(cwd, ".codex-requests.jsonl"), "a") as log:
        log.write(json.dumps({"method": method, "params": params}) + "\n")


def ask(method, params):
    global next_id
    next_id += 1
    send({"id": next_id, "method": method, "params": params})
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "turn/steer":
            data = message["params"]
            note(threads[data["threadId"]]["cwd"], "turn/steer", data)
            if data["expectedTurnId"] != "turn1":
                send({"id": message["id"], "error": {"code": -32602, "message": "turn mismatch"}})
            else:
                send({"id": message["id"], "result": {"turnId": "turn1"}})
            continue
        if message.get("id") == next_id and "method" not in message:
            return message.get("result") or {}
    sys.exit(0)


def event(tid, method, **params):
    send({"method": method, "params": {"threadId": tid, "turnId": "turn1", **params}})


def run_turn(request_id, params):
    tid = params["threadId"]
    cwd = threads[tid]["cwd"]
    send({"id": request_id, "result": {"turn": {"id": "turn1", "status": "inProgress", "items": []}}})
    if threads[tid]["approvalPolicy"] == "untrusted":
        read = {"type": "commandExecution", "id": "c1", "command": "/bin/bash -lc 'cat calc.py'", "status": "inProgress",
                "commandActions": [{"type": "read", "command": "cat calc.py", "name": "calc.py", "path": "calc.py"}]}
        event(tid, "item/started", item=read)
        answer = ask("item/commandExecution/requestApproval", {"threadId": tid, "turnId": "turn1", "itemId": "c1",
                                                                "command": read["command"], "cwd": cwd, "startedAtMs": 0,
                                                                "commandActions": read["commandActions"]})
        event(tid, "item/completed", item={**read, "status": "completed", "aggregatedOutput": "def add", "exitCode": 0})
        note(cwd, "answer/read", answer)
        change = {"type": "fileChange", "id": "f1", "status": "inProgress",
                  "changes": [{"path": f"{cwd}/calc.py", "kind": {"type": "update"}, "diff": "@@ -1 +1 @@\n-a - b\n+a + b"}]}
        event(tid, "item/started", item=change)
        answer = ask("item/fileChange/requestApproval", {"threadId": tid, "turnId": "turn1", "itemId": "f1", "startedAtMs": 0})
        note(cwd, "answer/edit", answer)
        event(tid, "item/completed", item={**change, "status": "completed" if answer["decision"] == "accept" else "declined"})
        run = {"type": "commandExecution", "id": "c2", "command": "/bin/bash -lc \"python3 -m pytest -q\"", "status": "inProgress",
               "commandActions": [{"type": "unknown", "command": "python3 -m pytest -q"}]}
        event(tid, "item/started", item=run)
        answer = ask("item/commandExecution/requestApproval", {"threadId": tid, "turnId": "turn1", "itemId": "c2",
                                                                "command": run["command"], "cwd": cwd, "startedAtMs": 0,
                                                                "commandActions": run["commandActions"]})
        note(cwd, "answer/run", answer)
        if answer["decision"] == "accept":
            event(tid, "item/commandExecution/outputDelta", itemId="c2", delta="3 passed\n")
        event(tid, "item/completed", item={**run, "status": "completed" if answer["decision"] == "accept" else "declined",
                                           "aggregatedOutput": "3 passed\n" if answer["decision"] == "accept" else None,
                                           "exitCode": 0 if answer["decision"] == "accept" else None})
        event(tid, "turn/plan/updated", plan=[{"step": "直す", "status": "completed"}, {"step": "試す", "status": "inProgress"}])
    event(tid, "item/reasoning/summaryTextDelta", itemId="r1", delta="考え中", summaryIndex=0)
    event(tid, "item/agentMessage/delta", itemId="m1", delta="受け取った: ")
    event(tid, "item/agentMessage/delta", itemId="m1", delta=params["input"][0]["text"])
    event(tid, "item/completed", item={"type": "agentMessage", "id": "m1", "text": "…"})
    if params["input"][0]["text"] == "二通":  # a second message right after the first, no tool in between
        event(tid, "item/agentMessage/delta", itemId="m2", delta="## 見出し")
        event(tid, "item/completed", item={"type": "agentMessage", "id": "m2", "text": "## 見出し"})
    event(tid, "thread/tokenUsage/updated", tokenUsage={"last": {"inputTokens": 30, "outputTokens": 4, "cachedInputTokens": 20,
                                                                  "reasoningOutputTokens": 1, "totalTokens": 900},
                                                         "total": {}, "modelContextWindow": 258400})
    if params["input"][0]["text"] == "上限":
        event(tid, "turn/completed", turn={"id": "turn1", "status": "failed", "items": [],
                                           "error": {"message": "limit", "codexErrorInfo": "usageLimitExceeded"}})
    else:
        event(tid, "turn/completed", turn={"id": "turn1", "status": "completed", "items": []})


account = {"signed_in": True}


def main():
    for line in sys.stdin:
        message = json.loads(line)
        method, params, request_id = message.get("method"), message.get("params") or {}, message.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            send({"id": request_id, "result": {"userAgent": "fake"}})
        elif method == "account/read":
            send({"id": request_id, "result": {"account": {"type": "chatgpt", "planType": "plus", "email": "me@example.invalid"}
                                               if account["signed_in"] else None}})
        elif method == "account/login/start":
            send({"id": request_id, "result": {"type": "chatgptDeviceCode", "loginId": "login-1", "userCode": "ABCD-1234",
                                               "verificationUrl": "https://example.invalid/device"}})
            account["signed_in"] = True
            send({"method": "account/login/completed", "params": {"loginId": "login-1", "success": True}})
        elif method == "account/logout":
            account["signed_in"] = False
            send({"id": request_id, "result": {}})
        elif method == "account/rateLimits/read":
            send({"id": request_id, "result": {"rateLimits": {"primary": {"usedPercent": 92, "windowDurationMins": 300, "resetsAt": 1},
                                                               "secondary": {"usedPercent": 16, "windowDurationMins": 10080, "resetsAt": 2}}}})
        elif method == "model/list":
            send({"id": request_id, "result": {"data": [
                {"id": "a", "model": "gpt-a", "displayName": "GPT A", "hidden": False, "isDefault": False,
                 "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
                {"id": "b", "model": "gpt-b", "displayName": "GPT B", "hidden": False, "isDefault": True, "supportedReasoningEfforts": []},
                {"id": "h", "model": "hidden", "displayName": "H", "hidden": True}]}})
        elif method == "thread/start":
            tid = f"thread-{len(threads) + 1}"
            threads[tid] = params
            note(params["cwd"], method, params)
            send({"id": request_id, "result": {"thread": {"id": tid}, "model": params.get("model")}})
        elif method == "thread/resume":
            threads[params["threadId"]] = params
            note(params["cwd"], method, params)
            send({"id": request_id, "result": {"thread": {"id": params["threadId"]}}})
        elif method == "thread/inject_items":
            note(threads[params["threadId"]]["cwd"], method, params)
            send({"id": request_id, "result": {}})
        elif method == "turn/start":
            note(threads[params["threadId"]]["cwd"], method, params)
            run_turn(request_id, params)
        else:
            send({"id": request_id, "result": {}})


main()
