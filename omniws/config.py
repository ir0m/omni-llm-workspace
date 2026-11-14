"""Settings from the environment and `.env`. What the user changes in the app lives in the database."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost")


class Settings(BaseSettings):
    # Real environment variables win over `.env`. Empty values count as unset.
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8",
                                      env_ignore_empty=True, extra="ignore")

    # The app itself. Sign-in uses passkeys; OMNI_PASSWORD is for development and tests only.
    omni_password: SecretStr | None = None
    omni_secret: SecretStr | None = None
    omni_host: str = "127.0.0.1"
    omni_port: int = 8300
    omni_data_dir: Path = ROOT / "data"
    # Code-mode projects are the folders directly under this directory
    omni_projects_dir: Path = Path.home() / "omni-projects"
    omni_allow_project_create: bool = True
    omni_allow_git_clone: bool = True
    # Extra host names the browser may use, comma separated (for example the Tailscale name)
    omni_allowed_hosts: str = ""
    # Where the app is opened, comma separated (https://host:port). Passkeys work only on these origins:
    # HTTPS (Tailscale Serve) or http://localhost.
    omni_public_urls: str = ""

    # API
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    omni_openai_responses: bool = False
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None

    # Local LLMs (OpenAI compatible)
    ollama_base_url: str | None = None
    local_openai_base_url: str | None = None
    local_openai_api_key: SecretStr | None = None

    # Which providers to offer, comma separated (empty: every provider that is configured)
    omni_providers: str = ""
    # Fixed model lists, comma separated (empty: ask the provider)
    omni_models_anthropic: str = ""
    omni_models_openai: str = ""
    omni_models_gemini: str = ""
    omni_models_openrouter: str = ""
    omni_models_ollama: str = ""
    omni_models_local: str = ""

    # Subscriptions use each official CLI's own login. The app never stores those credentials:
    # Anthropic does not allow third-party apps to collect or intermediate Claude.ai credentials.
    omni_allow_claude_subscription: bool = False
    # OpenCode's free models run on OpenCode's service: code is sent there
    omni_allow_opencode_free: bool = False
    # Antigravity CLI (`agy acp`) with its own Google login. Whether its terms allow use from another app is
    # unclear: personal use at the user's own risk, off by default
    omni_allow_agy: bool = False
    # MCP tools for Chat (API and local models). Local (stdio) servers run programs on this machine
    omni_allow_mcp: bool = False
    omni_allow_mcp_stdio: bool = False

    # Permissions for Code mode. .env is the upper limit; conversations can only be stricter.
    omni_approval_default: Literal["ask", "auto_edit", "auto"] = "auto_edit"
    omni_approval_max: Literal["ask", "auto_edit", "auto"] = "auto"
    omni_allow_shell: bool = True
    # Human-operated terminal: explicitly grants the server user's shell, outside agent policy.
    omni_allow_terminal: bool = False
    # Human-operated file editing from the screen (Docs): saves, new files, renames and deletions in a project
    omni_allow_file_edit: bool = False
    # Typst and LaTeX compilation of project documents on this machine (Docs)
    omni_allow_compile: bool = True
    omni_allow_native_session_changes: bool = True
    # Network access from the agents' sandboxes (package installs, git pull)
    omni_allow_network: bool = True
    # Requests outside the project folder: mode = as the approval mode decides, ask = always confirm, deny = never
    omni_outside_read: Literal["mode", "ask", "deny"] = "mode"
    omni_outside_write: Literal["mode", "ask", "deny"] = "mode"
    omni_shell_deny: str = "sudo,su,rm -rf /,rm -rf ~,mkfs,shutdown,reboot,git push --force,git push -f"

    @field_validator("omni_data_dir", "omni_projects_dir", mode="before")
    @classmethod
    def expand_home(cls, value: object) -> object:
        return Path(value).expanduser() if isinstance(value, (str, Path)) else value

    def public_origins(self) -> list[str]:
        origins = []
        for url in self.omni_public_urls.split(","):
            parts = urlsplit(url.strip())
            if parts.scheme in ("https", "http") and parts.hostname:
                origins.append(f"{parts.scheme}://{parts.netloc}".lower())
        return origins

    def allowed_hosts(self) -> list[str]:
        extra = [host.strip().lower() for host in self.omni_allowed_hosts.split(",") if host.strip()]
        if any("*" in host for host in extra):
            raise ValueError("OMNI_ALLOWED_HOSTS accepts exact host names only")
        public = [urlsplit(origin).hostname or "" for origin in self.public_origins()]
        return list(dict.fromkeys([*DEFAULT_ALLOWED_HOSTS, *extra, *public]))

    def enabled_providers(self) -> set[str] | None:
        names = {name.strip().lower() for name in self.omni_providers.split(",") if name.strip()}
        return names or None

    def shell_deny(self) -> list[str]:
        return [pattern.strip() for pattern in self.omni_shell_deny.split(",") if pattern.strip()]

    def fixed_models(self, provider: str) -> list[str]:
        value = getattr(self, f"omni_models_{provider}", "") or ""
        return [model.strip() for model in value.split(",") if model.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
