import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger

from worker import scheduler
from worker.ai.enrich import EnrichSummary
from worker.ai.mitre import MitreSummary
from worker.ai.scout import GauntletSummary, ScanSummary
from worker.cadence import FAST_INTERVAL_MINUTES, fast_minutes
from worker.models import Lane
from worker.publish.push import PushResult
from worker.schedule import public_schedule
from worker.ai.followup_writer import FollowupSummary
from worker.ai.report import ReportSummary
from worker.scheduler import (
    ALERT_JOB_ID,
    DAILY_REPORT_JOB_ID,
    DIGEST_JOB_ID,
    DISCOVERY_JOB_ID,
    ENRICH_JOB_ID,
    FOLLOWUP_WRITER_JOB_ID,
    GATE_JOB_ID,
    GAUNTLET_JOB_ID,
    GROUNDTRUTH_JOB_ID,
    MODEL_SCAN_JOB_ID,
    MONTHLY_COST_JOB_ID,
    ROUTINE_WAKE_JOB_ID,
    WATCHDOG_JOB_ID,
    alert_minutes,
    build_scheduler,
    job_id,
)


def fields(trigger: CronTrigger) -> dict[str, str]:
    return {f.name: str(f) for f in trigger.fields}


def fire_times(trigger: CronTrigger, start: datetime, end: datetime) -> list[datetime]:
    out = []
    at = trigger.get_next_fire_time(None, start)
    while at is not None and at < end:
        out.append(at.astimezone(UTC))
        at = trigger.get_next_fire_time(at, at + timedelta(seconds=1))
    return out


def minutes(spec: str) -> set[int]:
    """The minutes past the hour a cron line can start at."""
    trigger = CronTrigger.from_crontab(spec, timezone="UTC")
    start = datetime(2026, 10, 3, tzinfo=UTC)
    return {at.minute for at in fire_times(trigger, start, start + timedelta(days=7))}


def test_fast_and_normal_are_scheduled_deep_is_not():
    jobs = {j.id for j in build_scheduler().get_jobs()}
    assert jobs == {
        job_id(Lane.FAST), job_id(Lane.NORMAL), GROUNDTRUTH_JOB_ID, ENRICH_JOB_ID, DIGEST_JOB_ID,
        ALERT_JOB_ID, MONTHLY_COST_JOB_ID, DISCOVERY_JOB_ID, GATE_JOB_ID, MODEL_SCAN_JOB_ID,
        GAUNTLET_JOB_ID, WATCHDOG_JOB_ID, ROUTINE_WAKE_JOB_ID, DAILY_REPORT_JOB_ID,
        FOLLOWUP_WRITER_JOB_ID,
    }


def test_the_routine_wakes_run_hourly_on_a_minute_no_other_job_uses():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    assert fields(jobs[ROUTINE_WAKE_JOB_ID].trigger)["minute"] == "15"
    assert fields(jobs[ROUTINE_WAKE_JOB_ID].trigger)["hour"] == "*"
    start = datetime(2026, 10, 3, tzinfo=UTC)
    window = (start, start + timedelta(days=7))
    wakes = set(fire_times(jobs[ROUTINE_WAKE_JOB_ID].trigger, *window))
    others = {
        at for j in jobs.values() if j.id != ROUTINE_WAKE_JOB_ID
        for at in fire_times(j.trigger, *window)
    }
    assert wakes and not wakes & others


def test_asking_for_one_lane_does_not_bring_the_routine_wakes_along():
    assert ROUTINE_WAKE_JOB_ID not in {j.id for j in build_scheduler((Lane.FAST,)).get_jobs()}


def test_the_ground_truth_sync_runs_four_times_a_day_off_the_lane_hours():
    job = {j.id: j for j in build_scheduler().get_jobs()}[GROUNDTRUTH_JOB_ID]
    assert (fields(job.trigger)["minute"], fields(job.trigger)["hour"]) == ("25", "*/6")
    # Off the hour on purpose: both lanes fire at :00, and the sync competing with a collection for
    # the connection pool makes both slower for no gain.
    fast = {j.id: j for j in build_scheduler().get_jobs()}[job_id(Lane.FAST)]
    assert fields(job.trigger)["minute"] != fields(fast.trigger)["minute"]
    assert str(job.trigger.timezone) == "UTC"


def test_asking_for_one_lane_does_not_bring_the_register_sync_along():
    # `--lane fast` means "schedule this one thing". A sync arriving uninvited would make the narrow
    # form impossible to ask for, and on a machine running one lane deliberately that is a surprise.
    assert GROUNDTRUTH_JOB_ID not in {j.id for j in build_scheduler((Lane.FAST,)).get_jobs()}


def test_enrichment_runs_twice_an_hour_after_the_fast_lane():
    job = {j.id: j for j in build_scheduler().get_jobs()}[ENRICH_JOB_ID]
    assert (fields(job.trigger)["minute"], fields(job.trigger)["hour"]) == ("5,35", "*")
    assert str(job.trigger.timezone) == "UTC"


def test_asking_for_one_lane_does_not_bring_model_calls_along():
    assert ENRICH_JOB_ID not in {j.id for j in build_scheduler((Lane.FAST,)).get_jobs()}


