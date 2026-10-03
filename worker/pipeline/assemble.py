"""Turn a resolved `NormalisedItem` into a new `Event`, or into an update of an existing one.

Everything here is pure: no database, no clock. The pipeline runner decides *whether* an
item is new, an update or a duplicate (`resolve`); this module decides *what the event
looks like* afterwards.

Timestamps follow one rule: an event's time comes from when its sources *published*, never
from when we fetched them. A feed backfilled with year-old articles must not produce events
that look brand new, so `first_seen` is the earliest published time among the sources and
`last_material_update` starts there too. `last_seen` is the one field that tracks ingestion.

Only a genuinely new fact (a CVE the event did not have) moves `last_material_update`
forward. Another outlet repeating the story adds evidence and corroboration but does not
refresh prominence, which is the scoring engine's contract.
"""

import html
import re
from dataclasses import dataclass, field
from datetime import datetime

from worker.models import (
    AuRelevance,
    CveRef,
    Event,
    EventStatus,
    EvidenceClass,
    MaterialChange,
    NormalisedItem,
    SourceConfig,
    SourceRef,
    TimelineEntry,
)

SUMMARY_MAX_CHARS = 600

# The AU reason a fact gives, not a model: an Australian source reported the event. Enrichment
# keeps it in front of whatever reasons it adds (worker/db/enrichment.py).
AU_SOURCE_REASON = "reported by an Australian source"

# Registry `category` -> how strong a source of that kind is as evidence. Unknown
# categories are COMMUNITY: the least trusted class a real (non-AI) source can have.
EVIDENCE_CLASS_BY_CATEGORY: dict[str, EvidenceClass] = {
    "advisory": EvidenceClass.AUTHORITATIVE,
    "vendor_advisory": EvidenceClass.VENDOR,
    "research": EvidenceClass.SPECIALIST,
    "analysis": EvidenceClass.SPECIALIST,
    "ai_security": EvidenceClass.SPECIALIST,
    "news": EvidenceClass.NEWS,
    "social": EvidenceClass.SOCIAL,
}

_TAG = re.compile(r"<[^>]*>")


def evidence_class_for(source: SourceConfig) -> EvidenceClass:
    return EVIDENCE_CLASS_BY_CATEGORY.get(source.category, EvidenceClass.COMMUNITY)


def clean_summary(raw: str | None, fallback: str) -> str:
    """Plain-text summary: tags stripped, entities decoded, whitespace collapsed, capped.

    Feed descriptions are frequently HTML; the public site must never receive markup.
    """
    text = " ".join(html.unescape(_TAG.sub(" ", raw or "")).split())
    if not text:
        return fallback
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1].rstrip() + "\u2026"
    return text


def item_time(item: NormalisedItem, *, now: datetime) -> datetime:
    """When the item was published (`normalise` backfills fetched_at if it was undated),
    never later than `now`."""
    return min(item.published or item.fetched_at, now)


def source_ref(item: NormalisedItem, source: SourceConfig, *, independent: bool) -> SourceRef:
    return SourceRef(
        source_id=source.id,
        url=item.url,
        # An estimated date is our fetch time, not a publication date: do not present it as one.
        published=None if item.published_is_estimated else item.published,
        evidence_class=evidence_class_for(source),
        lineage_id=None,
        independent=independent,
    )


def build_new_event(
    item: NormalisedItem, source: SourceConfig, event_id: str, *, now: datetime
) -> Event:
    when = item_time(item, now=now)
    is_au = source.region.lower() == "au"
    return Event(
        event_id=event_id,
        first_seen=when,
        last_seen=max(now, when),
        last_material_update=when,
        last_independent_confirmation=None,
        status=EventStatus.NEW,
        title=item.title,
        summary=clean_summary(item.summary, item.title),
        categories=[source.category],
        au=AuRelevance(
            directly_reported_in_au=is_au,
            reasons=[AU_SOURCE_REASON] if is_au else [],
        ),
        cves=[CveRef(id=c) for c in item.cves],
        sources=[source_ref(item, source, independent=True)],
        timeline=[
            TimelineEntry(
                timestamp=when,
                type=MaterialChange.NEW_FACT,
                summary=f"First reported by {source.name}",
                sources=[source.id],
            )
        ],
        # Stage 1 has no model enrichment yet; flag every event so Stage 2 picks it up.
        pending_enrichment=True,
    )


@dataclass(frozen=True)
class EventUpdate:
    """The changes to apply to an existing event when `item` is merged into it."""

    event_id: str
    first_seen: datetime
    last_seen: datetime
    last_material_update: datetime
    last_independent_confirmation: datetime | None
    source: SourceRef
    new_cves: list[str] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)


def plan_update(
    event: Event, item: NormalisedItem, source: SourceConfig, *, now: datetime
) -> EventUpdate:
    when = item_time(item, now=now)
    known_cves = {c.id for c in event.cves}
    new_cves = [c for c in item.cves if c not in known_cves]
    independent = source.id not in {s.source_id for s in event.sources}

    first_seen = min(event.first_seen, when)
    material = event.last_material_update or event.first_seen
    confirmation = event.last_independent_confirmation

    timeline: list[TimelineEntry] = []
    if new_cves:
        material = max(material, when)
        timeline.append(
            TimelineEntry(
                timestamp=when,
                type=MaterialChange.NEW_CVE,
                summary=f"{source.name} added {', '.join(new_cves)}",
                sources=[source.id],
            )
        )
    if independent:
        confirmation = max(confirmation, when) if confirmation else when
        if not new_cves:
            timeline.append(
                TimelineEntry(
                    timestamp=when,
                    type=MaterialChange.NEW_EVIDENCE,
                    summary=f"Also reported by {source.name}",
                    sources=[source.id],
                )
            )
    return EventUpdate(
        event_id=event.event_id,
        first_seen=first_seen,
        last_seen=max(event.last_seen, now),
        last_material_update=max(material, first_seen),
        last_independent_confirmation=confirmation,
        source=source_ref(item, source, independent=independent),
        new_cves=new_cves,
        timeline=timeline,
    )
