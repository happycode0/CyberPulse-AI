"""The worker-facing publish wrapper: where the connection and output directory come from, and
why two publishes can never run at once."""

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from worker.publish import run as publish_run
from worker.publish.push import PushResult
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


# ─── Pushing ──────────────────────────────────────────────────────────────────────────────────────


async def test_a_host_without_a_publish_token_pushes_nothing(wired, monkeypatch):
    monkeypatch.setattr(publish_run, "publish_token_configured", lambda: False)
    monkeypatch.setattr(publish_run, "push_data", lambda *a, **k: pytest.fail("pushed"))
    assert await publish_run.push_now() is None


async def test_a_push_sends_the_configured_data_dir(wired, monkeypatch):
    _, _, data_dir = wired
    sent = []
    result = PushResult(pushed=True, commit_sha="a" * 40, reason=None)
    monkeypatch.setattr(publish_run, "publish_token_configured", lambda: True)
    monkeypatch.setattr(publish_run, "push_data", lambda d: sent.append(d) or result)
    assert await publish_run.push_now() is result
    assert sent == [data_dir]


async def test_a_push_never_overlaps_a_build(wired, monkeypatch):
    """The push hashes data/ while it runs; a build renaming new files in meanwhile would push a
    tree no single build produced."""
    events = []

    def slow(name):
        def run(*_):
            events.append(f"{name} in")
            time.sleep(0.05)
            events.append(f"{name} out")
            return [] if name == "build" else PushResult(False, None, "no changes")

        return run

    monkeypatch.setattr(publish_run, "_build", slow("build"))
    monkeypatch.setattr(publish_run, "push_data", slow("push"))
    monkeypatch.setattr(publish_run, "publish_token_configured", lambda: True)
    # A lock binds to the loop that first waits on it, and each test has its own loop.
    monkeypatch.setattr(publish_run, "_PUBLISH_LOCK", asyncio.Lock())
    await asyncio.gather(publish_run.push_now(), publish_run.publish_now())
    assert events in (
        ["push in", "push out", "build in", "build out"],
        ["build in", "build out", "push in", "push out"],
    )
