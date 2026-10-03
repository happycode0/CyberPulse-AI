"""RIPPERDOC's rows against a real Postgres (migration 014, worker/db/scout.py): the catalogue and
its withdrawals, the scans, which events may be pinned, the gauntlet's spend and reuse, the
proposal that is not raised twice, and what GET /ops/models reads."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from worker.ai.catalogue import Change, parse_listing
from worker.ai.gauntlet import Attempt, measure
from worker.ai.golden import GoldenEvent
from worker.ai.ladder import Tier
from worker.db import scout as db
from worker.db.digest import month_bounds
from worker.db.ledger import LedgerEntry, record
from worker.db.migrate import run_migrations
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION, UNENRICHED

NOW = datetime(2026, 10, 4, 3, 40, tzinfo=UTC)
TABLES = (
    "model_changes",
    "model_scans",
    "model_catalogue",
    "golden_events",
    "gauntlet_runs",
    "agent_proposals",
    "cost_ledger",
    "event_cves",
    "events",
)
LONG_TEXT = (
    "A heap overflow in the Acme SecureGate management interface lets an unauthenticated "
    "attacker run code as root."
)


@pytest.fixture
def conn(pg_engine):
    """A connection in a transaction that is always rolled back, with RIPPERDOC's tables empty."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        for table in TABLES:
            c.execute(text(f"delete from {table}"))
        try:
            yield c
        finally:
            trans.rollback()


def entry(slug, prompt="0.0000001", completion="0.0000004", **changes):
    return {
        "id": slug,
        "name": f"Vendor: {slug}",
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["tools", "structured_outputs"],
        **changes,
    }


def listing(*entries):
    return parse_listing({"data": list(entries)})


def changes(conn):
    return [
        (r.slug, r.kind, r.detail)
        for r in conn.execute(text("select slug, kind, detail from model_changes order by id"))
    ]


# ─── The catalogue ────────────────────────────────────────────────────────────────────────────────


def test_the_catalogue_keeps_every_model_and_marks_the_ones_withdrawn(conn):
    db.store_listing(conn, listing(entry("a/one"), entry("b/two")), [], NOW)
    later = NOW + timedelta(days=1)
    db.store_listing(
        conn,
        listing(entry("a/one", completion="0.0000006", expiration_date="2027-01-31")),
        [Change("b/two", "withdrawn", {})],
        later,
    )

    known = db.load_catalogue(conn)
    assert set(known) == {"a/one", "b/two"}
    assert known["a/one"].completion_per_mtok == Decimal("0.6")
    assert known["a/one"].expiration == date(2027, 1, 31) and not known["a/one"].withdrawn
    assert known["b/two"].withdrawn
    rows = {r.slug: r for r in db.load_listed(conn)}
    assert rows["a/one"].first_seen == NOW and rows["a/one"].capable
    assert changes(conn) == [("b/two", "withdrawn", {})]

    db.store_listing(conn, listing(entry("a/one"), entry("b/two")), [], later + timedelta(days=1))
    assert not db.load_catalogue(conn)["b/two"].withdrawn


def test_a_change_of_a_kind_the_table_does_not_know_is_refused(conn):
    db.insert_changes(conn, [], NOW)  # nothing to write is not an error
    with pytest.raises(IntegrityError), conn.begin_nested():
        db.insert_changes(conn, [Change("a/one", "renamed", {})], NOW)


# ─── Scans ────────────────────────────────────────────────────────────────────────────────────────


def test_the_last_ladder_is_from_the_newest_scan_that_checked_it(conn):
    assert db.last_scan_ladder(conn) is None
    ladder = {"tier1_cheap": {"configured": ["a/one"], "effective": [], "dropped": []}}
    db.record_scan(conn, ts=NOW, models_listed=2, changes=0, ladder=ladder, error=None)
    db.record_scan(
        conn,
        ts=NOW + timedelta(days=1),
        models_listed=None,
        changes=0,
        ladder=None,
        error="x" * 5000,
    )
    assert db.last_scan_ladder(conn) == ladder
    error = conn.execute(text("select error from model_scans where ladder is null")).scalar_one()
    assert len(error) < 5000 and error.endswith("...")


# ─── The golden set ───────────────────────────────────────────────────────────────────────────────


def insert_event(
    conn, n, *, severity="critical", source="cna", summary=LONG_TEXT, cve=True, merged_into=None
):
    event_id = f"evt-2026-{n:06d}"
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, status, title, summary, severity, "
            "severity_source, merged_into) values (:id, :schema, :pipeline, :scoring, "
            ":enrichment, :now, :now, :status, 'Acme SecureGate flaw', :summary, :severity, "
            ":source, :merged)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "enrichment": UNENRICHED,
            "now": NOW,
            "status": "archived" if merged_into else "new",
            "summary": summary,
            "severity": severity,
            "source": source,
            "merged": merged_into,
        },
    )
    if cve:
        cve_id = f"CVE-2026-{4000 + n}"
        conn.execute(
            text("insert into cves (cve_id) values (:c) on conflict do nothing"), {"c": cve_id}
        )
        conn.execute(
            text("insert into event_cves (event_id, cve_id) values (:e, :c)"),
            {"e": event_id, "c": cve_id},
        )
    return event_id


