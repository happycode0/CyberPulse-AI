"""The §11 rows the watchdog and the ops API answer for: Postgres down, Paperclip down, a stale
public site (the worker's side of "Pages build fails"), Telegram failing and retried, one agent
failing, the circuit breaker, and a duplicate storm.

The watchdog runs whole passes. Its real probes go over httpx.MockTransport, the real Telegram
and Incident routine too, and a down database is an engine that refuses every connection. When
the database is up, its tables are kept in memory: notices by tests/failure/conftest.py's
Notifications, which keeps the retry rule, and incidents by `Store`, which moves them with the
real reconcile.plan as worker/db/incidents.py does.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from worker import scheduler
from worker.db.incidents import BREAKER_LIMIT, Incident, PassResult, VerdictResult
from worker.db.jobs import JobRun
from worker.db.watchdog import ping
from worker.models import Lane
from worker.notify.telegram import Telegram
from worker.ops_api import WAKE_JOBS, OpsApi, Verdict
from worker.pipeline import run as pipeline_run
from worker.watchdog import run as watchdog_run
from worker.watchdog.checks import Snapshot, Volume
from worker.watchdog.paperclip import IncidentRoutine
from worker.watchdog.reconcile import Known, plan
from worker.watchdog.run import Watchdog

from .conftest import OPS_TOKEN, credential, down_engine, telegram_token, watchdog_settings

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
HEALTH = "http://server:3100/api/health"
AUTH = {"Authorization": f"Bearer {OPS_TOKEN}"}


class Store:
    """The incidents table in memory, moved by the real plan."""

    def __init__(self):
        self.rows: dict[int, Incident] = {}

    def record_pass(self, engine, findings, covered, now):
        known = [
            Known(i.id, i.kind, i.subject, i.status == "resolved", i.clear_since, i.resolved_at)
            for i in self.rows.values()
        ]
        p = plan(known, findings, covered, now)
        opened, resolved = [], []
        for f in p.insert:
            i = Incident(len(self.rows) + 1, f.kind, f.subject, f.severity, "open", f.title,
                         f.evidence, now, now, 1, None, None, 0, False, 0)  # fmt: skip
            self.rows[i.id] = i
            opened.append(i)
        for id_, f in p.reopen:
            i = replace(self.rows[id_], status="open", resolved_at=None, clear_since=None,
                        reopened=self.rows[id_].reopened + 1, last_seen=now, checks=1)  # fmt: skip
            self.rows[id_] = i
            opened.append(i)
        for id_, f in p.update:
            i = self.rows[id_]
            self.rows[id_] = replace(i, last_seen=now, checks=i.checks + 1, clear_since=None)
        for id_ in p.clearing:
            self.rows[id_] = replace(self.rows[id_], clear_since=now)
        for id_ in p.resolve:
            self.rows[id_] = replace(self.rows[id_], status="resolved", resolved_at=now)
            resolved.append(self.rows[id_])
        unresolved = [i for i in self.rows.values() if i.status != "resolved"]
        return PassResult(opened, resolved, unresolved)


@pytest.fixture
def world(monkeypatch, notifications):
    """The database's state, with `up` deciding whether ping reaches it."""
    state = {"up": True, "snapshot": lambda now: Snapshot(now), "jobs": []}
    store = Store()
    state["store"], state["notices"] = store, notifications

    def gather(engine, now, *, budget):
        return state["snapshot"](now)

    monkeypatch.setattr(watchdog_run, "ping", lambda engine: state["up"])
    monkeypatch.setattr(watchdog_run, "gather", gather)
    monkeypatch.setattr(watchdog_run, "record_pass", store.record_pass)
    monkeypatch.setattr(watchdog_run, "claim", notifications.claim)
    monkeypatch.setattr(watchdog_run, "settle", notifications.settle)
    monkeypatch.setattr(watchdog_run, "record_job", lambda engine, run: state["jobs"].append(run))
    return state