def test_the_digest_goes_at_ten_past_seven_sydney_time_with_two_retries():
    # Ten past, so it reports the 07:00 run and the enrichment pass after it.
    job = {j.id: j for j in build_scheduler().get_jobs()}[DIGEST_JOB_ID]
    assert (fields(job.trigger)["minute"], fields(job.trigger)["hour"]) == ("10", "7,8,9")
    assert str(job.trigger.timezone) == "Australia/Sydney"


def test_alerts_follow_each_fast_run_and_each_enrichment_pass():
    job = {j.id: j for j in build_scheduler().get_jobs()}[ALERT_JOB_ID]
    expected = {m + 3 for m in scheduler.FAST_MINUTES} | {m + 13 for m in scheduler.ENRICH_MINUTES}
    assert minutes(scheduler.ALERT_SCHEDULE) == expected
    assert fields(job.trigger)["hour"] == "*" and str(job.trigger.timezone) == "UTC"


@pytest.mark.parametrize(
    "every, expected",
    [(60, (3, 18, 48)), (30, (3, 18, 33, 48)), (15, (3, 18, 33, 48))],
)
def test_the_alert_minutes_follow_the_fast_cadence(every, expected):
    # Hourly: :03 after the run, :18 after the :05 pass, :48 after the :35 backlog pass. At 15
    # minutes it is the schedule the alerts started with.
    assert alert_minutes(fast_minutes(every), scheduler.ENRICH_MINUTES) == expected


def test_asking_for_one_lane_does_not_bring_notifications_along():
    jobs = {j.id for j in build_scheduler((Lane.FAST,)).get_jobs()}
    assert DIGEST_JOB_ID not in jobs and ALERT_JOB_ID not in jobs
    assert MONTHLY_COST_JOB_ID not in jobs


@pytest.mark.parametrize(
    "month, utc_hour",
    [(datetime(2026, 11, 1, tzinfo=UTC), 0), (datetime(2027, 7, 1, tzinfo=UTC), 1)],
)
def test_the_monthly_cost_summary_goes_on_the_first_once_the_ledgers_month_has_closed(
    month, utc_hour
):
    # RIPPERDOC's review ran at 09:00 Sydney on the 1st, which is still the last day of the month
    # in UTC, the ledger's months. 11:10 is past midnight UTC in daylight time (November) and
    # out of it (July); 12:10 and 13:10 send it only if 11:10 did not.
    job = {j.id: j for j in build_scheduler().get_jobs()}[MONTHLY_COST_JOB_ID]
    assert str(job.trigger.timezone) == "Australia/Sydney"
    runs = fire_times(job.trigger, month - timedelta(days=1), month + timedelta(days=1))
    assert [(at.day, at.hour, at.minute) for at in runs] == [
        (1, utc_hour, 10), (1, utc_hour + 1, 10), (1, utc_hour + 2, 10)
    ]
    assert all(at >= month for at in runs)
    later = fire_times(job.trigger, month + timedelta(days=1), month + timedelta(days=40))
    assert {at.astimezone(job.trigger.timezone).day for at in later} == {1}


def test_the_monthly_cost_summary_shares_its_minute_with_no_other_job():
    # The day it runs, across both offsets Sydney keeps.
    jobs = build_scheduler().get_jobs()
    for day in (datetime(2026, 11, 1, tzinfo=UTC), datetime(2027, 7, 1, tzinfo=UTC)):
        window = (day - timedelta(hours=12), day + timedelta(hours=36))
        cost = set(fire_times(
            next(j for j in jobs if j.id == MONTHLY_COST_JOB_ID).trigger, *window
        ))
        others = {
            at for j in jobs if j.id != MONTHLY_COST_JOB_ID for at in fire_times(j.trigger, *window)
        }
        assert len(cost) == 3 and not cost & others


def test_the_discovery_search_runs_nightly_sydney_time():
    job = {j.id: j for j in build_scheduler().get_jobs()}[DISCOVERY_JOB_ID]
    assert (fields(job.trigger)["minute"], fields(job.trigger)["hour"]) == ("10", "3")
    assert str(job.trigger.timezone) == "Australia/Sydney"


def test_the_gate_runs_ten_minutes_before_each_normal_run():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    gate, normal = jobs[GATE_JOB_ID].trigger, jobs[job_id(Lane.NORMAL)].trigger
    after = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    gate_at = gate.get_next_fire_time(None, after)
    assert normal.get_next_fire_time(None, gate_at) - gate_at == timedelta(minutes=10)
    assert str(gate.timezone) == "UTC"


def test_asking_for_one_lane_does_not_bring_discovery_along():
    jobs = {j.id for j in build_scheduler((Lane.FAST,)).get_jobs()}
    assert DISCOVERY_JOB_ID not in jobs and GATE_JOB_ID not in jobs


@pytest.mark.parametrize("name, runner, job", [("_discovery_job", "run_search", "discovery"),
                                               ("_gate_job", "run_gate", "source-gate")])
async def test_a_discovery_pass_that_raises_is_recorded_and_not_fatal(
    monkeypatch, caplog, name, runner, job
):
    async def boom():
        raise RuntimeError("database unreachable")

    recorded = []

    async def record(run):
        recorded.append(run)

    async def no_notices(engine, settings, *, now):
        return None

    monkeypatch.setattr(scheduler, runner, boom)
    monkeypatch.setattr(scheduler, "_record", record)
    monkeypatch.setattr(scheduler, "send_source_activations", no_notices)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    monkeypatch.setattr(scheduler, "get_settings", lambda: None)
    await getattr(scheduler, name)()  # must not raise
    assert "failed" in caplog.text
    assert [(r.job, r.completed) for r in recorded] == [(job, False)]


