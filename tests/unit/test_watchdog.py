"""The watchdog without a database: its signatures (worker/watchdog/checks.py), the plan that moves
incidents (reconcile.py), the Paperclip webhook and probe (paperclip.py), its messages, and one
pass (run.py) with the database stood in for.

The SQL is exercised against Postgres in tests/integration/test_watchdog_db.py.
"""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from worker.db.incidents import Incident, PassResult
from worker.db.jobs import JobRun
from worker.db.notifications import Claim
from worker.notify.messages import (
    database_back,
    database_unreachable,
    incident_breaker,
    incident_opened,
    incident_resolved,
)
from worker.notify.telegram import SendResult
from worker.settings import Settings
from worker.watchdog import run as watchdog_run
from worker.watchdog.checks import (
    Cost,
    Finding,
    Jobs,
    LaneRun,
    ProbeReading,
    SiteReading,
    Snapshot,
    SourceState,
    Volume,
    cost_findings,
    evaluate,
    job_findings,
    lane_findings,
    paperclip_findings,
    site_findings,
    source_findings,
    span,
    volume_findings,
)
from worker.watchdog.paperclip import IncidentRoutine, install_log_redaction, probe, redact
from worker.watchdog.reconcile import Known, plan
from worker.watchdog.run import MAX_REOPEN_NOTICES, Watchdog, routine_payload

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
HOOK = "http://server:3100/api/routine-triggers/public/" + "pub-TESTONLY-0123" + "/fire"
SECRET = "fake-webhook-secret-TESTONLY"


def settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": "postgresql://x",
        "site_url": "https://example.org/site/",
    }
    return Settings(**{**values, **overrides})


def kinds(findings) -> list[tuple[str, str, str]]:
    return [(f.kind, f.subject, f.severity) for f in findings]


# ─── Collection ───────────────────────────────────────────────────────────────────────────────────


def runs(*minutes_ago, items=90):
    return [LaneRun(NOW - m * MIN, items) for m in minutes_ago]


def test_a_lane_past_its_limit_has_stopped():
    found = lane_findings({"fast": runs(36), "normal": runs(270, items=320)}, NOW)
    assert kinds(found) == [("no-collection", "fast", "critical")]
    assert found[0].title == "The fast lane has not finished a run for 36 minutes"
    assert found[0].evidence["limit_minutes"] == 35


def test_a_normal_lane_that_stops_is_high():
    assert kinds(lane_findings({"normal": runs(276)}, NOW)) == [("no-collection", "normal", "high")]


def test_a_lane_within_its_limit_or_never_run_or_unscheduled_is_fine():
    assert lane_findings({"fast": runs(34), "normal": [], "deep": runs(9000)}, NOW) == []


def test_runs_that_fetch_nothing_for_half_an_hour_are_zero_volume():
    found = lane_findings({"fast": runs(1, 16, 31, items=0)}, NOW)
    assert kinds(found) == [("zero-volume", "fast", "high")]


def test_empty_runs_close_together_are_not_yet_zero_volume():
    """Three `--once` runs a minute apart are not half an hour of nothing."""
    assert lane_findings({"fast": runs(1, 2, 3, items=0)}, NOW) == []
    assert lane_findings({"fast": [*runs(1, 16, items=0), *runs(31)]}, NOW) == []


# ─── Sources ──────────────────────────────────────────────────────────────────────────────────────


def source(source_id="acsc-alerts", statuses=(), **overrides) -> SourceState:
    values = {
        "source_id": source_id,
        "name": source_id.replace("-", " ").title(),
        "lane": "fast",
        "priority": 1,
        "host": "www.cyber.gov.au",
        "lifecycle": "active",
        "statuses": tuple(statuses),
        "had_items": True,
        "last_error": None,
        "last_checked": NOW,
    }
    return SourceState(**{**values, **overrides})


