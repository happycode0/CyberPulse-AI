"""Advisories and the MITRE catalogues against a real Postgres (migration 007).

A fake connection would let the parts that matter be wrong: the `<> all(...)` that prunes only
on a complete answer, the recheck intervals, the `&&` overlap that picks attack stories, the
backoff in `events_due_for_mitre`, and the check constraints the migration adds.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.ai.tasks import TaskName
from worker.db.advisories import (
    RECHECK_HOURS,
    advisories_for,
    cves_due_for_advisories,
    record_advisories,
    record_advisory_check,
)
from worker.db.enrichment import mark_done, mark_failed
from worker.db.migrate import run_migrations
from worker.db.mitre import (
    AI_SUGGESTED,
    Suggestion,
    current_catalogue,
    events_due_for_mitre,
    load_catalogue,
    loaded_versions,
    record_mitre_inference,
    replace_suggestions,
)
from worker.groundtruth.mitre import Catalogue, Release, Technique
from worker.groundtruth.osv import Advisory, Package
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

NOW = datetime(2026, 10, 3, 2, 25, tzinfo=UTC)
CVE = "CVE-2021-44228"
CVE_2 = "CVE-2024-3400"
GHSA = "GHSA-jfh8-c2jp-5v3q"
EVENT = "evt-2026-000001"
EVENT_2 = "evt-2026-000002"
EVENT_3 = "evt-2026-000003"
V1 = "1"
ATTACK_CATEGORIES = ["vulnerability", "ransomware"]
AI_SUBDOMAINS = ["AI_SECURITY"]


@pytest.fixture
def conn(pg_engine):
    """A connection in a transaction that is always rolled back, starting from empty tables."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        for table in ("events", "cves", "mitre_catalogue", "mitre_dataset_versions"):
            c.execute(text(f"delete from {table}"))
        for cve_id in (CVE, CVE_2):
            c.execute(text("insert into cves (cve_id) values (:c)"), {"c": cve_id})
        try:
            yield c
        finally:
            trans.rollback()


def insert_event(
    conn,
    event_id,
    *,
    pending=False,
    prominence=0.5,
    status="new",
    categories=("vulnerability",),
    ai_subdomain=None,
    first_seen=NOW,
):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, status, title, summary, prominence, "
            "pending_enrichment, categories, ai_subdomain) "
            "values (:id, :schema, :pipeline, :scoring, '1', :first_seen, :first_seen, :status, "
            "'Acme VPN flaw exploited', 'The feed said this.', :prominence, :pending, "
            "cast(:categories as text[]), :subdomain)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "first_seen": first_seen,
            "status": status,
            "prominence": prominence,
            "pending": pending,
            "categories": list(categories),
            "subdomain": ai_subdomain,
        },
    )


# ─── Advisories ───────────────────────────────────────────────────────────────────────────────────


def advisory(advisory_id=GHSA, *, fixed=("2.15.0",), severity="critical", source="ghsa"):
    return Advisory(
        id=advisory_id,
        source=source,
        summary="Remote code injection in Log4j",
        severity=severity,
        reviewed=True,
        packages=(Package("Maven", "org.apache.logging.log4j:log4j-core", fixed),),
        published=NOW - timedelta(days=1000),
        modified=NOW - timedelta(days=10),
        aliases=(CVE,),
    )


def stored(conn, cve_id=CVE):
    return advisories_for(conn, [cve_id]).get(cve_id, [])


def test_advisories_are_written_and_published_github_first(conn):
    change = record_advisories(
        conn, CVE, [advisory(CVE, source="osv", severity=None), advisory()], now=NOW
    )
    assert change.written == (CVE, GHSA) and change.packages_moved == (CVE, GHSA)
    rows = stored(conn)
    assert [r["id"] for r in rows] == [GHSA, CVE]
    assert rows[0]["packages"] == [
        {"ecosystem": "Maven", "name": "org.apache.logging.log4j:log4j-core", "fixed": ["2.15.0"]}
    ]
    assert rows[0]["url"] == f"https://github.com/advisories/{GHSA}"


def test_an_unchanged_advisory_is_not_rewritten(conn):
    record_advisories(conn, CVE, [advisory()], now=NOW)
    again = record_advisories(conn, CVE, [advisory()], now=NOW + timedelta(hours=1))
    assert not again.changed


