from datetime import UTC, date, datetime

import pytest

from worker.ai.golden import (
    GOLDEN_SIZE,
    au_desk_label,
    blinded,
    digest,
    labels_for,
    pin,
    stratify,
    subject_from_record,
)
from worker.ai.tasks import SourceFacts, Subject, record
from worker.models import (
    AuRelevance,
    CveRef,
    CvssScore,
    EpssScore,
    Event,
    KevEntry,
    Severity,
    SeveritySource,
)

T0 = datetime(2026, 9, 20, 14, 30, tzinfo=UTC)
SOURCE_TEXT = (
    "A heap overflow in the Acme SecureGate management interface lets an unauthenticated "
    "attacker run code as root. Acme has released fixed builds."
)


CISA = (SourceFacts("CISA", "us", "advisory"),)


def subject(sources=CISA, au=False, **changes) -> Subject:
    base = {
        "event_id": "evt-2026-000100",
        "first_seen": T0,
        "last_seen": T0,
        "title": "Acme SecureGate heap overflow",
        "summary": SOURCE_TEXT,
        "severity": Severity.CRITICAL,
        "severity_source": SeveritySource.CNA,
        "cves": [
            CveRef(
                id="CVE-2026-4321",
                cvss=CvssScore(score=9.8, source="cna"),
                epss=EpssScore(score=0.42, status="scored"),
                kev=KevEntry(listed=True, date_added=date(2026, 9, 21)),
            ),
            CveRef(id="CVE-2026-4322"),
        ],
        "au": AuRelevance(directly_reported_in_au=au),
    }
    base.update(changes)
    return Subject(
        event=Event(**base),
        source_text=SOURCE_TEXT,
        headlines=("Acme patches SecureGate flaw",),
        sources=tuple(sources),
    )


# ─── Choosing the set ─────────────────────────────────────────────────────────────────────────────


def test_the_set_is_spread_across_severities():
    candidates = [(f"c{i}", "critical") for i in range(20)]
    candidates += [(f"h{i}", "high") for i in range(20)]
    candidates += [(f"m{i}", "medium") for i in range(20)]
    candidates += [(f"l{i}", "low") for i in range(20)]
    chosen = stratify(candidates)
    assert len(chosen) == GOLDEN_SIZE
    assert [sum(e.startswith(p) for e in chosen) for p in "chml"] == [9, 9, 9, 3]


def test_a_short_stratum_is_made_up_from_the_others_in_their_order():
    candidates = [(f"c{i}", "critical") for i in range(20)] + [("m0", "medium"), ("l0", "low")]
    chosen = stratify(candidates)
    assert len(chosen) == 22  # everything there is, without repeats
    assert chosen[:9] == [f"c{i}" for i in range(9)] and chosen[9:11] == ["m0", "l0"]
    assert len(set(chosen)) == len(chosen)


def test_nothing_to_choose_from_is_an_empty_set():
    assert stratify([]) == []


# ─── Labels ───────────────────────────────────────────────────────────────────────────────────────


def test_the_labels_come_from_the_registers():
    labels = labels_for(subject())
    assert labels == {
        "severity": "critical",
        "triage": {"domain": "cybersecurity", "categories": ["vulnerability", "zero-day"]},
        "au_desk": False,
        "from": "registers",
    }


def test_an_acsc_advisory_belongs_on_the_au_desk():
    s = subject(
        sources=(SourceFacts("ACSC", "au", "advisory"), SourceFacts("CISA", "us", "advisory"))
    )
    assert au_desk_label(s) is True


@pytest.mark.parametrize(
    "changes",
    [
        {"au": True},
        {"sources": (SourceFacts("iTnews", "au", "news"),)},
        {"title": "Acme SecureGate flaw hits Australian councils"},
    ],
)
def test_an_event_australia_touches_without_an_advisory_is_not_labelled(changes):
    assert au_desk_label(subject(**changes)) is None


# ─── Pinning ──────────────────────────────────────────────────────────────────────────────────────


def test_a_pinned_record_rebuilds_the_subject_it_came_from():
    s = subject()
    g = pin(s)
    rebuilt = subject_from_record(g.event_id, g.record)
    assert record(rebuilt) == g.record
    assert rebuilt.event.event_id == "evt-2026-000100"
    assert rebuilt.has_official_severity and rebuilt.event.severity is Severity.CRITICAL


def test_an_event_with_no_official_severity_rebuilds_as_unknown():
    s = subject(severity=Severity.HIGH, severity_source=SeveritySource.AI_ESTIMATE)
    g = pin(s)
    rebuilt = subject_from_record(g.event_id, g.record)
    assert record(rebuilt) == g.record and not rebuilt.has_official_severity


def test_the_severity_task_is_shown_neither_the_severity_nor_the_scores():
    s = blinded(subject_from_record("evt-2026-000100", pin(subject()).record))
    rec = record(s)
    assert rec["official_severity"] == "unknown" and not s.has_official_severity
    assert [c["cvss"] for c in rec["cves"]] == [None, None]
    # What the severity is judged from stays.
    assert rec["cves"][0]["kev_listed"] is True and rec["source_text"] == SOURCE_TEXT


def test_the_digest_names_the_exact_set():
    a, b = pin(subject()), pin(subject(event_id="evt-2026-000101"))
    assert digest([a, b]) == digest([b, a])
    assert digest([a, b]) != digest([a])
    changed = pin(subject(title="Acme SecureGate heap overflow, now exploited"))
    assert digest([changed, b]) != digest([a, b])
    assert len(digest([a])) == 12
