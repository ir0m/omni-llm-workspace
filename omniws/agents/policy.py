"""Who decides a permission request: .env limits, then what the conversation remembers, then its mode.

Modes:
  ask        everything except reading inside the project is confirmed
  auto_edit  edits inside the project go through; commands and anything outside are confirmed
  auto       everything goes through except what .env denies
"""

from __future__ import annotations

import re
from typing import Any, Literal

from ..config import Settings
from .base import PermissionRequest

LEVELS = ("ask", "auto_edit", "auto")
READING = {"read", "search", "think"}
EDITING = {"edit", "delete", "move"}

Verdict = Literal["allow", "deny", "ask"]


def clamp(mode: str | None, settings: Settings) -> str:
    """The conversation's mode, never looser than OMNI_APPROVAL_MAX."""
    mode = mode if mode in LEVELS else settings.omni_approval_default
    return LEVELS[min(LEVELS.index(mode), LEVELS.index(settings.omni_approval_max))]


# Chaining, pipes, redirection and substitution: a remembered prefix must not let these through
SHELL_CONTROL = re.compile(r"[;&|`<>\n]|\$\(")


def command_prefix(command: str, words: int = 2) -> str:
    return " ".join(command.split()[:words])


def is_simple(command: str) -> bool:
    return not SHELL_CONTROL.search(command)


def rule_for(request: PermissionRequest) -> dict[str, Any] | None:
    """What "always allow in this conversation" remembers: commands by their first two words.
    Compound commands (with ; && | > $( ...) are never remembered."""
    if request.kind == "execute":
        if not request.command or not is_simple(request.command):
            return None
        return {"kind": "execute", "prefix": command_prefix(request.command)}
    return {"kind": request.kind, "outside": request.outside_project}


def matches(rule: dict[str, Any], request: PermissionRequest) -> bool:
    if rule.get("kind") != request.kind:
        return False
    if request.kind == "execute":
        prefix = rule.get("prefix") or ""
        command = (request.command or "").strip()
        return bool(prefix) and is_simple(command) and (command == prefix or command.startswith(prefix + " "))
    return bool(rule.get("outside")) or not request.outside_project


def denied_by_env(request: PermissionRequest, settings: Settings) -> str | None:
    if request.kind != "execute":
        return None
    if not settings.omni_allow_shell:
        return "OMNI_ALLOW_SHELL=false"
    command = request.command or ""
    for pattern in settings.shell_deny():
        if re.search(r"(^|[\s;&|(`])" + re.escape(pattern) + r"(?=\s|$|[;&|)`])", command):
            return f"OMNI_SHELL_DENY: {pattern}"
    return None


def decide(request: PermissionRequest, mode: str | None, rules: list[dict[str, Any]], settings: Settings) -> tuple[Verdict, str | None]:
    reason = denied_by_env(request, settings)
    if reason:
        return "deny", reason
    if any(matches(rule, request) for rule in rules):
        return "allow", None
    level = clamp(mode, settings)
    if request.kind in READING and not request.outside_project:
        return "allow", None
    if level == "auto":
        return "allow", None
    if level == "auto_edit" and request.kind in EDITING and not request.outside_project:
        return "allow", None
    return "ask", None
