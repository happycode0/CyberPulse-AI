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
    # Product and version only, with no contact URL, because the URL is what gets us blocked.
    # Measured 2026-10-01 against https://www.cyber.gov.au/rss/alerts: the edge accepts
    # "CyberPulse-AI/1.0", "curl/8.5.0" and "feedparser/6.0.11" with 200, and drops the
    # connection outright (not a 403 — no response at all, so the client sees a 30s read
    # timeout) for "CyberPulse-AI/1.0 (+https://github.com/happycode0/CyberPulse-AI)" and for
    # a Mozilla-prefixed variant carrying the same URL. www.dta.gov.au behaves identically.
    # That cost all four ACSC feeds plus DTA — the primary Australian sources of an
    # Australia-first product — so the URL goes. The string still identifies the client
    # honestly; it is not disguised as a browser, which would be the other way to get through
    # and is not one we are taking. www.cisa.gov wants the opposite of what ACSC wants and
    # rejects this string — but only in combination with httpx's default Accept-Encoding, so
    # that conflict is settled in the headers rather than here. See worker/collectors/http.py.
    user_agent: str = "CyberPulse-AI/1.0"

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
