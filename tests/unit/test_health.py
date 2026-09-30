"""Source health assessment and lifecycle transitions (Review Focus #1: stale-but-200 feeds)."""

from datetime import UTC, datetime, timedelta

import pytest

from worker.collectors.http import FetchResult, FetchStatus
from worker.models import NormalisedItem, SourceConfig, SourceHealth
from worker.pipeline.health import (
    HealthStatus,
    LifecycleState,
    assess,
    consecutive_failures,
    next_lifecycle_state,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

src = SourceConfig(
    id="acsc-alerts", name="ACSC Alerts", type="rss", region="AU", category="advisory",
    source_class="AUTHORITATIVE", priority=1, lane="fast", enabled=True,
    url="https://example.org/feed", parser="rss", expected_frequency="daily",
)
degraded_src = src.model_copy(update={"lifecycle_state": LifecycleState.DEGRADED})

ok_fetch = FetchResult(FetchStatus.OK, body=b"<rss/>", duration_ms=240)
not_modified_fetch = FetchResult(FetchStatus.NOT_MODIFIED, duration_ms=35)


def item(*, published, estimated=False, n=0) -> NormalisedItem:
    return NormalisedItem(
        source_id="acsc-alerts", url=f"https://example.org/{n}", canonical_url=f"https://example.org/{n}",
        title=f"Item {n}", normalised_title=f"item {n}", published=published, fetched_at=NOW,
        url_hash=f"u{n}", title_hash=f"t{n}", published_is_estimated=estimated,
    )


def health(status, *, at=NOW, age=None, error=None) -> SourceHealth:
    return SourceHealth(
        source_id="acsc-alerts", checked_at=at, status=status, error=error,
        newest_item_age_days=age, items_fetched=0, duration_ms=0,
    )


ok_health = health(HealthStatus.OK, at=NOW - timedelta(minutes=15), age=0.5)


def err(minutes_ago=0):
    return health(HealthStatus.ERROR, at=NOW - timedelta(minutes=minutes_ago), error="boom")


def errs(n):
    return [err(minutes_ago=(n - i) * 15) for i in range(n)]


def test_successful_fetch_with_recent_items_is_ok():
    h = assess(src, ok_fetch, [item(published=NOW - timedelta(hours=2))], now=NOW, history=[])
    assert h.status is HealthStatus.OK


def test_http_200_with_only_old_items_is_stale_not_ok():
    """The Google Security Blog case: feed moved, old feed still serves 200 and valid XML."""
    h = assess(src, ok_fetch, [item(published=NOW - timedelta(days=120))], now=NOW, history=[])
    assert h.status is HealthStatus.STALE and h.newest_item_age_days == 120


def test_stale_feed_with_many_old_items_is_still_stale():
    items = [item(published=NOW - timedelta(days=120 + i), n=i) for i in range(50)]
    h = assess(src, ok_fetch, items, now=NOW, history=[])
    assert h.status is HealthStatus.STALE and h.newest_item_age_days == 120
    assert h.items_fetched == 50


def test_newest_item_decides_regardless_of_order():
    items = [
        item(published=NOW - timedelta(days=300), n=1),
        item(published=NOW - timedelta(hours=3), n=2),
        item(published=NOW - timedelta(days=200), n=3),
    ]
    h = assess(src, ok_fetch, items, now=NOW, history=[])
    assert h.status is HealthStatus.OK
    assert h.newest_item_age_days == pytest.approx(3 / 24)


def test_stale_health_carries_a_reason():
    h = assess(src, ok_fetch, [item(published=NOW - timedelta(days=120))], now=NOW, history=[])
    assert h.error and "120" in h.error


def test_estimated_dates_do_not_make_a_stale_feed_look_fresh():
    """normalise() backfills a missing date with fetched_at (estimated); that says nothing
    about when the feed last published."""
    items = [
        item(published=NOW - timedelta(days=120), n=1),
        item(published=NOW, estimated=True, n=2),
    ]
    h = assess(src, ok_fetch, items, now=NOW, history=[])
    assert h.status is HealthStatus.STALE and h.newest_item_age_days == 120


def test_implausible_future_date_does_not_mask_staleness():
    items = [
        item(published=NOW - timedelta(days=120), n=1),
        item(published=NOW + timedelta(days=400), n=2),
    ]
    h = assess(src, ok_fetch, items, now=NOW, history=[])
    assert h.status is HealthStatus.STALE and h.newest_item_age_days == 120


def test_small_clock_skew_into_the_future_is_fresh_with_age_zero():
    h = assess(src, ok_fetch, [item(published=NOW + timedelta(minutes=30))], now=NOW, history=[])
    assert h.status is HealthStatus.OK and h.newest_item_age_days == 0


def test_no_trustworthy_dates_is_ok_with_unknown_age():
    h = assess(src, ok_fetch, [item(published=NOW, estimated=True)], now=NOW, history=[])
    assert h.status is HealthStatus.OK and h.newest_item_age_days is None


@pytest.mark.parametrize(
    ("frequency", "limit_days"),
    [("real_time", 2), ("hourly", 2), ("daily", 7), ("weekly", 30), ("monthly", 90)],
)
def test_threshold_boundaries(frequency, limit_days):
    s = src.model_copy(update={"expected_frequency": frequency})
    just_in = assess(s, ok_fetch, [item(published=NOW - timedelta(days=limit_days))], now=NOW, history=[])
    just_out = assess(
        s, ok_fetch, [item(published=NOW - timedelta(days=limit_days, seconds=1))], now=NOW, history=[]
    )
    assert just_in.status is HealthStatus.OK
    assert just_out.status is HealthStatus.STALE


def test_unknown_frequency_falls_back_to_daily_threshold():
    s = src.model_copy(update={"expected_frequency": "whenever"})
    assert assess(s, ok_fetch, [item(published=NOW - timedelta(days=8))], now=NOW, history=[]).status is HealthStatus.STALE


def test_staleness_threshold_comes_from_expected_frequency():
    daily = src.model_copy(update={"expected_frequency": "daily"})
    assert assess(daily, ok_fetch, [item(published=NOW - timedelta(days=14))], now=NOW, history=[]).status is HealthStatus.STALE
    monthly = src.model_copy(update={"expected_frequency": "monthly"})
    assert assess(monthly, ok_fetch, [item(published=NOW - timedelta(days=14))], now=NOW, history=[]).status is HealthStatus.OK


def test_zero_items_is_empty():
    assert assess(src, ok_fetch, [], now=NOW, history=[]).status is HealthStatus.EMPTY


def test_error_and_timeout_fetches_map_through_with_message():
    e = assess(src, FetchResult(FetchStatus.ERROR, error="HTTP 500", duration_ms=90), [], now=NOW, history=[])
    assert e.status is HealthStatus.ERROR and e.error == "HTTP 500" and e.duration_ms == 90
    t = assess(src, FetchResult(FetchStatus.TIMEOUT, error="timed out", duration_ms=30000), [], now=NOW, history=[])
    assert t.status is HealthStatus.TIMEOUT and t.error == "timed out"


def test_disabled_source_is_disabled():
    off = src.model_copy(update={"enabled": False})
    assert assess(off, ok_fetch, [item(published=NOW)], now=NOW, history=[]).status is HealthStatus.DISABLED


def test_not_modified_preserves_previous_status():
    assert assess(src, not_modified_fetch, [], now=NOW, history=[ok_health]).status is HealthStatus.OK


def test_not_modified_uses_the_latest_history_entry_whatever_the_order():
    older = health(HealthStatus.ERROR, at=NOW - timedelta(hours=5), error="x")
    newest = ok_health
    assert assess(src, not_modified_fetch, [], now=NOW, history=[newest, older]).status is HealthStatus.OK
    assert assess(src, not_modified_fetch, [], now=NOW, history=[older, newest]).status is HealthStatus.OK


def test_not_modified_keeps_a_stale_feed_stale():
    prev = health(HealthStatus.STALE, at=NOW - timedelta(minutes=15), age=120.0, error="old")
    h = assess(src, not_modified_fetch, [], now=NOW, history=[prev])
    assert h.status is HealthStatus.STALE
    assert h.newest_item_age_days == pytest.approx(120 + 15 / 1440)


def test_not_modified_ages_out_a_feed_that_stops_publishing():
    """A feed answering 304 forever must still turn STALE once its newest item ages past
    the threshold."""
    prev = health(HealthStatus.OK, at=NOW - timedelta(days=3), age=6.0)
    h = assess(src, not_modified_fetch, [], now=NOW, history=[prev])
    assert h.status is HealthStatus.STALE
    assert h.newest_item_age_days == pytest.approx(9.0)


def test_not_modified_with_no_history_is_ok():
    assert assess(src, not_modified_fetch, [], now=NOW, history=[]).status is HealthStatus.OK


def test_not_modified_carries_forward_a_failure_status():
    h = assess(src, not_modified_fetch, [], now=NOW, history=[err(15)])
    assert h.status is HealthStatus.ERROR and h.error == "boom"


def test_health_records_duration_and_counts():
    i1, i2 = item(published=NOW, n=1), item(published=NOW, n=2)
    h = assess(src, ok_fetch, [i1, i2], now=NOW, history=[])
    assert (h.items_fetched, h.duration_ms) == (2, ok_fetch.duration_ms)
    assert h.source_id == src.id and h.checked_at == NOW


def test_source_health_status_widening_stays_string_compatible():
    h = health(HealthStatus.OK)
    assert h.status == "ok"
    assert SourceHealth(source_id="s", checked_at=NOW, status="ok").status is HealthStatus.OK


def test_three_failures_warn_five_consecutive_degrade():
    assert next_lifecycle_state(src, errs(3)) is LifecycleState.ACTIVE      # warning only
    assert next_lifecycle_state(src, errs(4)) is LifecycleState.ACTIVE
    assert next_lifecycle_state(src, errs(5)) is LifecycleState.DEGRADED


def test_consecutive_failures_counts_the_trailing_run():
    assert consecutive_failures([]) == 0
    assert consecutive_failures(errs(3)) == 3
    assert consecutive_failures(errs(4) + [ok_health.model_copy(update={"checked_at": NOW})]) == 0
    early_ok = ok_health.model_copy(update={"checked_at": NOW - timedelta(hours=2)})
    assert consecutive_failures([early_ok] + errs(3)) == 3


def test_a_success_in_between_resets_the_failure_streak():
    later_ok = health(HealthStatus.OK, at=NOW - timedelta(minutes=30))
    history = errs(3)[:1] + [later_ok] + [err(15), err(0)]
    assert next_lifecycle_state(src, history) is LifecycleState.ACTIVE


def test_failure_history_order_does_not_matter():
    assert next_lifecycle_state(src, list(reversed(errs(5)))) is LifecycleState.DEGRADED


def test_stale_and_empty_count_as_failures():
    mixed = [
        health(HealthStatus.STALE, at=NOW - timedelta(minutes=60)),
        health(HealthStatus.EMPTY, at=NOW - timedelta(minutes=45)),
        health(HealthStatus.TIMEOUT, at=NOW - timedelta(minutes=30)),
        health(HealthStatus.STALE, at=NOW - timedelta(minutes=15)),
        health(HealthStatus.ERROR, at=NOW),
    ]
    assert next_lifecycle_state(src, mixed) is LifecycleState.DEGRADED


def test_recovery_returns_a_degraded_source_to_testing_not_active():
    assert next_lifecycle_state(degraded_src, [ok_health]) is LifecycleState.TESTING


def test_degraded_source_stays_degraded_while_still_failing():
    assert next_lifecycle_state(degraded_src, [ok_health] + errs(1)) is LifecycleState.DEGRADED
    assert next_lifecycle_state(degraded_src, []) is LifecycleState.DEGRADED


def test_broken_source_recovers_to_testing_too():
    broken = src.model_copy(update={"lifecycle_state": LifecycleState.BROKEN})
    assert next_lifecycle_state(broken, [ok_health]) is LifecycleState.TESTING


def test_a_testing_source_is_never_promoted_by_health_alone():
    """Promotion is SERAPH's multi-run gate, not a side effect of one good fetch."""
    testing = src.model_copy(update={"lifecycle_state": LifecycleState.TESTING})
    good = [ok_health.model_copy(update={"checked_at": NOW - timedelta(minutes=15 * i)}) for i in range(10)]
    assert next_lifecycle_state(testing, good) is LifecycleState.TESTING


@pytest.mark.parametrize(
    "state",
    [LifecycleState.DISCOVERED, LifecycleState.CANDIDATE, LifecycleState.VALIDATED, LifecycleState.RETIRED],
)
def test_gate_owned_states_are_untouched_by_health(state):
    s = src.model_copy(update={"lifecycle_state": state})
    assert next_lifecycle_state(s, errs(9)) is state
    assert next_lifecycle_state(s, [ok_health]) is state


def test_unset_lifecycle_state_defaults_from_enabled_flag():
    assert src.lifecycle_state is None
    assert next_lifecycle_state(src, []) is LifecycleState.ACTIVE
    off = src.model_copy(update={"enabled": False})
    assert next_lifecycle_state(off, errs(9)) is LifecycleState.DISCOVERED


def test_disabled_health_entries_do_not_break_a_streak_or_count_as_recovery():
    disabled = health(HealthStatus.DISABLED, at=NOW - timedelta(minutes=7))
    assert next_lifecycle_state(degraded_src, [disabled]) is LifecycleState.DEGRADED
    history = errs(5)
    history.insert(2, disabled.model_copy(update={"checked_at": history[2].checked_at + timedelta(minutes=1)}))
    assert next_lifecycle_state(src, history) is LifecycleState.DEGRADED