def test_a_new_fixed_version_is_a_package_move(conn):
    record_advisories(conn, CVE, [advisory()], now=NOW)
    change = record_advisories(conn, CVE, [advisory(fixed=("2.15.0", "2.12.2"))], now=NOW)
    assert change.packages_moved == (GHSA,)
    rating = record_advisories(
        conn, CVE, [advisory(fixed=("2.15.0", "2.12.2"), severity="high")], now=NOW
    )
    assert rating.written == (GHSA,) and rating.packages_moved == ()


def test_only_a_complete_answer_removes_an_advisory(conn):
    record_advisories(conn, CVE, [advisory(), advisory(CVE, source="osv")], now=NOW)
    partial = record_advisories(conn, CVE, [advisory()], now=NOW)
    assert partial.removed == 0 and len(stored(conn)) == 2
    complete = record_advisories(conn, CVE, [advisory()], now=NOW, complete=True)
    assert complete.removed == 1 and [r["id"] for r in stored(conn)] == [GHSA]
    # Complete and empty: the register names nothing now.
    assert record_advisories(conn, CVE, [], now=NOW, complete=True).removed == 1


def test_a_cve_is_asked_again_once_its_answer_is_old_enough(conn):
    assert cves_due_for_advisories(conn, now=NOW, limit=10) == sorted([CVE, CVE_2])
    record_advisory_check(conn, CVE, "found", "1 advisories", now=NOW)
    record_advisory_check(conn, CVE_2, "error", "HTTP 503", now=NOW)
    assert cves_due_for_advisories(conn, now=NOW, limit=10) == []
    later = NOW + timedelta(hours=RECHECK_HOURS["error"], minutes=1)
    assert cves_due_for_advisories(conn, now=later, limit=10) == [CVE_2]
    much_later = NOW + timedelta(hours=RECHECK_HOURS["found"], minutes=1)
    assert sorted(cves_due_for_advisories(conn, now=much_later, limit=10)) == [CVE, CVE_2]


def test_the_tables_reject_what_the_register_cannot_say(conn):
    with pytest.raises(Exception, match="check"), conn.begin_nested():
        record_advisory_check(conn, CVE, "unscored", None, now=NOW)
    with pytest.raises(Exception, match="check"), conn.begin_nested():
        record_advisories(conn, CVE, [advisory(severity="moderate")], now=NOW)


# ─── MITRE catalogues ─────────────────────────────────────────────────────────────────────────────


def catalogue(version="ATT&CK v19.2", released=NOW, extra=()):
    release = Release("enterprise", version, "https://example.test/attack.json", released)
    techniques = (
        Technique("T1190", "Exploit Public-Facing Application", ("initial-access",), None),
        Technique("T1505", "Server Software Component", ("persistence",), None),
        Technique("T1505.003", "Web Shell", ("persistence",), "T1505"),
        *extra,
    )
    return Catalogue(release, techniques)


def test_a_release_is_loaded_whole_once(conn):
    assert load_catalogue(conn, catalogue()) == 3
    assert load_catalogue(conn, catalogue()) == 0
    assert loaded_versions(conn, "enterprise") == {"ATT&CK v19.2"}
    current = current_catalogue(conn, "enterprise")
    assert current is not None and current.version == "ATT&CK v19.2"
    assert current.by_id()["T1505.003"] == Technique(
        "T1505.003", "Web Shell", ("persistence",), "T1505"
    )
    assert current_catalogue(conn, "atlas") is None


def test_the_newest_release_is_current_and_older_ones_stay(conn):
    load_catalogue(conn, catalogue("ATT&CK v19.1", NOW - timedelta(days=90)))
    newer = (Technique("T1486", "Data Encrypted for Impact", ("impact",), None),)
    load_catalogue(conn, catalogue("ATT&CK v19.2", NOW, newer))
    current = current_catalogue(conn, "enterprise")
    assert current.version == "ATT&CK v19.2" and "T1486" in current.by_id()
    assert loaded_versions(conn, "enterprise") == {"ATT&CK v19.1", "ATT&CK v19.2"}


def test_a_matrix_the_migration_does_not_know_is_refused(conn):
    with pytest.raises(Exception, match="check"), conn.begin_nested():
        conn.execute(
            text("insert into mitre_dataset_versions (version, matrix) values ('x', 'mobile')")
        )


# ─── Suggestions ──────────────────────────────────────────────────────────────────────────────────


def due(conn, now=NOW):
    return events_due_for_mitre(
        conn,
        version=V1,
        now=now,
        floor=0.05,
        categories=ATTACK_CATEGORIES,
        subdomains=AI_SUBDOMAINS,
        limit=10,
    )


