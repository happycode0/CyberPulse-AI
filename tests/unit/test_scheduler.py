import logging
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger

from worker import scheduler
from worker.ai.enrich import EnrichSummary
from worker.ai.mitre import MitreSummary
from worker.models import Lane
from worker.publish.push import PushResult
from worker.scheduler import (
    ALERT_JOB_ID,
    DIGEST_JOB_ID,
    ENRICH_JOB_ID,
    GROUNDTRUTH_JOB_ID,
    build_scheduler,
    job_id,
)


def fields(trigger: CronTrigger) -> dict[str, str]:
    return {f.name: str(f) for f in trigger.fields}


def test_fast_and_normal_are_scheduled_deep_is_not():
    jobs = {j.id for j in build_scheduler().get_jobs()}
    assert jobs == {
        job_id(Lane.FAST), job_id(Lane.NORMAL), GROUNDTRUTH_JOB_ID, ENRICH_JOB_ID, DIGEST_JOB_ID,
        ALERT_JOB_ID,
    }


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


def test_the_digest_goes_at_seven_sydney_time_with_two_retries():
    job = {j.id: j for j in build_scheduler().get_jobs()}[DIGEST_JOB_ID]
    assert (fields(job.trigger)["minute"], fields(job.trigger)["hour"]) == ("0", "7,8,9")
    assert str(job.trigger.timezone) == "Australia/Sydney"


def test_alerts_follow_each_fast_run():
    job = {j.id: j for j in build_scheduler().get_jobs()}[ALERT_JOB_ID]
    assert (fields(job.trigger)["minute"], fields(job.trigger)["hour"]) == ("3,18,33,48", "*")


def test_asking_for_one_lane_does_not_bring_notifications_along():
    jobs = {j.id for j in build_scheduler((Lane.FAST,)).get_jobs()}
    assert DIGEST_JOB_ID not in jobs and ALERT_JOB_ID not in jobs


@pytest.mark.parametrize("name, sender", [("_digest_job", "send_daily_digest"),
                                          ("_alert_job", "send_critical_alerts")])
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


def test_cadences():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    fast, normal = jobs[job_id(Lane.FAST)].trigger, jobs[job_id(Lane.NORMAL)].trigger
    assert fields(fast)["minute"] == "*/15" and fields(fast)["hour"] == "*"
    assert (fields(normal)["minute"], fields(normal)["hour"]) == ("0", "*/4")
    assert str(fast.timezone) == str(normal.timezone) == "UTC"


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
    assert log == [("run", Lane.FAST), ("publish",), ("push",)]
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
    assert log[-1] == ("push",) and "push after the fast lane failed" in caplog.text


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
    assert log == [("run", Lane.FAST), ("publish",)]


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
    assert [entry[0] for entry in log] == ["sync", "record", "publish", "push"]
    run = log[1][1]
    assert (run.job, run.completed, run.errors, run.changed) == ("groundtruth", True, 1, True)
    assert run.started_at <= run.finished_at


async def test_a_ground_truth_sync_that_raised_is_recorded_as_not_completed(lane_job, monkeypatch):
    """LIBRARIAN's wake reads the newest completed pass, so a raising one must not count."""
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
    assert [entry[0] for entry in log] == ["record", "publish", "push"]
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
    [run] = state["recorded"]
    assert (run.job, run.completed, run.errors, run.changed) == ("enrichment", True, 4, True)


async def test_an_enrichment_half_that_raised_is_recorded_as_not_completed(enrich_job):
    _, state = enrich_job
    state["mitre_raises"] = True
    await scheduler._enrich_job()
    [run] = state["recorded"]
    assert (run.completed, run.changed) == (False, False)
