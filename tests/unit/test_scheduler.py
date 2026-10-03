from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger

from worker import scheduler
from worker.models import Lane
from worker.ai.enrich import EnrichSummary
from worker.scheduler import ENRICH_JOB_ID, GROUNDTRUTH_JOB_ID, build_scheduler, job_id


def fields(trigger: CronTrigger) -> dict[str, str]:
    return {f.name: str(f) for f in trigger.fields}


def test_fast_and_normal_are_scheduled_deep_is_not():
    jobs = {j.id for j in build_scheduler().get_jobs()}
    assert jobs == {job_id(Lane.FAST), job_id(Lane.NORMAL), GROUNDTRUTH_JOB_ID, ENRICH_JOB_ID}


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

    state = {"run_raises": False, "publish_raises": False}
    monkeypatch.setattr(scheduler.pipeline_run, "run_lane", fake_run_lane)
    monkeypatch.setattr(scheduler, "publish_now", fake_publish_now)
    return log, state


async def test_a_scheduled_lane_run_publishes_what_it_collected(lane_job):
    log, _ = lane_job
    await scheduler._run_lane_job(Lane.FAST)
    assert log == [("run", Lane.FAST), ("publish",)]


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


async def test_a_failed_run_is_swallowed_so_the_next_tick_still_fires(lane_job):
    _, state = lane_job
    state["run_raises"] = True
    await scheduler._run_lane_job(Lane.FAST)  # must not raise


@pytest.fixture
def enrich_job(monkeypatch):
    """The scheduled enrichment pass with the pass and the publisher stubbed."""
    log = []
    state = {"summary": EnrichSummary(), "raises": False}

    async def fake_enrich_pending(*, layer):
        log.append(("enrich", layer))
        if state["raises"]:
            raise RuntimeError("ledger write failed")
        return state["summary"]

    async def fake_publish_now():
        log.append(("publish",))
        return [Path("data/live.json")]

    monkeypatch.setattr(scheduler, "enrich_pending", fake_enrich_pending)
    monkeypatch.setattr(scheduler, "publish_now", fake_publish_now)
    monkeypatch.setattr(scheduler, "AiLayer", lambda: "the layer")
    monkeypatch.setattr(scheduler, "_ai_layer", None)
    return log, state


async def test_an_enrichment_that_changed_something_is_published(enrich_job):
    log, state = enrich_job
    state["summary"].done["brief"] = 1
    await scheduler._enrich_job()
    assert log == [("enrich", "the layer"), ("publish",)]


async def test_an_enrichment_that_changed_nothing_is_not_published(enrich_job):
    log, _ = enrich_job
    await scheduler._enrich_job()
    assert log == [("enrich", "the layer")]


async def test_the_ai_layer_is_kept_between_passes(enrich_job, monkeypatch):
    # The governor in it must remember a 402 from one pass to the next.
    log, _ = enrich_job
    made = []
    monkeypatch.setattr(scheduler, "AiLayer", lambda: made.append(1) or "the layer")
    await scheduler._enrich_job()
    await scheduler._enrich_job()
    assert made == [1] and [entry[1] for entry in log] == ["the layer", "the layer"]


async def test_a_failed_enrichment_is_swallowed_and_not_published(enrich_job):
    log, state = enrich_job
    state["raises"] = True
    await scheduler._enrich_job()  # must not raise
    assert log == [("enrich", "the layer")]
