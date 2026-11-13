"""Settings from the environment and `.env`. What the user changes in the app lives in the database."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost")


class Settings(BaseSettings):
    # Real environment variables win over `.env`. Empty values count as unset.
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8",
                                      env_ignore_empty=True, extra="ignore")

    # The app itself
    omni_password: SecretStr | None = None
    omni_secret: SecretStr | None = None
    omni_host: str = "127.0.0.1"
    omni_port: int = 8300
    omni_data_dir: Path = ROOT / "data"
    # Code-mode projects are the folders directly under this directory
    omni_projects_dir: Path = Path.home() / "omni-projects"
    # Extra host names the browser may use, comma separated (for example the Tailscale name)
    omni_allowed_hosts: str = ""

    # API
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None

    # Local LLMs (OpenAI compatible)
    ollama_base_url: str | None = None
    local_openai_base_url: str | None = None
    local_openai_api_key: SecretStr | None = None

    # Subscriptions through the official CLIs
    claude_code_oauth_token: SecretStr | None = None
    codex_auth_json_b64: SecretStr | None = None

    @field_validator("omni_data_dir", "omni_projects_dir", mode="before")
    @classmethod
    def expand_home(cls, value: object) -> object:
        return Path(value).expanduser() if isinstance(value, (str, Path)) else value

    def allowed_hosts(self) -> list[str]:
        extra = [host.strip().lower() for host in self.omni_allowed_hosts.split(",") if host.strip()]
        if any("*" in host for host in extra):
            raise ValueError("OMNI_ALLOWED_HOSTS accepts exact host names only")
        return list(dict.fromkeys([*DEFAULT_ALLOWED_HOSTS, *extra]))


@lru_cache
def get_settings() -> Settings:
    return Settings()
