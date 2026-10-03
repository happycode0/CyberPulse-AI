"""Writes the pipeline runner needs: new events, merged sources, scores.

Reads live in `worker.db.events`. Every function takes the caller's connection and never
commits: the runner owns the transaction (one per source), so a failure part-way through an
item rolls the whole item back rather than leaving half an event behind.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import Connection, text

from worker.models import Event, NormalisedItem, RelationshipType, TimelineEntry
from worker.pipeline.assemble import EventUpdate
from worker.pipeline.score import ScoredEvent

# Serialises resolve-then-write across concurrent runs (e.g. FAST overlapping NORMAL), so two
# lanes reporting the same story at once cannot both decide it is new. Always taken before
# the event-id lock in `next_event_id`, never after.
_INGEST_LOCK = 726_202_603


def lock_ingest(conn: Connection) -> None:
    """Transaction-scoped: released when the caller's transaction ends."""
    conn.execute(text("select pg_advisory_xact_lock(:k)"), {"k": _INGEST_LOCK})


def known_url_hashes(conn: Connection, url_hashes: Sequence[str]) -> dict[str, str]:
    """`url_hash -> event_id` for hashes already recorded against an event, in one query.

    This is `resolve`'s first rung (an identical canonical URL is a duplicate, whatever its
    age), evaluated in bulk so the steady state, where nearly every feed item was seen last
    run, does not pay a full candidate search per item.
    """
    if not url_hashes:
        return {}
    rows = conn.execute(
        text(
            "select url_hash, min(event_id) from event_sources "
            "where url_hash = any(cast(:hashes as text[])) group by url_hash"
        ),
        {"hashes": list(url_hashes)},
    )
    return {r[0]: r[1] for r in rows}


def touch_last_seen(conn: Connection, event_ids: Iterable[str], seen_at: datetime) -> None:
    """Record that these events were seen again (never moves `last_seen` backwards)."""
    ids = sorted(set(event_ids))
    if not ids:
        return
    conn.execute(
        text(
            "update events set last_seen = greatest(last_seen, :seen), updated_at = now() "
            "where event_id = any(cast(:ids as text[]))"
        ),
        {"seen": seen_at, "ids": ids},
    )


def _insert_source(
    conn: Connection,
    event_id: str,
    item: NormalisedItem,
    *,
    source_id: str,
    evidence_class: str,
    lineage_id: str | None,
    independent: bool,
    payload_hash: str | None,
) -> None:
    conn.execute(
        text(
            "insert into event_sources (event_id, source_id, url, canonical_url, guid, title, "
            "published, fetched_at, evidence_class, lineage_id, independent, url_hash, "
            "title_hash, payload_hash) values (:event_id, :source_id, :url, :canonical_url, "
            ":guid, :title, :published, :fetched_at, :evidence_class, :lineage_id, "
            ":independent, :url_hash, :title_hash, :payload_hash)"
        ),
        {
            "event_id": event_id,
            "source_id": source_id,
            "url": item.url,
            "canonical_url": item.canonical_url,
            "guid": item.guid,
            "title": item.title,
            "published": None if item.published_is_estimated else item.published,
            "fetched_at": item.fetched_at,
            "evidence_class": evidence_class,
            "lineage_id": lineage_id,
            "independent": independent,
            "url_hash": item.url_hash,
            "title_hash": item.title_hash,
            "payload_hash": payload_hash,
        },
    )


def _insert_cves(conn: Connection, event_id: str, cve_ids: Iterable[str]) -> None:
    for cve_id in sorted(set(cve_ids)):
        conn.execute(
            text("insert into cves (cve_id) values (:c) on conflict (cve_id) do nothing"),
            {"c": cve_id},
        )
        conn.execute(
            text(
                "insert into event_cves (event_id, cve_id) values (:e, :c) "
                "on conflict (event_id, cve_id) do nothing"
            ),
            {"e": event_id, "c": cve_id},
        )


def _insert_timeline(conn: Connection, event_id: str, entries: Iterable[TimelineEntry]) -> None:
    for t in entries:
        conn.execute(
            text(
                "insert into event_timeline (event_id, ts, type, summary, sources) "
                "values (:event_id, :ts, :type, :summary, cast(:sources as text[]))"
            ),
            {
                "event_id": event_id,
                "ts": t.timestamp,
                "type": t.type.value,
                "summary": t.summary,
                "sources": t.sources,
            },
        )


