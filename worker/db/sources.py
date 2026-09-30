"""Source health and lifecycle persistence."""

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