def test_only_events_with_an_official_severity_a_cve_and_enough_text_may_be_pinned(conn):
    good = insert_event(conn, 1)
    nvd = insert_event(conn, 2, severity="medium", source="nvd")
    insert_event(conn, 3, source="ai_estimate")
    insert_event(conn, 4, severity="unknown")
    insert_event(conn, 5, cve=False)
    insert_event(conn, 6, summary="Too short.")
    insert_event(conn, 7, merged_into=good)
    assert sorted(db.golden_candidates(conn)) == [(good, "critical"), (nvd, "medium")]


def carried_by_an_australian_advisory(conn, event_id):
    conn.execute(
        text(
            "insert into source_registry (id, name, type, region, category, source_class, "
            "priority, lane, enabled, url, parser, expected_frequency) values ('test-acsc', "
            "'ACSC Alerts', 'rss', 'au', 'advisory', 'AUTHORITATIVE', 1, 'fast', true, "
            "'https://example.test/feed', 'rss', 'hourly') on conflict do nothing"
        )
    )
    conn.execute(
        text(
            "insert into event_sources (event_id, source_id, url, evidence_class, url_hash) "
            "values (:e, 'test-acsc', :url, 'AUTHORITATIVE', :e)"
        ),
        {"e": event_id, "url": f"https://example.test/{event_id}"},
    )


def test_a_few_events_an_australian_advisory_carried_go_first(conn):
    events = [insert_event(conn, n) for n in range(11, 17)]
    au = set(events[:3])
    for event_id in au:
        carried_by_an_australian_advisory(conn, event_id)
    got = [e for e, _ in db.golden_candidates(conn)]
    assert sorted(got) == sorted(events)
    # Two of the three (AU_PER_STRATUM), then everything else in the usual order.
    assert set(got[:2]) <= au
    assert got[2:] == sorted(got[2:], key=lambda e: hashlib.md5(e.encode()).hexdigest())


def test_replacing_the_golden_set_keeps_the_records_as_they_were_pinned(conn):
    a = GoldenEvent("evt-2026-000001", {"title": "Acme — SecureGate"}, {"severity": "high"})
    b = GoldenEvent("evt-2026-000002", {"title": "Other"}, {"severity": "low", "au_desk": None})
    db.replace_golden(conn, [b, a], NOW)
    assert db.load_golden(conn) == [a, b] and db.golden_reviewed(conn) == 0
    conn.execute(
        text("update golden_events set reviewed = true where event_id = :e"), {"e": a.event_id}
    )
    assert db.golden_reviewed(conn) == 1
    db.replace_golden(conn, [b], NOW + timedelta(days=1))
    assert db.load_golden(conn) == [b]
    db.replace_golden(conn, [], NOW)
    assert db.load_golden(conn) == []


# ─── Gauntlet runs ────────────────────────────────────────────────────────────────────────────────


def spend(conn, cost, *, ts=NOW, agent="ripperdoc", stage="gauntlet"):
    record(
        conn,
        LedgerEntry(
            provider="openrouter",
            stage=stage,
            outcome="ok",
            agent=agent,
            cost_usd=None if cost is None else Decimal(cost),
        ),
    )
    conn.execute(
        text("update cost_ledger set ts = :ts where id = (select max(id) from cost_ledger)"),
        {"ts": ts},
    )


def test_the_gauntlets_spend_is_read_from_the_ledger_for_the_month(conn):
    spend(conn, "0.01")
    spend(conn, "0.02", ts=NOW - timedelta(days=1))
    spend(conn, None)  # not billed: counts as nothing, not as an error
    spend(conn, "0.50", ts=NOW - timedelta(days=40))
    spend(conn, "0.50", agent="deckard", stage="brief")
    assert db.gauntlet_spend(conn, *month_bounds(NOW)) == Decimal("0.03")


def measured(slug, *, at=NOW, digest="abc123def456"):
    return measure(
        Tier.CHEAP,
        slug,
        [Attempt(f"e{i}", "ok", 1.0, 1000, Decimal("0.0005")) for i in range(4)],
        incumbent=True,
        measured_at=at,
        golden_digest=digest,
    )


