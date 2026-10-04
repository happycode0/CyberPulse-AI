"""Event importance (worker/pipeline/importance.py, docs/wiki/importance-and-reputation.md)."""

from datetime import UTC, date, datetime

import pytest

from worker.models import (
    AiSignificance,
    Event,
    ImportanceTier,
    Severity,
    SeveritySource,
    Standing,
)
from worker.pipeline.importance import (
    IMPORTANCE_VERSION,
    KEY_AT,
    MAX_REASONS,
    NOTABLE_AT,
    OFF_DESK_CAP,
    Voice,
    assess_importance,
    names_government,
    tier_of,
    with_importance,
)

NOW = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)
VOICES = {
    "abc_cyber": Voice("ABC", Standing.ESTABLISHED, 60),
    "asd": Voice("Australian Signals Directorate", Standing.AUTHORITATIVE, 92),
    "cisa_kev": Voice("CISA", Standing.AUTHORITATIVE, 64),
    "thn": Voice("The Hacker News", Standing.SPECIALIST, 58),
}


def source(source_id: str, lineage: str | None = None, *, independent: bool = True) -> dict:
    return {
        "source_id": source_id, "url": f"https://{source_id}.example.org/a",
        "evidence_class": "NEWS", "lineage_id": lineage, "independent": independent,
    }


def event(**fields) -> Event:
    base = {
        "event_id": "evt-2026-000001", "title": "Something happened", "summary": "s",
        "first_seen": NOW, "last_seen": NOW, "domains": ["cybersecurity"],
    }
    return Event(**(base | fields))


def kev_cve(listed: bool = True) -> dict:
    return {
        "id": "CVE-2026-88771",
        "kev": {"listed": listed, "date_added": date(2026, 10, 1) if listed else None},
    }


def score(e: Event, voices=VOICES) -> int:
    return assess_importance(e, voices).score


# --- The two the brief names -------------------------------------------------------------------


def test_the_abc_rogue_agent_story_is_key():
    """evt-2026-004814 as published on 2026-10-04: one ABC report, triaged by the model."""
    abc = event(
        event_id="evt-2026-004814",
        title="Rogue OpenAI agent accessed second NSW government website",
        domains=["cybersecurity", "ai"],
        categories=["ai-incident", "emerging-threat", "news"],
        ai_significance=AiSignificance.NOTABLE,
        severity=Severity.LOW,
        severity_source=SeveritySource.AI_ESTIMATE,
        au={
            "relevance": 0.6, "directly_reported_in_au": True, "sectors": ["government"],
        },
        entities={
            "organisations": ["National Parks and Wildlife Service", "OpenAI"],
            "industries": ["government"], "countries": ["AU"],
        },
        sources=[source("abc_cyber", "abc")],
    )
    importance = assess_importance(abc, VOICES)
    assert (importance.score, importance.tier) == (75, ImportanceTier.KEY)
    assert importance.reasons == [
        "relevant to Australia, reported there",  # 25
        "AI incident",  # 15: the model guessed low, but harm was done
        "Australian government target",  # 15
        "reported by ABC (established)",  # 15
        "cyber and AI",  # 5
    ]


@pytest.mark.parametrize(
    "title",
    [
        "Rogue OpenAI agent accessed second NSW government website",
        "Department of Home Affairs confirms phishing campaign",
        "Services Australia warns of scam texts",
        "Regional council hit by ransomware",
        "Federal agency breached through VPN flaw",
    ],
)
def test_a_government_target_is_found_from_the_headline_alone(title):
    e = event(title=title, categories=["data-breach"])
    assert names_government(e)
    assert "government target" in assess_importance(e, VOICES).reasons


@pytest.mark.parametrize(
    "title",
    [
        "Governance tooling for AI agents ships",
        "Chrome patches a zero-day in V8",
        "Police arrest ransomware affiliate",
    ],
)
def test_no_government_where_none_is_named(title):
    assert not names_government(event(title=title))


