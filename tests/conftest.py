import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from worker.db.session import make_engine, to_sqlalchemy_url
from worker import settings as settings_module


@pytest.fixture(autouse=True)
def mock_settings(monkeypatch, tmp_path):
    """Mock settings for tests, avoiding database_url requirement."""
    # If DATABASE_URL is not set, provide a dummy value
    if "DATABASE_URL" not in os.environ:
        monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    
    yield


@pytest.fixture(scope="session")
def pg_engine():
    """Engine bound to a scratch database, created and dropped per test session.

    Skips when `DATABASE_URL` is unset so DB-backed tests never error offline.
    """
    base_url = os.environ.get("DATABASE_URL")
    if not base_url:
        pytest.skip("DATABASE_URL not set")

    scratch = f"cyberpulse_test_{uuid.uuid4().hex[:12]}"
    admin = make_engine(base_url).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'create database "{scratch}"'))

    scratch_url = make_url(to_sqlalchemy_url(base_url)).set(database=scratch)
    engine = make_engine(scratch_url.render_as_string(hide_password=False))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'drop database if exists "{scratch}" with (force)'))
        admin.dispose()