async def test_a_discovery_pass_records_what_it_found_for_the_site(monkeypatch):
    from worker.discovery.run import GateSummary, SearchSummary

    gate = GateSummary(homes=5, probes=10, healthy=7, rejected=3, activated=["found_dev_to"],
                       pruned=["found_spiceworks_com"], waiting=39, promoted=["a.example"])
    search = SearchSummary(searches=16, hits=140, new_hosts=4, credits=16)
    recorded = []

    async def record(run):
        recorded.append(run)

    async def no_notices(engine, settings, *, now):
        return None

    async def gate_pass():
        return gate

    async def search_pass():
        return search

    monkeypatch.setattr(scheduler, "run_gate", gate_pass)
    monkeypatch.setattr(scheduler, "run_search", search_pass)
    monkeypatch.setattr(scheduler, "_record", record)
    monkeypatch.setattr(scheduler, "send_source_activations", no_notices)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    monkeypatch.setattr(scheduler, "get_settings", lambda: None)
    await scheduler._gate_job()
    await scheduler._discovery_job()
    gate_run, search_run = recorded
    assert gate_run.detail == {
        "homes": 5, "feeds_found": 0, "probes": 10, "healthy": 7, "rejected": 3,
        "activated": ["found_dev_to"], "retired": [], "pruned": ["found_spiceworks_com"],
        "waiting": 39, "promoted": 1,
    }
    assert search_run.detail == {"searches": 16, "hits": 140, "new_hosts": 4, "credits": 16,
                                 "skipped": False}


async def test_the_gate_sends_new_source_notices_even_after_a_failed_pass(monkeypatch):
    async def boom():
        raise RuntimeError("database unreachable")

    sent = []

    async def notices(engine, settings, *, now):
        sent.append(now)

    async def record(run):
        return None

    monkeypatch.setattr(scheduler, "run_gate", boom)
    monkeypatch.setattr(scheduler, "_record", record)
    monkeypatch.setattr(scheduler, "send_source_activations", notices)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    monkeypatch.setattr(scheduler, "get_settings", lambda: None)
    await scheduler._gate_job()
    # An earlier pass's activation whose notice failed is retried here.
    assert len(sent) == 1


@pytest.mark.parametrize("name, sender", [("_digest_job", "send_daily_digest"),
                                          ("_alert_job", "send_critical_alerts"),
                                          ("_monthly_cost_job", "send_monthly_cost"),
                                          ("_routine_wake_job", "wake_routines")])
async def test_a_notification_pass_that_raises_does_not_stop_the_schedule(
    monkeypatch, caplog, name, sender
):
    async def boom(engine, settings, *, now):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(scheduler, sender, boom)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    monkeypatch.setattr(scheduler, "get_settings", lambda: None)
    await getattr(scheduler, name)()  # must not raise
    assert "failed" in caplog.text


def test_the_model_scan_runs_daily_after_the_discovery_search():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    scan = jobs[MODEL_SCAN_JOB_ID].trigger
    assert (fields(scan)["minute"], fields(scan)["hour"]) == ("20", "3")
    assert str(scan.timezone) == "Australia/Sydney"
    after = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    search = jobs[DISCOVERY_JOB_ID].trigger.get_next_fire_time(None, after)
    assert scan.get_next_fire_time(None, after) - search == timedelta(minutes=10)


def test_the_gauntlet_runs_on_sunday_after_the_scan():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    gauntlet = jobs[GAUNTLET_JOB_ID].trigger
    after = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)  # a Saturday
    at = gauntlet.get_next_fire_time(None, after)
    # Sunday in Sydney, not Monday: APScheduler counts the days of the week from Monday.
    assert at.strftime("%A %H:%M") == "Sunday 03:40"
    assert at - jobs[MODEL_SCAN_JOB_ID].trigger.get_next_fire_time(None, after) == timedelta(
        minutes=20
    )
    assert gauntlet.get_next_fire_time(None, at + timedelta(minutes=1)) - at == timedelta(days=7)


def test_asking_for_one_lane_does_not_bring_the_model_scout_along():
    jobs = {j.id for j in build_scheduler((Lane.FAST,)).get_jobs()}
    assert MODEL_SCAN_JOB_ID not in jobs and GAUNTLET_JOB_ID not in jobs


@pytest.fixture
def scout_jobs(monkeypatch):
    """The scan and gauntlet jobs with the passes and their notices stubbed, and a fake AI
    layer. `state["told"]` lists the notice passes that ran, in order."""
    state = {"scan": ScanSummary(), "gauntlet": GauntletSummary(), "raises": False, "told": []}
    recorded, adopted = [], []

    class Layer:
        def adopt(self, verified):
            adopted.append(verified)

    async def fake_scan():
        if state["raises"]:
            raise RuntimeError("database unreachable")
        return state["scan"]

    async def fake_gauntlet():
        if state["raises"]:
            raise RuntimeError("database unreachable")
        return state["gauntlet"]

    async def record(run):
        recorded.append(run)

    def notices(name):
        async def send(engine, settings, *, now):
            state["told"].append((name, len(recorded)))

        return send

    monkeypatch.setattr(scheduler, "send_ladder_changes", notices("ladder"))
    monkeypatch.setattr(scheduler, "send_model_proposals", notices("proposals"))
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    monkeypatch.setattr(scheduler, "get_settings", lambda: None)
    monkeypatch.setattr(scheduler, "scan_models", fake_scan)
    monkeypatch.setattr(scheduler, "run_gauntlet", fake_gauntlet)
    monkeypatch.setattr(scheduler, "_record", record)
    monkeypatch.setattr(scheduler, "AiLayer", Layer)
    monkeypatch.setattr(scheduler, "_ai_layer", None)
    return state, recorded, adopted


