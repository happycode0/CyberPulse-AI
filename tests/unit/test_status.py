"""worker/pipeline/status.py: each status from an event's record, and config/followup.yaml."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from worker.models import EventStatus, Severity
from worker.pipeline.status import (
    FollowupConfig,
    StatusFacts,
    StatusRule,
    Transition,
    derive_status,
    transitions,
)

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
RULE = StatusRule(monitoring_after_days=3, resolved_after_days=14)
S = EventStatus


def facts(
    *,
    status: EventStatus = S.NEW,
    age_days: float = 1,
    quiet_days: float | None = None,
    confirmed: bool = False,
    fixed_days_ago: float | None = None,
    changed_days_ago: float | None = None,
    event_id: str = "evt-2026-000001",
) -> StatusFacts:
    def ago(days):
        return None if days is None else NOW - timedelta(days=days)

    return StatusFacts(
        event_id=event_id,
        status=status,
        first_seen=ago(age_days),
        last_material_update=ago(quiet_days),
        confirmed=confirmed,
        fixed_at=ago(fixed_days_ago),
        changed_at=ago(changed_days_ago),
    )


@pytest.mark.parametrize(
    "record, expected",
    [
        # One report and nothing else stays new, however old: there is nothing to follow.
        ({}, S.NEW),
        ({"age_days": 40}, S.NEW),
        # Confirmed by an independent source.
        ({"confirmed": True, "quiet_days": 1}, S.ACTIVE),
        ({"confirmed": True, "quiet_days": 3}, S.MONITORING),
        # Something material changed.
        ({"changed_days_ago": 1, "quiet_days": 1}, S.DEVELOPING),
        ({"changed_days_ago": 1, "quiet_days": 1, "confirmed": True}, S.DEVELOPING),
        ({"changed_days_ago": 5, "quiet_days": 5}, S.MONITORING),
        # A patch or mitigation, then quiet.
        ({"fixed_days_ago": 1, "quiet_days": 1}, S.CONTAINED),
        ({"fixed_days_ago": 10, "quiet_days": 10}, S.CONTAINED),
        ({"fixed_days_ago": 14, "quiet_days": 14}, S.RESOLVED),
        # A change after the fix reopens the event while it is fresh ...
        ({"fixed_days_ago": 2, "changed_days_ago": 1, "quiet_days": 1}, S.DEVELOPING),
        # ... and it settles back to contained, and then resolved, once quiet.
        ({"fixed_days_ago": 6, "changed_days_ago": 5, "quiet_days": 5}, S.CONTAINED),
        ({"fixed_days_ago": 20, "changed_days_ago": 15, "quiet_days": 15}, S.RESOLVED),
        # A change before the fix does not reopen it.
        ({"fixed_days_ago": 1, "changed_days_ago": 2, "quiet_days": 1}, S.CONTAINED),
        # With no material update recorded, quiet counts from first seen.
        ({"confirmed": True, "age_days": 4}, S.MONITORING),
    ],
)
def test_the_status_follows_the_record(record, expected):
    assert derive_status(facts(**record), RULE, now=NOW) is expected


def test_a_repeated_run_moves_nothing_twice():
    record = [facts(changed_days_ago=1, quiet_days=1)]
    [moved] = transitions(record, RULE, now=NOW)
    assert moved == Transition("evt-2026-000001", S.NEW, S.DEVELOPING)
    settled = [facts(status=S.DEVELOPING, changed_days_ago=1, quiet_days=1)]
    assert transitions(settled, RULE, now=NOW) == []


def test_an_archived_event_is_never_moved():
    record = [facts(status=S.ARCHIVED, changed_days_ago=1, quiet_days=1)]
    assert transitions(record, RULE, now=NOW) == []


def test_a_missed_run_lands_where_the_record_says():
    """Status is worked out afresh, not stepped: a developing event quiet for 15 days after its
    fix goes straight to resolved."""
    record = [facts(status=S.DEVELOPING, fixed_days_ago=15, changed_days_ago=16, quiet_days=15)]
    assert transitions(record, RULE, now=NOW) == [
        Transition("evt-2026-000001", S.DEVELOPING, S.RESOLVED)
    ]


# --- config/followup.yaml --------------------------------------------------------------------------


def test_the_shipped_config_loads():
    config = FollowupConfig.load()
    assert config.status.monitoring_after_days < config.status.resolved_after_days
    cadence = config.followup.cadence()
    assert {status for status, _, _ in cadence} == {S.DEVELOPING, S.MONITORING}
    # Critical events are checked at least as often as anything else in the same status.
    for status in (S.DEVELOPING, S.MONITORING):
        hours = {sev: h for st, sev, h in cadence if st is status}
        assert hours[Severity.CRITICAL] == min(hours.values())
    assert set(config.followup.final_summary) <= set(Severity)


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "followup.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def base() -> dict:
    return {
        "status": {"monitoring_after_days": 3, "resolved_after_days": 14},
        "followup": {
            "every_hours": {"developing": {"critical": 6}},
            "batch": 5,
            "final_summary": ["critical"],
        },
    }


@pytest.mark.parametrize(
    "change, says",
    [
        (lambda d: d["followup"]["every_hours"].update(new={"high": 6}), "developing and"),
        (lambda d: d["followup"]["every_hours"]["developing"].update(critical=0), "greater than"),
        (lambda d: d["followup"].update(batch=0), "greater than or equal"),
        (lambda d: d["followup"].update(batch=500), "less than or equal"),
        (lambda d: d["status"].update(resolved_after_days=-1), "greater than"),
        (lambda d: d["followup"].update(extra=1), "Extra inputs"),
    ],
)
def test_a_bad_config_is_refused(tmp_path, change, says):
    data = base()
    change(data)
    with pytest.raises(ValidationError, match=says):
        FollowupConfig.load(write(tmp_path, data))
