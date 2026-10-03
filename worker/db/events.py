"""Event reads and id allocation.

Writes of new events and sources belong to the pipeline runner; this module owns the
queries the resolver and the publisher share.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from sqlalchemy import Connection, text

from worker.db.advisories import advisories_for
from worker.models import (
    Claim,
    CveRef,
    CvssScore,
    EpssScore,
    Event,
    KevEntry,
    MitreTechnique,
    NormalisedItem,
    Relationship,
    SourceRef,
    TimelineEntry,
)
from worker.pipeline.resolve import EventCandidate

# Fuzzy candidate signals (title, CVE, trigram) only consider events active this recently.
CANDIDATE_LOOKBACK_DAYS = 30
TRIGRAM_THRESHOLD = 0.4
CANDIDATE_LIMIT = 200

# First key of the two-int advisory lock that serialises event-id allocation per year.
_EVENT_ID_LOCK = 726_202_602
_EVENT_ID_PREFIX_LEN = len("evt-2026-")
_MAX_SEQUENCE = 999_999


def next_event_id(conn: Connection, year: int) -> str:
    """Return the next free `evt-<year>-<nnnnnn>` id.

    Takes a transaction-scoped advisory lock for the year and derives the number from the
    events already stored, so concurrent allocators are serialised until the caller's
    transaction ends. The caller must insert the event in that same transaction, and the
    transaction must be READ COMMITTED (the default) so the max is read after the lock is
    granted. Because the number comes from stored ids, a rolled-back allocation leaves no
    gap and manually inserted ids are respected.
    """
    if not 0 <= year <= 9999:
        raise ValueError(f"year must have at most four digits: {year}")
    conn.execute(text("select pg_advisory_xact_lock(:k, :y)"), {"k": _EVENT_ID_LOCK, "y": year})
    last = conn.execute(
        text(
            "select max(substr(event_id, :start)::int) from events "
            "where event_id like :pattern"
        ),
        {"start": _EVENT_ID_PREFIX_LEN + 1, "pattern": f"evt-{year:04d}-%"},
    ).scalar_one()
    seq = (last or 0) + 1
    if seq > _MAX_SEQUENCE:
        raise OverflowError(f"event ids for {year} are exhausted")
    return f"evt-{year:04d}-{seq:06d}"


def _url_variants(canonical: str) -> list[str]:
    """The canonical URL with http/https and with/without `www.` (matches the resolver's
    `canonical_url` rung)."""
    bare = canonical.split("://", 1)[-1].removeprefix("www.")
    return [f"{scheme}://{www}{bare}" for scheme in ("http", "https") for www in ("", "www.")]


def find_candidates(conn: Connection, item: NormalisedItem) -> list[Event]:
    """Events that might be the same story as `item`, hydrated for `resolve`.

    Identity signals (URL hash / canonical URL, and GUID within the same source) match
    events of any age: the same article showing up again must never become a second event
    just because the first sighting is old. Fuzzy signals (identical normalised title,
    shared CVE, trigram title similarity above 0.4 via the pg_trgm GIN index) are limited
    to events whose `last_seen` is within 30 days of the item. Deciding what actually
    matches is `resolve`'s job; this only has to be a superset. An event merged into another
    is never a candidate: its sources now belong to the event it joined.
    """
    when = item.published or item.fetched_at
    params: dict[str, Any] = {
        "url_hash": item.url_hash,
        "urls": _url_variants(item.canonical_url),
        "title_hash": item.title_hash,
        "nt": item.normalised_title,
        "cves": item.cves,
        "since": when,
        "lookback": f"{CANDIDATE_LOOKBACK_DAYS} days",
        "threshold": TRIGRAM_THRESHOLD,
        "limit": CANDIDATE_LIMIT,
    }
    guid_clause = ""
    if item.guid is not None:
        guid_clause = "or (es.guid = :guid and es.source_id = :source_id)"
        params.update(guid=item.guid, source_id=item.source_id)

    sql = f"""
        select e.event_id
        from events e
        where e.merged_into is null and (exists (
                select 1 from event_sources es
                where es.event_id = e.event_id
                  and (es.url_hash = :url_hash
                       or es.canonical_url = any(cast(:urls as text[]))
                       {guid_clause})
              )
           or (
                e.last_seen >= cast(:since as timestamptz) - cast(:lookback as interval)
                and (
                    e.normalised_title = :nt
                    or exists (
                        select 1 from event_sources es
                        where es.event_id = e.event_id and es.title_hash = :title_hash
                    )
                    or exists (
                        select 1 from event_cves ec
                        where ec.event_id = e.event_id
                          and ec.cve_id = any(cast(:cves as text[]))
                    )
                    or (e.normalised_title % :nt
                        and similarity(e.normalised_title, :nt) > :threshold)
                )
              ))
        order by e.last_seen desc, e.event_id
        limit :limit
    """
    ids = [r[0] for r in conn.execute(text(sql), params)]
    return _hydrate(conn, ids, candidates=True)


def load_live_events(conn: Connection, *, min_prominence: float, limit: int) -> list[Event]:
    """Events with `prominence > min_prominence`, most prominent first.

    The comparison is strict (the publisher's rule is "prominence > 0.05") and unscored
    events (NULL prominence) are excluded, and so are events merged into another (their score
    is left as it was). Status is not filtered otherwise: prominence already decays for stale
    events, and the publisher decides what else to hide.
    """
    ids = [
        r[0]
        for r in conn.execute(
            text(
                "select event_id from events where prominence > :min and merged_into is null "
                "order by prominence desc, last_material_update desc nulls last, event_id "
                "limit :limit"
            ),
            {"min": min_prominence, "limit": limit},
        )
    ]
    return _hydrate(conn, ids, candidates=False)


def load_events(conn: Connection, event_ids: Sequence[str]) -> list[Event]:
    """The given events, fully hydrated, in `event_ids` order. Unknown ids are an error."""
    return _hydrate(conn, event_ids, candidates=False)


def load_event_dates(conn: Connection) -> list[date]:
    """Distinct UTC dates on which events were first seen, newest first."""
    rows = conn.execute(
        text(
            "select distinct (first_seen at time zone 'UTC')::date as d "
            "from events where merged_into is null order by d desc"
        )
    )
    return [r[0] for r in rows]


def _hydrate(conn: Connection, event_ids: Sequence[str], *, candidates: bool) -> list[Event]:
    """Load full events (in `event_ids` order), batching one query per child table."""
    if not event_ids:
        return []
    p = {"ids": list(event_ids)}
    any_ids = "event_id = any(cast(:ids as text[]))"

    def rows(sql: str, params: dict[str, Any] = p) -> list[Any]:
        return list(conn.execute(text(sql), params).mappings())

    event_rows = {r["event_id"]: r for r in rows(f"select * from events where {any_ids}")}

    def group(sql: str) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = defaultdict(list)
        for r in rows(sql):
            out[r["event_id"]].append(r)
        return out

    sources = group(
        f"select * from event_sources where {any_ids} order by published nulls last, id"
    )
    claims = group(f"select * from claims where {any_ids} order by id")
    timeline = group(f"select * from event_timeline where {any_ids} order by ts, id")
    relationships = group(
        f"select * from event_relationships where {any_ids} order by related_event_id, type"
    )
    techniques = group(f"select * from mitre_techniques where {any_ids} order by technique_id")
    event_cves = group(
        "select ec.event_id, c.* from event_cves ec join cves c using (cve_id) "
        f"where ec.{any_ids} order by ec.cve_id"
    )
    cve_refs = _cve_refs(conn, {r["cve_id"] for rs in event_cves.values() for r in rs}, event_cves)

    out: list[Event] = []
    for event_id in event_ids:
        r = event_rows[event_id]
        fields: dict[str, Any] = dict(
            event_id=event_id,
            schema_version=r["schema_version"],
            pipeline_version=r["pipeline_version"],
            scoring_version=r["scoring_version"],
            enrichment_version=r["enrichment_version"],
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
            last_material_update=r["last_material_update"],
            last_independent_confirmation=r["last_independent_confirmation"],
            status=r["status"],
            title=r["title"],
            summary=r["summary"],
            why_it_matters=r["why_it_matters"],
            domains=r["domains"],
            categories=r["categories"],
            ai_subdomain=r["ai_subdomain"],
            severity=r["severity"],
            severity_source=r["severity_source"],
            risk={k: r[k] for k in ("urgency", "confidence", "novelty", "prominence")},
            au={
                "relevance": r["au_relevance"],
                "directly_reported_in_au": r["au_directly_reported"],
                "reasons": r["au_reasons"],
                "sectors": r["au_sectors"],
                "soci_asset_classes": r["au_soci_asset_classes"],
            },
            entities={
                "actors": r["entity_actors"],
                "organisations": r["entity_organisations"],
                "products": r["entity_products"],
                "countries": r["entity_countries"],
                "industries": r["entity_industries"],
            },
            cves=cve_refs.get(event_id, []),
            mitre_techniques=[
                MitreTechnique(
                    id=t["technique_id"],
                    name=t["name"],
                    confidence=t["confidence"],
                    confidence_type=t["confidence_type"],
                    dataset_version=t["dataset_version"],
                    evidence=t["evidence"],
                )
                for t in techniques[event_id]
            ],
            claims=[
                Claim(text=c["text"], confidence=c["confidence"], evidence=c["evidence"])
                for c in claims[event_id]
            ],
            sources=[
                SourceRef(
                    source_id=s["source_id"],
                    url=s["url"],
                    published=s["published"],
                    evidence_class=s["evidence_class"],
                    lineage_id=s["lineage_id"],
                    independent=s["independent"],
                )
                for s in sources[event_id]
            ],
            timeline=[
                TimelineEntry(
                    timestamp=t["ts"], type=t["type"], summary=t["summary"], sources=t["sources"]
                )
                for t in timeline[event_id]
            ],
            relationships=[
                Relationship(type=x["type"], event_id=x["related_event_id"])
                for x in relationships[event_id]
            ],
            tags=r["tags"],
            pending_enrichment=r["pending_enrichment"],
        )
        if not candidates:
            out.append(Event(**fields))
            continue
        src = sources[event_id]
        titles = [s["title"] for s in src if s["title"]]
        if r["normalised_title"]:
            titles.append(r["normalised_title"])
        out.append(
            EventCandidate(
                **fields,
                source_url_hashes=frozenset(s["url_hash"] for s in src),
                source_canonical_urls=frozenset(
                    s["canonical_url"] for s in src if s["canonical_url"]
                ),
                source_guids=frozenset((s["source_id"], s["guid"]) for s in src if s["guid"]),
                source_titles=tuple(titles),
            )
        )
    return out


def _cve_refs(
    conn: Connection, cve_ids: Iterable[str], event_cves: dict[str, list[Any]]
) -> dict[str, list[CveRef]]:
    """Build `CveRef`s from the CVE rows plus the latest CVSS / EPSS observation of each.

    A CVE with no recorded score keeps `cvss=None` / `epss.status="unknown"`; nothing is
    ever coerced to zero.
    """
    ids = sorted(set(cve_ids))
    latest: dict[tuple[str, str], Any] = {}
    if ids:
        scores = conn.execute(
            text(
                "select distinct on (cve_id, kind) cve_id, kind, score, vector, source, status "
                "from cve_scores where cve_id = any(cast(:ids as text[])) "
                "order by cve_id, kind, observed_at desc, id desc"
            ),
            {"ids": ids},
        ).mappings()
        latest = {(s["cve_id"], s["kind"]): s for s in scores}
    advisories = advisories_for(conn, ids)

    out: dict[str, list[CveRef]] = {}
    for event_id, rs in event_cves.items():
        refs = []
        for c in rs:
            cvss = latest.get((c["cve_id"], "cvss"))
            epss = latest.get((c["cve_id"], "epss"))
            refs.append(
                CveRef(
                    id=c["cve_id"],
                    cvss=(
                        CvssScore(
                            score=cvss["score"],
                            vector=cvss["vector"],
                            source=cvss["source"] or "unknown",
                        )
                        if cvss and cvss["score"] is not None
                        else None
                    ),
                    epss=(
                        EpssScore(score=epss["score"], status=epss["status"])
                        if epss
                        else EpssScore()
                    ),
                    kev=KevEntry(
                        listed=c["kev_listed"],
                        date_added=c["kev_date_added"],
                        due_date=c["kev_due_date"],
                    ),
                    advisories=advisories.get(c["cve_id"], []),
                )
            )
        out[event_id] = refs
    return out
