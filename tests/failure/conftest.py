"""Shared by the failure-injection tests (PLAN.md §11): a sealed environment, a guard that fails
any test which reaches the real network, a database that refuses every connection, and fake
credentials built at run time so none is ever written out in a credential's shape."""

import httpcore
import psycopg
import pytest
from sqlalchemy import Engine, create_engine

from worker.db.notifications import MAX_ATTEMPTS, Claim
from worker.settings import Settings, get_settings

SECRET_ENV = (
    "OPENROUTER_API_KEY",
    "TAVILY_API_KEY",
    "NVD_API_KEY",
    "GITHUB_TOKEN",
    "CYBERPULSE_PUBLISH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "CYBERPULSE_OPS_TOKEN",
    "PAPERCLIP_INCIDENT_WEBHOOK_URL",
    "PAPERCLIP_INCIDENT_WEBHOOK_SECRET",
)
KEY = "fake-key-TESTONLY"
OPS_TOKEN = "fake-ops-token-TESTONLY-0123456789abcdef"


def telegram_token() -> str:
    return "1000001" + ":" + "fake-bot-token-TESTONLY-0123456789"


def credential() -> str:
    """Shaped like a GitHub token, so the secret scan must catch it."""
    return "gh" + "p_" + "a1B2" * 9


def watchdog_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": "postgresql://x",
        "site_url": "https://example.org/site/",
        "watchdog_paperclip_url": "",
    }
    return Settings(**{**values, **overrides})


def down_engine() -> Engine:
    """An engine whose every connection is refused, as with Postgres stopped."""

    def refuse():
        raise psycopg.OperationalError("connection refused (test)")

    return create_engine("postgresql+psycopg://", creator=refuse)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No real .env, no real credentials, and every path under tmp_path."""
    monkeypatch.chdir(tmp_path)
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WATCHDOG_PAPERCLIP_URL", "")
    monkeypatch.setenv("RAW_CACHE_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Any request that gets past the mock transports to httpcore fails the test."""
    blocked: list[str] = []

    async def handle_async_request(self, request):
        blocked.append(str(request.url))
        raise RuntimeError("real network blocked in tests")

    def handle_request(self, request):
        blocked.append(str(request.url))
        raise RuntimeError("real network blocked in tests")

    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", handle_async_request)
    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", handle_request)
    yield
    assert blocked == [], f"a test reached the real network: {blocked}"


class Notifications:
    """The notifications table's claim and settle, kept in memory with its retry rule: a key is
    claimed again only after a failed send, and at most MAX_ATTEMPTS times in all."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def claim(self, engine, *, kind, key, now, event_id=None, channel="telegram"):
        row = self.rows.get(key)
        if row is None:
            row = {"id": len(self.rows) + 1, "status": "sending", "attempts": 1, "error": None}
            row["channel"] = channel
            self.rows[key] = row
            return Claim(row["id"], 1)
        if row["status"] == "failed" and row["attempts"] < MAX_ATTEMPTS:
            row["status"], row["attempts"] = "sending", row["attempts"] + 1
            return Claim(row["id"], row["attempts"])
        return None

    def settle(self, engine, claimed, outcome, *, now, error=None):
        [row] = [r for r in self.rows.values() if r["id"] == claimed.id]
        row["status"], row["error"] = outcome, error


@pytest.fixture
def notifications():
    return Notifications()
