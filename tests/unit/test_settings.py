"""Tests for Settings and version configuration."""

import pytest
from pydantic import ValidationError
from worker.settings import get_settings


def test_settings_reads_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert get_settings.__wrapped__().database_url == "postgresql://u:p@h/db"


def test_settings_missing_database_url_raises(monkeypatch, tmp_path):
    """DATABASE_URL must be required even when .env is present."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Point env_file at a non-existent path so .env doesn't supply DATABASE_URL
    monkeypatch.setattr("worker.settings.Settings.model_config", {"env_file": str(tmp_path / "nonexistent.env"), "extra": "ignore"})
    with pytest.raises(ValidationError):
        get_settings.__wrapped__()


def test_optional_keys_default_to_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Ensure .env doesn't supply the key either
    monkeypatch.setattr("worker.settings.Settings.model_config", {"env_file": "/nonexistent", "extra": "ignore"})
    assert get_settings.__wrapped__().openrouter_api_key is None


def test_repr_does_not_leak_secrets(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secretpw@h/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-TOPSECRET")
    text = repr(get_settings.__wrapped__())
    assert "TOPSECRET" not in text and "secretpw" not in text


def test_the_retired_paperclip_settings_are_ignored_if_left_in_the_environment(monkeypatch):
    """Paperclip was retired on 2026-10-31. A .env that still names its webhooks or its health
    probe starts the worker as before, and nothing of them is read or shown."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    hook = "http://server:3100/api/routine-triggers/public/pub-TESTONLY-9/fire"
    for name in ("INCIDENT", "FOLLOWUP", "DISCOVERY"):
        monkeypatch.setenv(f"PAPERCLIP_{name}_WEBHOOK_URL", hook)
        monkeypatch.setenv(f"PAPERCLIP_{name}_WEBHOOK_SECRET", f"webhook-secret-{name}-TESTONLY")
    monkeypatch.setenv("WATCHDOG_PAPERCLIP_URL", "http://server:3100/api/health")
    settings = get_settings.__wrapped__()
    assert not any(name.startswith(("paperclip_", "watchdog_paperclip")) for name in
                   type(settings).model_fields)
    text = repr(settings)
    assert "pub-TESTONLY" not in text and "webhook-secret" not in text and "paperclip" not in text


def test_user_agent_identifies_project(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert get_settings.__wrapped__().user_agent == "CyberPulse-AI/1.0"


def test_user_agent_carries_no_url(monkeypatch):
    """A URL in the User-Agent is what cyber.gov.au and dta.gov.au drop the connection on.

    Guarded as a test rather than only a comment because adding a contact URL back is the
    obvious, well-intentioned change to make here, and it silently costs every ACSC feed:
    the request does not fail fast, it hangs until the read timeout, which looks like the
    source being slow rather than the client being rejected. See worker/settings.py.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    ua = get_settings.__wrapped__().user_agent
    assert "http" not in ua and "://" not in ua and "." not in ua.split("/")[0], ua
