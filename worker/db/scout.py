"""RIPPERDOC's rows (migration 014): the model catalogue and what changed in it, the daily scans,
the golden set, the gauntlet runs, and model proposals in `agent_proposals`.

The decisions are worker/ai/catalogue.py's, worker/ai/golden.py's and worker/ai/gauntlet.py's;
this module only reads and writes. `detail`, `results` and the proposal payload hold figures
and our own words, never model output.
"""

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from worker.ai.catalogue import Change, Known, ListedModel
from worker.ai.gauntlet import AGENT, STAGE, Listed, Measured
from worker.ai.golden import AU_PER_STRATUM, MIN_SOURCE_CHARS, OFFICIAL_SOURCES, GoldenEvent
from worker.db.digest import truncate, usd

PROPOSAL_KIND = "model-promotion"
# What /ops/models shows of the change log.
RECENT_CHANGES = timedelta(days=14)
MAX_CHANGES_SHOWN = 100


# ─── The catalogue ────────────────────────────────────────────────────────────────────────────────


def load_catalogue(conn: Connection) -> dict[str, Known]:
    rows = conn.execute(
        text(
            "select slug, prompt_per_mtok, completion_per_mtok, capable, expiration_date, "
            "withdrawn_at is not null as withdrawn from model_catalogue"
        )
    )
    return {
        r.slug: Known(
            r.slug,
            r.prompt_per_mtok,
            r.completion_per_mtok,
            r.capable,
            r.expiration_date,
            r.withdrawn,
        )
        for r in rows
    }


def load_listed(conn: Connection) -> list[Listed]:
    rows = conn.execute(
        text(
            "select slug, first_seen, prompt_per_mtok, completion_per_mtok, capable, "
            "expiration_date, intelligence_index, withdrawn_at is not null as withdrawn "
            "from model_catalogue order by slug"
        )
    )
    return [
        Listed(
            r.slug,
            r.first_seen,
            r.prompt_per_mtok,
            r.completion_per_mtok,
            r.capable,
            r.expiration_date,
            r.intelligence_index,
            r.withdrawn,
        )
        for r in rows
    ]


def store_listing(
    conn: Connection, listing: Sequence[ListedModel], changes: Sequence[Change], now: datetime
) -> None:
    """Every listed model as it is now, a withdrawal date for each one no longer listed, and
    the changes. One transaction, so the catalogue and its change log agree."""
    conn.execute(
        text(
            "insert into model_catalogue (slug, name, first_seen, last_seen, withdrawn_at, "
            "prompt_per_mtok, completion_per_mtok, capable, expiration_date, intelligence_index, "
            "updated_at) values (:slug, :name, :now, :now, null, :prompt, :completion, :capable, "
            ":expiration, :index, :now) "
            "on conflict (slug) do update set name = excluded.name, last_seen = excluded.last_seen, "
            "withdrawn_at = null, prompt_per_mtok = excluded.prompt_per_mtok, "
            "completion_per_mtok = excluded.completion_per_mtok, capable = excluded.capable, "
            "expiration_date = excluded.expiration_date, "
            "intelligence_index = excluded.intelligence_index, updated_at = excluded.updated_at"
        ),
        [
            {
                "slug": m.slug,
                "name": m.name,
                "now": now,
                "prompt": m.prompt_per_mtok,
                "completion": m.completion_per_mtok,
                "capable": m.capable,
                "expiration": m.expiration,
                "index": m.intelligence_index,
            }
            for m in listing
        ],
    )
    conn.execute(
        text(
            "update model_catalogue set withdrawn_at = :now, updated_at = :now "
            "where withdrawn_at is null and not (slug = any(cast(:slugs as text[])))"
        ),
        {"now": now, "slugs": [m.slug for m in listing]},
    )
    insert_changes(conn, changes, now)


def insert_changes(conn: Connection, changes: Sequence[Change], now: datetime) -> None:
    if not changes:
        return
    conn.execute(
        text(
            "insert into model_changes (ts, slug, kind, detail) "
            "values (:ts, :slug, :kind, cast(:detail as jsonb))"
        ),
        [
            {"ts": now, "slug": c.slug, "kind": c.kind, "detail": json.dumps(c.detail)}
            for c in changes
        ],
    )


# ─── Scans ────────────────────────────────────────────────────────────────────────────────────────


def last_scan_ladder(conn: Connection) -> dict[str, Any] | None:
    """The ladder report of the newest scan that checked the ladder."""
    return conn.execute(
        text(
            "select ladder from model_scans where ladder is not null "
            "order by ts desc, id desc limit 1"
        )
    ).scalar()


def record_scan(
    conn: Connection,
    *,
    ts: datetime,
    models_listed: int | None,
    changes: int,
    ladder: dict[str, Any] | None,
    error: str | None,
) -> int:
    return conn.execute(
        text(
            "insert into model_scans (ts, models_listed, changes, ladder, error) "
            "values (:ts, :listed, :changes, cast(:ladder as jsonb), :error) returning id"
        ),
        {
            "ts": ts,
            "listed": models_listed,
            "changes": changes,
            "ladder": json.dumps(ladder) if ladder is not None else None,
            "error": truncate(error) if error else None,
        },
    ).scalar_one()