async def test_a_ladder_the_scan_checked_goes_to_the_ai_layer(scout_jobs):
    state, recorded, adopted = scout_jobs
    state["scan"].verified = "the pruned ladder"
    state["scan"].changes["price"] = 1
    await scheduler._model_scan_job()
    assert adopted == ["the pruned ladder"]
    [run] = recorded
    assert (run.job, run.completed, run.changed) == ("model-scan", True, True)


async def test_a_ladder_that_failed_the_scan_makes_enrichment_check_again(scout_jobs):
    state, _, adopted = scout_jobs
    state["scan"].ladder_failed = True
    state["scan"].errors.append("the ladder did not pass")
    await scheduler._model_scan_job()
    assert adopted == [None]


async def test_a_ladder_the_scan_could_not_check_changes_nothing(scout_jobs):
    # OpenRouter's list was down: the ladder enrichment has stands.
    state, recorded, adopted = scout_jobs
    state["scan"].errors.append("model list: HTTP 503")
    await scheduler._model_scan_job()
    assert adopted == [] and recorded[0].errors == 1


@pytest.mark.parametrize("name, job", [("_model_scan_job", "model-scan"),
                                       ("_gauntlet_job", "model-gauntlet")])
async def test_a_model_scout_pass_that_raises_is_recorded_and_not_fatal(
    scout_jobs, caplog, name, job
):
    state, recorded, adopted = scout_jobs
    state["raises"] = True
    await getattr(scheduler, name)()  # must not raise
    assert "failed" in caplog.text
    assert [(r.job, r.completed) for r in recorded] == [(job, False)] and adopted == []


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("name, notice", [("_model_scan_job", "ladder"),
                                         ("_gauntlet_job", "proposals")])
async def test_the_owner_is_told_after_each_scout_pass_even_one_that_failed(
    scout_jobs, name, notice, raises
):
    # After the pass is recorded, so a slow Telegram never delays the watchdog's row. A failed
    # pass still sends: an earlier notice whose message failed is tried again.
    state, recorded, _ = scout_jobs
    state["raises"] = raises
    await getattr(scheduler, name)()
    assert state["told"] == [(notice, 1)] and len(recorded) == 1


async def test_a_scout_notice_that_raises_does_not_stop_the_schedule(scout_jobs, monkeypatch,
                                                                    caplog):
    state, recorded, adopted = scout_jobs

    async def boom(engine, settings, *, now):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(scheduler, "send_ladder_changes", boom)
    state["scan"].verified = "the pruned ladder"
    await scheduler._model_scan_job()  # must not raise
    assert "model ladder notification pass failed" in caplog.text
    assert adopted == ["the pruned ladder"] and recorded[0].completed is True


async def test_a_gauntlet_is_recorded_with_the_proposals_it_made(scout_jobs):
    state, recorded, _ = scout_jobs
    state["gauntlet"].proposals.append("[MODEL] Proposal: tier0_free -> a/b:free")
    await scheduler._gauntlet_job()
    [run] = recorded
    assert (run.job, run.completed, run.changed) == ("model-gauntlet", True, True)


def test_cadences():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    fast, normal = jobs[job_id(Lane.FAST)].trigger, jobs[job_id(Lane.NORMAL)].trigger
    after = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    first = fast.get_next_fire_time(None, after)
    assert first == after  # from the top of the hour
    gap = fast.get_next_fire_time(first, first + timedelta(seconds=1)) - first
    assert gap == timedelta(minutes=FAST_INTERVAL_MINUTES)
    assert fields(fast)["hour"] == "*"
    assert (fields(normal)["minute"], fields(normal)["hour"]) == ("0", "*/4")
    assert str(fast.timezone) == str(normal.timezone) == "UTC"


@pytest.mark.parametrize(
    "every, expected", [(60, (0,)), (30, (0, 30)), (15, (0, 15, 30, 45))]
)
def test_the_fast_minutes_come_from_one_interval(every, expected):
    assert fast_minutes(every) == expected


def test_a_late_fast_run_still_ends_before_the_next_is_due():
    grace = scheduler.MISFIRE_GRACE_SECONDS[Lane.FAST]
    assert FAST_INTERVAL_MINUTES * 60 / 2 < grace < FAST_INTERVAL_MINUTES * 60


def test_no_two_jobs_start_in_the_same_minute_but_the_two_lanes():
    # Eight days from Saturday 3 October 2026, across Sydney's change to daylight time: the
    # digest, discovery, the model scan and the gauntlet run on Sydney time.
    start = datetime(2026, 10, 3, tzinfo=UTC)
    starts: dict[datetime, list[str]] = defaultdict(list)
    for job in build_scheduler().get_jobs():
        for at in fire_times(job.trigger, start, start + timedelta(days=8)):
            starts[at].append(job.id)
    shared = [sorted(ids) for ids in starts.values() if len(ids) > 1]
    # Both lanes at :00 every four hours is by design: `publish_now` holds a lock.
    lanes = sorted([job_id(Lane.FAST), job_id(Lane.NORMAL)])
    assert shared and all(ids == lanes for ids in shared)


