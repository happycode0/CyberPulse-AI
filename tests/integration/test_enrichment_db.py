"""Enrichment persistence against a real Postgres (migration 006, worker/db/enrichment.py).

A fake connection would let the parts that matter be wrong: the `not exists` that makes a task
in backoff hold its event back, the re-check of `severity_source` inside the update that keeps
an official score from being overwritten, and the check constraints on the new table.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.ai.tasks import Brief, SeverityJudgment, SourceFacts, TaskName, Triage
from worker.db.enrichment import (
    BACKOFF,
    apply_brief,
    apply_severity,
    apply_triage,
    due_event_ids,
    load_subjects,
    mark_done,
    mark_failed,
    maybe_complete,
    task_states,
)
from worker.db.migrate import run_migrations
from worker.models import AiSubdomain, Severity
from worker.pipeline.assemble import AU_SOURCE_REASON
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION, UNENRICHED

NOW = datetime(2026, 10, 3, 1, 5, tzinfo=UTC)
V1 = "1"
FLOOR = 0.05

EVENT = "evt-2026-000001"
EVENT_2 = "evt-2026-000002"
EVENT_3 = "evt-2026-000003"


@pytest.fixture
def conn(pg_engine):
    """A connection in a transaction that is always rolled back, with two sources registered."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        c.execute(text("delete from events"))
        c.execute(text("delete from source_registry"))
        for sid, name, region, category in (
            ("itnews", "iTnews", "au", "news"),
            ("cisa", "CISA", "us", "advisory"),
        ):
            c.execute(
                text(
                    "insert into source_registry (id, name, type, region, category, "
                    "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                    "values (:id, :name, 'rss', :region, :category, 'NEWS', 1, 'fast', true, "
                    "'https://example.test/feed', 'rss', 'hourly')"
                ),
                {"id": sid, "name": name, "region": region, "category": category},
            )
        try:
            yield c
        finally:
            trans.rollback()


def insert_event(
    conn,
    event_id,
    *,
    pending=True,
    prominence=0.5,
    status="new",
    severity="unknown",
    severity_source="unknown",
    summary="The feed said this.",
    source_summary=None,
    au_directly_reported=False,
    title="Acme VPN flaw exploited",
):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, status, title, summary, source_summary, "
            "severity, severity_source, prominence, pending_enrichment, au_directly_reported) "
            "values (:id, :schema, :pipeline, :scoring, :enrichment, :now, :now, :status, "
            ":title, :summary, :source_summary, :severity, :source, :prominence, :pending, :au)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "enrichment": UNENRICHED,
            "now": NOW,
            "status": status,
            "title": title,
            "summary": summary,
            "source_summary": source_summary,
            "severity": severity,
            "source": severity_source,
            "prominence": prominence,
            "pending": pending,
            "au": au_directly_reported,
        },
    )


def add_source(conn, event_id, source_id, title, *, published=NOW, n=0):
    conn.execute(
        text(
            "insert into event_sources (event_id, source_id, url, title, published, "
            "evidence_class, url_hash) values (:e, :s, :url, :title, :published, 'NEWS', :hash)"
        ),
        {
            "e": event_id,
            "s": source_id,
            "url": f"https://example.test/{source_id}/{n}",
            "title": title,
            "published": published,
            "hash": f"{source_id}-{n}",
        },
    )


def event_row(conn, event_id):
    return conn.execute(
        text("select * from events where event_id = :e"), {"e": event_id}
    ).mappings().one()


def subject(conn, event_id):
    [s] = load_subjects(conn, [event_id])
    return s


def due(conn, now=NOW):
    return due_event_ids(conn, version=V1, now=now, floor=FLOOR, limit=50)


# ─── What is due ──────────────────────────────────────────────────────────────────────────────────


def test_pending_published_events_are_due_most_prominent_first(conn):
    insert_event(conn, EVENT, prominence=0.2)
    insert_event(conn, EVENT_2, prominence=0.9)
    insert_event(conn, EVENT_3, prominence=0.2)
    assert due(conn) == [EVENT_2, EVENT, EVENT_3]


