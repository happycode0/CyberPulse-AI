"""Source health and lifecycle persistence.

`source_registry.lifecycle_state` is owned by the database. The static YAML never carries it
(`load_registry` rejects it), so a `SourceConfig` with a populated state is built by
overlaying `load_lifecycle_states()` on the YAML-loaded config, never by editing the YAML:

    states = load_lifecycle_states(conn)
    source = source.model_copy(update={"lifecycle_state": states.get(source.id)})

Whichever task adds the YAML -> `source_registry` upsert must leave `lifecycle_state` out of
its `ON CONFLICT DO UPDATE` column list, or re-syncing the static config would overwrite
the dynamically tracked state.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import Connection, text

from worker.models import LifecycleState, SourceConfig, SourceHealth
from worker.pipeline.reputation import Corroboration

DEFAULT_HISTORY_LIMIT = 20


def record_health(conn: Connection, health: SourceHealth) -> None:
    conn.execute(
        text(
            "insert into source_health (source_id, checked_at, status, error, "
            "newest_item_age_days, items_fetched, duration_ms) "
            "values (:source_id, :checked_at, :status, :error, :age, :items, :duration)"
        ),
        {
            "source_id": health.source_id,
            "checked_at": health.checked_at,
            "status": health.status.value,
            "error": health.error,
            "age": health.newest_item_age_days,
            "items": health.items_fetched,
            "duration": health.duration_ms,
        },
    )


def load_health_history(
    conn: Connection, source_id: str, limit: int = DEFAULT_HISTORY_LIMIT
) -> list[SourceHealth]:
    """The source's most recent `limit` health records, oldest first."""
    rows = conn.execute(
        text(
            "select source_id, checked_at, status, error, newest_item_age_days, "
            "items_fetched, duration_ms from source_health "
            "where source_id = :source_id order by checked_at desc, id desc limit :limit"
        ),
        {"source_id": source_id, "limit": limit},
    ).mappings()
    return [
        SourceHealth(
            source_id=r["source_id"],
            checked_at=r["checked_at"],
            status=r["status"],
            error=r["error"],
            newest_item_age_days=r["newest_item_age_days"],
            items_fetched=r["items_fetched"],
            duration_ms=r["duration_ms"],
        )
        for r in reversed(list(rows))
    ]


def set_lifecycle_state(conn: Connection, source_id: str, state: LifecycleState) -> None:
    conn.execute(
        text(
            "update source_registry set lifecycle_state = :state, updated_at = now() "
            "where id = :source_id"
        ),
        {"state": state.value, "source_id": source_id},
    )


def load_lifecycle_states(conn: Connection) -> dict[str, LifecycleState]:
    """Lifecycle state per source id, for every registry row that has one set."""
    rows = conn.execute(
        text("select id, lifecycle_state from source_registry where lifecycle_state is not null")
    )
    return {r.id: LifecycleState(r.lifecycle_state) for r in rows}


def upsert_registry(conn: Connection, sources: Sequence[SourceConfig]) -> None:
    """Sync the static YAML registry into `source_registry`.

    `lifecycle_state` is deliberately absent from the update list: it is tracked in the
    database and re-syncing the YAML must never overwrite it.
    """
    for s in sources:
        conn.execute(
            text(
                "insert into source_registry (id, name, type, region, category, source_class, "
                "priority, lane, enabled, url, parser, expected_frequency, notes) "
                "values (:id, :name, :type, :region, :category, :source_class, :priority, "
                ":lane, :enabled, :url, :parser, :expected_frequency, :notes) "
                "on conflict (id) do update set name = excluded.name, type = excluded.type, "
                "region = excluded.region, category = excluded.category, "
                "source_class = excluded.source_class, priority = excluded.priority, "
                "lane = excluded.lane, enabled = excluded.enabled, url = excluded.url, "
                "parser = excluded.parser, expected_frequency = excluded.expected_frequency, "
                "notes = excluded.notes, updated_at = now()"
            ),
            {
                "id": s.id,
                "name": s.name,
                "type": s.type,
                "region": s.region,
                "category": s.category,
                "source_class": s.source_class,
                "priority": s.priority,
                "lane": s.lane.value,
                "enabled": s.enabled,
                "url": s.url,
                "parser": s.parser,
                "expected_frequency": s.expected_frequency,
                "notes": s.notes,
            },
        )


class FetchState(NamedTuple):
    etag: str | None
    last_modified: str | None


def load_fetch_states(conn: Connection) -> dict[str, FetchState]:
    """Stored conditional-request validators, keyed by source id."""
    rows = conn.execute(text("select source_id, etag, last_modified from source_fetch_state"))
    return {r.source_id: FetchState(r.etag, r.last_modified) for r in rows}


def save_fetch_state(conn: Connection, source_id: str, state: FetchState) -> None:
    conn.execute(
        text(
            "insert into source_fetch_state (source_id, etag, last_modified) "
            "values (:source_id, :etag, :last_modified) "
            "on conflict (source_id) do update set etag = excluded.etag, "
            "last_modified = excluded.last_modified, updated_at = now()"
        ),
        {"source_id": source_id, "etag": state.etag, "last_modified": state.last_modified},
    )


class RegistryRow(NamedTuple):
    id: str
    name: str
    region: str
    category: str
    lane: str
    enabled: bool
    url: str = ""
    priority: int = 0
    expected_frequency: str | None = None


def load_registry_rows(conn: Connection) -> list[RegistryRow]:
    """Every registered source, ordered by id, for the public source-health report."""
    rows = conn.execute(
        text(
            "select id, name, region, category, lane, enabled, url, priority, expected_frequency "
            "from source_registry order by id"
        )
    )
    return [RegistryRow(*r) for r in rows]


def load_corroboration(conn: Connection, *, since: datetime) -> dict[str, Corroboration]:
    """Per source, its events first seen since `since` and how many of them another
    independent lineage also reported (worker/pipeline/reputation.py). An event merged into
    another is counted as the one it joined; a source with no events is absent.

    A source's own lineages are every lineage its reports on that event carry, so a copy of its
    own headline, or its second feed, never corroborates it."""
    rows = conn.execute(
        text(
            "with mine as ("
            "select s.source_id, s.event_id, "
            "array_agg(distinct coalesce(s.lineage_id, s.source_id)) as lineages "
            "from event_sources s join events e on e.event_id = s.event_id "
            "where e.first_seen >= :since and e.merged_into is null "
            "group by s.source_id, s.event_id) "
            "select m.source_id, count(*) as events, count(*) filter (where exists ("
            "select 1 from event_sources o where o.event_id = m.event_id and o.independent "
            "and coalesce(o.lineage_id, o.source_id) <> all(m.lineages))) as corroborated "
            "from mine m group by m.source_id"
        ),
        {"since": since},
    )
    return {r.source_id: Corroboration(r.events, r.corroborated) for r in rows}
