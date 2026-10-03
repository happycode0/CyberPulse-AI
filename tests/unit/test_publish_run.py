"""The worker-facing publish wrapper: where the connection and output directory come from, and
why two publishes can never run at once."""

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from worker.publish import run as publish_run
from worker.publish.validate import ValidationFailure


class _Conn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self.log.append("connect")
        return self

    def __exit__(self, *exc):
        self.log.append("close")
        return False


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Stub the engine, settings and builder, recording what `build_all` was handed."""
    log = []
    seen = {}

    class Engine:
        def connect(self):
            return _Conn(log)

    class Settings:
        data_dir = tmp_path / "data"

    def fake_build_all(conn, out_dir, *, now):
        seen["out_dir"] = out_dir
        seen["now"] = now
        log.append("build")
        return [out_dir / "live.json"]

    monkeypatch.setattr(publish_run, "get_engine", Engine)
    monkeypatch.setattr(publish_run, "get_settings", Settings)
    monkeypatch.setattr(publish_run, "build_all", fake_build_all)
    return log, seen, Settings.data_dir


async def test_it_builds_into_the_configured_data_dir(wired):
    log, seen, data_dir = wired
    written = await publish_run.publish_now()
    assert seen["out_dir"] == data_dir
    assert written == [data_dir / "live.json"]
    assert log == ["connect", "build", "close"]


async def test_the_connection_is_closed_even_when_the_build_fails(wired, monkeypatch):
    """A publish that leaked a connection every 15 minutes would exhaust the pool within a day."""
    log, _, _ = wired

    def boom(conn, out_dir, *, now):
        log.append("build")
        raise ValidationFailure("a secret reached a payload")

    monkeypatch.setattr(publish_run, "build_all", boom)
    with pytest.raises(ValidationFailure):
        await publish_run.publish_now()
    assert log == ["connect", "build", "close"]


async def test_a_validation_failure_is_raised_rather_than_reported_as_a_publish(wired, monkeypatch):
    """Fail-closed has to reach the caller: a swallowed failure would log files that do not exist."""

    def boom(conn, out_dir, *, now):
        raise ValidationFailure("schema mismatch")

    monkeypatch.setattr(publish_run, "build_all", boom)
    with pytest.raises(ValidationFailure):
        await publish_run.publish_now()


async def test_the_supplied_moment_is_used_so_payloads_can_be_stamped_consistently(wired):
    _, seen, _ = wired
    moment = datetime(2026, 10, 1, 11, 38, tzinfo=UTC)
    await publish_run.publish_now(now=moment)
    assert seen["now"] == moment


async def test_an_omitted_moment_defaults_to_utc_now(wired):
    _, seen, _ = wired
    before = datetime.now(UTC)
    await publish_run.publish_now()
    assert before <= seen["now"] <= datetime.now(UTC)
    assert seen["now"].tzinfo is not None


async def test_two_publishes_never_overlap(monkeypatch):
    """The FAST and NORMAL cadences coincide at 00:00, 04:00, 08:00 and so on.

    `build_all` stages files and then renames them into place, so overlapping publishes could
    leave data/ holding files from two different builds — each individually schema-valid, which is
    why the lock has to prevent it rather than validation catching it afterwards.
    """
    events = []

    def slow_build(now):
        events.append("enter")
        # A real sleep, not an await: _build runs in a worker thread via asyncio.to_thread, so this
        # is what overlapping publishes would actually look like.
        time.sleep(0.05)
        events.append("exit")
        return [Path("data/live.json")]

    monkeypatch.setattr(publish_run, "_build", slow_build)
    await asyncio.gather(publish_run.publish_now(), publish_run.publish_now())
    assert events == ["enter", "exit", "enter", "exit"]


async def test_the_build_runs_off_the_event_loop(monkeypatch):
    """A synchronous build on the loop would delay the next lane's trigger, which makes a slow
    publish indistinguishable from a stalled collection."""
    seen = {}

    def record(now):
        seen["thread"] = threading.get_ident()
        return []

    monkeypatch.setattr(publish_run, "_build", record)
    await publish_run.publish_now()
    assert seen["thread"] != threading.get_ident()