# ─── The golden set ───────────────────────────────────────────────────────────────────────────────


def golden_candidates(conn: Connection, limit: int = 1000) -> list[tuple[str, str]]:
    """(event_id, severity) of every event that could be pinned, in a stable order that does not
    follow collection order: an official severity, a CVE, enough of the feed's own text, and
    not merged into another event. Up to `AU_PER_STRATUM` per severity that an Australian
    advisory carried come first, so the AU desk label has both answers in the set."""
    rows = conn.execute(
        text(
            "with c as (select e.event_id, e.severity, exists ("
            "select 1 from event_sources es join source_registry r on r.id = es.source_id "
            "where es.event_id = e.event_id and r.region = 'au' and r.category = 'advisory'"
            ") as au from events e "
            "where e.severity_source = any(cast(:official as text[])) "
            "and e.severity <> 'unknown' and e.merged_into is null "
            "and length(coalesce(e.source_summary, e.summary)) >= :min_chars "
            "and exists (select 1 from event_cves ec where ec.event_id = e.event_id)), "
            "ranked as (select c.*, row_number() over (partition by severity, au "
            "order by md5(event_id), event_id) as n from c) "
            "select event_id, severity from ranked "
            "order by (au and n <= :au_per_stratum) desc, md5(event_id), event_id limit :limit"
        ),
        {
            "official": [s.value for s in OFFICIAL_SOURCES],
            "min_chars": MIN_SOURCE_CHARS,
            "au_per_stratum": AU_PER_STRATUM,
            "limit": limit,
        },
    )
    return [(r.event_id, r.severity) for r in rows]


def load_golden(conn: Connection) -> list[GoldenEvent]:
    rows = conn.execute(
        text("select event_id, record, labels from golden_events order by event_id")
    )
    return [GoldenEvent(r.event_id, r.record, r.labels) for r in rows]


def golden_reviewed(conn: Connection) -> int:
    return conn.execute(text("select count(*) from golden_events where reviewed")).scalar_one()


def replace_golden(conn: Connection, golden: Sequence[GoldenEvent], now: datetime) -> None:
    conn.execute(text("delete from golden_events"))
    if not golden:
        return
    conn.execute(
        text(
            "insert into golden_events (event_id, pinned_at, record, labels) "
            "values (:event_id, :now, cast(:record as jsonb), cast(:labels as jsonb))"
        ),
        [
            {
                "event_id": g.event_id,
                "now": now,
                "record": json.dumps(g.record, ensure_ascii=False),
                "labels": json.dumps(g.labels),
            }
            for g in golden
        ],
    )


# ─── Gauntlet runs ────────────────────────────────────────────────────────────────────────────────


def gauntlet_spend(conn: Connection, start: datetime, end: datetime) -> Decimal:
    """What the gauntlet's calls cost in [start, end), from the ledger rather than our own sums,
    so a run that crashed after paying still counts."""
    return conn.execute(
        text(
            "select coalesce(sum(cost_usd), 0) from cost_ledger "
            "where agent = :agent and stage = :stage and ts >= :start and ts < :end"
        ),
        {"agent": AGENT, "stage": STAGE, "start": start, "end": end},
    ).scalar_one()


def recent_results(conn: Connection, golden_digest: str, since: datetime) -> list[Measured]:
    """Every model result measured on this golden set since `since`, newest first."""
    rows = conn.execute(
        text(
            "select r.value as result from gauntlet_runs g, "
            "jsonb_array_elements(g.results) r(value) "
            "where g.golden_digest = :digest and g.started_at >= :since "
            "order by g.started_at desc, g.id desc"
        ),
        {"digest": golden_digest, "since": since},
    )
    out = []
    for r in rows:
        try:
            m = Measured.from_json(r.result)
        except TypeError:
            continue  # written by an older shape of the gauntlet
        if datetime.fromisoformat(m.measured_at) >= since:
            out.append(m)
    return out


def record_gauntlet(
    conn: Connection,
    *,
    started_at: datetime,
    finished_at: datetime,
    golden_digest: str | None,
    spent_usd: Decimal,
    results: Sequence[Measured],
    note: str | None,
) -> int:
    return conn.execute(
        text(
            "insert into gauntlet_runs (started_at, finished_at, golden_digest, spent_usd, "
            "results, note) values (:started, :finished, :digest, :spent, "
            "cast(:results as jsonb), :note) returning id"
        ),
        {
            "started": started_at,
            "finished": finished_at,
            "digest": golden_digest,
            "spent": spent_usd,
            "results": json.dumps([m.to_json() for m in results]),
            "note": truncate(note) if note else None,
        },
    ).scalar_one()


# ─── Proposals ────────────────────────────────────────────────────────────────────────────────────


