"""Consolidation's reads and writes (worker/pipeline/correlate.py decides; this carries it out).

`merge_events` folds each loser into the winner: sources, CVEs, timeline, claims, evidence,
follow-ups and MITRE techniques move across, relationships are repointed, and the loser is
archived with `merged_into` set (migration 009). It is never deleted. Its enrichment rows and
ledger costs stay with it, because that is the event they were spent on. Which of the
winner's sources are independent is settled after, in the same transaction, by the lineage
pass (worker/db/lineage.py).

Like every writer here it takes the caller's connection and never commits.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from worker.pipeline.correlate import MergeGroup, StoryRecord
from worker.pipeline.normalise import normalise_title
from worker.pipeline.resolve import REGISTER_CLASSES, TokenWeights, story_keys

_FIRST_REPORT = "First reported by "
_ALSO_REPORTED = "Also reported by "


def load_story_records(conn: Connection, *, since: datetime) -> list[StoryRecord]:
    """Every event still standing on its own (not merged, not archived) seen since `since`."""
    events = list(
        conn.execute(
            text(
                "select event_id, title, first_seen, pending_enrichment, prominence from events "
                "where merged_into is null and status <> 'archived' and last_seen >= :since "
                "order by event_id"
            ),
            {"since": since},
        ).mappings()
    )
    if not events:
        return []
    ids = {"ids": [e["event_id"] for e in events]}
    sources: dict[str, list[Any]] = defaultdict(list)
    for s in conn.execute(
        text(
            "select event_id, source_id, title, published, evidence_class from event_sources "
            "where event_id = any(cast(:ids as text[])) order by id"
        ),
        ids,
    ).mappings():
        sources[s["event_id"]].append(s)
    cves: dict[str, list[str]] = defaultdict(list)
    for event_id, cve_id in conn.execute(
        text("select event_id, cve_id from event_cves where event_id = any(cast(:ids as text[]))"),
        ids,
    ):
        cves[event_id].append(cve_id)

    registers = {c.value for c in REGISTER_CLASSES}
    out = []
    for e in events:
        src = sources[e["event_id"]]
        anchor = min([e["first_seen"], *(s["published"] for s in src if s["published"])])
        keys = story_keys(
            e["title"],
            (s["title"] for s in src if s["title"]),
            cves[e["event_id"]],
            anchor,
            {s["source_id"] for s in src if s["evidence_class"] in registers},
            {s["source_id"] for s in src},
        )
        out.append(
            StoryRecord(
                event_id=e["event_id"],
                title=e["title"],
                first_seen=e["first_seen"],
                enriched=not e["pending_enrichment"],
                prominence=e["prominence"],
                keys=keys,
            )
        )
    return out


def load_token_weights(conn: Connection, *, since: datetime) -> TokenWeights:
    """Word weights from the headlines recorded since `since`, each headline once per event."""
    rows = conn.execute(
        text(
            "select distinct event_id, title_hash, title from event_sources "
            "where title is not null and created_at >= :since"
        ),
        {"since": since},
    )
    return TokenWeights.from_titles(r[2] for r in rows)


def load_live_ids(conn: Connection, *, min_prominence: float) -> list[str]:
    rows = conn.execute(
        text(
            "select event_id from events where merged_into is null and prominence > :min "
            "order by event_id"
        ),
        {"min": min_prominence},
    )
    return [r[0] for r in rows]


def merge_events(conn: Connection, group: MergeGroup) -> None:
    """Fold `group.losers` into `group.winner` (see the module docstring)."""
    p: dict[str, Any] = {"w": group.winner, "l": list(group.losers)}
    losers = "event_id = any(cast(:l as text[]))"

    def run(sql: str, params: dict[str, Any] = p) -> None:
        conn.execute(text(sql), params)

    # The winner's dates first, while the losers' rows still say what they were. As at ingest
    # (plan_update), another outlet's report is not a material update; a CVE the winner did
    # not have is, so only a loser bringing one can move `last_material_update`.
    run(
        "update events w set first_seen = least(w.first_seen, m.first_seen), "
        "last_seen = greatest(w.last_seen, m.last_seen), "
        "last_material_update = greatest(w.last_material_update, "
        "  least(w.first_seen, m.first_seen), m.lmu), "
        "categories = w.categories || array(select distinct c from events l, "
        "  unnest(l.categories) c where l." + losers + " and not c = any(w.categories) "
        "  order by c), "
        "updated_at = now() "
        "from (select min(first_seen) first_seen, max(last_seen) last_seen, "
        "      max(last_material_update) filter (where exists ("
        "        select 1 from event_cves c where c.event_id = l.event_id "
        "        and c.cve_id not in (select cve_id from event_cves where event_id = :w))) lmu "
        "      from events l where l." + losers + ") m "
        "where w.event_id = :w"
    )
    if group.title is not None:
        run(
            "update events set title = :t, normalised_title = :nt where event_id = :w",
            {"w": group.winner, "t": group.title, "nt": normalise_title(group.title)},
        )

    # Sources: an article already on the winner (or on an earlier loser) is not added twice.
    run(
        "delete from event_sources s using event_sources t "
        "where s." + losers + " and t.url_hash = s.url_hash and t.id <> s.id "
        "and (t.event_id = :w or (t.event_id = any(cast(:l as text[])) and t.id < s.id))"
    )
    # Which of them are independent now is the lineage pass's to say (worker/db/lineage.py).
    run("update event_sources set event_id = :w where " + losers)

    run(
        "insert into event_cves (event_id, cve_id) select distinct :w, cve_id from event_cves "
        "where " + losers + " on conflict (event_id, cve_id) do nothing"
    )
    run("delete from event_cves where " + losers)

    # The timeline: only the group's earliest report stays "first reported".
    run("update event_timeline set event_id = :w where " + losers)
    run(
        "update event_timeline t set type = 'NEW_EVIDENCE', "
        "summary = :also || substr(t.summary, length(:first) + 1) "
        "where t.event_id = :w and t.type = 'NEW_FACT' and starts_with(t.summary, :first) "
        "and t.id <> (select id from event_timeline where event_id = :w and type = 'NEW_FACT' "
        "  and starts_with(summary, :first) order by ts, id limit 1)",
        {**p, "first": _FIRST_REPORT, "also": _ALSO_REPORTED},
    )

    # Claims with the same words are one claim: the winner's, else the earliest. A duplicate's
    # evidence moves to the claim kept before the duplicate goes (deleting it would cascade).
    run(
        "with dup as ("
        "  select c.id, (select d.id from claims d where lower(d.text) = lower(c.text) "
        "    and (d.event_id = :w or (d.event_id = any(cast(:l as text[])) and d.id < c.id)) "
        "    order by d.event_id = :w desc, d.id limit 1) keep "
        "  from claims c where c." + losers + ""
        ") update evidence e set claim_id = dup.keep from dup "
        "where e.claim_id = dup.id and dup.keep is not null"
    )
    run(
        "delete from claims c using claims d "
        "where c." + losers + " and d.id <> c.id and lower(d.text) = lower(c.text) "
        "and (d.event_id = :w or (d.event_id = any(cast(:l as text[])) and d.id < c.id))"
    )
    # A loser's open follow-up task is cancelled, not moved: an event has one open task of each
    # kind (migration 012), and the next sweep gives the winner what it needs.
    run(
        "update followup_tasks set status = 'cancelled', updated_at = now() where " + losers
        + " and status in ('pending', 'in_progress')"
    )
    for table in ("claims", "evidence", "followup_tasks"):
        run(f"update {table} set event_id = :w where " + losers)

    run(
        "delete from mitre_techniques m using mitre_techniques d "
        "where m." + losers + " and d.id <> m.id and d.technique_id = m.technique_id "
        "and d.dataset_version = m.dataset_version "
        "and (d.event_id = :w or (d.event_id = any(cast(:l as text[])) and d.id < m.id))"
    )
    run("update mitre_techniques set event_id = :w where " + losers)

    # Relationships point at the winner now; one that would point at itself goes.
    run(
        "insert into event_relationships (event_id, related_event_id, type) "
        "select distinct e, r, type from ("
        "  select case when event_id = any(cast(:l as text[])) then :w else event_id end e, "
        "         case when related_event_id = any(cast(:l as text[])) then :w "
        "              else related_event_id end r, type "
        "  from event_relationships "
        "  where event_id = any(cast(:l as text[])) or related_event_id = any(cast(:l as text[]))"
        ") x where e <> r on conflict (event_id, related_event_id, type) do nothing"
    )
    run(
        "delete from event_relationships where event_id = any(cast(:l as text[])) "
        "or related_event_id = any(cast(:l as text[]))"
    )

    run(
        "update events set status = 'archived', merged_into = :w, updated_at = now() "
        "where " + losers
    )
    # Anything merged into a loser earlier now points at the winner directly.
    run("update events set merged_into = :w where merged_into = any(cast(:l as text[]))")


def merged_redirects(conn: Connection, *, since: datetime) -> dict[str, str]:
    """`loser -> winner` for events first seen since `since` that were merged away."""
    rows = conn.execute(
        text(
            "select event_id, merged_into from events "
            "where merged_into is not null and first_seen >= :since order by event_id"
        ),
        {"since": since},
    )
    return {r[0]: r[1] for r in rows}


def merged_count(conn: Connection, event_ids: Sequence[str]) -> int:
    """How many of these events are merged away (a test and report helper)."""
    return conn.execute(
        text(
            "select count(*) from events where merged_into is not null "
            "and event_id = any(cast(:ids as text[]))"
        ),
        {"ids": list(event_ids)},
    ).scalar_one()