@pytest.mark.parametrize("every", [15, 30, 60])
def test_the_hourly_jobs_keep_to_their_own_minutes_at_any_fast_cadence(every):
    fast = set(fast_minutes(every))
    hourly = [
        fast,
        set(alert_minutes(fast, scheduler.ENRICH_MINUTES)),
        set(scheduler.ENRICH_MINUTES),
        minutes(scheduler.WATCHDOG_SCHEDULE),
        minutes(scheduler.FOLLOWUP_WRITER_SCHEDULE),
    ]
    daily = {
        int(spec.split()[0])
        for spec in (
            scheduler.GROUNDTRUTH_SCHEDULE,
            scheduler.GATE_SCHEDULE,
            scheduler.DIGEST_SCHEDULE,
            scheduler.DISCOVERY_SCHEDULE,
            scheduler.MODEL_SCAN_SCHEDULE,
            scheduler.GAUNTLET_SCHEDULE,
            scheduler.MONTHLY_COST_SCHEDULE,
            scheduler.DAILY_REPORT_SCHEDULE,
        )
    }
    groups = [*hourly, daily]
    assert sum(len(g) for g in groups) == len(set().union(*groups))


def test_runs_never_overlap_and_missed_ticks_collapse():
    for job in build_scheduler().get_jobs():
        assert job.max_instances == 1 and job.coalesce is True


def test_a_single_lane_can_be_scheduled():
    assert [j.id for j in build_scheduler((Lane.NORMAL,)).get_jobs()] == [job_id(Lane.NORMAL)]


def test_building_a_scheduler_does_not_start_it():
    assert build_scheduler().running is False


@pytest.fixture
def lane_job(monkeypatch):
    """Record what a scheduled lane run does, with both halves stubbed.

    Returns the call log plus setters for making either half fail, so each test states only the
    failure it is about.
    """
    log = []

    async def fake_run_lane(lane, *, once=False):
        log.append(("run", lane))
        if state["run_raises"]:
            raise RuntimeError("database unreachable")

    async def fake_publish_now():
        log.append(("publish",))
        if state["publish_raises"]:
            raise RuntimeError("schema validation failed")
        return [Path("data/live.json")]

    async def fake_push_now():
        log.append(("push",))
        if state["push_raises"]:
            raise RuntimeError("git push failed: could not resolve host")
        return state["pushed"]

    def fake_record_job(engine, run):
        log.append(("record", run))
        if state["record_raises"]:
            raise RuntimeError("database unreachable")

    state = {
        "run_raises": False,
        "publish_raises": False,
        "push_raises": False,
        "record_raises": False,
        "pushed": PushResult(pushed=True, commit_sha="a" * 40, reason=None),
    }
    monkeypatch.setattr(scheduler.pipeline_run, "run_lane", fake_run_lane)
    monkeypatch.setattr(scheduler, "publish_now", fake_publish_now)
    monkeypatch.setattr(scheduler, "push_now", fake_push_now)
    monkeypatch.setattr(scheduler, "record_job", fake_record_job)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    return log, state


async def test_a_scheduled_lane_run_publishes_what_it_collected_and_pushes_it(lane_job, caplog):
    log, _ = lane_job
    caplog.set_level(logging.INFO, logger="worker.scheduler")
    await scheduler._run_lane_job(Lane.FAST)
    assert [entry[0] for entry in log] == ["run", "publish", "record", "push", "record"]
    assert "published 1 files after the fast lane" in caplog.text
    assert f"pushed {'a' * 40} to the data branch" in caplog.text


async def test_a_failed_publish_is_not_pushed(lane_job):
    """The push would send the previous files again, as if they were this run's."""
    log, state = lane_job
    state["publish_raises"] = True
    await scheduler._run_lane_job(Lane.FAST)
    assert ("push",) not in log


async def test_a_failed_push_does_not_stop_the_schedule(lane_job, caplog):
    log, state = lane_job
    state["push_raises"] = True
    await scheduler._run_lane_job(Lane.FAST)  # must not raise
    assert log[-2] == ("push",) and "push after the fast lane failed" in caplog.text


async def test_each_publish_and_push_leaves_a_job_row_for_the_watchdog(lane_job):
    log, _ = lane_job
    await scheduler._run_lane_job(Lane.FAST)
    runs = [entry[1] for entry in log if entry[0] == "record"]
    assert [(r.job, r.completed, r.changed, r.note) for r in runs] == [
        ("publish", True, True, None),
        ("push", True, True, None),
    ]
    assert all(r.started_at <= r.finished_at for r in runs)


@pytest.mark.parametrize("failing, job", [("publish_raises", "publish"), ("push_raises", "push")])
async def test_a_failed_publish_or_push_is_recorded_with_its_type_only(lane_job, failing, job):
    """The note is the exception's type name: its message may carry a URL or a path."""
    log, state = lane_job
    state[failing] = True
    await scheduler._run_lane_job(Lane.FAST)
    run = log[-1][1]
    assert (run.job, run.completed, run.note) == (job, False, "RuntimeError")


async def test_a_host_with_no_publish_token_records_no_push(lane_job):
    log, state = lane_job
    state["pushed"] = None
    await scheduler._run_lane_job(Lane.FAST)
    assert [entry[1].job for entry in log if entry[0] == "record"] == ["publish"]