class Answers:
    """A MockTransport handler answering each request with the next scripted reply."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, type) and issubclass(reply, Exception):
            raise reply("injected", request=request)
        return httpx.Response(reply, json={"ok": reply == 200, "description": "injected"})


def telegram(answers: Answers) -> Telegram:
    return Telegram(
        SecretStr(telegram_token()), "42", user_agent="t", transport=httpx.MockTransport(answers)
    )


def routine(answers: Answers) -> IncidentRoutine:
    return IncidentRoutine(
        "http://server:3100/api/routine-triggers/public/pub-TESTONLY/fire",
        SecretStr("fake-webhook-secret-TESTONLY"),
        user_agent="t",
        transport=httpx.MockTransport(answers),
    )


def watchdog(tg=None, crew=None, *, token=False, paperclip_url="", **transports) -> Watchdog:
    return Watchdog(
        watchdog_settings(watchdog_paperclip_url=paperclip_url),
        telegram=lambda s: tg,
        routine=lambda s: crew,
        token_configured=lambda: token,
        **transports,
    )


def explosion(now):
    return Snapshot(now, volume=Volume(fresh_24h=400, fresh_6h=300, usual=(40, 40, 40)))


# ─── Postgres down ────────────────────────────────────────────────────────────────────────────────


def test_a_refused_connection_is_a_failed_ping():
    assert ping(down_engine()) is False


async def test_postgres_down_is_alerted_once_after_three_passes_and_its_return_too(monkeypatch):
    sent = Answers(200)
    dog = watchdog(telegram(sent))
    engine = down_engine()
    for n in range(5):
        summary = await dog.run_pass(engine, NOW + n * 5 * MIN)
        assert summary.database is False
    assert len(sent.requests) == 1
    assert "has not reached its database" in json.loads(sent.requests[0].content)["text"]

    claimed: list[str] = []
    monkeypatch.setattr(watchdog_run, "ping", lambda engine: True)
    monkeypatch.setattr(watchdog_run, "gather", lambda engine, now, *, budget: Snapshot(now))
    monkeypatch.setattr(watchdog_run, "record_pass", lambda *a: PassResult())
    monkeypatch.setattr(watchdog_run, "claim", lambda e, **kw: claimed.append(kw["key"]) or None)
    monkeypatch.setattr(watchdog_run, "record_job", lambda engine, run: None)
    await dog.run_pass(engine, NOW + 30 * MIN)
    assert claimed == [f"system_failure:db:{NOW.isoformat()}"] and dog.db_failures == 0


async def test_a_database_alert_that_fails_to_send_is_tried_on_the_next_pass():
    sent = Answers(502, 200)
    dog = watchdog(telegram(sent))
    for n in range(4):
        await dog.run_pass(down_engine(), NOW + n * 5 * MIN)
    assert len(sent.requests) == 2 and dog.db_alerted is True


async def test_postgres_down_halts_collection_before_any_fetch_and_nothing_is_published(
    monkeypatch,
):
    """The run stops at its first read; no_real_network proves no feed was fetched."""
    engine = down_engine()
    with pytest.raises(OperationalError):
        await pipeline_run.run_lane(Lane.FAST, engine=engine, now=NOW)

    published: list[str] = []

    async def run_lane(lane):
        await pipeline_run.run_lane(lane, engine=engine, now=NOW)

    async def publish(after, *, stands):
        published.append(after)

    monkeypatch.setattr(scheduler.pipeline_run, "run_lane", run_lane)
    monkeypatch.setattr(scheduler, "_publish", publish)
    await scheduler._run_lane_job(Lane.FAST)
    assert published == []


def test_the_ops_api_answers_503_while_the_database_is_down():
    api = ops_api(engine=down_engine())
    events = api.handle("GET", "/ops/events", AUTH)
    verdict = post_verdict(api)
    report = api.handle("POST", "/ops/followup/3", AUTH, b'{"result": "nothing new"}')
    assert (events.status, verdict.status, report.status) == (503, 503, 503)
    assert "send it again" in verdict.json()["error"] and "send it again" in report.json()["error"]


# ─── Paperclip down ───────────────────────────────────────────────────────────────────────────────


async def test_paperclip_down_is_found_on_the_third_probe_and_told_to_telegram_only(world):
    probes, sent, fires = Answers(httpx.ConnectError), Answers(200), Answers(httpx.ConnectError)
    dog = watchdog(
        telegram(sent), routine(fires), paperclip_url=HEALTH,
        paperclip_transport=httpx.MockTransport(probes),
    )  # fmt: skip
    for n in range(3):
        await dog.run_pass(None, NOW + n * 5 * MIN)
    [incident] = world["store"].rows.values()
    assert (incident.kind, incident.severity) == ("paperclip-down", "high")
    assert len(probes.requests) == 3 and len(sent.requests) == 1 and fires.requests == []
    assert "TELETRAAN" not in json.loads(sent.requests[0].content)["text"]


async def test_with_the_routine_unreachable_telegram_is_still_told_and_the_fire_retried(world):
    world["snapshot"] = explosion
    sent, fires = Answers(200), Answers(httpx.ConnectError, httpx.ConnectError, 200)
    dog = watchdog(telegram(sent), routine(fires))
    for n in range(4):
        await dog.run_pass(None, NOW + n * 5 * MIN)
    notices = world["notices"].rows
    assert notices["incident:1"]["status"] == "sent" and len(sent.requests) == 1
    assert notices["paperclip:incident:1"] == {
        "id": 2, "status": "sent", "attempts": 3, "error": None, "channel": "paperclip"
    }  # fmt: skip
    assert len(fires.requests) == 3


async def test_with_paperclip_down_the_worker_keeps_collecting_and_publishing(monkeypatch):
    """No step of a lane run or its publish asks Paperclip anything."""
    steps: list[str] = []

    async def run_lane(lane):
        steps.append(f"collect {lane.value}")

    async def publish_now(**kwargs):
        steps.append("publish")
        return []

    async def push_now():  # None: this host has no publish token
        steps.append("push")

    async def record(run):
        steps.append(f"record {run.job}")

    monkeypatch.setattr(scheduler.pipeline_run, "run_lane", run_lane)
    monkeypatch.setattr(scheduler, "publish_now", publish_now)
    monkeypatch.setattr(scheduler, "push_now", push_now)
    monkeypatch.setattr(scheduler, "_record", record)
    await scheduler._run_lane_job(Lane.FAST)
    assert steps == ["collect fast", "publish", "record publish", "push"]


# ─── Pages build fails: the public site goes stale ────────────────────────────────────────────────


def site(generated_at=None, status=200):
    def handler(request):
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"generated_at": generated_at.isoformat()})

    return httpx.MockTransport(handler)


async def test_site_data_four_hours_old_opens_a_site_stale_incident(world):
    sent = Answers(200)
    dog = watchdog(telegram(sent), token=True, site_transport=site(NOW - timedelta(hours=4)))
    await dog.run_pass(None, NOW)
    [incident] = world["store"].rows.values()
    assert (incident.kind, incident.severity) == ("site-stale", "high") and sent.requests


@pytest.mark.parametrize("status", [404, 500])
async def test_an_unreadable_site_opens_an_incident_only_on_the_third_failed_read(world, status):
    dog = watchdog(token=True, site_transport=site(status=status))
    for n in range(2):
        await dog.run_pass(None, NOW + n * 5 * MIN)
        assert world["store"].rows == {}
    await dog.run_pass(None, NOW + 10 * MIN)
    [incident] = world["store"].rows.values()
    assert incident.kind == "site-stale" and incident.evidence["error"] == f"HTTP {status}"


# ─── Telegram fails, then is retried ──────────────────────────────────────────────────────────────


async def test_a_failed_telegram_notice_is_retried_and_the_token_never_kept(world):
    world["snapshot"] = explosion
    sent = Answers(502, httpx.ConnectError, 200)
    dog = watchdog(telegram(sent))
    errors = []
    for n in range(4):
        await dog.run_pass(None, NOW + n * 5 * MIN)
        errors.append(world["notices"].rows["incident:1"]["error"])
    row = world["notices"].rows["incident:1"]
    assert (row["status"], row["attempts"], len(sent.requests)) == ("sent", 3, 3)
    assert errors[:2] == ["HTTP 502: injected", "ConnectError"]
    assert not any(telegram_token() in (e or "") for e in errors)


async def test_a_notice_that_keeps_failing_stops_after_three_attempts(world):
    world["snapshot"] = explosion
    sent = Answers(502)
    dog = watchdog(telegram(sent))
    for n in range(6):
        await dog.run_pass(None, NOW + n * 5 * MIN)
    row = world["notices"].rows["incident:1"]
    assert (row["status"], row["attempts"], len(sent.requests)) == ("failed", 3, 3)


# ─── Duplicate storm ──────────────────────────────────────────────────────────────────────────────


async def test_a_duplicate_storm_opens_a_high_incident_and_is_told(world):
    world["snapshot"] = explosion
    sent = Answers(200)
    await watchdog(telegram(sent)).run_pass(None, NOW)
    [incident] = world["store"].rows.values()
    assert (incident.kind, incident.severity) == ("duplicate-explosion", "high")
    assert incident.evidence["limit_6h"] == 100 and len(sent.requests) == 1


# ─── The circuit breaker ──────────────────────────────────────────────────────────────────────────


def ops_api(engine=None) -> OpsApi:
    return OpsApi(
        engine=engine, token=SecretStr(OPS_TOKEN), data_dir=Path("."), key_status=lambda: None,
        monthly_budget=20, clock=lambda: NOW,
    )  # fmt: skip


def post_verdict(api, verdict="fail", reasons="Ran the parser tests: the new fixture fails."):
    body = json.dumps({"verdict": verdict, "pr": 42, "reasons": reasons}).encode()
    return api.handle("POST", "/ops/incidents/1/verdict", AUTH, body)


async def test_three_failed_verdicts_trip_the_breaker_and_then_only_a_person_is_told(
    world, monkeypatch
):
    """The breaker rule is the SQL in record_verdict (tests/failure/test_breaker_db.py runs it);
    here its answer is relayed by the API, and the next pass stops the crew."""
    world["snapshot"] = explosion
    sent, fires = Answers(200), Answers(200)
    dog = watchdog(telegram(sent), routine(fires))
    await dog.run_pass(None, NOW)
    assert len(fires.requests) == 1

    store = world["store"]

    def record_verdict(engine, incident_id, *, verdict, pr, reasons, now):
        i = store.rows[incident_id]
        if i.needs_human:
            return VerdictResult("halted", i)
        failures = i.fix_failures + 1
        store.rows[incident_id] = i = replace(
            i, fix_failures=failures, needs_human=failures >= BREAKER_LIMIT
        )
        return VerdictResult("recorded", i, tripped=i.needs_human)

    monkeypatch.setattr("worker.ops_api.record_verdict", record_verdict)
    api = ops_api()
    answers = [post_verdict(api) for _ in range(BREAKER_LIMIT + 1)]
    assert [a.status for a in answers] == [200, 200, 200, 409]
    assert "circuit breaker has tripped" in answers[2].json()["message"]
    assert answers[3].json()["result"] == "halted"

    await dog.run_pass(None, NOW + 5 * MIN)
    assert len(fires.requests) == 1  # the crew is not woken again
    assert "incident-breaker:1" in world["notices"].rows
    assert "needs a human" in json.loads(sent.requests[-1].content)["text"]


def test_a_verdict_holding_a_secret_is_refused():
    answer = post_verdict(ops_api(), reasons=f"the token is {credential()}")
    assert answer.status == 400 and answer.json()["result"] == "rejected"


# ─── One agent fails ──────────────────────────────────────────────────────────────────────────────


def test_one_agent_s_wake_failing_does_not_stop_the_others(monkeypatch):
    api = ops_api()

    def verdict(job):
        if job == "groundtruth":
            raise RuntimeError("injected")
        return Verdict(True, {"checked": job})

    monkeypatch.setattr(api, "verdict", verdict)
    answers = {
        slug: api.handle("POST", f"/ops/agents/{slug}/wake", {}, json.dumps({"job": job}).encode())
        for slug, job in WAKE_JOBS.items()
    }
    assert answers.pop("librarian").status == 500
    assert {slug: a.status for slug, a in answers.items()} == dict.fromkeys(answers, 200)


async def test_a_watchdog_pass_that_raises_is_recorded_and_the_schedule_goes_on(monkeypatch):
    recorded: list[JobRun] = []

    class Broken:
        async def run_pass(self, engine, now):
            raise RuntimeError("injected")

    async def record(run):
        recorded.append(run)

    monkeypatch.setattr(scheduler, "_the_watchdog", Broken)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    monkeypatch.setattr(scheduler, "_record", record)
    await scheduler._watchdog_job()
    assert [(r.job, r.completed, r.note) for r in recorded] == [("watchdog", False, "RuntimeError")]
