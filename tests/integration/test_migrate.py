import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from worker.db.migrate import _migration_files, current_version, run_migrations
from worker.version import (
    ENRICHMENT_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
)

EXPECTED_TABLES = {
    "events", "event_sources", "event_timeline", "event_relationships", "claims",
    "evidence", "cves", "cve_scores", "mitre_techniques", "mitre_dataset_versions",
    "organisations", "products", "threat_actors", "threat_actor_aliases", "countries",
    "sectors", "source_registry", "source_health", "source_lineage", "runs", "trends",
    "cost_ledger", "followup_tasks", "incidents", "agent_proposals", "schema_migrations",
    "job_runs", "incident_verdicts",
}


def insert_event(engine, event_id):
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into events (event_id, schema_version, pipeline_version, "
                "scoring_version, enrichment_version, first_seen, last_seen, title, summary) "
                "values (:id, :schema, :pipeline, :scoring, :enrichment, now(), now(), 't', 's')"
            ),
            {
                "id": event_id,
                "schema": SCHEMA_VERSION,
                "pipeline": PIPELINE_VERSION,
                "scoring": SCORING_VERSION,
                "enrichment": ENRICHMENT_VERSION,
            },
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
    # Read off the directory rather than hardcoded: the claim under test is "a second run applies
    # nothing and the ledger agrees with what is on disk", and a literal here would instead fail every
    # time a migration is added, which says nothing about idempotency. The highest prefix rather than
    # the file count, because that is what `current_version` returns and the two differ the moment a
    # number is ever skipped.
    run_migrations(pg_engine)
    assert run_migrations(pg_engine) == []
    newest = max(int(p.name.split("_")[0]) for p in _migration_files())
    assert current_version(pg_engine) == newest


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


# ─── 016: the AI beat ─────────────────────────────────────────────────────────────────────────────


def _repairs_016() -> list[str]:
    """016's data repairs without its schema change, which has already run by then."""
    [path] = [p for p in _migration_files() if p.name.startswith("016_")]
    out = []
    for chunk in path.read_text(encoding="utf-8").split(";\n"):
        sql = "\n".join(line for line in chunk.splitlines() if not line.lstrip().startswith("--"))
        if sql.strip() and not sql.strip().startswith("alter table"):
            out.append(sql)
    return out


def _old_event(conn, event_id, sources, *, domains=(), subdomain=None, severity=("unknown",) * 2,
               triaged=False):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, title, summary, domains, ai_subdomain, "
            "severity, severity_source) values (:id, :schema, :pipeline, :scoring, :enrichment, "
            "now(), now(), 't', 's', cast(:domains as text[]), :subdomain, :sev, :src)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "enrichment": ENRICHMENT_VERSION,
            "domains": list(domains),
            "subdomain": subdomain,
            "sev": severity[0],
            "src": severity[1],
        },
    )
    for n, source_id in enumerate(sources):
        conn.execute(
            text(
                "insert into event_sources (event_id, source_id, url, evidence_class, url_hash) "
                "values (:e, :s, :url, 'NEWS', :hash)"
            ),
            {"e": event_id, "s": source_id, "url": f"https://example.test/{event_id}/{n}",
             "hash": f"{event_id}-{n}"},
        )
    if triaged:
        conn.execute(
            text(
                "insert into event_enrichment (event_id, task, version, status) "
                "values (:e, 'triage', '1', 'done')"
            ),
            {"e": event_id},
        )


def test_016_seeds_old_events_and_takes_cyber_ratings_off_ai_only_stories(pg_engine):
    run_migrations(pg_engine)
    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("delete from events"))
            for sid in ("openai_news", "embracethered", "cisa"):
                conn.execute(
                    text(
                        "insert into source_registry (id, name, type, region, category, "
                        "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                        "values (:id, :id, 'rss', 'global', 'news', 'NEWS', 1, 'normal', true, "
                        "'https://example.test/feed', 'rss', 'daily') on conflict (id) do nothing"
                    ),
                    {"id": sid},
                )
            _old_event(conn, "evt-2026-000001", ["openai_news"])
            _old_event(conn, "evt-2026-000002", ["openai_news", "cisa"])
            _old_event(conn, "evt-2026-000003", ["embracethered"])
            _old_event(conn, "evt-2026-000004", ["cisa"])
            _old_event(conn, "evt-2026-000005", ["openai_news"], triaged=True)  # found neither
            _old_event(conn, "evt-2026-000006", ["openai_news"], domains=["ai"],
                       subdomain="AI_SECURITY", triaged=True)
            _old_event(conn, "evt-2026-000007", ["openai_news"], domains=["ai"],
                       subdomain="AI_INDUSTRY", severity=("high", "ai_estimate"), triaged=True)
            _old_event(conn, "evt-2026-000008", ["openai_news"], domains=["ai"],
                       subdomain="AI_INDUSTRY", severity=("low", "nvd"), triaged=True)
            for sql in _repairs_016():
                conn.exec_driver_sql(sql)
            rows = {
                r.event_id: (r.domains, r.severity, r.severity_source)
                for r in conn.execute(
                    text("select event_id, domains, severity, severity_source from events")
                )
            }
            unknown = ("unknown", "unknown")
            assert rows == {
                "evt-2026-000001": (["ai"], *unknown),
                "evt-2026-000002": (["cybersecurity", "ai"], *unknown),
                "evt-2026-000003": (["cybersecurity", "ai"], *unknown),
                "evt-2026-000004": (["cybersecurity"], *unknown),
                "evt-2026-000005": ([], *unknown),
                "evt-2026-000006": (["cybersecurity", "ai"], *unknown),
                "evt-2026-000007": (["ai"], *unknown),
                "evt-2026-000008": (["ai"], "low", "nvd"),
            }
        finally:
            trans.rollback()
