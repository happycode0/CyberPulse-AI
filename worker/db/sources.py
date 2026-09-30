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

from sqlalchemy import Connection, text

from worker.models import LifecycleState, SourceHealth

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