@pytest.mark.parametrize(
    "pushed, says",
    [
        (None, None),  # no token on this host
        (PushResult(pushed=False, commit_sha=None, reason="no changes"), "already current"),
    ],
)
async def test_a_push_with_nothing_to_send_is_quiet(lane_job, caplog, pushed, says):
    _, state = lane_job
    state["pushed"] = pushed
    caplog.set_level(logging.INFO, logger="worker.scheduler")
    await scheduler._run_lane_job(Lane.FAST)
    assert "pushed " not in caplog.text
    assert says is None or says in caplog.text


async def test_a_lane_that_could_not_run_does_not_publish(lane_job):
    """Nothing was collected, so there is nothing new to publish and the old files stand."""
    log, state = lane_job
    state["run_raises"] = True
    await scheduler._run_lane_job(Lane.NORMAL)
    assert log == [("run", Lane.NORMAL)]


async def test_a_broken_publisher_does_not_stop_the_schedule(lane_job):
    """Collection is the half that cannot be redone: an item gone from a feed is gone."""
    log, state = lane_job
    state["publish_raises"] = True
    await scheduler._run_lane_job(Lane.FAST)  # must not raise
    assert [entry[0] for entry in log] == ["run", "publish", "record"]


async def test_the_ground_truth_sync_publishes_and_pushes_what_it_changed(lane_job, monkeypatch):
    log, _ = lane_job

    class Changed:
        changed_anything = True
        errors = ("kev: HTTP 503",)

    async def fake_sync():
        log.append(("sync",))
        return Changed()

    monkeypatch.setattr(scheduler, "sync_groundtruth", fake_sync)
    await scheduler._groundtruth_job()
    assert [entry[0] for entry in log] == ["sync", "record", "publish", "record", "push", "record"]
    run = log[1][1]
    assert (run.job, run.completed, run.errors, run.changed) == ("groundtruth", True, 1, True)
    assert run.started_at <= run.finished_at


async def test_a_ground_truth_sync_that_raised_is_recorded_as_not_completed(lane_job, monkeypatch):
    """SERAPH's wake reads the newest completed pass, so a raising one must not count."""
    log, _ = lane_job

    async def fake_sync():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(scheduler, "sync_groundtruth", fake_sync)
    await scheduler._groundtruth_job()  # must not raise
    [(_, run)] = log
    assert (run.job, run.completed, run.changed) == ("groundtruth", False, False)


async def test_a_pass_whose_record_fails_still_publishes(lane_job, monkeypatch, caplog):
    """The row is for the wakes; the pass's own work matters more and stands."""
    log, state = lane_job
    state["record_raises"] = True

    class Changed:
        changed_anything = True
        errors = ()

    async def fake_sync():
        return Changed()

    monkeypatch.setattr(scheduler, "sync_groundtruth", fake_sync)
    await scheduler._groundtruth_job()
    assert [entry[0] for entry in log] == ["record", "publish", "record", "push", "record"]
    assert "could not record the groundtruth pass" in caplog.text


async def test_a_failed_run_is_swallowed_so_the_next_tick_still_fires(lane_job):
    _, state = lane_job
    state["run_raises"] = True
    await scheduler._run_lane_job(Lane.FAST)  # must not raise


@pytest.fixture
def enrich_job(monkeypatch):
    """The scheduled enrichment pass with the pass and the publisher stubbed."""
    log = []
    state = {
        "summary": EnrichSummary(),
        "raises": False,
        "mitre": MitreSummary(),
        "mitre_raises": False,
    }

    async def fake_enrich_pending(*, layer):
        log.append(("enrich", layer))
        if state["raises"]:
            raise RuntimeError("ledger write failed")
        return state["summary"]

    async def fake_suggest_techniques(*, layer):
        log.append(("mitre", layer))
        if state["mitre_raises"]:
            raise RuntimeError("ledger write failed")
        return state["mitre"]

    async def fake_publish_now():
        log.append(("publish",))
        return [Path("data/live.json")]

    async def fake_push_now():  # as on a host with no publish token
        log.append(("push",))

    def fake_record_job(engine, run):
        state["recorded"].append(run)

    state["recorded"] = []
    monkeypatch.setattr(scheduler, "record_job", fake_record_job)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    monkeypatch.setattr(scheduler, "enrich_pending", fake_enrich_pending)
    monkeypatch.setattr(scheduler, "suggest_techniques", fake_suggest_techniques)
    monkeypatch.setattr(scheduler, "publish_now", fake_publish_now)
    monkeypatch.setattr(scheduler, "push_now", fake_push_now)
    monkeypatch.setattr(scheduler, "AiLayer", lambda: "the layer")
    monkeypatch.setattr(scheduler, "_ai_layer", None)
    return log, state


async def test_an_enrichment_that_changed_something_is_published_and_pushed(enrich_job):
    log, state = enrich_job
    state["summary"].done["brief"] = 1
    await scheduler._enrich_job()
    assert log == [("enrich", "the layer"), ("mitre", "the layer"), ("publish",), ("push",)]


async def test_an_enrichment_that_changed_nothing_is_not_published(enrich_job):
    log, _ = enrich_job
    await scheduler._enrich_job()
    assert log == [("enrich", "the layer"), ("mitre", "the layer")]


