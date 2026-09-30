from apscheduler.triggers.cron import CronTrigger

from worker.models import Lane
from worker.scheduler import build_scheduler, job_id


def fields(trigger: CronTrigger) -> dict[str, str]:
    return {f.name: str(f) for f in trigger.fields}


def test_fast_and_normal_are_scheduled_deep_is_not():
    jobs = {j.id for j in build_scheduler().get_jobs()}
    assert jobs == {job_id(Lane.FAST), job_id(Lane.NORMAL)}


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
