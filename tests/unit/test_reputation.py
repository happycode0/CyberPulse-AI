"""Source reputation (worker/pipeline/reputation.py, docs/wiki/importance-and-reputation.md)."""

from datetime import UTC, datetime, timedelta

import pytest

from worker.models import HealthStatus, SourceHealth, Standing
from worker.pipeline.reputation import (
    CORROBORATION_MIN_EVENTS,
    REPUTATION_VERSION,
    STANDING_BASE,
    Corroboration,
    assess_reputation,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def checks(*statuses: str) -> list[SourceHealth]:
    return [
        SourceHealth(source_id="s", checked_at=NOW - timedelta(hours=len(statuses) - i), status=s)
        for i, s in enumerate(statuses)
    ]


@pytest.mark.parametrize("standing", list(Standing))
def test_with_nothing_else_known_the_score_is_the_standing(standing):
    rep = assess_reputation(standing, [], None)
    assert rep.score == STANDING_BASE[standing]
    assert (rep.uptime, rep.corroboration, rep.events_90d) == (None, None, 0)
    assert rep.basis == (
        "standing only; left out: no health checks yet; 0 events in 90 days, too few to judge "
        "corroboration (needs 5)"
    )


def test_the_bases_rank_the_standings():
    assert [STANDING_BASE[s] for s in Standing] == [90, 75, 60, 40]


def test_all_three_parts_are_weighted_60_15_25():
    rep = assess_reputation(
        Standing.ESTABLISHED, checks("ok", "ok", "error", "ok"), Corroboration(20, 5)
    )
    # 0.60 * 75 + 0.15 * 75 + 0.25 * 25 = 62.5, half up.
    assert (rep.score, rep.uptime, rep.corroboration, rep.events_90d) == (63, 0.75, 0.25, 20)
    assert rep.basis == (
        "standing, uptime over the last 4 checks and corroboration of 20 events in 90 days"
    )


def test_a_missing_part_is_left_out_and_the_rest_renormalised():
    # No history: (0.60 * 90 + 0.25 * 100) / 0.85 = 92.9
    rep = assess_reputation(Standing.AUTHORITATIVE, [], Corroboration(10, 10))
    assert rep.score == 93 and rep.uptime is None
    assert rep.basis.endswith("; left out: no health checks yet")
    # No corroboration: (0.60 * 90 + 0.15 * 0) / 0.75 = 72
    rep = assess_reputation(Standing.AUTHORITATIVE, checks("timeout", "timeout"), None)
    assert rep.score == 72 and rep.uptime == 0.0 and rep.corroboration is None


def test_corroboration_needs_enough_events():
    few = assess_reputation(
        Standing.SPECIALIST, [], Corroboration(CORROBORATION_MIN_EVENTS - 1, 0)
    )
    assert few.corroboration is None and few.score == 60 and few.events_90d == 4
    assert "4 events in 90 days, too few to judge corroboration (needs 5)" in few.basis
    enough = assess_reputation(Standing.SPECIALIST, [], Corroboration(CORROBORATION_MIN_EVENTS, 0))
    # (0.60 * 60 + 0.25 * 0) / 0.85 = 42.4
    assert enough.corroboration == 0.0 and enough.score == 42


def test_a_source_with_no_standing_is_community():
    rep = assess_reputation(None, checks("ok"), None)
    assert rep.standing is Standing.COMMUNITY
    # (0.60 * 40 + 0.15 * 100) / 0.75 = 52
    assert rep.score == 52
    assert rep.basis.startswith("standing and uptime over the last check;")


def test_checks_of_a_disabled_source_do_not_count_against_it():
    rep = assess_reputation(Standing.SPECIALIST, checks("ok", "disabled", "disabled"), None)
    assert rep.uptime == 1.0
    assert assess_reputation(Standing.SPECIALIST, checks("disabled"), None).uptime is None


def test_the_public_shape():
    rep = assess_reputation(Standing.ESTABLISHED, checks("ok", "stale"), Corroboration(19, 0))
    assert rep.public() == {
        "version": REPUTATION_VERSION,
        "score": 53,  # 0.60 * 75 + 0.15 * 50 + 0.25 * 0 = 52.5
        "standing": "established",
        "uptime": 0.5,
        "corroboration": 0.0,
        "events_90d": 19,
        "basis": (
            "standing, uptime over the last 2 checks and corroboration of 19 events in 90 days"
        ),
    }


def test_a_score_stays_within_0_and_100():
    best = assess_reputation(Standing.AUTHORITATIVE, checks(*["ok"] * 20), Corroboration(50, 50))
    worst = assess_reputation(
        Standing.COMMUNITY, checks(*[HealthStatus.ERROR] * 20), Corroboration(50, 0)
    )
    assert (best.score, worst.score) == (94, 24)