def test_five_failed_checks_in_a_row_is_a_failing_feed():
    failing = source(statuses=["ok", "error", "timeout", "error", "error", "error"],
                     last_error="HTTP 503")
    sibling = source("acsc-advisories", ["ok", "ok"])
    other = source("bleeping", ["error"], host="www.bleepingcomputer.com")
    found = source_findings([failing, sibling, other])
    assert kinds(found) == [("feed-failing", "acsc-alerts", "high")]
    evidence = found[0].evidence
    assert evidence["failures_in_a_row"] == 5 and evidence["last_error"] == "HTTP 503"
    assert evidence["same_host"] == [{"source": "acsc-advisories", "latest": "ok"}]
    assert evidence["same_host_failing"] == 0


def test_a_failing_feed_says_when_its_whole_host_is_down():
    a = source("a", ["error"] * 5)
    b = source("b", ["timeout"] * 2)
    [finding] = source_findings([a, b])
    assert finding.evidence["same_host_failing"] == 1


def test_a_lower_priority_failing_feed_is_medium():
    assert kinds(source_findings([source(statuses=["error"] * 5, priority=2)]))[0][2] == "medium"


def test_four_failures_or_a_success_between_them_is_not_yet_failing():
    assert source_findings([source(statuses=["error"] * 4)]) == []
    assert source_findings([source(statuses=["error"] * 4 + ["ok"])]) == []


def test_a_feed_that_answers_with_nothing_three_times_has_drifted():
    found = source_findings([source(statuses=["ok", "empty", "empty", "empty"])])
    assert kinds(found) == [("parser-drift", "acsc-alerts", "high")]
    assert found[0].evidence["had_items_before"] is True


def test_a_feed_stale_three_times_is_low():
    found = source_findings([source(statuses=["stale"] * 3)])
    assert kinds(found) == [("stale-feed", "acsc-alerts", "low")]


def test_a_long_error_is_cut():
    [finding] = source_findings([source(statuses=["error"] * 5, last_error="x" * 2000)])
    assert len(finding.evidence["last_error"]) < 400


# ─── Volume ───────────────────────────────────────────────────────────────────────────────────────


def test_far_fewer_fresh_events_than_usual_is_a_collapse():
    found = volume_findings(Volume(fresh_24h=2, fresh_6h=0, usual=(10, 12, 10)))
    assert kinds(found) == [("volume-collapse", "", "high")]
    assert found[0].evidence["usual_median"] == 10


@pytest.mark.parametrize(
    "volume",
    [
        Volume(3, 0, (10, 12, 10)),  # a quarter of usual or more
        Volume(0, 0, (10, 10)),  # too few usual days to judge
        Volume(0, 0, (4, 5, 6)),  # a usual day too small to judge
        Volume(0, 0, ()),  # no history
    ],
)
def test_a_quiet_day_is_not_always_a_collapse(volume):
    assert volume_findings(volume) == []


def test_far_more_fresh_events_than_usual_is_an_explosion():
    found = volume_findings(Volume(fresh_24h=150, fresh_6h=101, usual=(40, 40)))
    assert kinds(found) == [("duplicate-explosion", "", "high")]
    assert found[0].evidence["limit_6h"] == 100


def test_a_busy_day_on_a_busy_system_is_not_an_explosion():
    assert volume_findings(Volume(fresh_24h=900, fresh_6h=450, usual=(400, 420))) == []
    assert volume_findings(Volume(fresh_24h=900, fresh_6h=500, usual=(400,))) == []


# ─── Jobs, publish and push ───────────────────────────────────────────────────────────────────────


def job(name, minutes_ago, *, completed=True, note=None) -> JobRun:
    at = NOW - minutes_ago * MIN
    return JobRun(name, at - MIN, at, completed, note=note)


def jobs(*rows: JobRun) -> Jobs:
    latest, completed = {}, {}
    for r in sorted(rows, key=lambda r: r.finished_at):
        latest[r.job] = r
        if r.completed:
            completed[r.job] = r
    return Jobs(latest, completed)