def propose(
    conn: Connection, *, title: str, body: str, payload: dict[str, Any], now: datetime
) -> int | None:
    """A model proposal, unless the same one is already open. Returns its id, or None."""
    open_already = conn.execute(
        text(
            "select 1 from agent_proposals where agent = :agent and kind = :kind "
            "and title = :title and status = 'proposed' limit 1"
        ),
        {"agent": AGENT, "kind": PROPOSAL_KIND, "title": title},
    ).first()
    if open_already:
        return None
    return conn.execute(
        text(
            "insert into agent_proposals (agent, kind, title, body, payload, created_at) "
            "values (:agent, :kind, :title, :body, cast(:payload as jsonb), :now) returning id"
        ),
        {
            "agent": AGENT,
            "kind": PROPOSAL_KIND,
            "title": title,
            "body": body,
            "payload": json.dumps(payload),
            "now": now,
        },
    ).scalar_one()


# ─── What RIPPERDOC reads (GET /ops/models) ───────────────────────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def models_report(conn: Connection, now: datetime) -> dict[str, Any]:
    scan = (
        conn.execute(
            text(
                "select ts, models_listed, changes, ladder, error from model_scans "
                "order by ts desc, id desc limit 1"
            )
        )
        .mappings()
        .first()
    )
    since = now - RECENT_CHANGES
    changes = [
        {"ts": _iso(r.ts), "slug": r.slug, "kind": r.kind, "detail": r.detail}
        for r in conn.execute(
            text(
                "select ts, slug, kind, detail from model_changes where ts >= :since "
                "order by ts desc, id desc limit :limit"
            ),
            {"since": since, "limit": MAX_CHANGES_SHOWN},
        )
    ]
    new_free = [
        {
            "slug": r.slug,
            "name": r.name,
            "first_seen": _iso(r.first_seen),
            "capable": r.capable,
            "expiration_date": r.expiration_date.isoformat() if r.expiration_date else None,
        }
        for r in conn.execute(
            text(
                "select slug, name, first_seen, capable, expiration_date from model_catalogue "
                "where slug like :free and withdrawn_at is null and first_seen >= :since "
                # Not the first scan's: that one recorded everything listed, new or not.
                "and first_seen > (select min(first_seen) from model_catalogue) "
                "order by first_seen desc, slug"
            ),
            {"free": "%:free", "since": since},
        )
    ]
    ladder = scan["ladder"] if scan and scan["ladder"] else None
    in_ladder = sorted({s for t in (ladder or {}).values() for s in t.get("configured") or ()})
    expiring = [
        {"slug": r.slug, "expiration_date": r.expiration_date.isoformat()}
        for r in conn.execute(
            text(
                "select slug, expiration_date from model_catalogue "
                "where slug = any(cast(:slugs as text[])) and expiration_date is not null "
                "order by expiration_date, slug"
            ),
            {"slugs": in_ladder},
        )
    ]
    run = (
        conn.execute(
            text(
                "select id, started_at, finished_at, golden_digest, spent_usd, results, note "
                "from gauntlet_runs order by started_at desc, id desc limit 1"
            )
        )
        .mappings()
        .first()
    )
    proposals = [
        {
            "id": r.id,
            "title": r.title,
            "body": r.body,
            "created_at": _iso(r.created_at),
            "tier": (r.payload or {}).get("tier"),
            "slug": (r.payload or {}).get("slug"),
            "replaces": (r.payload or {}).get("replaces"),
            "why": (r.payload or {}).get("why"),
        }
        for r in conn.execute(
            text(
                "select id, title, body, created_at, payload from agent_proposals "
                "where agent = :agent and kind = :kind and status = 'proposed' "
                "order by created_at desc, id desc"
            ),
            {"agent": AGENT, "kind": PROPOSAL_KIND},
        )
    ]
    golden = (
        conn.execute(
            text(
                "select count(*) as size, count(*) filter (where reviewed) as reviewed, "
                "max(pinned_at) as pinned_at from golden_events"
            )
        )
        .mappings()
        .one()
    )
    return {
        "scan": (
            {
                "ts": _iso(scan["ts"]),
                "models_listed": scan["models_listed"],
                "changes": scan["changes"],
                "error": scan["error"],
            }
            if scan
            else None
        ),
        "ladder": ladder,
        "changes": changes,
        "new_free": new_free,
        "expiring_in_ladder": expiring,
        "gauntlet": (
            {
                "id": run["id"],
                "started_at": _iso(run["started_at"]),
                "finished_at": _iso(run["finished_at"]),
                "golden_digest": run["golden_digest"],
                "spent_usd": usd(run["spent_usd"]),
                "note": run["note"],
                "results": run["results"],
            }
            if run
            else None
        ),
        "proposals": proposals,
        "golden": {
            "size": golden["size"],
            "reviewed": golden["reviewed"],
            "pinned_at": _iso(golden["pinned_at"]),
        },
        "prices_in": "US$ per million tokens",
    }
