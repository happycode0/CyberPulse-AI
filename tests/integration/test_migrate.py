import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from worker.db.migrate import current_version, run_migrations

EXPECTED_TABLES = {
    "events", "event_sources", "event_timeline", "event_relationships", "claims",
    "evidence", "cves", "cve_scores", "mitre_techniques", "mitre_dataset_versions",
    "organisations", "products", "threat_actors", "threat_actor_aliases", "countries",
    "sectors", "source_registry", "source_health", "source_lineage", "runs", "trends",
    "cost_ledger", "followup_tasks", "incidents", "agent_proposals", "schema_migrations",
}


def insert_event(engine, event_id):
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into events (event_id, first_seen, last_seen, title, summary) "
                "values (:id, now(), now(), 't', 's')"
            ),
            {"id": event_id},
        )


def test_migrations_create_expected_tables(pg_engine):
    run_migrations(pg_engine)
    names = set(inspect(pg_engine).get_table_names())
    assert {"events", "event_sources", "event_timeline", "event_relationships", "claims",
            "evidence", "cves", "cve_scores", "mitre_techniques", "source_registry",
            "source_health", "source_lineage", "runs", "trends", "cost_ledger",
            "followup_tasks", "incidents", "schema_migrations"} <= names


def test_all_plan_tables_present(pg_engine):
    run_migrations(pg_engine)
    assert EXPECTED_TABLES <= set(inspect(pg_engine).get_table_names())


def test_migrations_are_idempotent(pg_engine):
    run_migrations(pg_engine)
    assert run_migrations(pg_engine) == []
    assert current_version(pg_engine) == 1


def test_required_extensions_present(pg_engine):
    run_migrations(pg_engine)
    with pg_engine.connect() as conn:
        exts = {r[0] for r in conn.execute(text("select extname from pg_extension"))}
    assert {"pg_trgm", "vector"} <= exts


def test_event_id_is_unique(pg_engine):
    run_migrations(pg_engine)
    insert_event(pg_engine, "evt-2026-000001")
    with pytest.raises(IntegrityError):
        insert_event(pg_engine, "evt-2026-000001")


def test_url_hash_index_exists(pg_engine):
    run_migrations(pg_engine)
    idx = {i["name"] for i in inspect(pg_engine).get_indexes("event_sources")}
    assert "ix_event_sources_url_hash" in idx
