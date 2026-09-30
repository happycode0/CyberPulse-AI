from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from worker.models import (
    AuRelevance,
    CveRef,
    Event,
    EvidenceClass,
    KevEntry,
    Severity,
    SourceRef,
)
from worker.pipeline.score import (
    ScoringConfig,
    base_weight,
    freshness,
    independent_confirmations,
    score_event,
)
from worker.version import SCORING_VERSION

CRITICAL, HIGH, MEDIUM, LOW, UNKNOWN = (
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.UNKNOWN,
)
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
CONFIG_PATH = Path(__file__).parents[2] / "config" / "scoring.yaml"
cfg = ScoringConfig.load(CONFIG_PATH)


def src(source_id, *, lineage=None, independent=False, cls=EvidenceClass.NEWS):
    return SourceRef(
        source_id=source_id,
        url=f"https://example.org/{source_id}",
        evidence_class=cls,
        lineage_id=lineage,
        independent=independent,
    )


def event(**overrides):
    fields = dict(
        event_id="evt-2026-000001",
        first_seen=NOW - timedelta(hours=1),
        last_seen=NOW,
        last_material_update=NOW,
        title="t",
        summary="s",
        severity=MEDIUM,
    )
    fields.update(overrides)
    return Event(**fields)


def test_severity_base_weights_match_config():
    assert [base_weight(s, cfg) for s in (CRITICAL, HIGH, MEDIUM, LOW)] == [4, 3, 2, 1]


def test_unknown_severity_scores_between_low_and_medium():
    assert base_weight(LOW, cfg) < base_weight(UNKNOWN, cfg) < base_weight(MEDIUM, cfg)


def test_kev_listing_adds_a_bonus():
    kev = event(cves=[CveRef(id="CVE-2026-0001", kev=KevEntry(listed=True))])
    plain = event(cves=[CveRef(id="CVE-2026-0001")])
    assert score_event(kev, cfg, now=NOW).risk.urgency > score_event(plain, cfg, now=NOW).risk.urgency


def test_independent_confirmations_ignores_shared_lineage():
    e = event(
        sources=[
            src("vendor", lineage="L1", independent=True),
            src("reuters", lineage="L1", independent=False),
            src("thn", lineage="L1", independent=False),
        ]
    )
    assert independent_confirmations(e) == 1


def test_independent_confirmations_dedupes_independent_sources_sharing_a_lineage():
    e = event(
        sources=[
            src("a", lineage="L1", independent=True),
            src("b", lineage="L1", independent=True),
        ]
    )
    assert independent_confirmations(e) == 1


def test_independent_confirmations_counts_distinct_lineages():
    e = event(
        sources=[
            src("vendor", lineage="L1", independent=True),
            src("acsc", lineage="L2", independent=True),
        ]
    )
    assert independent_confirmations(e) == 2


def test_independent_source_without_lineage_counts_once_each():
    e = event(sources=[src("a", independent=True), src("b", independent=True)])
    assert independent_confirmations(e) == 2


def test_no_independent_sources_means_zero_confirmations():
    assert independent_confirmations(event(sources=[src("a", lineage="L1")])) == 0
    assert independent_confirmations(event()) == 0


@pytest.mark.parametrize("sev,hours", [(LOW, 24), (MEDIUM, 24), (HIGH, 72), (CRITICAL, 168)])
def test_freshness_halves_at_the_configured_half_life(sev, hours):
    e = event(severity=sev, last_material_update=NOW - timedelta(hours=hours))
    assert freshness(e, cfg, NOW) == pytest.approx(0.5, abs=0.01)


def test_decay_resets_on_material_update_not_on_last_seen():
    stale = event(last_material_update=NOW - timedelta(days=7), last_seen=NOW)
    fresh = event(last_material_update=NOW, last_seen=NOW)
    assert freshness(fresh, cfg, NOW) > freshness(stale, cfg, NOW)


def test_freshness_ignores_last_seen_entirely():
    a = event(last_material_update=NOW - timedelta(days=2), last_seen=NOW)
    b = event(last_material_update=NOW - timedelta(days=2), last_seen=NOW - timedelta(days=2))
    assert freshness(a, cfg, NOW) == freshness(b, cfg, NOW)


def test_freshness_falls_back_to_first_seen_without_a_material_update():
    e = event(
        last_material_update=None,
        first_seen=NOW - timedelta(hours=24),
        last_seen=NOW,
    )
    assert freshness(e, cfg, NOW) == pytest.approx(0.5, abs=0.01)


def test_future_timestamps_do_not_boost_freshness_above_one():
    e = event(last_material_update=NOW + timedelta(hours=5))
    assert freshness(e, cfg, NOW) == 1.0


def test_repeat_sightings_do_not_refresh_prominence():
    old = dict(
        first_seen=NOW - timedelta(days=7),
        last_material_update=NOW - timedelta(days=7),
    )
    seen_long_ago = score_event(event(last_seen=NOW - timedelta(days=7), **old), cfg, now=NOW)
    seen_today = score_event(event(last_seen=NOW, **old), cfg, now=NOW)
    assert seen_long_ago.risk.prominence == seen_today.risk.prominence


