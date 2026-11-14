"""Settings changed in the app. `.env` is the upper limit: the app can only make permissions stricter.

The overrides are applied to the shared Settings object in place, so every part of the app (the permission
policy, the agents, the project API) sees the effective value at once. The values `.env` gave are kept
separately as the limits.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .config import Settings
from .models import AppSetting

LEVELS = ("ask", "auto_edit", "auto")
OUTSIDE = ("deny", "ask", "mode")
# What the permission screen can change, and how "stricter" is measured (lower index is stricter)
ORDERED = {"omni_approval_default": LEVELS, "omni_approval_max": LEVELS,
           "omni_outside_read": OUTSIDE, "omni_outside_write": OUTSIDE}
SWITCHES = ("omni_allow_native_session_changes", "omni_allow_terminal", "omni_allow_shell", "omni_allow_network", "omni_allow_project_create", "omni_allow_git_clone",
            "omni_allow_claude_subscription", "omni_allow_opencode_free", "omni_allow_agy", "omni_allow_mcp",
            "omni_allow_mcp_stdio")
DENY_EXTRA = "omni_shell_deny_extra"
PERMISSIONS = "permissions"
MODEL_DEFAULTS = "model_defaults"


class SettingError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def read(session: Session, key: str, default: Any) -> Any:
    row = session.get(AppSetting, key)
    return row.value if row is not None and row.value is not None else default


def write(session: Session, key: str, value: Any) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def env_value(env: Settings, field: str) -> Any:
    return getattr(env, field)


def check(env: Settings, overrides: dict[str, Any]) -> dict[str, Any]:
    """Only known fields, each no looser than `.env`. Returns the cleaned overrides."""
    clean: dict[str, Any] = {}
    for field, value in overrides.items():
        if value is None:
            continue
        if field in ORDERED:
            order = ORDERED[field]
            if value not in order:
                raise SettingError(field, f"{field} の値が正しくありません")
            if order.index(value) > order.index(env_value(env, field)):
                raise SettingError(field, f"{field.upper()} は .env の上限（{env_value(env, field)}）より緩くできません")
        elif field in SWITCHES:
            if not isinstance(value, bool):
                raise SettingError(field, f"{field} の値が正しくありません")
            if value and not env_value(env, field):
                raise SettingError(field, f"{field.upper()} は .env で無効です")
        elif field == DENY_EXTRA:
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise SettingError(field, "拒否するコマンドの値が正しくありません")
            value = list(dict.fromkeys(item.strip() for item in value if item.strip() and "," not in item))
        else:
            raise SettingError(field, f"{field} は変更できません")
        clean[field] = value
    return clean


def apply(settings: Settings, env: Settings, overrides: dict[str, Any]) -> None:
    """Writes the effective values into the shared Settings object."""
    for field in (*ORDERED, *SWITCHES):
        setattr(settings, field, overrides.get(field, env_value(env, field)))
    # The default is never looser than the (possibly stricter) maximum
    if LEVELS.index(settings.omni_approval_default) > LEVELS.index(settings.omni_approval_max):
        settings.omni_approval_default = settings.omni_approval_max
    extra = [item for item in overrides.get(DENY_EXTRA, []) if item not in env.shell_deny()]
    settings.omni_shell_deny = ",".join([*env.shell_deny(), *extra])


def load(sessions, settings: Settings, env: Settings) -> None:
    with sessions() as session:
        overrides = read(session, PERMISSIONS, {})
    try:
        overrides = check(env, overrides)
    except SettingError:
        # .env became stricter than what was saved: fall back to .env rather than start looser
        overrides = {}
    apply(settings, env, overrides)


def permissions(settings: Settings, env: Settings, overrides: dict[str, Any]) -> dict[str, Any]:
    fields = (*ORDERED, *SWITCHES)
    return {"values": {field: getattr(settings, field) for field in fields},
            "limits": {field: env_value(env, field) for field in fields},
            "changed": sorted(field for field in overrides if field != DENY_EXTRA),
            "shell_deny_env": env.shell_deny(),
            "shell_deny_extra": [item for item in settings.shell_deny() if item not in env.shell_deny()],
            "projects_dir": str(env.omni_projects_dir)}
