"""Source lineage: which of an event's reports come from one origin (PLAN.md §9, Stage 3).

A vendor's release and three rewrites of it are one claim, not four confirmations. Each
report on an event belongs to one lineage, and one report per lineage is independent (the
originator's earliest, else the earliest); `independent_confirmations`
(worker/pipeline/score.py) counts those. A report's lineage is the first of these that
applies:

1. **A relay.** Its headline names an agency that also reported the event ("CISA Adds
   Exploited Cisco Flaw to KEV", "ACSC warns of ..."). It is that agency's lineage. Only
   publishers with `names` in config/sources.yaml are matched, and only agencies have them:
   an agency named in a headline is the one speaking, while "Microsoft" or "Cisco" is as
   often the product. An agency's own report is never a relay, even of another agency (a
   joint advisory is each author's own).
2. **A copy.** Its headline is word for word (as normalised) an earlier report's from another
   publisher: a syndicated wire story or press release. It is that report's lineage. A
   headline shorter than `COPY_MIN_WORDS` ("CVE-2026-1234", "Weekly update") is one two
   outlets can arrive at on their own, so it is never read as a copy.
3. **Its publisher's own.** One organisation's feeds are one voice, so ACSC's alert and its
   news item on the same thing are one lineage (`publisher` in the registry). A source with
   no publisher is its own, its lineage id the source id.

Ingest guesses independence one outlet at a time. The run's correlation pass
(worker/pipeline/run.py `consolidate`) settles it with these rules for every recent event,
before anything is scored. Everything here is pure; worker/db/lineage.py loads and writes.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Self

from worker.models import PublisherConfig, SourceConfig
from worker.pipeline.normalise import normalise_title

COPY_MIN_WORDS = 5


@dataclass(frozen=True)
class Publishers:
    """Who is behind each source, and how headlines name the agencies among them."""

    by_source: Mapping[str, str]
    labels: Mapping[str, str]
    patterns: Mapping[str, re.Pattern[str]]

    @classmethod
    def from_registry(
        cls, sources: Iterable[SourceConfig], publishers: Mapping[str, PublisherConfig]
    ) -> Self:
        sources = list(sources)
        return cls(
            by_source={s.id: s.publisher for s in sources if s.publisher},
            labels={
                **{s.id: s.name for s in sources},
                **{k: p.name for k, p in publishers.items()},
            },
            # Case-sensitive: agencies go by acronyms, and a headline capitalises a name.
            patterns={
                k: re.compile(r"\b(?:" + "|".join(map(re.escape, p.names)) + r")\b")
                for k, p in publishers.items()
                if p.names
            },
        )

    @classmethod
    def none(cls) -> Self:
        """Every source its own publisher, no agency named: lineage by copy alone."""
        return cls({}, {}, {})

    def of(self, source_id: str) -> str:
        return self.by_source.get(source_id, source_id)

    def named_in(self, title: str | None) -> set[str]:
        return {k for k, p in self.patterns.items() if title and p.search(title)}


@dataclass(frozen=True)
class Report:
    """One of an event's reports: an `event_sources` row."""

    id: int
    source_id: str
    title: str | None
    at: datetime


@dataclass(frozen=True)
class Assigned:
    lineage: str
    independent: bool


def assign(reports: Sequence[Report], publishers: Publishers) -> dict[int, Assigned]:
    """Each report's lineage and whether it is its lineage's first, by report id."""
    ordered = sorted(reports, key=lambda r: (r.at, r.id))
    # When each publisher first reported the event: a relay names whichever came first.
    first_at: dict[str, datetime] = {}
    for r in ordered:
        first_at.setdefault(publishers.of(r.source_id), r.at)

    lineage: dict[int, str] = {}
    by_headline: dict[str, Report] = {}
    for r in ordered:
        own = publishers.of(r.source_id)
        relayed = (
            []
            if own in publishers.patterns
            else sorted(
                (p for p in publishers.named_in(r.title) if p in first_at),
                key=lambda p: (first_at[p], p),
            )
        )
        headline = normalise_title(r.title) if r.title else ""
        if len(headline.split()) < COPY_MIN_WORDS:
            headline = ""
        original = by_headline.get(headline) if headline else None
        if relayed:
            lineage[r.id] = relayed[0]
        elif original is not None and publishers.of(original.source_id) != own:
            lineage[r.id] = lineage[original.id]
        else:
            lineage[r.id] = own
        if headline:
            by_headline.setdefault(headline, r)

    # A lineage's independent report is its originator's earliest, else its earliest (a relay
    # can land before the agency's own feed is read).
    first: dict[str, Report] = {}
    for r in ordered:
        line = lineage[r.id]
        if line not in first or (
            publishers.of(r.source_id) == line != publishers.of(first[line].source_id)
        ):
            first[line] = r
    return {r.id: Assigned(lineage[r.id], first[lineage[r.id]] is r) for r in ordered}


def last_independent_confirmation(
    reports: Sequence[Report], assigned: Mapping[int, Assigned]
) -> datetime | None:
    """When the latest independent report arrived, once there are two; else None."""
    times = [r.at for r in reports if assigned[r.id].independent]
    return max(times) if len(times) > 1 else None
