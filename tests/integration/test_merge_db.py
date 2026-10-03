"""Consolidation against a real Postgres (migration 009, worker/db/merge.py and lineage.py,
the run's pass).

A fake connection would let the parts that matter be wrong: the conflict handling when a
loser's rows collide with the winner's (a shared article, CVE, claim, technique or link),
the evidence that must follow a duplicate claim rather than cascade away with it, the
`merged_into` constraints, and the 009 title repair agreeing with `normalise_title`.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from worker.db.events import find_candidates, load_event_dates, load_live_events
from worker.db.lineage import refresh_lineage
from worker.db.merge import (
    load_story_records,
    load_token_weights,
    merge_events,
    merged_redirects,
)
from worker.db.migrate import MIGRATIONS_DIR, run_migrations
from worker.models import RawItem
from worker.pipeline.correlate import MergeGroup
from worker.pipeline.lineage import Publishers
from worker.pipeline.normalise import clean_title, normalise, normalise_title
from worker.pipeline.resolve import WEIGHTED_MIN_HEADLINES
from worker.pipeline.run import consolidate
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

NOW = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)
W, L1, L2, OTHER = "evt-2026-000001", "evt-2026-000002", "evt-2026-000003", "evt-2026-000009"


@pytest.fixture(autouse=True)
def clean_db(pg_engine):
    run_migrations(pg_engine)
    with pg_engine.begin() as c:
        for table in (
            "events",
            "source_health",
            "source_fetch_state",
            "source_registry",
            "mitre_dataset_versions",
        ):
            c.execute(text(f"delete from {table}"))
        for sid, category in (("wire", "news"), ("itnews", "news"), ("ics", "advisory")):
            c.execute(
                text(
                    "insert into source_registry (id, name, type, region, category, "
                    "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                    "values (:id, :id, 'rss', 'global', :cat, 'NEWS', 1, 'fast', true, "
                    "'https://example.test/feed', 'rss', 'hourly')"
                ),
                {"id": sid, "cat": category},
            )
        c.execute(text("insert into mitre_dataset_versions (version) values ('v1')"))
    yield
    with pg_engine.begin() as c:
        c.execute(text("delete from events"))


@pytest.fixture
def conn(pg_engine):
    with pg_engine.connect() as c:
        trans = c.begin()
        try:
            yield c
        finally:
            trans.rollback()


def insert_event(
    conn, event_id, title, *, at=NOW, categories=("news",), pending=True, prominence=0.5
):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, last_material_update, status, title, "
            "normalised_title, summary, categories, pending_enrichment, prominence) values "
            "(:id, :schema, :pipeline, :scoring, '1', :at, :at, :at, 'new', :title, :nt, 's', "
            "cast(:cats as text[]), :pending, :prominence)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "at": at,
            "title": title,
            "nt": normalise_title(title),
            "cats": list(categories),
            "pending": pending,
            "prominence": prominence,
        },
    )


def add_source(
    conn,
    event_id,
    source_id,
    url_hash,
    *,
    title="t",
    at=NOW,
    independent=True,
    evidence_class="NEWS",
):
    return conn.execute(
        text(
            "insert into event_sources (event_id, source_id, url, title, published, fetched_at, "
            "evidence_class, independent, url_hash) values (:e, :s, :url, :title, :at, :at, "
            ":cls, :ind, :hash) returning id"
        ),
        {
            "e": event_id,
            "s": source_id,
            "url": f"https://example.test/{url_hash}",
            "title": title,
            "at": at,
            "cls": evidence_class,
            "ind": independent,
            "hash": url_hash,
        },
    ).scalar_one()


def add_cve(conn, event_id, cve):
    conn.execute(text("insert into cves (cve_id) values (:c) on conflict do nothing"), {"c": cve})
    conn.execute(text("insert into event_cves values (:e, :c)"), {"e": event_id, "c": cve})


def add_timeline(conn, event_id, at, kind, summary):
    conn.execute(
        text("insert into event_timeline (event_id, ts, type, summary) values (:e, :t, :k, :s)"),
        {"e": event_id, "t": at, "k": kind, "s": summary},
    )


def add_claim(conn, event_id, claim):
    claim_id = conn.execute(
        text("insert into claims (event_id, text, confidence) values (:e, :t, 0.9) returning id"),
        {"e": event_id, "t": claim},
    ).scalar_one()
    conn.execute(
        text(
            "insert into evidence (event_id, claim_id, kind, evidence_class) "
            "values (:e, :c, 'quote', 'NEWS')"
        ),
        {"e": event_id, "c": claim_id},
    )
    return claim_id


def add_technique(conn, event_id, technique):
    conn.execute(
        text(
            "insert into mitre_techniques (event_id, technique_id, name, confidence, "
            "confidence_type, dataset_version) values (:e, :t, 'n', 0.5, 'ai', 'v1')"
        ),
        {"e": event_id, "t": technique},
    )


def link(conn, a, b):
    conn.execute(
        text(
            "insert into event_relationships (event_id, related_event_id, type) "
            "values (:a, :b, 'related_event')"
        ),
        {"a": a, "b": b},
    )


def rows(conn, sql, **params):
    return conn.execute(text(sql), params).all()


def event(conn, event_id):
    return (
        conn.execute(text("select * from events where event_id = :e"), {"e": event_id})
        .mappings()
        .one()
    )


def test_merging_moves_everything_and_keeps_one_of_each(conn):
    insert_event(conn, W, "Acme gateway flaw", at=NOW, pending=False)
    insert_event(
        conn, L1, "Acme fixes gateway", at=NOW - timedelta(hours=5), categories=("advisory", "news")
    )
    insert_event(conn, L2, "Gateway flaw exploited", at=NOW + timedelta(hours=2))
    insert_event(conn, OTHER, "Unrelated", at=NOW)

    add_source(conn, W, "wire", "h-shared", title="Acme gateway flaw", at=NOW)
    # The same article
    add_source(conn, L1, "wire", "h-shared", title="Acme gateway flaw", at=NOW - timedelta(hours=5))
    add_source(conn, L1, "itnews", "h-l1", title="Acme fixes gateway", at=NOW - timedelta(hours=5))
    # wire again: not independent
    add_source(
        conn, L2, "wire", "h-l2", title="Gateway flaw exploited", at=NOW + timedelta(hours=2)
    )
    add_cve(conn, W, "CVE-2026-1001")
    add_cve(conn, L1, "CVE-2026-1001")
    add_cve(conn, L2, "CVE-2026-1002")
    add_timeline(conn, W, NOW, "NEW_FACT", "First reported by wire")
    add_timeline(conn, L1, NOW - timedelta(hours=5), "NEW_FACT", "First reported by itnews")
    add_timeline(conn, L2, NOW + timedelta(hours=2), "NEW_CVE", "wire added CVE-2026-1002")
    kept_claim = add_claim(conn, W, "Acme patched the gateway")
    add_claim(conn, L1, "acme PATCHED the gateway")  # the same claim
    add_claim(conn, L2, "Attackers used it")
    add_technique(conn, W, "T1190")
    add_technique(conn, L1, "T1190")
    add_technique(conn, L2, "T1078")
    link(conn, L1, W)  # would point at itself: dropped
    link(conn, L2, OTHER)
    link(conn, OTHER, L1)
    # The winner and a loser each have an open check; the winner keeps one open.
    for e in (W, L2):
        conn.execute(
            text("insert into followup_tasks (event_id, kind, due_at) values (:e, 'check', :t)"),
            {"e": e, "t": NOW},
        )
    # An event merged into a loser earlier now points at the winner.
    insert_event(conn, "evt-2026-000004", "Older piece", at=NOW - timedelta(days=1))
    conn.execute(
        text("update events set status = 'archived', merged_into = :l where event_id = :e"),
        {"l": L1, "e": "evt-2026-000004"},
    )

    merge_events(conn, MergeGroup(W, (L1, L2), "Acme gateway flaw exploited", ("cve_set",)))
    assert refresh_lineage(conn, Publishers.none(), since=NOW - timedelta(days=30)) >= {W}

    w = event(conn, W)
    assert w["first_seen"] == NOW - timedelta(hours=5)
    assert w["last_seen"] == NOW + timedelta(hours=2)
    assert w["last_material_update"] == NOW + timedelta(hours=2)
    assert w["categories"] == ["news", "advisory"]
    assert w["title"] == "Acme gateway flaw exploited"
    assert w["normalised_title"] == "acme gateway flaw exploited"

    sources = rows(
        conn,
        "select source_id, url_hash, lineage_id, independent from event_sources "
        "where event_id = :w order by url_hash",
        w=W,
    )
    assert sources == [
        ("itnews", "h-l1", "itnews", True),
        ("wire", "h-l2", "wire", False),
        ("wire", "h-shared", "wire", True),
    ]
    assert w["last_independent_confirmation"] == NOW
    assert rows(conn, "select cve_id from event_cves where event_id = :w order by 1", w=W) == [
        ("CVE-2026-1001",),
        ("CVE-2026-1002",),
    ]
    assert rows(
        conn, "select type, summary from event_timeline where event_id = :w order by ts", w=W
    ) == [
        ("NEW_FACT", "First reported by itnews"),
        ("NEW_EVIDENCE", "Also reported by wire"),
        ("NEW_CVE", "wire added CVE-2026-1002"),
    ]
    claims = rows(conn, "select id, text from claims where event_id = :w order by id", w=W)
    assert [c[1] for c in claims] == ["Acme patched the gateway", "Attackers used it"]
    # The duplicate claim's evidence followed it to the claim kept.
    assert rows(
        conn,
        "select count(*) from evidence where claim_id = :c and event_id = :w",
        c=kept_claim,
        w=W,
    ) == [(2,)]
    assert rows(
        conn, "select technique_id from mitre_techniques where event_id = :w order by 1", w=W
    ) == [("T1078",), ("T1190",)]
    assert rows(
        conn, "select event_id, related_event_id from event_relationships order by 1, 2"
    ) == [(W, OTHER), (OTHER, W)]
    assert rows(conn, "select event_id, status from followup_tasks order by id") == [
        (W, "pending"), (W, "cancelled")
    ]

    for loser in (L1, L2, "evt-2026-000004"):
        row = event(conn, loser)
        assert (row["status"], row["merged_into"]) == ("archived", W)
    for table in ("event_sources", "event_cves", "event_timeline", "claims", "mitre_techniques"):
        assert rows(conn, f"select count(*) from {table} where event_id = any(:l)", l=[L1, L2]) == [
            (0,)
        ]


def test_a_merged_event_is_kept_out_of_resolution_and_the_site(conn):
    insert_event(conn, W, "Acme gateway flaw", at=NOW)
    insert_event(conn, L1, "Acme gateway flaw", at=NOW - timedelta(days=3))
    add_source(conn, W, "wire", "h-w")
    merge_events(conn, MergeGroup(W, (L1,), None, ("title_hash",)))
    conn.execute(text("update events set first_seen = :t where event_id = :w"), {"t": NOW, "w": W})

    item = normalise(
        RawItem(
            source_id="itnews",
            url="https://example.test/new",
            title="Acme gateway flaw",
            published=NOW,
            fetched_at=NOW,
            payload_hash="p",
        ),
        now=NOW,
    )
    assert [c.event_id for c in find_candidates(conn, item)] == [W]
    assert [e.event_id for e in load_live_events(conn, min_prominence=0.05, limit=10)] == [W]
    assert load_event_dates(conn) == [NOW.date()]
    assert merged_redirects(conn, since=NOW - timedelta(days=90)) == {L1: W}
    assert merged_redirects(conn, since=NOW) == {}
    assert [r.event_id for r in load_story_records(conn, since=NOW - timedelta(days=30))] == [W]


def set_beat(conn, event_id, domains, *, subdomain=None, significance=None, triaged=True,
             severity=("unknown", "unknown")):
    conn.execute(
        text(
            "update events set domains = cast(:d as text[]), ai_subdomain = :sub, "
            "ai_significance = :sig, severity = :sev, severity_source = :src where event_id = :e"
        ),
        {"e": event_id, "d": list(domains), "sub": subdomain, "sig": significance,
         "sev": severity[0], "src": severity[1]},
    )
    if triaged:
        conn.execute(
            text(
                "insert into event_enrichment (event_id, task, version, status) "
                "values (:e, 'triage', '1', 'done')"
            ),
            {"e": event_id},
        )


def test_a_merge_keeps_the_domains_of_every_story_it_joins(conn):
    for e in (W, L1, L2):
        insert_event(conn, e, "OpenAI model jailbroken")
    set_beat(conn, W, ["cybersecurity"], severity=("high", "ai_estimate"))
    set_beat(conn, L1, ["ai"], subdomain="AI_INDUSTRY", significance="notable")
    set_beat(conn, L2, ["ai"], subdomain="AI_SECURITY", significance="major")
    merge_events(conn, MergeGroup(W, (L1, L2), None, ("title_hash",)))
    w = event(conn, W)
    assert w["domains"] == ["cybersecurity", "ai"]
    assert (w["ai_subdomain"], w["ai_significance"]) == ("AI_INDUSTRY", "major")
    # Still on the cyber desk, so its severity estimate stands.
    assert (w["severity"], w["severity_source"]) == ("high", "ai_estimate")


def test_a_seed_gives_way_to_triage_and_an_ai_only_story_loses_its_cyber_rating(conn):
    for e in (W, L1):
        insert_event(conn, e, "Lab ships a new model")
    set_beat(conn, W, ["cybersecurity"], triaged=False, severity=("medium", "ai_estimate"))
    set_beat(conn, L1, ["ai"], subdomain="AI_INDUSTRY", significance="minor")
    merge_events(conn, MergeGroup(W, (L1,), None, ("title_hash",)))
    w = event(conn, W)
    assert (w["domains"], w["ai_significance"]) == (["ai"], "minor")
    assert (w["severity"], w["severity_source"]) == ("unknown", "unknown")


def test_merged_into_must_be_archived_and_another_event(conn):
    insert_event(conn, W, "a")
    insert_event(conn, L1, "b")
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(
            text("update events set merged_into = :w where event_id = :l"), {"w": W, "l": L1}
        )
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(
            text("update events set status = 'archived', merged_into = :w where event_id = :w"),
            {"w": W},
        )


def test_story_records_carry_the_register_and_every_headline(conn):
    insert_event(conn, W, "Siemens SCALANCE advisory", pending=False, prominence=0.3)
    add_source(
        conn,
        W,
        "ics",
        "h1",
        title="Siemens SCALANCE advisory",
        evidence_class="AUTHORITATIVE",
        at=NOW - timedelta(hours=1),
    )
    add_source(conn, W, "wire", "h2", title="Siemens switches flaw")
    add_cve(conn, W, "CVE-2026-2001")
    (r,) = load_story_records(conn, since=NOW - timedelta(days=30))
    assert r.enriched and r.prominence == 0.3
    assert r.keys.registers == frozenset({"ics"})
    assert r.keys.outlets == frozenset({"ics", "wire"})
    assert r.keys.cves == frozenset({"CVE-2026-2001"})
    assert r.keys.anchor == NOW - timedelta(hours=1)
    assert {"scalance", "switches"} <= r.keys.tokens
    # `created_at` is the database's clock, not NOW: take every headline.
    weights = load_token_weights(conn, since=datetime(2000, 1, 1, tzinfo=UTC))
    assert weights.weight("siemens") < weights.weight("switches")


def test_the_009_title_repair_agrees_with_normalisation(conn):
    raw = '<a href="https://dta.example/x">Digital ID &amp; you:  a <b>guide</b>&#39;s view</a>'
    insert_event(conn, W, raw)
    add_source(conn, W, "wire", "h1", title=raw)
    add_source(conn, W, "wire", "h2", title="Plain headline & more")
    sql = (MIGRATIONS_DIR / "009_merged_into.sql").read_text()
    repair = sql[sql.index("create or replace function pg_temp.clean_title") :]
    conn.execution_options(no_parameters=True).exec_driver_sql(repair)

    expected = clean_title(raw)
    assert expected == "Digital ID & you: a guide 's view"
    assert rows(conn, "select title, title_hash from event_sources where url_hash = 'h1'") == [
        (expected, hashlib.sha256(normalise_title(expected).encode()).hexdigest())
    ]
    e = event(conn, W)
    assert (e["title"], e["normalised_title"]) == (expected, normalise_title(expected))
    # A headline without markup is left exactly as it was.
    assert rows(conn, "select title from event_sources where url_hash = 'h2'") == [
        ("Plain headline & more",)
    ]


def test_the_runs_pass_merges_a_split_story_and_reports_it(pg_engine):
    with pg_engine.begin() as c:
        insert_event(c, W, "KillSec claims Medibank ransomware attack", pending=False)
        add_source(c, W, "wire", "h1", title="KillSec claims Medibank ransomware attack")
        insert_event(c, L1, "Medibank confirms KillSec breach", at=NOW + timedelta(hours=20))
        add_source(
            c,
            L1,
            "itnews",
            "h2",
            title="Medibank confirms KillSec breach",
            at=NOW + timedelta(hours=20),
        )
        # Older headlines, outside the pass's 30 days but inside the word weights' 180, so
        # the common words weigh little (and there are enough to weigh words by at all).
        for n in range(WEIGHTED_MIN_HEADLINES):
            old = f"evt-2026-{100 + n:06d}"
            insert_event(c, old, "Ransomware gang claims attack", at=NOW - timedelta(days=40 + n))
            add_source(
                c,
                old,
                "wire",
                f"bg{n}",
                title="Ransomware gang claims attack on firm",
                at=NOW - timedelta(days=40 + n),
            )

    done = consolidate(pg_engine, now=NOW + timedelta(days=1), publishers=Publishers.none())
    assert done.errors == []
    assert done.archived == 1
    assert W in done.touched
    with pg_engine.connect() as c:
        assert event(c, L1)["merged_into"] == W
        assert rows(c, "select count(*) from event_sources where event_id = :w", w=W) == [(2,)]
        # Another outlet's report brought no new CVE: not a material update.
        assert event(c, W)["last_material_update"] == NOW

    again = consolidate(pg_engine, now=NOW + timedelta(days=1), publishers=Publishers.none())
    assert (again.archived, again.errors) == (0, [])


def test_the_lineage_pass_counts_one_publisher_once_and_settles(conn):
    """wire and itnews are one publisher here; ics speaks for itself."""
    publishers = Publishers({"wire": "group", "itnews": "group"}, {"group": "Wire Group"}, {})
    insert_event(conn, W, "Acme gateway flaw")
    add_source(conn, W, "ics", "h1", title="Acme advisory", at=NOW - timedelta(hours=2))
    add_source(conn, W, "wire", "h2", title="Acme gateway flaw", at=NOW - timedelta(hours=1))
    add_source(conn, W, "itnews", "h3", title="Acme flaw exploited", at=NOW)
    # Ingest's guess: one confirmation per outlet.
    conn.execute(
        text("update events set last_independent_confirmation = :t where event_id = :w"),
        {"t": NOW, "w": W},
    )
    insert_event(conn, OTHER, "Unrelated", at=NOW - timedelta(days=40))
    add_source(conn, OTHER, "wire", "h9", at=NOW - timedelta(days=40), independent=False)

    since = NOW - timedelta(days=30)
    assert refresh_lineage(conn, publishers, since=since) == {W}
    assert rows(
        conn,
        "select source_id, lineage_id, independent from event_sources where event_id = :w "
        "order by id",
        w=W,
    ) == [("ics", "ics", True), ("wire", "group", True), ("itnews", "group", False)]
    assert event(conn, W)["last_independent_confirmation"] == NOW - timedelta(hours=1)
    assert rows(
        conn,
        "select lineage_id, description from source_lineage where lineage_id = any(:l) order by 1",
        l=["group", "ics"],
    ) == [("group", "Wire Group"), ("ics", None)]
    # Outside the window: left as it was.
    assert rows(
        conn, "select lineage_id, independent from event_sources where url_hash = 'h9'"
    ) == [(None, False)]
    assert refresh_lineage(conn, publishers, since=since) == set()

    # One voice left: no independent confirmation at all.
    conn.execute(text("delete from event_sources where url_hash = 'h1'"))
    assert refresh_lineage(conn, publishers, since=since) == {W}
    assert event(conn, W)["last_independent_confirmation"] is None
