"""SQLAlchemy engine construction for CyberPulse-AI."""

import functools

from sqlalchemy import Engine, create_engine

from worker.settings import get_settings


def to_sqlalchemy_url(url: str) -> str:
    """Return `url` with the psycopg (v3) driver selected.

    `DATABASE_URL` is a plain libpq-style URL (`postgresql://...`); SQLAlchemy
    would otherwise default that scheme to psycopg2.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    """Create an engine for `url`. Connections are opened lazily."""
    return create_engine(to_sqlalchemy_url(url), pool_pre_ping=True)


@functools.lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide engine built from `DATABASE_URL`."""
    return make_engine(get_settings().database_url)