@pytest.mark.parametrize(
    "changes",
    [
        {"pending": False},
        {"status": "archived"},
        {"prominence": FLOOR},  # the site would not publish it
        {"prominence": None},
    ],
)
def test_events_nobody_sees_or_already_enriched_are_not_due(conn, changes):
    insert_event(conn, EVENT, **changes)
    assert due(conn) == []


def test_a_task_in_backoff_holds_its_event_back_until_the_retry_time(conn):
    insert_event(conn, EVENT)
    retry_at = mark_failed(
        conn, EVENT, TaskName.BRIEF, version=V1, model=None, reason="no", now=NOW
    )
    assert retry_at == NOW + BACKOFF[0]
    assert due(conn) == []
    assert due(conn, now=retry_at) == [EVENT]


def test_a_task_that_gave_up_holds_its_event_back_until_the_version_changes(conn):
    insert_event(conn, EVENT)
    for _ in range(len(BACKOFF) + 1):
        last = mark_failed(
            conn, EVENT, TaskName.TRIAGE, version=V1, model=None, reason="no", now=NOW
        )
    assert last is None
    assert due(conn, now=NOW + timedelta(days=365)) == []
    assert due_event_ids(conn, version="2", now=NOW, floor=FLOOR, limit=50) == [EVENT]


def test_a_done_task_does_not_hold_its_event_back(conn):
    insert_event(conn, EVENT)
    mark_done(conn, EVENT, TaskName.TRIAGE, version=V1, model="m", now=NOW)
    assert due(conn) == [EVENT]
    assert task_states(conn, [EVENT])[EVENT]["triage"].status == "done"


# ─── What a model is shown ────────────────────────────────────────────────────────────────────────


def test_a_subject_carries_the_source_text_other_headlines_and_who_reported_it(conn):
    insert_event(conn, EVENT, summary="An enriched summary.", source_summary="The feed's words.")
    add_source(conn, EVENT, "itnews", "Acme VPN flaw exploited", n=1)  # the event's own title
    add_source(conn, EVENT, "cisa", "CISA adds Acme flaw to KEV", n=2)
    add_source(conn, EVENT, "itnews", "cisa adds acme flaw to kev", n=3)  # casefold differs
    add_source(conn, EVENT, "itnews", None, published=None, n=4)
    s = subject(conn, EVENT)
    assert s.source_text == "The feed's words."
    assert s.headlines == ("CISA adds Acme flaw to KEV", "cisa adds acme flaw to kev")
    assert s.sources == (SourceFacts("iTnews", "au", "news"), SourceFacts("CISA", "us", "advisory"))


def test_an_event_from_before_migration_006_falls_back_to_its_summary(conn):
    insert_event(conn, EVENT, summary="The feed said this.", source_summary=None)
    assert subject(conn, EVENT).source_text == "The feed said this."


def test_subjects_come_back_in_the_order_asked_for(conn):
    insert_event(conn, EVENT)
    insert_event(conn, EVENT_2)
    assert [s.event.event_id for s in load_subjects(conn, [EVENT_2, EVENT])] == [EVENT_2, EVENT]
    assert load_subjects(conn, []) == []


# ─── Writing answers ──────────────────────────────────────────────────────────────────────────────


def test_triage_puts_the_models_categories_first_and_keeps_the_sources(conn):
    insert_event(conn, EVENT)
    add_source(conn, EVENT, "cisa", "a", n=1)
    add_source(conn, EVENT, "itnews", "b", n=2)
    t = Triage(
        domains=("cybersecurity", "ai"),
        categories=("vulnerability", "news"),
        ai_subdomain=AiSubdomain.AI_SECURITY,
        actors=(),
        organisations=("Acme",),
        products=("SecureGate VPN",),
        countries=("AU",),
        industries=("health",),
        tags=("vpn",),
    )
    apply_triage(conn, subject(conn, EVENT), t)
    row = event_row(conn, EVENT)
    assert row["categories"] == ["vulnerability", "news", "advisory"]
    assert row["domains"] == ["cybersecurity", "ai"] and row["ai_subdomain"] == "AI_SECURITY"
    assert row["entity_organisations"] == ["Acme"] and row["entity_products"] == ["SecureGate VPN"]
    assert row["entity_countries"] == ["AU"] and row["entity_industries"] == ["health"]
    assert row["tags"] == ["vpn"] and row["pending_enrichment"] is True


