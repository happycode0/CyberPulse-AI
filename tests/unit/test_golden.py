import dataclasses
from datetime import UTC, date, datetime

import pytest

from worker.ai.golden import (
    GOLDEN_SIZE,
    AiCandidate,
    GoldenEvent,
    au_desk_label,
    au_label_counts,
    blinded,
    digest,
    labels_for,
    load_ai_candidates,
    pin,
    stratify,
    subject_from_record,
)
from worker.ai.tasks import SourceFacts, Subject, record
from worker.models import (
    AiSignificance,
    AuRelevance,
    Beat,
    CveRef,
    CvssScore,
    EpssScore,
    Event,
    KevEntry,
    Severity,
    SeveritySource,
)
from worker.pipeline.run import DEFAULT_REGISTRY_PATH
from worker.sources.registry import load_registry

T0 =datetime(2026, 9, 20, 14, 30, tzinfo=UTC)
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


def test_the_au_labels_are_counted_by_answer():
    golden = [GoldenEvent(f"e{i}", {}, {"au_desk": v}) for i, v in enumerate((True, False, None))]
    assert au_label_counts([*golden, GoldenEvent("e9", {}, {})]) == (1, 1)


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


# ─── AI stories the owner labels (worker/ai/golden_ai.yaml) ───────────────────────────────────────


def test_the_ai_stories_come_from_ai_sources_and_each_appears_once():
    candidates = load_ai_candidates()
    assert 8 <= len(candidates) <= 15
    assert len({c.url for c in candidates}) == len(candidates)
    beats = {s.id: s.beat for s in load_registry(DEFAULT_REGISTRY_PATH)}
    assert {beats.get(c.source_id) for c in candidates} <= {Beat.AI, Beat.BOTH}
    # Whatever the owner has done so far, nothing unchecked is used.
    assert all(c.reviewed for c in candidates if c.labelled)


def test_a_story_counts_once_labelled_and_reviewed():
    blank = AiCandidate("https://news.example/a", "ars_ai", "A model launch")
    assert not blank.labelled
    assert not dataclasses.replace(blank, reviewed=True).labelled
    ai = dataclasses.replace(blank, beat=Beat.AI, reviewed=True)
    assert not ai.labelled  # an AI story needs its significance too
    assert dataclasses.replace(ai, ai_significance=AiSignificance.MINOR).labelled
    assert not dataclasses.replace(ai, ai_significance=AiSignificance.MINOR, reviewed=False).labelled
    assert dataclasses.replace(blank, beat=Beat.OTHER, reviewed=True).labelled


def test_significance_belongs_to_the_ai_desk():
    with pytest.raises(ValueError, match="only for the ai or both beat"):
        AiCandidate("https://news.example/a", "ars_ai", "A", Beat.CYBER, AiSignificance.MAJOR)


def test_the_file_is_read_with_blanks_as_none(tmp_path):
    path = tmp_path / "golden_ai.yaml"
    path.write_text(
        "candidates:\n"
        "  - {url: 'https://news.example/a', source_id: ars_ai, headline: A,\n"
        "     labels: {beat: null, ai_significance: null, au_desk: null}, reviewed: false}\n"
        "  - {url: 'https://news.example/b', source_id: ars_ai, headline: B,\n"
        "     labels: {beat: both, ai_significance: major, au_desk: true}, reviewed: true}\n",
        encoding="utf-8",
    )
    a, b = load_ai_candidates(path)
    assert (a.beat, a.ai_significance, a.au_desk, a.reviewed) == (None, None, None, False)
    assert b.labelled and b.labels() == {
        "triage": {"beat": "both", "ai_significance": "major"},
        "au_desk": True,
        "from": "owner",
    }
    path.write_text(
        "candidates:\n  - {url: x, source_id: y, headline: z, labels: {au_desk: maybe}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="au_desk"):
        load_ai_candidates(path)


def test_the_digest_names_the_exact_set():
    a, b = pin(subject()), pin(subject(event_id="evt-2026-000101"))
    assert digest([a, b]) == digest([b, a])
    assert digest([a, b]) != digest([a])
    changed = pin(subject(title="Acme SecureGate heap overflow, now exploited"))
    assert digest([changed, b]) != digest([a, b])
    assert len(digest([a])) == 12
