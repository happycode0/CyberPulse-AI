"""The golden set the gauntlet judges every model on (PLAN.md §7.6).

About thirty events whose expected answers did not come from a model. Each has a severity
from a register (the CNA, CISA's ADP, NVD or the vendor), names a CVE, and has enough of the
feed's own text to work from. They are spread across severities, so a model that calls
everything critical does not score well.

Each is pinned as the record a model is shown (worker/ai/tasks.py `record`), frozen at the time
it was pinned. A later change to the event does not move the set, and the set's digest names
exactly what a result was measured on.

The labels:

- `severity`: the register's. The severity task is shown the record without it, and without
  the CVSS scores it came from (`blinded`), as it is in production, where it runs only for
  events with no official score.
- `triage`: a CVE record is cybersecurity, and is about a vulnerability or a zero-day.
- `au_desk`: True when an Australian advisory source (the ACSC) carried it; False when no
  Australian source did and nothing in it names Australia; None, and not scored, otherwise.

They come from the registers and the source registry, not from a person, so `reviewed` stays
false until someone has checked them, and every proposal says so.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from worker.ai.tasks import SourceFacts, Subject, record
from worker.models import (
    AuRelevance,
    CveRef,
    CvssScore,
    EpssScore,
    Event,
    EventStatus,
    KevEntry,
    Severity,
    SeveritySource,
)

GOLDEN_SIZE = 30
# How many of each severity, in this order. Short strata are made up from the others.
STRATA: tuple[tuple[Severity, int], ...] = (
    (Severity.CRITICAL, 9),
    (Severity.HIGH, 9),
    (Severity.MEDIUM, 9),
    (Severity.LOW, 3),
)
# The registers' severities: anything else is a model's estimate or nothing.
OFFICIAL_SOURCES = (
    SeveritySource.CNA,
    SeveritySource.CISA_ADP,
    SeveritySource.NVD,
    SeveritySource.VENDOR,
)
# Less of the feed's own text than this, and there is not much for a model to work from.
MIN_SOURCE_CHARS = 80
TRIAGE_CATEGORIES = ("vulnerability", "zero-day")


@dataclass(frozen=True)
class GoldenEvent:
    event_id: str
    record: dict[str, Any]
    labels: dict[str, Any]


def stratify(candidates: Sequence[tuple[str, str]], size: int = GOLDEN_SIZE) -> list[str]:
    """Event ids to pin, from (event_id, severity) pairs already in a stable order."""
    chosen: list[str] = []
    for severity, quota in STRATA:
        chosen += [e for e, s in candidates if s == severity.value][:quota]
    taken = set(chosen)
    chosen += [e for e, _ in candidates if e not in taken][: max(0, size - len(chosen))]
    return chosen[:size]


def au_desk_label(subject: Subject) -> bool | None:
    if any(s.region == "au" and s.category == "advisory" for s in subject.sources):
        return True
    australian = subject.event.au.directly_reported_in_au or any(
        s.region == "au" for s in subject.sources
    )
    text = " ".join([subject.event.title, subject.source_text, *subject.headlines]).casefold()
    if not australian and "australia" not in text:
        return False
    return None


def labels_for(subject: Subject) -> dict[str, Any]:
    return {
        "severity": subject.event.severity.value,
        "triage": {"domain": "cybersecurity", "categories": list(TRIAGE_CATEGORIES)},
        "au_desk": au_desk_label(subject),
        "from": "registers",
    }


def pin(subject: Subject) -> GoldenEvent:
    return GoldenEvent(subject.event.event_id, record(subject), labels_for(subject))


def subject_from_record(event_id: str, rec: dict[str, Any]) -> Subject:
    """The subject a pinned record was made from, as far as `record` and the checks need it:
    `record(subject_from_record(id, r)) == r`."""
    first = datetime.combine(date.fromisoformat(rec["first_seen"]), time(), UTC)
    official = Severity(rec["official_severity"])
    cves = [
        CveRef(
            id=c["id"],
            cvss=(
                CvssScore(score=c["cvss"], source=c["cvss_source"] or "unknown")
                if c["cvss"] is not None
                else None
            ),
            epss=EpssScore(score=c["epss"], status="unknown" if c["epss"] is None else "scored"),
            kev=KevEntry(
                listed=c["kev_listed"],
                date_added=date.fromisoformat(c["kev_date_added"]) if c["kev_date_added"] else None,
            ),
        )
        for c in rec["cves"]
    ]
    event = Event(
        event_id=event_id,
        first_seen=first,
        last_seen=first,
        title=rec["title"],
        summary=rec["source_text"] or rec["title"],
        status=EventStatus(rec["status"]),
        severity=official,
        # Which register it was does not reach the record; that one did is what matters.
        severity_source=(
            SeveritySource.UNKNOWN if official is Severity.UNKNOWN else SeveritySource.VENDOR
        ),
        au=AuRelevance(directly_reported_in_au=rec["reported_by_australian_source"]),
        cves=cves,
    )
    return Subject(
        event=event,
        source_text=rec["source_text"],
        headlines=tuple(rec["other_headlines"]),
        sources=tuple(SourceFacts(s["name"], s["region"], s["kind"]) for s in rec["sources"]),
    )


def blinded(subject: Subject) -> Subject:
    """The subject without its official severity or the CVSS scores behind it, which is how the
    severity task sees an event in production."""
    e = subject.event
    event = e.model_copy(
        update={
            "severity": Severity.UNKNOWN,
            "severity_source": SeveritySource.UNKNOWN,
            "cves": [c.model_copy(update={"cvss": None}) for c in e.cves],
        }
    )
    return Subject(event, subject.source_text, subject.headlines, subject.sources)


def digest(golden: Sequence[GoldenEvent]) -> str:
    """Names the set's exact content, so a result is only reused against the same set."""
    body = json.dumps(
        sorted([g.event_id, g.record, g.labels] for g in golden),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()[:12]