def insert_new_event(
    conn: Connection, event: Event, item: NormalisedItem, *, payload_hash: str | None
) -> None:
    """Insert a freshly built event with its single source, CVEs and opening timeline.

    The feed's text goes into `source_summary` as well as `summary`: enrichment replaces the
    second with an original summary and reads the first (migration 006).
    """
    (source,) = event.sources
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, last_material_update, "
            "last_independent_confirmation, status, title, normalised_title, summary, "
            "source_summary, severity, severity_source, au_directly_reported, au_reasons, "
            "categories, pending_enrichment) values (:event_id, :schema_version, "
            ":pipeline_version, :scoring_version, :enrichment_version, :first_seen, :last_seen, "
            ":last_material_update, :last_independent_confirmation, :status, :title, "
            ":normalised_title, :summary, :summary, :severity, :severity_source, "
            ":au_directly_reported, cast(:au_reasons as text[]), cast(:categories as text[]), "
            ":pending_enrichment)"
        ),
        {
            "event_id": event.event_id,
            "schema_version": event.schema_version,
            "pipeline_version": event.pipeline_version,
            "scoring_version": event.scoring_version,
            "enrichment_version": event.enrichment_version,
            "first_seen": event.first_seen,
            "last_seen": event.last_seen,
            "last_material_update": event.last_material_update,
            "last_independent_confirmation": event.last_independent_confirmation,
            "status": event.status.value,
            "title": event.title,
            "normalised_title": item.normalised_title,
            "summary": event.summary,
            "severity": event.severity.value,
            "severity_source": event.severity_source.value,
            "au_directly_reported": event.au.directly_reported_in_au,
            "au_reasons": event.au.reasons,
            "categories": event.categories,
            "pending_enrichment": event.pending_enrichment,
        },
    )
    _insert_source(
        conn,
        event.event_id,
        item,
        source_id=source.source_id,
        evidence_class=source.evidence_class.value,
        lineage_id=source.lineage_id,
        independent=source.independent,
        payload_hash=payload_hash,
    )
    _insert_cves(conn, event.event_id, (c.id for c in event.cves))
    _insert_timeline(conn, event.event_id, event.timeline)


def apply_update(
    conn: Connection, update: EventUpdate, item: NormalisedItem, *, payload_hash: str | None
) -> None:
    """Merge `item` into an existing event."""
    conn.execute(
        text(
            "update events set first_seen = :first_seen, last_seen = :last_seen, "
            "last_material_update = :lmu, last_independent_confirmation = :lic, "
            "status = case when :material and status in ('new', 'archived') then 'developing' "
            "else status end, "
            "title = coalesce(:title, title), "
            "normalised_title = coalesce(:normalised_title, normalised_title), "
            "updated_at = now() where event_id = :event_id"
        ),
        {
            "event_id": update.event_id,
            "first_seen": update.first_seen,
            "last_seen": update.last_seen,
            "lmu": update.last_material_update,
            "lic": update.last_independent_confirmation,
            "material": update.material,
            "title": update.title,
            "normalised_title": item.normalised_title if update.title is not None else None,
        },
    )
    _insert_source(
        conn,
        update.event_id,
        item,
        source_id=update.source.source_id,
        evidence_class=update.source.evidence_class.value,
        lineage_id=update.source.lineage_id,
        independent=update.source.independent,
        payload_hash=payload_hash,
    )
    _insert_cves(conn, update.event_id, update.new_cves)
    _insert_timeline(conn, update.event_id, update.timeline)


def add_relationship(
    conn: Connection, event_id: str, related_event_id: str, kind: RelationshipType
) -> None:
    conn.execute(
        text(
            "insert into event_relationships (event_id, related_event_id, type) "
            "values (:e, :r, :t) on conflict (event_id, related_event_id, type) do nothing"
        ),
        {"e": event_id, "r": related_event_id, "t": kind.value},
    )


def events_needing_score(
    conn: Connection, *, scoring_version: str, touched: Iterable[str], floor: float
) -> list[str]:
    """Ids of events whose stored score may be out of date.

    Prominence only ever decays with time, so an event already scored below `floor` and not
    touched this run cannot have risen and is skipped. Everything else is rescored: decay
    means the stored number goes stale even when nothing new arrived. Archived events are
    left alone.
    """
    rows = conn.execute(
        text(
            "select event_id from events where status <> 'archived' and ("
            "prominence is null or prominence >= :floor or scoring_version <> :version "
            "or event_id = any(cast(:touched as text[]))) order by event_id"
        ),
        {"floor": floor, "version": scoring_version, "touched": sorted(set(touched))},
    )
    return [r[0] for r in rows]


def save_scores(conn: Connection, events: Iterable[ScoredEvent]) -> None:
    for e in events:
        conn.execute(
            text(
                "update events set urgency = :urgency, confidence = :confidence, "
                "novelty = :novelty, prominence = :prominence, scoring_version = :version, "
                "updated_at = now() where event_id = :event_id"
            ),
            {
                "event_id": e.event_id,
                "urgency": e.risk.urgency,
                "confidence": e.risk.confidence,
                "novelty": e.risk.novelty,
                "prominence": e.risk.prominence,
                "version": e.scoring_version,
            },
        )
