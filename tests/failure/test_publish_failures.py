"""§11 "GitHub push fails": output retained locally and pushed again; and the publisher failing
closed on a secret or on schema-invalid output, with the previous files left as they were.

The pushes run real git against local repositories in tmp_path. The origin is a local path, so
nothing can reach GitHub.
"""

import asyncio
import logging
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from tests.unit.test_build import NOW, db, make_event, snapshot  # noqa: F401 - db is a fixture
from worker import scheduler
from worker.db.jobs import JobRun
from worker.db.sources import RegistryRow
from worker.notify.telegram import Telegram
from worker.ops_api import OpsApi, Reply
from worker.publish.build import build_all
from worker.publish.push import push_data
from worker.publish.validate import ValidationFailure
from worker.watchdog.checks import Jobs, job_findings
from worker.watchdog.paperclip import IncidentRoutine

from .conftest import OPS_TOKEN, credential, telegram_token

# ─── Fail closed ──────────────────────────────────────────────────────────────────────────────────


def test_a_secret_in_the_output_blocks_every_write_and_keeps_the_last_files(tmp_path, db):  # noqa: F811
    out = tmp_path / "out"
    build_all(None, out, now=NOW)
    before = snapshot(out)
    db["events"] = [make_event(1, summary=f"leaked {credential()} here")]
    with pytest.raises(ValidationFailure, match="secret"):
        build_all(None, out, now=NOW + timedelta(minutes=15))
    assert snapshot(out) == before


def test_schema_invalid_output_blocks_every_write_and_keeps_the_last_files(tmp_path, db):  # noqa: F811
    out = tmp_path / "out"
    build_all(None, out, now=NOW)
    before = snapshot(out)
    db["registry"] = [RegistryRow("s", "n", "AU", "x", "sideways", True)]
    with pytest.raises(ValidationFailure, match="source-health"):
        build_all(None, out, now=NOW + timedelta(minutes=15))
    assert snapshot(out) == before


@pytest.fixture
def recorded(monkeypatch):
    runs: list[JobRun] = []

    async def record(run):
        runs.append(run)

    monkeypatch.setattr(scheduler, "_record", record)
    return runs


async def test_a_blocked_publish_is_not_pushed_and_the_watchdog_calls_it_schema_drift(
    monkeypatch, recorded
):
    async def blocked(**kwargs):
        raise ValidationFailure("source-health.json: schema says no")

    async def push():
        raise AssertionError("a blocked publish must not be pushed")

    monkeypatch.setattr(scheduler, "publish_now", blocked)
    monkeypatch.setattr(scheduler, "push_now", push)
    await scheduler._publish("a test", stands="collection is unaffected")
    assert [(r.job, r.completed, r.note) for r in recorded] == [
        ("publish", False, "ValidationFailure")
    ]
    jobs = Jobs(latest={"publish": recorded[0]}, completed={})
    found = job_findings(jobs, recorded[0].started_at + timedelta(minutes=31))
    assert [(f.kind, f.severity) for f in found] == [("schema-drift", "high")]


async def test_every_outbound_channel_withholds_a_secret_instead_of_sending_it():
    sent: list[httpx.Request] = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    telegram = Telegram(SecretStr(telegram_token()), "42", user_agent="t", transport=transport)
    told = await telegram.send(f"the key is {credential()}")
    routine = IncidentRoutine(
        "http://paperclip.invalid/hook", SecretStr("fake-webhook-secret-TESTONLY"),
        user_agent="t", transport=transport,
    )  # fmt: skip
    fired = await routine.fire({"title": credential()})
    assert told.withheld and fired.withheld and sent == []

    api = OpsApi(
        engine=None, token=SecretStr(OPS_TOKEN), data_dir=Path("."), key_status=lambda: None,
        monthly_budget=20, clock=lambda: NOW,
    )  # fmt: skip
    response = api.render(Reply(200, {"note": credential()}))
    assert response.status == 500 and credential().encode() not in response.data
    assert "withheld" in response.json()["error"]


# ─── GitHub push fails ────────────────────────────────────────────────────────────────────────────


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A repo whose origin is a local path with no repository at it yet: every push fails."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(tmp_path / "remote.git"))
    (repo / "data").mkdir()
    (repo / "data" / "live.json").write_text('{"events": []}\n')
    monkeypatch.setenv("CYBERPULSE_PUBLISH_TOKEN", "fake-publish-token-TESTONLY-0123456789")
    monkeypatch.setenv("DATA_DIR", str(repo / "data"))
    return repo


async def test_a_failed_push_keeps_the_data_and_the_next_publish_pushes_it(
    repo, tmp_path, monkeypatch, recorded, caplog
):
    async def published(**kwargs):
        return [repo / "data" / "live.json"]

    monkeypatch.setattr(scheduler, "publish_now", published)
    caplog.set_level(logging.INFO)
    await scheduler._publish("the fast lane", stands="collection is unaffected")
    assert [(r.job, r.completed, r.note) for r in recorded] == [
        ("publish", True, None),
        ("push", False, "RuntimeError"),
    ]
    assert (repo / "data" / "live.json").read_text() == '{"events": []}\n'
    assert "fake-publish-token-TESTONLY" not in caplog.text

    # GitHub is back: the next scheduled publish pushes what was kept.
    await asyncio.to_thread(git, tmp_path, "init", "--bare", str(tmp_path / "remote.git"))
    await scheduler._publish("the fast lane", stands="collection is unaffected")
    assert (recorded[-1].job, recorded[-1].completed, recorded[-1].changed) == ("push", True, True)
    assert git(tmp_path / "remote.git", "rev-parse", "--verify", "refs/heads/data")


def test_push_failures_past_45_minutes_open_a_push_failure_incident():
    failed = JobRun("push", NOW, NOW, False, note="RuntimeError")
    last_good = JobRun("push", NOW - timedelta(minutes=1), NOW - timedelta(minutes=1), True)
    jobs = Jobs(latest={"push": failed}, completed={"push": last_good})
    assert job_findings(jobs, NOW + timedelta(minutes=43)) == []
    found = job_findings(jobs, NOW + timedelta(minutes=46))
    assert [(f.kind, f.severity) for f in found] == [("push-failure", "high")]


def test_push_data_raises_rather_than_reporting_success(repo):
    with pytest.raises(RuntimeError, match="failed"):
        push_data(repo / "data")