def test_a_job_with_no_completed_pass_in_its_limit_is_failing():
    found = job_findings(
        jobs(
            job("groundtruth", 14 * 60),
            job("groundtruth", 5, completed=False, note="OperationalError"),
            job("enrichment", 30),
            job("discovery", 60, completed=False),
        ),
        NOW,
    )
    assert kinds(found) == [
        ("job-failing", "groundtruth", "high"),
        ("job-failing", "discovery", "medium"),
    ]
    groundtruth, discovery = found
    assert groundtruth.title == "The groundtruth job has not completed a pass for 14 hours"
    assert groundtruth.evidence["latest_note"] == "OperationalError"
    assert discovery.title == "The discovery job has never completed a pass"


def test_a_job_that_never_ran_here_is_not_failing():
    assert job_findings(jobs(), NOW) == []


def test_a_publish_failing_past_its_limit_is_a_publish_failure():
    found = job_findings(
        jobs(job("publish", 31), job("publish", 1, completed=False, note="OSError")), NOW
    )
    assert kinds(found) == [("publish-failure", "", "high")]
    assert found[0].title == "Building the site's data has failed for 31 minutes"


def test_a_publish_that_failed_once_or_recovered_is_fine():
    assert job_findings(jobs(job("publish", 20), job("publish", 1, completed=False)), NOW) == []
    assert job_findings(jobs(job("publish", 40, completed=False), job("publish", 1)), NOW) == []


def test_a_publish_the_validator_blocks_is_schema_drift():
    found = job_findings(jobs(job("publish", 1, completed=False, note="ValidationFailure")), NOW)
    assert kinds(found) == [("schema-drift", "", "high")]


def test_a_push_failing_past_its_limit_is_a_push_failure():
    found = job_findings(jobs(job("push", 46), job("push", 2, completed=False)), NOW)
    assert kinds(found) == [("push-failure", "", "high")]
    assert job_findings(jobs(job("push", 44), job("push", 2, completed=False)), NOW) == []


# ─── The site, the spend and Paperclip ────────────────────────────────────────────────────────────


def test_site_data_older_than_its_limit_is_stale():
    found = site_findings(SiteReading(NOW - timedelta(hours=3, minutes=1)), NOW)
    assert kinds(found) == [("site-stale", "", "high")]
    assert site_findings(SiteReading(NOW - timedelta(hours=2, minutes=59)), NOW) == []


def test_site_data_that_cannot_be_read_three_times_is_stale():
    found = site_findings(SiteReading(None, "HTTP 404", 3), NOW)
    assert kinds(found) == [("site-stale", "", "high")]
    assert found[0].evidence == {"error": "HTTP 404", "failures_in_a_row": 3}
    assert site_findings(SiteReading(None, "HTTP 404", 2), NOW) == []


def cost(last, usual=(), month="0", budget="20") -> Cost:
    return Cost(Decimal(last), tuple(Decimal(u) for u in usual), Decimal(month), Decimal(budget))


def test_spend_far_above_a_usual_day_is_a_cost_anomaly():
    found = cost_findings(cost("1.50", ("0.10", "0.12", "0.08")))
    assert kinds(found) == [("cost-anomaly", "rate", "high")]
    assert found[0].title == "AI spend in 24 hours is $1.50, against a usual $0.10"


def test_spend_under_the_floor_is_never_an_anomaly():
    assert cost_findings(cost("0.90", ("0.01", "0.01"))) == []
    assert kinds(cost_findings(cost("1.01"))) == [("cost-anomaly", "rate", "high")]


def test_spend_near_the_month_s_budget_is_a_cost_anomaly():
    assert kinds(cost_findings(cost("0", month="18"))) == [("cost-anomaly", "budget", "high")]
    assert cost_findings(cost("0", month="17.99")) == []
    assert cost_findings(cost("0", month="18", budget="0")) == []


def test_paperclip_down_three_times_in_a_row():
    assert kinds(paperclip_findings(ProbeReading(3, "ConnectError"))) == [
        ("paperclip-down", "", "high")
    ]
    assert paperclip_findings(ProbeReading(2, "ConnectError")) == []


