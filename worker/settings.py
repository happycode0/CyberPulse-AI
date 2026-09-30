"""Settings and configuration for CyberPulse-AI worker."""

import functools
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    openrouter_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    nvd_api_key: SecretStr | None = None
    github_token: SecretStr | None = None
    github_repository: str = "happycode0/CyberPulse-AI"
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    raw_cache_dir: Path = Path("/var/cache/cyberpulse")
    data_dir: Path = Path("data")
    user_agent: str = "CyberPulse-AI/1.0 (+https://github.com/happycode0/CyberPulse-AI)"

    def __repr__(self) -> str:
        """Return a string representation without leaking secrets."""
        # Redact the database URL password
        safe_db_url = self.database_url.replace(
            self.database_url.split("://")[1].split("@")[0],
            "***:***"
        ) if "@" in self.database_url else self.database_url
        
        fields_repr = [
            f"database_url={safe_db_url!r}",
            f"openrouter_api_key={self.openrouter_api_key!r}",
            f"tavily_api_key={self.tavily_api_key!r}",
            f"nvd_api_key={self.nvd_api_key!r}",
            f"github_token={self.github_token!r}",
            f"github_repository={self.github_repository!r}",
            f"telegram_bot_token={self.telegram_bot_token!r}",
            f"telegram_chat_id={self.telegram_chat_id!r}",
            f"raw_cache_dir={self.raw_cache_dir!r}",
            f"data_dir={self.data_dir!r}",
            f"user_agent={self.user_agent!r}",
        ]
        return f"Settings({', '.join(fields_repr)})"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get the cached Settings instance.

    Returns
    -------
    Settings
        Cached application settings.
    """
    return Settings()
