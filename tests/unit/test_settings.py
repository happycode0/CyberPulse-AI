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


def test_user_agent_identifies_project(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert get_settings.__wrapped__().user_agent == (
        "CyberPulse-AI/1.0 (+https://github.com/happycode0/CyberPulse-AI)"
    )