@pytest.mark.parametrize(
    "direct, expected",
    [
        (True, [AU_SOURCE_REASON, "Victorian hospitals were hit"]),
        (False, ["Victorian hospitals were hit"]),
    ],
)
def test_a_brief_keeps_the_australian_source_fact_as_the_first_reason(conn, direct, expected):
    insert_event(conn, EVENT, au_directly_reported=direct, source_summary="The feed's words.")
    b = Brief(
        summary="Our own words.",
        why_it_matters=None,
        au_relevance=0.8,
        au_reasons=("Victorian hospitals were hit", AU_SOURCE_REASON),
        au_sectors=("health",),
    )
    apply_brief(conn, subject(conn, EVENT), b)
    row = event_row(conn, EVENT)
    assert row["au_reasons"] == expected
    assert row["summary"] == "Our own words." and row["source_summary"] == "The feed's words."
    assert row["au_relevance"] == 0.8 and row["au_sectors"] == ["health"]


def ai_evidence(conn, event_id):
    return [
        r[0]
        for r in conn.execute(
            text(
                "select detail from evidence where event_id = :e and kind = 'ai_severity' "
                "and evidence_class = 'AI_INFERENCE' order by id"
            ),
            {"e": event_id},
        )
    ]


FIRM = SeverityJudgment(Severity.HIGH, 0.8, "Exploited in the wild.")
WEAK = SeverityJudgment(Severity.CRITICAL, 0.4, "Little to go on.")


def test_a_firm_judgment_is_published_as_an_estimate(conn):
    insert_event(conn, EVENT)
    assert apply_severity(conn, subject(conn, EVENT), FIRM, model="m", version=V1) is True
    row = event_row(conn, EVENT)
    assert (row["severity"], row["severity_source"]) == ("high", "ai_estimate")
    [detail] = ai_evidence(conn, EVENT)
    assert detail == {
        "severity": "high",
        "confidence": 0.8,
        "rationale": "Exploited in the wild.",
        "model": "m",
        "enrichment_version": V1,
        "applied": True,
    }


def test_an_official_score_that_arrived_meanwhile_is_never_overwritten(conn):
    insert_event(conn, EVENT)
    s = subject(conn, EVENT)  # read while the severity was still unknown
    conn.execute(
        text("update events set severity = 'medium', severity_source = 'nvd' where event_id = :e"),
        {"e": EVENT},
    )
    assert apply_severity(conn, s, FIRM, model="m", version=V1) is False
    row = event_row(conn, EVENT)
    assert (row["severity"], row["severity_source"]) == ("medium", "nvd")
    assert [d["applied"] for d in ai_evidence(conn, EVENT)] == [False]


def test_a_weak_judgment_takes_back_an_earlier_estimate(conn):
    insert_event(conn, EVENT, severity="high", severity_source="ai_estimate")
    assert apply_severity(conn, subject(conn, EVENT), WEAK, model="m", version=V1) is False
    row = event_row(conn, EVENT)
    assert (row["severity"], row["severity_source"]) == ("unknown", "unknown")
    assert [d["severity"] for d in ai_evidence(conn, EVENT)] == ["critical"]


def test_a_weak_judgment_leaves_an_official_score_alone(conn):
    insert_event(conn, EVENT, severity="low", severity_source="cna")
    apply_severity(conn, subject(conn, EVENT), WEAK, model="m", version=V1)
    row = event_row(conn, EVENT)
    assert (row["severity"], row["severity_source"]) == ("low", "cna")