def test_a_government_named_without_a_threat_is_only_involved():
    e = event(title="Minister announces a cyber strategy", categories=["policy"])
    assert "government involved" in assess_importance(e, VOICES).reasons
    assert score(e) - score(event(categories=["policy"])) == 8


def test_critical_infrastructure_counts_as_public_sector():
    e = event(au={"soci_asset_classes": ["electricity"]}, categories=["ransomware"])
    assert "critical infrastructure (electricity) target" in assess_importance(e, VOICES).reasons
    sector = event(au={"relevance": 0.8, "sectors": ["health"]}, categories=["ransomware"])
    assert "Australian health sector target" in assess_importance(sector, VOICES).reasons


# --- Each part ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("au", "points", "reason"),
    [
        ({}, 0, None),
        ({"relevance": 0.2}, 8, "some Australian relevance"),
        ({"relevance": 0.3, "directly_reported_in_au": True}, 15, "reported in Australia"),
        ({"relevance": 0.6}, 15, "relevant to Australia"),
        ({"relevance": 0.6, "directly_reported_in_au": True}, 25,
         "relevant to Australia, reported there"),
        ({"relevance": 0.8}, 25, "strongly relevant to Australia"),
        ({"relevance": 0.9, "directly_reported_in_au": True}, 25,
         "strongly relevant to Australia"),
    ],
)
def test_australia_in_steps(au, points, reason):
    e = event(au=au)
    assert score(e) - score(event()) == points
    if reason:
        assert reason in assess_importance(e, VOICES).reasons


@pytest.mark.parametrize(
    ("fields", "points", "reason"),
    [
        ({"severity": "critical", "severity_source": "cna"}, 25, "critical severity (CNA score)"),
        ({"severity": "high", "severity_source": "nvd"}, 18, "high severity (NVD score)"),
        ({"severity": "medium", "severity_source": "vendor"}, 8, "medium severity (vendor rating)"),
        ({"severity": "low", "severity_source": "nvd"}, 0, None),
        ({"severity": "critical", "cves": [kev_cve()]}, 25,
         "critical severity, actively exploited (CISA KEV)"),
        ({"severity": "high", "cves": [kev_cve()]}, 20,
         "high severity, actively exploited (CISA KEV)"),
        ({"cves": [kev_cve()]}, 20, "actively exploited (CISA KEV)"),
        ({"cves": [kev_cve(listed=False)]}, 0, None),
        ({"categories": ["zero-day"]}, 20, "exploited as a zero-day"),
        ({"categories": ["active-exploitation"]}, 20, "actively exploited"),
        # An incident is harm done: at least this when no register has scored it...
        ({"categories": ["data-breach"]}, 15, "data breach"),
        ({"categories": ["ransomware"], "severity": "low", "severity_source": "ai_estimate"},
         15, "ransomware attack"),
        # ...but a register's score is a fact, and stands.
        ({"categories": ["data-breach"], "severity": "medium", "severity_source": "nvd"},
         8, "medium severity (NVD score)"),
    ],
)
def test_harm(fields, points, reason):
    e = event(**fields)
    assert score(e) - score(event()) == points
    if reason:
        assert reason in assess_importance(e, VOICES).reasons


def test_the_ai_desk_scale_counts_on_the_ai_beat_alone():
    major = event(domains=["ai"], ai_significance=AiSignificance.MAJOR)
    assert score(major) - score(event(domains=["ai"])) == 22
    assert "major AI story" in assess_importance(major, VOICES).reasons
    notable = event(domains=["ai"], ai_significance=AiSignificance.NOTABLE)
    assert score(notable) - score(event(domains=["ai"])) == 12
    # On both desks the cyber facts carry harm, and convergence adds its 5.
    both = event(domains=["cybersecurity", "ai"], ai_significance=AiSignificance.MAJOR)
    assert score(both) - score(event()) == 5
    assert assess_importance(both, VOICES).reasons == ["cyber and AI"]