def test_evaluate_covers_only_the_parts_it_has():
    assert evaluate(Snapshot(NOW)) == ([], frozenset())
    findings, covered = evaluate(Snapshot(NOW, lanes={"fast": runs(40)}, jobs=jobs()))
    assert kinds(findings) == [("no-collection", "fast", "critical")]
    assert covered == {
        "no-collection", "zero-volume", "job-failing", "publish-failure", "schema-drift",
        "push-failure",
    }


@pytest.mark.parametrize(
    "delta, said",
    [(MIN, "1 minute"), (47 * MIN, "47 minutes"), (5 * 60 * MIN, "5 hours"),
     (timedelta(days=3, hours=2), "3 days")],
)
def test_span(delta, said):
    assert span(delta) == said


# ─── Reconcile ────────────────────────────────────────────────────────────────────────────────────

COVERED = frozenset({"feed-failing", "parser-drift", "stale-feed", "paperclip-down"})


def finding(kind="feed-failing", subject="acsc-alerts", severity="high") -> Finding:
    return Finding(kind, subject, severity, f"{kind} {subject}")


def known(id, kind="feed-failing", subject="acsc-alerts", **overrides) -> Known:
    return Known(id, kind, subject, overrides.pop("resolved", False), **overrides)


def test_a_new_fault_opens_an_incident_once():
    p = plan([], [finding(), finding()], COVERED, NOW)
    assert p.insert == [finding()] and not (p.update or p.reopen or p.clearing or p.resolve)


def test_a_fault_seen_again_updates_its_incident():
    p = plan([known(1)], [finding()], COVERED, NOW)
    assert p.update == [(1, finding())] and p.insert == []


def test_a_fault_back_soon_after_it_resolved_reopens_the_latest_incident():
    p = plan(
        [
            known(1, resolved=True, resolved_at=NOW - timedelta(hours=5)),
            known(2, resolved=True, resolved_at=NOW - timedelta(hours=1)),
        ],
        [finding()],
        COVERED,
        NOW,
    )
    assert p.reopen == [(2, finding())] and p.insert == []


def test_a_fault_back_long_after_it_resolved_opens_a_new_incident():
    p = plan([known(1, resolved=True, resolved_at=NOW - timedelta(hours=7))], [finding()],
             COVERED, NOW)
    assert p.insert == [finding()] and p.reopen == []


def test_an_incident_not_seen_clears_then_resolves():
    assert plan([known(1)], [], COVERED, NOW).clearing == [1]
    almost = plan([known(1, clear_since=NOW - 14 * MIN)], [], COVERED, NOW)
    assert almost.clearing == [] and almost.resolve == []
    assert plan([known(1, clear_since=NOW - 15 * MIN)], [], COVERED, NOW).resolve == [1]


def test_an_incident_whose_kind_was_not_checked_is_left_alone():
    p = plan([known(1, kind="job-failing", subject="groundtruth")], [], COVERED, NOW)
    assert not (p.clearing or p.resolve or p.update)


def test_a_fault_seen_while_clearing_is_updated_not_resolved():
    p = plan([known(1, clear_since=NOW - 30 * MIN)], [finding()], COVERED, NOW)
    assert p.update == [(1, finding())] and p.resolve == []


# ─── Paperclip ────────────────────────────────────────────────────────────────────────────────────


def routine(handler) -> IncidentRoutine:
    return IncidentRoutine(HOOK, SecretStr(SECRET), user_agent="CyberPulse-AI/1.0",
                           transport=httpx.MockTransport(handler))


async def test_a_fire_posts_the_payload_with_the_bearer_secret():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(202, json={"status": "issue_created"})

    result = await routine(handler).fire({"incident": 7, "kind": "feed-failing"})
    assert result.sent and result.error is None
    [request] = seen
    assert request.method == "POST" and str(request.url) == HOOK
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert json.loads(request.content) == {"payload": {"incident": 7, "kind": "feed-failing"}}


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_a_refused_fire_is_described_by_its_status_alone(status):
    result = await routine(lambda r: httpx.Response(status, text=f"no {HOOK}")).fire({})
    assert (result.sent, result.error) == (False, f"HTTP {status}")


