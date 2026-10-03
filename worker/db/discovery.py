"""source_candidates and discovery_searches rows (migration 013): what discovery found and where
each find stands at SERAPH's gate (worker/discovery/gate.py)."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Connection, text

from worker.db.sources import set_lifecycle_state, upsert_registry
from worker.discovery.gate import (
    DISCOVERED_CLASS,
    EVIDENCE_KEPT,
    GateConfig,
    Probe,
    Proposal,
    host_of,
    same_site,
)
from worker.models import Lane, LifecycleState, SourceConfig

OPEN_STATES = ("candidate", "testing")
# A home page with no feed is looked at again after this long, up to the rejection count.
HOME_RETRY = timedelta(days=7)
# The gate runs every 4 hours; a probe may come a little early and still count as the next one.
PROBE_GAP = timedelta(hours=3)
# An active discovered source with no healthy fetch in this long is retired.
RETIRE_AFTER = timedelta(days=21)
ERROR_MAX_CHARS = 300
# TACHIKOMA's proposals a day. A home page skips the cap on open candidates (it has no feed yet),
# so this is what bounds the rows a run of the agent can add.
PROPOSALS_PER_DAY = 20

_CANDIDATE_COLUMNS = (
    "id, host, feed_url, name, found_by, reason, proposed_at, evidence, state, passes, failures, "
    "last_probe_at, last_result, last_error, source_id, created_at, updated_at"
)


@dataclass(frozen=True)
class Candidate:
    id: int
    host: str
    feed_url: str | None
    name: str | None
    found_by: str
    reason: str | None
    proposed_at: datetime | None
    evidence: list[dict[str, Any]]
    state: str
    passes: int
    failures: int
    last_probe_at: datetime | None
    last_result: dict[str, Any] | None
    last_error: str | None
    source_id: str | None
    created_at: datetime
    updated_at: datetime


def _candidates(conn: Connection, where: str, params: dict[str, Any]) -> list[Candidate]:
    rows = conn.execute(
        text(f"select {_CANDIDATE_COLUMNS} from source_candidates where {where}"), params
    ).mappings()
    return [Candidate(**r) for r in rows]


def _short(message: str | None) -> str | None:
    if message is None:
        return None
    return message if len(message) <= ERROR_MAX_CHARS else message[: ERROR_MAX_CHARS - 1] + "…"


# --- Searches ------------------------------------------------------------------------------------


def record_search(
    conn: Connection,
    *,
    query: str,
    results: int,
    credits: int | None,
    error: str | None,
    now: datetime,
) -> None:
    conn.execute(
        text(
            "insert into discovery_searches (ts, query, results, credits, error) "
            "values (:ts, :query, :results, :credits, :error)"
        ),
        {"ts": now, "query": query, "results": results, "credits": credits,
         "error": _short(error)},
    )


def credits_used(conn: Connection, *, since: datetime) -> int:
    """Credits spent since `since`. A search Tavily answered without saying counts as one; a
    failed one, as none."""
    return int(
        conn.execute(
            text(
                "select coalesce(sum(coalesce(credits, case when error is null then 1 else 0 end)),"
                " 0) from discovery_searches where ts >= :since"
            ),
            {"since": since},
        ).scalar_one()
    )


# --- Finds ---------------------------------------------------------------------------------------


def registered_hosts(conn: Connection) -> set[str]:
    """The host of every registered source, enabled or not."""
    return {host_of(url) for (url,) in conn.execute(text("select url from source_registry"))}


def add_found(conn: Connection, *, host: str, evidence: dict[str, Any], now: datetime) -> bool:
    """Record a search hit on `host`: a new row, or one more piece of evidence for a host still
    `discovered`. True when the host is new."""
    row = conn.execute(
        text(
            "insert into source_candidates (host, found_by, evidence, created_at, updated_at) "
            "values (:host, 'search', cast(:evidence as jsonb), :now, :now) "
            "on conflict (host) do update set "
            "evidence = source_candidates.evidence || excluded.evidence "
            "where source_candidates.state = 'discovered' "
            "and jsonb_array_length(source_candidates.evidence) < :kept "
            "returning (xmax = 0) as inserted"
        ),
        {"host": host, "evidence": json.dumps([evidence]), "now": now, "kept": EVIDENCE_KEPT},
    ).first()
    return bool(row and row.inserted)


ProposalResult = Literal["created", "updated", "exists", "full"]


def add_proposal(
    conn: Connection, proposal: Proposal, *, max_open: int, now: datetime
) -> tuple[ProposalResult, Candidate | None]:
    """Queue TACHIKOMA's proposal.

    "updated": the host was found by a search and TACHIKOMA has given it its feed. "exists": the
    host is registered or already past that. "full": `max_open` candidates are being tested, or
    TACHIKOMA has made `PROPOSALS_PER_DAY` proposals in the last 24 hours.
    """
    if any(same_site(proposal.host, h) for h in registered_hosts(conn)):
        return "exists", None
    [existing] = _candidates(conn, "host = :host for update", {"host": proposal.host}) or [None]
    if existing is not None and not (
        existing.state == "discovered" and existing.feed_url is None and not proposal.is_home
    ):
        return "exists", existing
    proposed_today = conn.execute(
        text(
            "select count(*) from source_candidates where proposed_at >= :since"
        ),
        {"since": now - timedelta(days=1)},
    ).scalar_one()
    if proposed_today >= PROPOSALS_PER_DAY:
        return "full", None
    if not proposal.is_home:
        open_now = conn.execute(
            text("select count(*) from source_candidates where state in ('candidate', 'testing')")
        ).scalar_one()
        if open_now >= max_open:
            return "full", None
    evidence = json.dumps([{"by": "tachikoma", "url": u} for u in proposal.examples])
    params = {
        "host": proposal.host,
        "feed_url": None if proposal.is_home else proposal.url,
        "state": "discovered" if proposal.is_home else "candidate",
        "name": proposal.name,
        "reason": proposal.reason,
        "evidence": evidence,
        "now": now,
    }
    if existing is not None:
        conn.execute(
            text(
                "update source_candidates set feed_url = :feed_url, state = :state, "
                "name = coalesce(:name, name), reason = :reason, proposed_at = :now, "
                "evidence = evidence || cast(:evidence as jsonb), passes = 0, failures = 0, "
                "last_error = null, updated_at = :now where host = :host"
            ),
            params,
        )
        result: ProposalResult = "updated"
    else:
        conn.execute(
            text(
                "insert into source_candidates (host, feed_url, name, found_by, reason, "
                "proposed_at, evidence, state, created_at, updated_at) values (:host, :feed_url, "
                ":name, 'tachikoma', :reason, :now, cast(:evidence as jsonb), :state, :now, :now)"
            ),
            params,
        )
        result = "created"
    [row] = _candidates(conn, "host = :host", {"host": proposal.host})
    return result, row


# --- The gate ------------------------------------------------------------------------------------


def homes_due(conn: Connection, *, limit: int, now: datetime, gate: GateConfig) -> list[Candidate]:
    """Found hosts whose feed is still to look for: TACHIKOMA's first, then the hosts the most
    searches turned up."""
    return _candidates(
        conn,
        "state = 'discovered' and feed_url is null and failures < :reject "
        "and (last_probe_at is null or last_probe_at <= :retry) "
        "order by (found_by = 'tachikoma') desc, jsonb_array_length(evidence) desc, created_at, id "
        "limit :limit",
        {"reject": gate.failures_to_reject, "retry": now - HOME_RETRY, "limit": limit},
    )


def record_home(
    conn: Connection,
    candidate_id: int,
    *,
    feed_url: str | None,
    name: str | None,
    error: str | None,
    gate: GateConfig,
    now: datetime,
) -> str:
    """The home page was read: with a feed the host becomes a candidate; without one it waits a
    week to be read again, and is rejected after `failures_to_reject` tries."""
    if feed_url is not None:
        return conn.execute(
            text(
                "update source_candidates set feed_url = :feed_url, name = coalesce(name, :name), "
                "state = 'candidate', passes = 0, failures = 0, last_probe_at = :now, "
                "last_result = null, last_error = null, updated_at = :now "
                "where id = :id returning state"
            ),
            {"id": candidate_id, "feed_url": feed_url, "name": name, "now": now},
        ).scalar_one()
    return conn.execute(
        text(
            "update source_candidates set failures = failures + 1, "
            "state = case when failures + 1 >= :reject then 'rejected' else state end, "
            "last_probe_at = :now, last_error = :error, updated_at = :now "
            "where id = :id returning state"
        ),
        {"id": candidate_id, "error": _short(error), "reject": gate.failures_to_reject, "now": now},
    ).scalar_one()


def probes_due(conn: Connection, *, limit: int, now: datetime) -> list[Candidate]:
    """Candidates due a probe, those furthest through the gate first."""
    return _candidates(
        conn,
        "state in ('candidate', 'testing') and (last_probe_at is null or last_probe_at <= :due) "
        "order by passes desc, last_probe_at nulls first, id limit :limit",
        {"due": now - PROBE_GAP, "limit": limit},
    )


def record_probe(
    conn: Connection,
    candidate_id: int,
    *,
    probe: Probe | None,
    error: str | None,
    gate: GateConfig,
    now: datetime,
) -> tuple[str, int]:
    """One probe's outcome: its new state and healthy probes in a row. A healthy probe moves the
    candidate to `testing`; any other breaks the run, and `failures_to_reject` in a row reject
    it. `probe` is None when the feed could not be fetched or read, and `error` says why. "gone"
    when the candidate is no longer open."""
    healthy = probe is not None and probe.healthy
    if error is None and probe is not None and not healthy:
        error = "; ".join(probe.failures)
    row = conn.execute(
        text(
            "update source_candidates set "
            "passes = case when :healthy then passes + 1 else 0 end, "
            "failures = case when :healthy then 0 else failures + 1 end, "
            "state = case when :healthy then 'testing' "
            "when failures + 1 >= :reject then 'rejected' else 'candidate' end, "
            "last_probe_at = :now, last_result = cast(:result as jsonb), last_error = :error, "
            "updated_at = :now where id = :id and state in ('candidate', 'testing') "
            "returning state, passes"
        ),
        {
            "id": candidate_id,
            "healthy": healthy,
            "reject": gate.failures_to_reject,
            "result": json.dumps(probe.as_json()) if probe is not None else None,
            "error": None if healthy else _short(error),
            "now": now,
        },
    ).first()
    return (row.state, row.passes) if row is not None else ("gone", 0)


def ready(conn: Connection, gate: GateConfig) -> list[Candidate]:
    return _candidates(
        conn,
        "state = 'testing' and passes >= :n order by passes desc, id",
        {"n": gate.probes_to_activate},
    )


def active_discovered(conn: Connection) -> int:
    return conn.execute(
        text("select count(*) from source_registry where source_class = :c and enabled"),
        {"c": DISCOVERED_CLASS},
    ).scalar_one()


def activate(
    conn: Connection, candidate: Candidate, source: SourceConfig, *, now: datetime
) -> bool:
    """Register `source` for collection and mark its candidate active. False, and nothing
    written, when its id is already taken."""
    taken = conn.execute(
        text("select 1 from source_registry where id = :id"), {"id": source.id}
    ).first()
    if taken is not None:
        return False
    upsert_registry(conn, [source])
    set_lifecycle_state(conn, source.id, LifecycleState.ACTIVE)
    conn.execute(
        text(
            "update source_candidates set state = 'active', source_id = :source_id, "
            "updated_at = :now where id = :id"
        ),
        {"id": candidate.id, "source_id": source.id, "now": now},
    )
    return True


def retire_silent(conn: Connection, *, now: datetime) -> list[str]:
    """Disable each active discovered source with no healthy fetch in `RETIRE_AFTER`, once it
    has been collected that long. Its candidate is `retired`, so the host is not found again."""
    retired = list(
        conn.execute(
            text(
                "update source_registry r set enabled = false, lifecycle_state = 'retired', "
                "updated_at = :now where r.source_class = :c and r.enabled "
                "and (select min(h.checked_at) from source_health h where h.source_id = r.id) "
                "<= :cutoff "
                "and not exists (select 1 from source_health h where h.source_id = r.id "
                "and h.status = 'ok' and h.checked_at > :cutoff) returning r.id"
            ),
            {"now": now, "c": DISCOVERED_CLASS, "cutoff": now - RETIRE_AFTER},
        ).scalars()
    )
    if retired:
        conn.execute(
            text(
                "update source_candidates set state = 'retired', updated_at = :now, "
                "last_error = 'no healthy fetch in 21 days' "
                "where source_id = any(cast(:ids as text[]))"
            ),
            {"ids": retired, "now": now},
        )
    return retired


def forget_unfed(conn: Connection, *, now: datetime, gate: GateConfig) -> int:
    """Forget search finds never given a feed. Rejected and retired rows stay."""
    return conn.execute(
        text("delete from source_candidates where state = 'discovered' and created_at < :cutoff"),
        {"cutoff": now - timedelta(days=gate.forget_after_days)},
    ).rowcount


def load_discovered_sources(conn: Connection) -> list[SourceConfig]:
    """The sources the gate activated and that are still enabled, for the collection."""
    rows = conn.execute(
        text(
            "select id, name, type, region, category, source_class, priority, lane, enabled, url, "
            "parser, expected_frequency, notes, lifecycle_state from source_registry "
            "where source_class = :c and enabled order by id"
        ),
        {"c": DISCOVERED_CLASS},
    ).mappings()
    return [
        SourceConfig(
            **{k: v for k, v in r.items() if k not in ("lane", "lifecycle_state")},
            lane=Lane(r["lane"]),
            lifecycle_state=LifecycleState(r["lifecycle_state"]) if r["lifecycle_state"] else None,
        )
        for r in rows
    ]


@dataclass(frozen=True)
class Activation:
    source_id: str
    name: str
    host: str
    region: str
    found_by: str
    passes: int
    last_result: dict[str, Any] | None
    activated_at: datetime


def load_activations(conn: Connection, *, since: datetime) -> list[Activation]:
    """Candidates activated since `since`, oldest first, for the Telegram notice."""
    rows = conn.execute(
        text(
            "select c.source_id, r.name, c.host, r.region, c.found_by, c.passes, c.last_result, "
            "c.updated_at as activated_at from source_candidates c "
            "join source_registry r on r.id = c.source_id "
            "where c.state = 'active' and c.updated_at >= :since order by c.updated_at, c.id"
        ),
        {"since": since},
    ).mappings()
    return [Activation(**r) for r in rows]


# --- What the ops API shows ----------------------------------------------------------------------


def load_overview(conn: Connection, *, now: datetime, open_limit: int = 30) -> dict[str, Any]:
    """Where discovery stands, for GET /ops/candidates."""
    states = dict(
        conn.execute(text("select state, count(*) from source_candidates group by state")).all()
    )
    open_rows = _candidates(
        conn,
        "state in ('candidate', 'testing') order by passes desc, updated_at desc, id limit :n",
        {"n": open_limit},
    )
    waiting = _candidates(
        conn,
        "state = 'discovered' order by (found_by = 'tachikoma') desc, "
        "jsonb_array_length(evidence) desc, created_at desc, id limit :n",
        {"n": open_limit},
    )
    settled = _candidates(
        conn,
        "state in ('active', 'rejected', 'retired') and updated_at >= :since "
        "order by updated_at desc, id limit :n",
        {"since": now - timedelta(days=14), "n": open_limit},
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "states": states,
        "open": open_rows,
        "waiting_for_a_feed": waiting,
        "settled_last_14_days": settled,
        "credits": {
            "last_24h": credits_used(conn, since=now - timedelta(days=1)),
            "this_month": credits_used(conn, since=month_start),
        },
        "active_discovered_sources": active_discovered(conn),
    }