def test_standing_is_a_quarter_of_the_best_reputation():
    one = event(sources=[source("thn")])
    best = event(sources=[source("thn"), source("asd")])
    assert score(best) - score(one) == 23 - 15 + 6  # and a second independent source
    assert "reported by Australian Signals Directorate (authoritative)" in assess_importance(
        best, VOICES).reasons
    # An unregistered source counts as community's 40, and is not named.
    unknown = assess_importance(event(sources=[source("gone")]), VOICES)
    assert unknown.score == 10 and unknown.reasons == []


@pytest.mark.parametrize(
    ("sources", "points"),
    [
        ([source("thn")], 0),
        ([source("thn", "x"), source("abc_cyber", "x")], 0),  # one story, syndicated
        ([source("thn"), source("abc_cyber", independent=False)], 0),
        ([source("thn"), source("abc_cyber")], 6),
        ([source("thn"), source("abc_cyber"), source("cisa_kev")], 12),
        ([source(f"s{n}") for n in range(6)], 12),
    ],
)
def test_corroboration(sources, points):
    voices = {}  # standing out of the way: every source the same floor
    assert score(event(sources=sources), voices) - score(event(sources=sources[:1]), voices) == (
        points
    )


# --- Tiers, reasons, limits --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "tier"),
    [(100, "key"), (KEY_AT, "key"), (KEY_AT - 1, "notable"), (NOTABLE_AT, "notable"),
     (NOTABLE_AT - 1, "routine"), (0, "routine")],
)
def test_tiers(value, tier):
    assert tier_of(value) is ImportanceTier(tier)


def test_the_tier_thresholds():
    assert (KEY_AT, NOTABLE_AT) == (60, 40)


def test_the_most_an_event_can_score_is_100():
    everything = event(
        title="Department of Health breached",
        domains=["cybersecurity", "ai"],
        severity="critical", cves=[kev_cve()],
        au={"relevance": 0.95, "directly_reported_in_au": True},
        sources=[source("asd"), source("thn"), source("abc_cyber")],
    )
    importance = assess_importance(everything, VOICES)
    # 25 + 25 + 15 + 23 + 12 + 5 = 105
    assert (importance.score, importance.tier) == (100, ImportanceTier.KEY)
    assert len(importance.reasons) == MAX_REASONS == 5
    assert importance.reasons[0] == "critical severity, actively exploited (CISA KEV)"
    assert "cyber and AI" not in importance.reasons  # the smallest part is the one left out


def test_an_event_on_neither_desk_is_routine_whatever_it_adds_up_to():
    off = event(
        title="Gallagher names a new head for NSW and ACT", domains=[],
        au={"relevance": 0.9, "directly_reported_in_au": True}, categories=["data-breach"],
        sources=[source("asd"), source("thn"), source("abc_cyber")],
    )
    importance = assess_importance(off, VOICES)
    assert (importance.score, importance.tier) == (OFF_DESK_CAP, ImportanceTier.ROUTINE)
    assert importance.reasons[0] == "neither cyber nor AI"
    assert len(importance.reasons) <= MAX_REASONS


def test_an_unread_event_is_rated_on_what_is_known():
    """No triage, no severity, no model at all: still a valid rating."""
    bare = assess_importance(event(domains=[], sources=[]), {})
    assert (bare.version, bare.score, bare.tier, bare.reasons) == (
        IMPORTANCE_VERSION, 10, ImportanceTier.ROUTINE, [],
    )


def test_with_importance_rates_a_copy():
    e = event(sources=[source("asd")])
    rated = with_importance(e, VOICES)
    assert e.importance is None
    assert rated.importance == assess_importance(e, VOICES)
    assert rated.model_dump_public()["importance"] == {
        "version": "1", "score": 23, "tier": "routine",
        "reasons": ["reported by Australian Signals Directorate (authoritative)"],
    }
