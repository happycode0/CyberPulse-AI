"""The AU engine's reads and writes (migration 008). The rules are worker/pipeline/au.py's.

The facts are what the sources said, never the model's summary: the title, the feed's own text
(`source_summary`), every source's headline, and who reported it. The model's reading comes
from the `au_model_*` columns the brief task writes.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence

from sqlalchemy import Connection, text

from worker.models import AuRelevance
from worker.pipeline.au import AuFacts, AuSource, ModelReading


def load_au_facts(conn: Connection, event_ids: Sequence[str]) -> dict[str, AuFacts]:
    if not event_ids:
        return {}
    ids = {"ids": list(event_ids)}
    headlines: dict[str, list[str]] = defaultdict(list)
    sources: dict[str, list[AuSource]] = defaultdict(list)
    rows = conn.execute(
        text(
            "select es.event_id, es.title, es.source_id, r.name, r.region from event_sources es "
            "join source_registry r on r.id = es.source_id "
            "where es.event_id = any(cast(:ids as text[])) "
            "order by es.event_id, es.published nulls last, es.id"
        ),
        ids,
    )
    for r in rows:
        if r.title:
            headlines[r.event_id].append(r.title)
        sources[r.event_id].append(AuSource(r.source_id, r.name, r.region))

    out: dict[str, AuFacts] = {}
    rows = conn.execute(
        text(
            "select event_id, title, source_summary, au_model_relevance, au_model_reasons, "
            "au_model_sectors from events where event_id = any(cast(:ids as text[]))"
        ),
        ids,
    )
    for r in rows:
        texts = dict.fromkeys(t for t in (r.title, r.source_summary, *headlines[r.event_id]) if t)
        out[r.event_id] = AuFacts(
            texts=tuple(texts),
            sources=tuple(dict.fromkeys(sources[r.event_id])),
            model=ModelReading(
                r.au_model_relevance, tuple(r.au_model_reasons), tuple(r.au_model_sectors)
            ),
        )
    return out


def save_au(conn: Connection, assessed: Mapping[str, AuRelevance]) -> int:
    """Write each event's AU fields where they changed. Returns how many events changed."""
    changed = 0
    for event_id, au in assessed.items():
        result = conn.execute(
            text(
                "update events set au_relevance = cast(:relevance as double precision), "
                "au_directly_reported = :reported, "
                "au_reasons = cast(:reasons as text[]), au_sectors = cast(:sectors as text[]), "
                "au_soci_asset_classes = cast(:soci as text[]), updated_at = now() "
                "where event_id = :event_id "
                "and (au_relevance is distinct from cast(:relevance as double precision) "
                "or au_directly_reported is distinct from :reported "
                "or au_reasons is distinct from cast(:reasons as text[]) "
                "or au_sectors is distinct from cast(:sectors as text[]) "
                "or au_soci_asset_classes is distinct from cast(:soci as text[]))"
            ),
            {
                "event_id": event_id,
                "relevance": au.relevance,
                "reported": au.directly_reported_in_au,
                "reasons": au.reasons,
                "sectors": au.sectors,
                "soci": au.soci_asset_classes,
            },
        )
        changed += result.rowcount
    return changed