async def test_a_fire_that_cannot_connect_is_named_by_its_type():
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {HOOK}")

    result = await routine(handler).fire({})
    assert (result.sent, result.error) == (False, "ConnectError")


async def test_a_payload_carrying_a_secret_is_withheld_and_never_sent():
    seen = []
    credential = "gh" + "p_" + "a1B2" * 9
    result = await routine(lambda r: seen.append(r) or httpx.Response(200)).fire(
        {"title": credential}
    )
    assert result.withheld and not result.sent and seen == []


async def test_the_trigger_id_never_reaches_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    await routine(lambda r: httpx.Response(200)).fire({})
    assert "/routine-triggers/public/<redacted>/fire" in caplog.text
    assert "pub-TESTONLY-0123" not in caplog.text


def test_redact_and_the_filter_installed_once():
    assert redact(HOOK) == "http://server:3100/api/routine-triggers/public/<redacted>/fire"
    install_log_redaction()
    install_log_redaction()
    filters = logging.getLogger("httpx").filters
    assert sum(type(f).__name__ == "_RedactTriggerIds" for f in filters) == 1


@pytest.mark.parametrize(
    "url, secret, configured",
    [(None, None, False), (HOOK, None, False), (HOOK, "  ", False), ("", SECRET, False),
     (HOOK, SECRET, True)],
)
def test_the_routine_exists_only_with_both_url_and_secret(url, secret, configured):
    s = settings(paperclip_incident_webhook_url=url, paperclip_incident_webhook_secret=secret)
    assert (IncidentRoutine.from_settings(s) is not None) is configured


def test_neither_the_routine_nor_the_settings_show_the_url_or_secret():
    s = settings(paperclip_incident_webhook_url=HOOK, paperclip_incident_webhook_secret=SECRET)
    for text in (repr(IncidentRoutine.from_settings(s)), repr(s)):
        assert "pub-TESTONLY" not in text and SECRET not in text


@pytest.mark.parametrize(
    "answer, said", [(200, None), (403, None), (404, None), (502, "HTTP 502")]
)
async def test_any_answer_below_500_means_paperclip_is_up(answer, said):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(answer)

    url = "http://server:3100/api/health"
    assert await probe(url, user_agent="ua", transport=httpx.MockTransport(handler)) == said
    assert "Authorization" not in seen[0].headers


async def test_a_probe_that_cannot_connect_names_the_error():
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    transport = httpx.MockTransport(handler)
    assert await probe("http://server:3100/api/health", user_agent="ua",
                       transport=transport) == "ConnectTimeout"


# ─── Messages ─────────────────────────────────────────────────────────────────────────────────────


def incident(**overrides) -> Incident:
    values = {
        "id": 7,
        "kind": "feed-failing",
        "subject": "acsc-alerts",
        "severity": "high",
        "status": "open",
        "title": "Acsc Alerts has failed its last 5 checks",
        "evidence": {"failures_in_a_row": 5},
        "opened_at": NOW - 10 * MIN,
        "last_seen": NOW,
        "checks": 3,
        "clear_since": None,
        "resolved_at": None,
        "fix_failures": 0,
        "needs_human": False,
        "reopened": 0,
    }
    return Incident(**{**values, **overrides})


def test_an_opened_incident_names_itself_and_says_who_fixes_it():
    text = incident_opened(incident(), crew=True)
    assert text.startswith("CyberPulse-AI · incident · HIGH\n")
    assert "INC-7 · feed-failing · acsc-alerts\nAcsc Alerts has failed its last 5 checks" in text
    assert "Seen 18:00 Sydney time, Sat 3 Oct 2026" in text
    assert "WHEELJACK proposes the new URL" in text
    assert "TELETRAAN reads it from GET /ops/incidents" in text