async def test_the_pass_records_the_budget_mode_it_ran_in(enrich_job):
    """The SYSTEM page says what mode the AI layer is in from this row (build.py's `ai`)."""
    _, state = enrich_job
    state["summary"].mode = "free_only"
    await scheduler._enrich_job()
    [run] = state["recorded"]
    assert run.job == "enrichment" and run.detail == {"mode": "free_only"}


async def test_a_pass_with_the_ai_layer_off_records_no_mode(enrich_job):
    _, state = enrich_job
    await scheduler._enrich_job()
    [run] = state["recorded"]
    assert run.detail is None


async def test_new_suggestions_alone_are_published(enrich_job):
    log, state = enrich_job
    state["mitre"].done = 1
    await scheduler._enrich_job()
    assert log[-2:] == [("publish",), ("push",)]


async def test_suggestions_still_run_after_an_enrichment_that_raised(enrich_job):
    log, state = enrich_job
    state["raises"] = True
    state["mitre"].done = 1
    await scheduler._enrich_job()  # must not raise
    assert log == [("enrich", "the layer"), ("mitre", "the layer"), ("publish",), ("push",)]


async def test_suggestions_that_raise_do_not_stop_the_enrichment_publishing(enrich_job):
    log, state = enrich_job
    state["summary"].done["brief"] = 1
    state["mitre_raises"] = True
    await scheduler._enrich_job()  # must not raise
    assert log[-2:] == [("publish",), ("push",)]


async def test_the_ai_layer_is_kept_between_passes(enrich_job, monkeypatch):
    # The governor in it must remember a 402 from one pass to the next.
    log, _ = enrich_job
    made = []
    monkeypatch.setattr(scheduler, "AiLayer", lambda: made.append(1) or "the layer")
    await scheduler._enrich_job()
    await scheduler._enrich_job()
    assert made == [1] and {entry[1] for entry in log} == {"the layer"} and len(log) == 4


async def test_a_failed_enrichment_is_swallowed_and_not_published(enrich_job):
    log, state = enrich_job
    state["raises"] = True
    await scheduler._enrich_job()  # must not raise
    assert log == [("enrich", "the layer"), ("mitre", "the layer")]


async def test_an_enrichment_pass_is_recorded_with_what_it_absorbed(enrich_job):
    _, state = enrich_job
    state["summary"].done["brief"] = 2
    state["summary"].failed = 1
    state["summary"].errors.append("rescore evt-2026-000001: deadlock")
    state["mitre"].failed = 2
    await scheduler._enrich_job()
    [run] = [r for r in state["recorded"] if r.job == "enrichment"]
    assert (run.job, run.completed, run.errors, run.changed) == ("enrichment", True, 4, True)


async def test_an_enrichment_half_that_raised_is_recorded_as_not_completed(enrich_job):
    _, state = enrich_job
    state["mitre_raises"] = True
    await scheduler._enrich_job()
    [run] = state["recorded"]
    assert (run.completed, run.changed) == (False, False)


def test_the_watchdog_runs_every_five_minutes_off_the_lane_minutes():
    job = {j.id: j for j in build_scheduler().get_jobs()}[WATCHDOG_JOB_ID]
    assert fields(job.trigger)["minute"] == "2-57/5"
    assert str(job.trigger.timezone) == "UTC"


def test_asking_for_one_lane_does_not_bring_the_watchdog_along():
    assert WATCHDOG_JOB_ID not in {j.id for j in build_scheduler((Lane.FAST,)).get_jobs()}


async def test_a_watchdog_pass_that_raises_is_recorded_and_not_fatal(monkeypatch, caplog):
    recorded = []

    class Broken:
        async def run_pass(self, engine, now):
            raise RuntimeError("snapshot unreadable")

    async def record(run):
        recorded.append(run)

    monkeypatch.setattr(scheduler, "_the_watchdog", lambda: Broken())
    monkeypatch.setattr(scheduler, "_record", record)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    await scheduler._watchdog_job()  # must not raise
    assert "watchdog pass failed" in caplog.text
    assert [(r.job, r.completed, r.note) for r in recorded] == [
        ("watchdog", False, "RuntimeError")
    ]


async def test_the_watchdog_is_kept_between_passes(monkeypatch):
    """Its probes count failures in a row, so a new one each pass would never alarm."""
    made, passes = [], []

    class Fake:
        def __init__(self, settings, **kwargs):
            made.append(settings)

        async def run_pass(self, engine, now):
            passes.append(now)

    monkeypatch.setattr(scheduler, "Watchdog", Fake)
    monkeypatch.setattr(scheduler, "_watchdog", None)
    monkeypatch.setattr(scheduler, "_layer", lambda: "the layer")
    monkeypatch.setattr(scheduler, "get_settings", lambda: "settings")
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    await scheduler._watchdog_job()
    await scheduler._watchdog_job()
    assert made == ["settings"] and len(passes) == 2


def test_the_published_schedule_is_the_one_that_runs():
    # system-status.json's `schedule` (worker/schedule.py): every job the default scheduler adds,
    # with its own cron line and timezone, and nothing it does not add.
    scheduled = {j.id: j for j in build_scheduler().get_jobs()}
    published = {entry["job"]: entry for entry in public_schedule()}
    assert published.keys() == scheduled.keys()
    for job, entry in published.items():
        trigger = scheduled[job].trigger
        expected = CronTrigger.from_crontab(entry["cron"], timezone=entry["timezone"])
        assert fields(trigger) == fields(expected), job
        assert str(trigger.timezone) == entry["timezone"] in ("UTC", "Australia/Sydney")
        assert entry["kind"] in ("code", "ai", "mixed") and entry["label"] and entry["summary"]


