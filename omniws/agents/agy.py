"""Antigravity CLI (`agy acp`) through the same ACP client as OpenCode.

Uses only the CLI's own Google login; the app never sees those credentials. Off unless OMNI_ALLOW_AGY is set:
whether Antigravity's terms allow driving the CLI from another app is unclear, so it is personal use at the
user's own risk (as with the Claude subscription).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..config import Settings
from ..providers.base import ModelInfo, ProviderError
from .acp import AcpConnection, AcpError
from .base import Turn
from .opencode import OpenCodeAgent

DEFAULT_MODEL = "default"
AUTH_REQUIRED = -32000  # ACP's "authentication required"


def find_executable() -> str | None:
    found = shutil.which("agy")
    local = Path.home() / ".local" / "bin" / "agy"
    return found or (str(local) if local.is_file() else None)


def model_options(result: dict[str, Any]) -> list[dict[str, str]]:
    """The model choices in session/new's configOptions (flat or grouped)."""
    option = next((item for item in result.get("configOptions") or [] if item.get("id") == "model"), None)
    found: list[dict[str, str]] = []

    def collect(items: list[Any]) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("options"), list):
                collect(item["options"])
            elif item.get("value"):
                found.append({"id": str(item["value"]), "name": str(item.get("name") or item["value"])})

    collect((option or {}).get("options") or [])
    return found


class AgyAgent(OpenCodeAgent):
    id = "agy"
    label = "Antigravity（agy）"

    def __init__(self, settings: Settings, executable: str | None = None) -> None:
        super().__init__(settings, registry=None, executable=executable or find_executable())  # type: ignore[arg-type]
        self.models_file = settings.omni_data_dir / "agy-models.json"

    def installed(self) -> bool:
        return self.executable is not None and self.settings.omni_allow_agy

    def acp_argv(self) -> list[str]:
        return [self.executable or "agy", "acp"]

    async def build_config(self) -> tuple[dict[str, str], str]:
        # Only HOME, PATH and LANG: no API keys from .env, agy uses its own login
        return self.base_env(), "agy"

    async def list_models(self) -> list[ModelInfo]:
        """agy's default model, then the ones a session offered last time (there is no listing without one)."""
        try:
            known = json.loads(self.models_file.read_text())
        except (OSError, ValueError):
            known = []
        models = [ModelInfo(id=DEFAULT_MODEL, name="agy の既定")]
        models += [ModelInfo(id=item["id"], name=item["name"]) for item in known
                   if isinstance(item, dict) and item.get("id") and item["id"] != DEFAULT_MODEL]
        return models

    def remember_options(self, result: dict[str, Any]) -> None:
        options = model_options(result)
        if options:
            self.models_file.parent.mkdir(parents=True, exist_ok=True)
            self.models_file.write_text(json.dumps(options, ensure_ascii=False))

    def session_error(self, exc: AcpError) -> ProviderError:
        if exc.code == AUTH_REQUIRED or "auth" in (exc.message or "").lower():
            return ProviderError("auth", "agy にログインしていません。サーバーの端末で agy を起動してログインしてください")
        return super().session_error(exc)

    async def select_model(self, connection: AcpConnection, session_id: str, turn: Turn) -> None:
        if turn.model == DEFAULT_MODEL:
            return
        await super().select_model(connection, session_id, turn)

    async def forget(self, native_session: str, cwd: Path) -> None:
        """agy has no command to delete a session: temporary conversations stay in agy's own history."""