# ─── Bookkeeping ──────────────────────────────────────────────────────────────────────────────────


def enrichment_row(conn, event_id, task):
    return conn.execute(
        text("select * from event_enrichment where event_id = :e and task = :t"),
        {"e": event_id, "t": task},
    ).mappings().one()


def test_failures_back_off_then_give_up_and_a_success_resets_them(conn):
    insert_event(conn, EVENT)

    def fail(version=V1):
        return mark_failed(
            conn, EVENT, TaskName.BRIEF, version=version, model="m", reason="x" * 500, now=NOW
        )

    assert [fail() for _ in range(4)] == [NOW + b for b in BACKOFF] + [None]
    row = enrichment_row(conn, EVENT, "brief")
    assert row["failures"] == 4 and len(row["last_error"]) == 300

    mark_done(conn, EVENT, TaskName.BRIEF, version=V1, model="m", now=NOW)
    row = enrichment_row(conn, EVENT, "brief")
    assert (row["status"], row["failures"], row["last_error"]) == ("done", 0, None)
    assert row["done_at"] == NOW
    assert fail() == NOW + BACKOFF[0]  # counting starts again


def test_a_new_version_starts_the_count_again(conn):
    insert_event(conn, EVENT)
    for _ in range(4):
        mark_failed(conn, EVENT, TaskName.TRIAGE, version=V1, model=None, reason="no", now=NOW)
    retry = mark_failed(conn, EVENT, TaskName.TRIAGE, version="2", model=None, reason="no", now=NOW)
    assert retry == NOW + BACKOFF[0]


def test_the_table_rejects_a_task_or_status_it_does_not_know(conn):
    insert_event(conn, EVENT)
    with pytest.raises(Exception, match="check"):
        with conn.begin_nested():
            conn.execute(
                text(
                    "insert into event_enrichment (event_id, task, version, status) "
                    "values (:e, 'mitre', '1', 'done')"
                ),
                {"e": EVENT},
            )


def done(conn, event_id, *tasks):
    for task in tasks:
        mark_done(conn, event_id, task, version=V1, model="m", now=NOW)


def test_an_event_completes_once_triage_brief_and_severity_are_done(conn):
    insert_event(conn, EVENT)
    done(conn, EVENT, TaskName.TRIAGE)
    assert maybe_complete(conn, EVENT, version=V1) is False
    done(conn, EVENT, TaskName.BRIEF)
    assert maybe_complete(conn, EVENT, version=V1) is False  # no official score: severity applies
    done(conn, EVENT, TaskName.SEVERITY)
    assert maybe_complete(conn, EVENT, version=V1) is True
    row = event_row(conn, EVENT)
    assert row["pending_enrichment"] is False and row["enrichment_version"] == V1
    assert maybe_complete(conn, EVENT, version=V1) is False  # already complete


def test_an_event_with_an_official_score_needs_no_severity_judgment(conn):
    insert_event(conn, EVENT, severity="high", severity_source="nvd")
    done(conn, EVENT, TaskName.TRIAGE, TaskName.BRIEF)
    assert maybe_complete(conn, EVENT, version=V1) is True


def test_an_ai_estimate_still_needs_its_judgment_done_at_this_version(conn):
    insert_event(conn, EVENT, severity="high", severity_source="ai_estimate")
    done(conn, EVENT, TaskName.TRIAGE, TaskName.BRIEF)
    mark_done(conn, EVENT, TaskName.SEVERITY, version="0", model="m", now=NOW)
    assert maybe_complete(conn, EVENT, version=V1) is False


def test_the_evidence_detail_is_json(conn):
    insert_event(conn, EVENT)
    apply_severity(conn, subject(conn, EVENT), FIRM, model=None, version=V1)
    raw = conn.execute(
        text("select detail::text from evidence where event_id = :e"), {"e": EVENT}
    ).scalar_one()
    assert json.loads(raw)["model"] is None