def test_prominence_is_bounded_between_zero_and_one():
    extreme = event(
        severity=CRITICAL,
        first_seen=NOW,
        cves=[CveRef(id="CVE-2026-0001", kev=KevEntry(listed=True))],
        au=AuRelevance(relevance=1.0),
        sources=[
            src(f"s{i}", lineage=f"L{i}", independent=True, cls=EvidenceClass.PRIMARY)
            for i in range(20)
        ],
    )
    risk = score_event(extreme, cfg, now=NOW).risk
    assert 0.0 <= risk.prominence <= 1.0
    for value in (risk.urgency, risk.confidence, risk.novelty):
        assert 0.0 <= value <= 1.0


def test_ancient_event_prominence_approaches_zero():
    old = event(
        severity=LOW,
        first_seen=NOW - timedelta(days=60),
        last_material_update=NOW - timedelta(days=60),
    )
    assert 0.0 <= score_event(old, cfg, now=NOW).risk.prominence < 0.05


@pytest.mark.parametrize(
    "weaker,stronger",
    [
        (dict(severity=LOW), dict(severity=CRITICAL)),
        (dict(), dict(au=AuRelevance(relevance=0.9))),
        (
            dict(sources=[src("a", lineage="L1", independent=True)]),
            dict(
                sources=[
                    src("a", lineage="L1", independent=True),
                    src("b", lineage="L2", independent=True),
                ]
            ),
        ),
        (
            dict(last_material_update=NOW - timedelta(hours=30)),
            dict(last_material_update=NOW - timedelta(hours=2)),
        ),
    ],
)
def test_prominence_is_monotonic_in_severity_corroboration_au_and_freshness(weaker, stronger):
    lo = score_event(event(**weaker), cfg, now=NOW).risk.prominence
    hi = score_event(event(**stronger), cfg, now=NOW).risk.prominence
    assert hi > lo


def test_syndicated_copies_do_not_raise_confidence_or_prominence():
    one = event(sources=[src("vendor", lineage="L1", independent=True)])
    many = event(
        sources=[
            src("vendor", lineage="L1", independent=True),
            src("reuters", lineage="L1"),
            src("thn", lineage="L1"),
        ]
    )
    a, b = score_event(one, cfg, now=NOW).risk, score_event(many, cfg, now=NOW).risk
    assert (a.confidence, a.prominence) == (b.confidence, b.prominence)


def test_confidence_rises_with_evidence_class_and_independent_lineages():
    def conf(sources):
        return score_event(event(sources=sources), cfg, now=NOW).risk.confidence

    social = conf([src("a", lineage="L1", independent=True, cls=EvidenceClass.SOCIAL)])
    primary = conf([src("a", lineage="L1", independent=True, cls=EvidenceClass.PRIMARY)])
    corroborated = conf(
        [
            src("a", lineage="L1", independent=True, cls=EvidenceClass.SOCIAL),
            src("b", lineage="L2", independent=True, cls=EvidenceClass.SOCIAL),
        ]
    )
    assert social < corroborated < 1.0
    assert social < primary


def test_novelty_decays_from_first_seen():
    fresh = score_event(event(first_seen=NOW), cfg, now=NOW).risk.novelty
    older = score_event(event(first_seen=NOW - timedelta(days=5)), cfg, now=NOW).risk.novelty
    assert fresh == 1.0
    assert older < fresh


def test_scoring_populates_all_four_risk_numbers_without_mutating_the_input():
    e = event()
    assert e.risk.prominence is None
    scored = score_event(e, cfg, now=NOW)
    assert None not in scored.risk.model_dump().values()
    assert e.risk.prominence is None


def test_scoring_version_is_recorded_on_the_event():
    assert score_event(event(), cfg, now=NOW).scoring_version == SCORING_VERSION


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        score_event(event(), cfg, now=datetime(2026, 9, 30, 12, 0))


def _config_dict(**changes):
    import yaml

    data = yaml.safe_load(CONFIG_PATH.read_text())
    data.update(changes)
    return data


def test_config_rejects_a_version_that_disagrees_with_the_code():
    with pytest.raises(ValidationError, match="SCORING_VERSION"):
        ScoringConfig.model_validate(_config_dict(version="999"))


def test_config_rejects_prominence_weights_not_summing_to_one():
    bad = dict(severity=0.5, corroboration=0.5, au_relevance=0.5, novelty=0.5)
    with pytest.raises(ValidationError, match="sum to 1"):
        ScoringConfig.model_validate(_config_dict(prominence_weights=bad))


def test_config_rejects_missing_severity():
    weights = _config_dict()["severity_weights"]
    del weights["unknown"]
    with pytest.raises(ValidationError, match="severity_weights missing"):
        ScoringConfig.model_validate(_config_dict(severity_weights=weights))