def test_an_incident_the_crew_is_not_told_of_says_nothing_of_them():
    text = incident_opened(incident(kind="paperclip-down", subject=""), crew=False)
    assert "INC-7 · paperclip-down\n" in text and "TELETRAAN" not in text


def test_a_reopened_incident_says_so_and_counts_its_failed_fixes():
    text = incident_opened(incident(reopened=2, fix_failures=1), crew=True)
    assert "reopened (2 times)" in text and "failed TELETRAAN's tests so far: 1." in text


@pytest.mark.parametrize("n, said", [(1, "1 fix failed"), (3, "3 fixes failed")])
def test_the_breaker_notice_says_a_person_decides(n, said):
    text = incident_breaker(incident(fix_failures=n, needs_human=True))
    assert "needs a human" in text and said in text and "A person decides" in text


def test_a_resolved_incident_says_how_long_it_was_open():
    text = incident_resolved(incident(status="resolved", resolved_at=NOW + 50 * MIN))
    assert "incident resolved" in text
    assert "Open from 17:50 Sat 3 Oct 2026 to 18:50 Sat 3 Oct 2026 Sydney time (60 minutes)." in text


def test_the_database_notices():
    assert "since 17:00 Sydney time, Sat 3 Oct 2026" in database_unreachable(NOW - 60 * MIN)
    assert "after 60 minutes without it" in database_back(NOW - 60 * MIN, NOW)


# ─── One pass ─────────────────────────────────────────────────────────────────────────────────────


class FakeTelegram:
    def __init__(self, result=None):
        self.sent: list[str] = []
        self.result = result or SendResult(True)

    async def send(self, message: str) -> SendResult:
        self.sent.append(message)
        return self.result


class FakeRoutine:
    def __init__(self, result=None):
        self.fired: list[dict] = []
        self.result = result or SendResult(True)

    async def fire(self, payload) -> SendResult:
        self.fired.append(payload)
        return self.result


@pytest.fixture
def db(monkeypatch):
    """The database stood in for: what each pass found, and the notices claimed and settled."""
    state = {
        "up": True,
        "snapshot": None,
        "result": PassResult(),
        "passes": [],
        "claimed": {},
        "settled": [],
        "jobs": [],
    }

    def ping(engine):
        return state["up"]

    def gather(engine, now, *, budget):
        return state["snapshot"] or Snapshot(now)

    def record_pass(engine, findings, covered, now):
        state["passes"].append((findings, covered))
        return state["result"]

    def claim(engine, *, kind, key, now, channel):
        if key in state["claimed"]:
            return None
        state["claimed"][key] = channel
        return Claim(len(state["claimed"]), 1)

    def settle(engine, claimed, outcome, *, now, error=None):
        state["settled"].append((claimed.id, outcome, error))

    def record_job(engine, run):
        state["jobs"].append(run)

    for name, fake in [("ping", ping), ("gather", gather), ("record_pass", record_pass),
                       ("claim", claim), ("settle", settle), ("record_job", record_job)]:
        monkeypatch.setattr(watchdog_run, name, fake)
    return state


def watchdog(telegram=None, routine=None, *, token=False, site=None, paperclip=None, **kwargs):
    return Watchdog(
        settings(watchdog_paperclip_url=kwargs.pop("paperclip_url", "")),
        telegram=lambda s: telegram,
        routine=lambda s: routine,
        token_configured=lambda: token,
        site_transport=httpx.MockTransport(site) if site else None,
        paperclip_transport=httpx.MockTransport(paperclip) if paperclip else None,
    )


async def test_a_high_incident_is_told_to_telegram_and_the_crew_once(db):
    telegram, crew = FakeTelegram(), FakeRoutine()
    db["result"] = PassResult(opened=[incident()], unresolved=[incident()])
    dog = watchdog(telegram, crew)
    summary = await dog.run_pass(None, NOW)
    assert (summary.opened, summary.unresolved, summary.sent, summary.failed) == (1, 1, 2, 0)
    assert db["claimed"] == {"incident:7": "telegram", "paperclip:incident:7": "paperclip"}
    assert crew.fired == [routine_payload(incident())]
    assert crew.fired[0]["read"] == "GET /ops/incidents/7" and "evidence" not in crew.fired[0]
    assert "The crew has been told" in telegram.sent[0]

    db["result"] = PassResult(unresolved=[incident(checks=4)])
    again = await dog.run_pass(None, NOW + 5 * MIN)
    assert again.sent == 0 and len(telegram.sent) == 1 and len(crew.fired) == 1