def test_recent_results_are_on_the_same_set_and_newest_first(conn):
    since = NOW - timedelta(days=28)
    old = measured("v/old", at=NOW - timedelta(days=7))
    new = measured("v/new")
    for started, digest, results in (
        (NOW - timedelta(days=7), "abc123def456", [old]),
        (NOW, "abc123def456", [new]),
        (NOW, "000000000000", [measured("v/other-set", digest="000000000000")]),
        (NOW - timedelta(days=40), "abc123def456", [measured("v/stale")]),
    ):
        db.record_gauntlet(
            conn,
            started_at=started,
            finished_at=started + timedelta(minutes=5),
            golden_digest=digest,
            spent_usd=Decimal("0.004"),
            results=results,
            note=None,
        )
    # A result in a shape this code no longer reads is passed over.
    conn.execute(
        text(
            "insert into gauntlet_runs (started_at, golden_digest, results) values "
            "(:now, 'abc123def456', '[{\"slug\": \"v/older-shape\"}]')"
        ),
        {"now": NOW},
    )
    got = db.recent_results(conn, "abc123def456", since)
    assert got == [new, old]


def test_a_run_with_a_negative_spend_is_refused(conn):
    with pytest.raises(IntegrityError), conn.begin_nested():
        db.record_gauntlet(
            conn,
            started_at=NOW,
            finished_at=NOW,
            golden_digest=None,
            spent_usd=Decimal(-1),
            results=[],
            note=None,
        )


# ─── Proposals ────────────────────────────────────────────────────────────────────────────────────


def test_a_proposal_is_not_raised_again_while_it_is_open(conn):
    kwargs = {
        "title": "[MODEL] Proposal: tier1_cheap -> v/cheap",
        "body": "Why.",
        "payload": {"tier": "tier1_cheap", "slug": "v/cheap"},
        "now": NOW,
    }
    first = db.propose(conn, **kwargs)
    assert first is not None and db.propose(conn, **kwargs) is None
    conn.execute(
        text("update agent_proposals set status = 'rejected' where id = :id"), {"id": first}
    )
    assert db.propose(conn, **kwargs) not in (None, first)


# ─── What RIPPERDOC reads ─────────────────────────────────────────────────────────────────────────


def test_the_report_before_anything_has_run(conn):
    report = db.models_report(conn, NOW)
    assert report["scan"] is None and report["ladder"] is None and report["gauntlet"] is None
    assert report["changes"] == report["new_free"] == report["proposals"] == []
    assert report["golden"] == {"size": 0, "reviewed": 0, "pinned_at": None}


def test_the_report_shows_the_last_scan_new_free_models_and_the_last_run(conn):
    first = NOW - timedelta(days=3)
    db.store_listing(conn, listing(entry("a/one"), entry("b/old:free", "0", "0")), [], first)
    db.store_listing(
        conn,
        listing(
            entry("a/one", expiration_date="2026-12-01"),
            entry("b/old:free", "0", "0"),
            entry("c/new:free", "0", "0"),
        ),
        [Change("c/new:free", "new", {"free": True})],
        NOW,
    )
    ladder = {"tier1_cheap": {"configured": ["a/one"], "effective": ["a/one"], "dropped": []}}
    db.record_scan(conn, ts=NOW, models_listed=3, changes=1, ladder=ladder, error=None)
    db.replace_golden(conn, [GoldenEvent("evt-2026-000001", {}, {})], NOW)
    run_id = db.record_gauntlet(
        conn,
        started_at=NOW,
        finished_at=NOW,
        golden_digest="abc",
        spent_usd=Decimal("0.0044"),
        results=[measured("v/cheap")],
        note="a note",
    )
    db.propose(
        conn,
        title="[MODEL] Proposal: tier1_cheap -> v/cheap",
        body="Why.",
        payload={"tier": "tier1_cheap", "slug": "v/cheap", "replaces": "a/one", "why": "cheaper"},
        now=NOW,
    )

    report = db.models_report(conn, NOW)
    assert report["scan"] == {
        "ts": NOW.isoformat(),
        "models_listed": 3,
        "changes": 1,
        "error": None,
    }
    assert report["ladder"] == ladder
    assert [(c["slug"], c["kind"]) for c in report["changes"]] == [("c/new:free", "new")]
    # The first scan's models were all "first seen" then, so they are not new.
    assert [m["slug"] for m in report["new_free"]] == ["c/new:free"]
    assert report["expiring_in_ladder"] == [{"slug": "a/one", "expiration_date": "2026-12-01"}]
    run = report["gauntlet"]
    assert (run["id"], run["spent_usd"], run["note"]) == (run_id, 0.0044, "a note")
    assert run["results"][0]["slug"] == "v/cheap"
    [p] = report["proposals"]
    assert (p["tier"], p["slug"], p["replaces"], p["why"]) == (
        "tier1_cheap",
        "v/cheap",
        "a/one",
        "cheaper",
    )
    assert p["body"] == "Why."
    assert report["golden"]["size"] == 1 and report["golden"]["pinned_at"] == NOW.isoformat()