def test_enriched_attack_stories_are_due_most_prominent_first(conn):
    insert_event(conn, EVENT, prominence=0.3)
    insert_event(conn, EVENT_2, prominence=0.9, categories=["ransomware", "news"])
    insert_event(conn, EVENT_3, prominence=0.6, categories=["policy"], ai_subdomain="AI_SECURITY")
    insert_event(conn, "evt-2026-000004", pending=True)
    insert_event(conn, "evt-2026-000005", categories=["policy"])
    insert_event(conn, "evt-2026-000006", status="archived")
    insert_event(conn, "evt-2026-000007", prominence=0.01)
    assert due(conn) == [EVENT_2, EVENT_3, EVENT]


def test_a_suggestion_done_or_in_backoff_holds_the_event_back(conn):
    insert_event(conn, EVENT)
    insert_event(conn, EVENT_2)
    mark_done(conn, EVENT, TaskName.MITRE, version=V1, model="m", now=NOW)
    retry = mark_failed(conn, EVENT_2, TaskName.MITRE, version=V1, model="m", reason="x", now=NOW)
    assert due(conn) == []
    assert due(conn, now=retry + timedelta(seconds=1)) == [EVENT_2]
    # A new enrichment version asks again.
    assert events_due_for_mitre(
        conn,
        version="2",
        now=NOW,
        floor=0.05,
        categories=ATTACK_CATEGORIES,
        subdomains=AI_SUBDOMAINS,
        limit=10,
    ) == [EVENT, EVENT_2]


def suggestion(tid, name, confidence, version="ATT&CK v19.2"):
    return Suggestion(Technique(tid, name, (), None), version, confidence, ("vendor_acme",))


def published(conn, event_id=EVENT):
    rows = conn.execute(
        text(
            "select technique_id, confidence, confidence_type, dataset_version, evidence "
            "from mitre_techniques where event_id = :e order by technique_id"
        ),
        {"e": event_id},
    )
    return [tuple(r) for r in rows]


def test_suggestions_replace_earlier_ones_and_leave_published_mappings_alone(conn):
    load_catalogue(conn, catalogue())
    insert_event(conn, EVENT)
    conn.execute(
        text(
            "insert into mitre_techniques (event_id, technique_id, name, confidence, "
            "confidence_type, dataset_version, evidence) values (:e, 'T1505.003', 'Web Shell', "
            "1, 'source_reported', 'ATT&CK v19.2', '{cisa}')"
        ),
        {"e": EVENT},
    )
    replace_suggestions(conn, EVENT, [suggestion("T1190", "Exploit Public-Facing App", 0.8)])
    replace_suggestions(conn, EVENT, [suggestion("T1505", "Server Software Component", 0.6)])
    assert published(conn) == [
        ("T1505", 0.6, AI_SUGGESTED, "ATT&CK v19.2", ["vendor_acme"]),
        ("T1505.003", 1.0, "source_reported", "ATT&CK v19.2", ["cisa"]),
    ]
    replace_suggestions(conn, EVENT, [])
    assert [r[0] for r in published(conn)] == ["T1505.003"]


def test_a_suggestion_must_name_a_loaded_release(conn):
    insert_event(conn, EVENT)
    with pytest.raises(Exception, match="foreign key"), conn.begin_nested():
        replace_suggestions(conn, EVENT, [suggestion("T1190", "x", 0.8, "ATT&CK v99")])


def test_the_models_reasons_are_kept_as_ai_inference(conn):
    load_catalogue(conn, catalogue())
    insert_event(conn, EVENT)
    pick = suggestion("T1190", "Exploit Public-Facing Application", 0.8)
    record_mitre_inference(
        conn, EVENT, [(pick, "The VPN portal flaw was the way in.")], model="m/strong", version=V1
    )
    row = conn.execute(
        text("select kind, evidence_class, detail from evidence where event_id = :e"), {"e": EVENT}
    ).one()
    detail = row.detail if isinstance(row.detail, dict) else json.loads(row.detail)
    assert (row.kind, row.evidence_class) == ("ai_mitre", "AI_INFERENCE")
    assert detail == {
        "techniques": [
            {
                "id": "T1190",
                "dataset_version": "ATT&CK v19.2",
                "confidence": 0.8,
                "basis": "The VPN portal flaw was the way in.",
            }
        ],
        "model": "m/strong",
        "enrichment_version": V1,
    }