async def test_low_and_medium_incidents_wait_in_the_api(db):
    telegram, crew = FakeTelegram(), FakeRoutine()
    quiet = [incident(id=1, severity="low"), incident(id=2, severity="medium")]
    db["result"] = PassResult(opened=quiet, unresolved=quiet)
    summary = await watchdog(telegram, crew).run_pass(None, NOW)
    assert summary.sent == 0 and telegram.sent == [] and crew.fired == []


async def test_paperclip_down_is_told_to_telegram_only(db):
    telegram, crew = FakeTelegram(), FakeRoutine()
    down = incident(kind="paperclip-down", subject="")
    db["result"] = PassResult(opened=[down], unresolved=[down])
    await watchdog(telegram, crew).run_pass(None, NOW)
    assert len(telegram.sent) == 1 and crew.fired == []
    assert "TELETRAAN" not in telegram.sent[0]


async def test_a_tripped_breaker_tells_a_person_and_not_the_crew(db):
    telegram, crew = FakeTelegram(), FakeRoutine()
    stuck = incident(severity="medium", fix_failures=3, needs_human=True)
    db["result"] = PassResult(unresolved=[stuck])
    await watchdog(telegram, crew).run_pass(None, NOW)
    assert list(db["claimed"]) == ["incident-breaker:7"]
    assert "needs a human" in telegram.sent[0] and crew.fired == []


async def test_a_reopened_incident_is_told_again_until_the_cap(db):
    telegram, crew = FakeTelegram(), FakeRoutine()
    dog = watchdog(telegram, crew)
    for n in range(1, MAX_REOPEN_NOTICES + 2):
        again = incident(reopened=n)
        db["result"] = PassResult(opened=[again], unresolved=[again])
        await dog.run_pass(None, NOW + n * MIN)
    assert sorted(db["claimed"]) == sorted(
        [f"incident:7:r{n}" for n in range(1, MAX_REOPEN_NOTICES + 1)]
        + [f"paperclip:incident:7:r{n}" for n in range(1, MAX_REOPEN_NOTICES + 1)]
    )


async def test_a_resolved_high_incident_is_told_to_telegram(db):
    telegram = FakeTelegram()
    done = incident(status="resolved", resolved_at=NOW)
    db["result"] = PassResult(resolved=[done])
    summary = await watchdog(telegram, FakeRoutine()).run_pass(None, NOW)
    assert summary.resolved == 1 and list(db["claimed"]) == ["incident-resolved:7"]
    assert "incident resolved" in telegram.sent[0]


async def test_with_no_channels_nothing_is_claimed(db):
    db["result"] = PassResult(opened=[incident()], unresolved=[incident()])
    summary = await watchdog().run_pass(None, NOW)
    assert summary.sent == 0 and db["claimed"] == {}


async def test_a_failed_fire_is_settled_and_counted(db):
    crew = FakeRoutine(SendResult(False, error="HTTP 401"))
    db["result"] = PassResult(opened=[incident()], unresolved=[incident()])
    summary = await watchdog(None, crew).run_pass(None, NOW)
    assert summary.failed == 1 and db["settled"] == [(1, "failed", "HTTP 401")]
    [run] = db["jobs"]
    # The stand-in snapshot has no parts: five uncovered, and the failed fire.
    assert (run.job, run.completed, run.errors, run.changed) == ("watchdog", True, 6, True)