def test_only_the_jobs_that_call_a_model_are_ai():
    kinds = {entry["job"]: entry["kind"] for entry in public_schedule()}
    assert {job for job, kind in kinds.items() if kind != "code"} == {
        ENRICH_JOB_ID, GAUNTLET_JOB_ID, DAILY_REPORT_JOB_ID, FOLLOWUP_WRITER_JOB_ID,
    }
    assert kinds[ENRICH_JOB_ID] == kinds[GAUNTLET_JOB_ID] == kinds[DAILY_REPORT_JOB_ID] == "ai"
    # It closes quiet checks by rule and asks a model only about the rest.
    assert kinds[FOLLOWUP_WRITER_JOB_ID] == "mixed"


# ─── The worker's own AI jobs ─────────────────────────────────────────────────────────────────────


def test_the_daily_report_runs_at_07_40_sydney_with_two_retries():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    trigger = fields(jobs[DAILY_REPORT_JOB_ID].trigger)
    assert (trigger["minute"], trigger["hour"]) == ("40", "7,8,9")
    assert str(jobs[DAILY_REPORT_JOB_ID].trigger.timezone) == "Australia/Sydney"


def test_the_follow_up_writer_runs_hourly_on_its_own_minute():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    assert fields(jobs[FOLLOWUP_WRITER_JOB_ID].trigger)["minute"] == "55"


def test_asking_for_one_lane_does_not_bring_the_ai_jobs_along():
    ids = {j.id for j in build_scheduler((Lane.FAST,)).get_jobs()}
    assert DAILY_REPORT_JOB_ID not in ids and FOLLOWUP_WRITER_JOB_ID not in ids


@pytest.fixture
def ai_jobs(monkeypatch):
    log = {"published": [], "recorded": [], "sent": 0}

    async def publish(after, *, stands):
        log["published"].append(after)

    async def record(run):
        log["recorded"].append(run)

    async def send(engine, settings, *, now):
        log["sent"] += 1

    monkeypatch.setattr(scheduler, "_publish", publish)
    monkeypatch.setattr(scheduler, "_record", record)
    monkeypatch.setattr(scheduler, "send_daily_report", send)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    monkeypatch.setattr(scheduler, "get_settings", lambda: None)
    monkeypatch.setattr(scheduler, "_layer", lambda: "the layer")
    return log


async def test_a_written_report_is_published_sent_and_recorded(ai_jobs, monkeypatch):
    async def write(*, layer):
        return ReportSummary(written=True, mode="full")

    monkeypatch.setattr(scheduler, "write_daily_report", write)
    await scheduler._daily_report_job()
    assert ai_jobs["published"] == ["the daily report"] and ai_jobs["sent"] == 1
    [run] = ai_jobs["recorded"]
    assert run.job == "daily-report" and run.completed and run.changed
    assert run.detail == {"mode": "full"}


async def test_a_report_already_written_is_still_sent_if_it_was_not(ai_jobs, monkeypatch):
    async def write(*, layer):
        return ReportSummary(skipped="the report for 2026-11-01 is written")

    monkeypatch.setattr(scheduler, "write_daily_report", write)
    await scheduler._daily_report_job()
    assert ai_jobs["published"] == [] and ai_jobs["sent"] == 1


async def test_a_report_pass_that_raises_does_not_stop_the_schedule(ai_jobs, monkeypatch, caplog):
    async def boom(*, layer):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(scheduler, "write_daily_report", boom)
    with caplog.at_level(logging.ERROR):
        await scheduler._daily_report_job()
    [run] = ai_jobs["recorded"]
    assert not run.completed and "daily report failed" in caplog.text
    assert ai_jobs["sent"] == 1


async def test_follow_up_reports_are_recorded_and_published(ai_jobs, monkeypatch):
    async def write(*, layer):
        return FollowupSummary(mode="full", quiet=2, recorded=1, failed=1)

    monkeypatch.setattr(scheduler, "write_followups", write)
    await scheduler._followup_writer_job()
    [run] = ai_jobs["recorded"]
    assert (run.job, run.completed, run.changed, run.errors) == ("followup-writer", True, True, 1)
    assert run.detail == {"mode": "full", "quiet": 2, "recorded": 1}
    assert ai_jobs["published"] == ["the follow-up writer"]


async def test_a_follow_up_pass_that_raises_is_recorded_and_not_fatal(ai_jobs, monkeypatch):
    async def boom(*, layer):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(scheduler, "write_followups", boom)
    await scheduler._followup_writer_job()
    [run] = ai_jobs["recorded"]
    assert not run.completed and ai_jobs["published"] == []


def test_the_watchdog_is_given_the_diagnosis(monkeypatch):
    monkeypatch.setattr(scheduler, "_watchdog", None)
    monkeypatch.setattr(scheduler, "get_settings", lambda: _settings_for_watchdog())
    monkeypatch.setattr(scheduler, "_layer", lambda: "the layer")
    dog = scheduler._the_watchdog()
    assert dog._diagnose is not None and dog._diagnose.keywords == {"layer": "the layer"}


def _settings_for_watchdog():
    from worker.settings import Settings

    return Settings(_env_file=None, database_url="postgresql://x")