async def test_a_pass_is_recorded_with_the_parts_it_could_not_read(db):
    db["snapshot"] = Snapshot(NOW, lanes={"fast": runs(40)}, sources=[], volume=None,
                              jobs=jobs(), cost=None)
    db["result"] = PassResult(opened=[incident()])
    summary = await watchdog().run_pass(None, NOW)
    assert summary.uncovered == ["volume", "cost"] and summary.findings == 1
    [(findings, covered)] = db["passes"]
    assert kinds(findings) == [("no-collection", "fast", "critical")]
    assert "volume-collapse" not in covered and "site-stale" not in covered
    [run] = db["jobs"]
    assert (run.errors, run.changed) == (2, True)


async def test_the_database_down_is_told_once_and_its_return_too(db):
    telegram = FakeTelegram()
    dog = watchdog(telegram)
    db["up"] = False
    for n in range(5):
        summary = await dog.run_pass(None, NOW + n * 5 * MIN)
        assert summary.database is False
    assert len(telegram.sent) == 1 and "has not reached its database" in telegram.sent[0]
    assert db["passes"] == [] and db["jobs"] == []

    db["up"] = True
    await dog.run_pass(None, NOW + 30 * MIN)
    assert len(telegram.sent) == 2 and "reaches its database again" in telegram.sent[1]
    assert list(db["claimed"]) == [f"system_failure:db:{NOW.isoformat()}"]


async def test_a_short_database_blip_tells_nobody(db):
    telegram = FakeTelegram()
    dog = watchdog(telegram)
    db["up"] = False
    await dog.run_pass(None, NOW)
    db["up"] = True
    await dog.run_pass(None, NOW + 5 * MIN)
    assert telegram.sent == [] and dog.db_failures == 0


def site_answer(generated_at: datetime | None = None, status=200):
    def handler(request):
        assert request.url.path == "/site/data/system-status.json"
        assert "watchdog" in request.url.params
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"generated_at": generated_at.isoformat()})

    return handler


async def test_old_site_data_is_found_when_this_host_pushes(db):
    dog = watchdog(token=True, site=site_answer(NOW - timedelta(hours=4)))
    await dog.run_pass(None, NOW)
    [(findings, covered)] = db["passes"]
    assert kinds(findings) == [("site-stale", "", "high")] and "site-stale" in covered


async def test_the_site_is_not_read_where_nothing_is_pushed(db):
    def never(request):
        raise AssertionError("the site was read")

    await watchdog(token=False, site=never).run_pass(None, NOW)
    [(_, covered)] = db["passes"]
    assert "site-stale" not in covered


async def test_site_reads_count_only_after_three_failures_in_a_row(db):
    dog = watchdog(token=True, site=site_answer(status=404))
    for n in range(3):
        await dog.run_pass(None, NOW + n * 5 * MIN)
    covered_each = ["site-stale" in covered for _, covered in db["passes"]]
    assert covered_each == [False, False, True]
    assert kinds(db["passes"][-1][0]) == [("site-stale", "", "high")]


async def test_a_paperclip_blip_neither_opens_nor_clears(db):
    answers = iter([503, 503, 503, 200])

    def handler(request):
        return httpx.Response(next(answers))

    dog = watchdog(paperclip=handler, paperclip_url="http://server:3100/api/health")
    for n in range(4):
        await dog.run_pass(None, NOW + n * 5 * MIN)
    covered_each = ["paperclip-down" in covered for _, covered in db["passes"]]
    assert covered_each == [False, False, True, True]
    assert kinds(db["passes"][2][0]) == [("paperclip-down", "", "high")]
    assert db["passes"][3][0] == [] and dog.paperclip_failures == 0


async def test_no_paperclip_probe_without_a_url(db):
    await watchdog(paperclip_url="").run_pass(None, NOW)
    [(_, covered)] = db["passes"]
    assert "paperclip-down" not in covered


def test_the_routine_payload_holds_no_evidence():
    payload = routine_payload(replace(incident(), evidence={"last_error": "a page's words"}))
    assert set(payload) == {"incident", "ref", "kind", "subject", "severity", "title", "read"}
    assert payload["ref"] == "INC-7"
