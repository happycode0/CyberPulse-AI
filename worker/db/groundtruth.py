"""Persisting looked-up facts about CVEs: KEV listings, EPSS scores, CVSS base scores.

`worker/groundtruth/` reads the published registers and answers questions about a CVE. This module
is where those answers are written down, and it exists separately because the honest-data rule of
PLAN.md §2 is mostly a rule about *writes*:

- A CVE absent from a register is never written as a zero. An unscored CVE gets an `epss` row with
  `status='unknown'` and a NULL score, which is the shape `001_initial.sql` describes; a CVE with
  no CVSS anywhere gets no `cve_scores` row at all, because that table's check constraint forbids
  a `cvss` row without a score.
- A row is only written when the answer *changed*. `cve_scores` is history, so re-recording an
  unchanged score every few hours would turn it into a log of how often the worker polled, and the
  question the history exists to answer — when did this score move — would then need a reader who
  knows the polling cadence to make sense of it.
- Severity is only ever written when there is a measurement behind it. `set_event_severity` never
  writes `unknown` over a stored answer: a failed lookup must not erase a severity that something
  else established.

Only CVEs the pipeline has already seen are annotated. The registers between them cover around
300,000 CVEs while `cves` holds the few thousand some advisory actually mentioned; importing the
rest would turn a table of "vulnerabilities this platform has reported on" into a partial mirror of
NVD, and nothing on the site asks a question that needs one.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import Connection, text

from worker.groundtruth.kev import KevCatalogue
from worker.groundtruth.severity import severity_from_cvss
from worker.models import CvssScore, EpssScore, Severity, SeveritySource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KevResult:
    """What `apply_kev` changed.

    Attributes
    ----------
    updated : int
        CVEs whose listing or dates now differ from what was stored.
    delisted : int
        CVEs previously recorded as listed that the catalogue no longer carries.
    unchanged : int
        CVEs already in agreement with the catalogue.
    delist_withheld : int
        CVEs that *would* have been de-listed but were left alone because the catalogue did not
        look complete. See `apply_kev`.
    changed_cves : tuple[str, ...]
        The CVEs actually written, listed and de-listed together. KEV is an input to `urgency`, so
        these are the CVEs whose events now hold a stale score — `events_for_cves` turns them into
        the `touched` set `pipeline.run.rescore` needs.

        Excluded from the repr. The first live sync wrote 1,731 listings, and because this result is
        logged as a whole dataclass by both `sync_groundtruth` and `worker.main`, every id appeared
        in the log twice — some 140 KB for one line that was supposed to be a summary. Nothing is
        lost: `len(changed_cves)` is `updated + delisted`, both of which are in the repr.
    """

    updated: int = 0
    delisted: int = 0
    unchanged: int = 0
    delist_withheld: int = 0
    changed_cves: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ScoreResult:
    """A score sync's tally: `recorded` rows written, `unchanged` CVEs left alone."""

    recorded: int = 0
    unchanged: int = 0


@dataclass(frozen=True)
class LatestScore:
    """The newest `cve_scores` row for one CVE and kind, as the change check needs it."""

    score: float | None
    vector: str | None
    source: str | None
    status: str


def known_cve_ids(conn: Connection) -> list[str]:
    """Every CVE the pipeline has seen, in a stable order."""
    return [r[0] for r in conn.execute(text("select cve_id from cves order by cve_id"))]


def apply_kev(conn: Connection, catalogue: KevCatalogue) -> KevResult:
    """Write the catalogue's answer onto every CVE already known.

    De-listing is the delicate half. CISA does occasionally withdraw an entry, so a CVE recorded as
    listed that the catalogue no longer carries should stop being reported as exploited — but a
    *truncated* download looks exactly the same, arriving as valid JSON with a short
    `vulnerabilities` array, and would silently clear thousands of true listings. Absence is
    therefore only acted on when `KevCatalogue.is_complete` holds, meaning CISA's own declared count
    agrees with what was parsed. When it does not, stored listings are kept and counted in
    `delist_withheld` — the catalogue is still trusted to *add*, because an entry that is present
    was really published.

    Returns
    -------
    KevResult
        Counts per outcome. Only changed rows are written, so an unchanged catalogue costs one read
        and no writes.
    """
    trust_absence = catalogue.is_complete
    if not trust_absence:
        logger.warning(
            "KEV catalogue looks incomplete (declared %s, parsed %d with %d skipped); "
            "adding listings but removing none",
            catalogue.declared_count,
            len(catalogue.entries),
            catalogue.skipped,
        )

    rows = conn.execute(
        text("select cve_id, kev_listed, kev_date_added, kev_due_date from cves order by cve_id")
    ).all()

    updated = delisted = unchanged = withheld = 0
    changed: list[str] = []
    for cve_id, was_listed, had_added, had_due in rows:
        entry = catalogue.lookup(cve_id)
        if entry.listed:
            if (was_listed, had_added, had_due) == (True, entry.date_added, entry.due_date):
                unchanged += 1
            else:
                _write_kev(conn, cve_id, True, entry.date_added, entry.due_date)
                changed.append(cve_id)
                updated += 1
        elif not was_listed:
            unchanged += 1
        elif trust_absence:
            _write_kev(conn, cve_id, False, None, None)
            changed.append(cve_id)
            delisted += 1
        else:
            withheld += 1

    return KevResult(
        updated=updated,
        delisted=delisted,
        unchanged=unchanged,
        delist_withheld=withheld,
        changed_cves=tuple(changed),
    )


def _write_kev(
    conn: Connection,
    cve_id: str,
    listed: bool,
    date_added: date | None,
    due_date: date | None,
) -> None:
    conn.execute(
        text(
            "update cves set kev_listed = :listed, kev_date_added = :added, "
            "kev_due_date = :due, updated_at = now() where cve_id = :cve_id"
        ),
        {"cve_id": cve_id, "listed": listed, "added": date_added, "due": due_date},
    )


def latest_scores(conn: Connection, kind: str) -> dict[str, LatestScore]:
    """The newest row per CVE for one score kind.

    `distinct on` ordered by `observed_at desc, id desc` is what makes "newest" well defined when
    rows share a timestamp — which they always do within one sync, since `now()` is fixed for the
    whole transaction in Postgres. Without the `id` tie-break the "did it change" comparison would
    read an arbitrary row from the batch and could rewrite the same value forever.
    """
    rows = conn.execute(
        text(
            "select distinct on (cve_id) cve_id, score, vector, source, status from cve_scores "
            "where kind = :kind order by cve_id, observed_at desc, id desc"
        ),
        {"kind": kind},
    ).all()
    return {r[0]: LatestScore(score=r[1], vector=r[2], source=r[3], status=r[4]) for r in rows}


def latest_score(conn: Connection, cve_id: str, kind: str) -> LatestScore | None:
    """The newest row for one CVE and kind, or None if it has never been scored.

    The one-CVE form rather than `latest_scores`, because the CVSS sync asks this per CVE and
    `ix_cve_scores_cve_kind_observed` is `(cve_id, kind, observed_at desc)` — exactly this query.
    Reaching for the bulk version here would read every score in the table once per CVE in the
    batch, which is the kind of thing that works fine on a test fixture and not at all on a year
    of history.
    """
    row = conn.execute(
        text(
            "select score, vector, source, status from cve_scores "
            "where cve_id = :cve_id and kind = :kind order by observed_at desc, id desc limit 1"
        ),
        {"cve_id": cve_id, "kind": kind},
    ).first()
    if row is None:
        return None
    return LatestScore(score=row[0], vector=row[1], source=row[2], status=row[3])


def record_epss(conn: Connection, scores: Mapping[str, EpssScore]) -> ScoreResult:
    """Record today's EPSS answer for every known CVE, writing only what moved.

    An unscored CVE gets a row too — `score=NULL, status='unknown'` — but only the first time, so
    the absence is on the record without being re-asserted every run. That row is the difference
    between "EPSS has not modelled this CVE" and "nobody has looked", and the site needs it to say
    `unknown` rather than leaving a gap a reader could take for 0%.

    Parameters
    ----------
    scores : Mapping[str, EpssScore]
        `EpssSnapshot.scores` — scored CVEs only. A CVE missing from the mapping is treated as
        unscored, which is what absence from the snapshot means.
    """
    latest = latest_scores(conn, "epss")
    recorded = unchanged = 0
    for cve_id in known_cve_ids(conn):
        answer = scores.get(cve_id) or EpssScore()
        previous = latest.get(cve_id)
        if previous is not None and (previous.score, previous.status) == (
            answer.score,
            answer.status,
        ):
            unchanged += 1
            continue
        conn.execute(
            text(
                "insert into cve_scores (cve_id, kind, score, status) "
                "values (:cve_id, 'epss', :score, :status)"
            ),
            {"cve_id": cve_id, "score": answer.score, "status": answer.status},
        )
        recorded += 1
    return ScoreResult(recorded=recorded, unchanged=unchanged)


def record_cvss(conn: Connection, cve_id: str, cvss: CvssScore) -> bool:
    """Record a resolved CVSS base score, returning whether a row was written.

    Unlike EPSS there is no "unknown" row to write: the `cve_scores` check constraint requires a
    `cvss` row to carry a score, precisely so an unscored CVE cannot be filed as a 0.0. A CVE with
    no score anywhere is recorded in `cve_cvss_checks` instead — see `record_cvss_check`.

    The vector is part of the change check, not just the score. Two different vectors can compute
    to the same number, and the vector is what a reader needs to disagree with the banding, so a
    CNA replacing one with another is a real change even when the score stands still.
    """
    previous = latest_score(conn, cve_id, "cvss")
    if previous is not None and (previous.score, previous.vector, previous.source) == (
        cvss.score,
        cvss.vector,
        cvss.source,
    ):
        return False
    conn.execute(
        text(
            "insert into cve_scores (cve_id, kind, score, vector, source, status) "
            "values (:cve_id, 'cvss', :score, :vector, :source, 'known')"
        ),
        {"cve_id": cve_id, "score": cvss.score, "vector": cvss.vector, "source": cvss.source},
    )
    return True


def record_cvss_check(
    conn: Connection, cve_id: str, outcome: str, detail: str | None = None
) -> None:
    """Note that this CVE was looked up, and what came back.

    `outcome` is one of 'scored', 'unscored', 'absent' or 'error' — `cve_cvss_checks` enforces
    that rather than this function, so a typo fails the write instead of quietly creating a fifth
    outcome that `RECHECK_HOURS` has no interval for and that would therefore never be retried.
    """
    conn.execute(
        text(
            "insert into cve_cvss_checks (cve_id, checked_at, outcome, detail) "
            "values (:cve_id, now(), :outcome, :detail) "
            "on conflict (cve_id) do update set checked_at = excluded.checked_at, "
            "outcome = excluded.outcome, detail = excluded.detail"
        ),
        {"cve_id": cve_id, "outcome": outcome, "detail": detail},
    )


# How long an answer stands before the CVE is asked about again, keyed by what the last answer was.
# A CNA can add a CVSS metric days after reserving the id and CISA's ADP enrichment arrives later
# still, so 'unscored' and 'absent' are provisional and have to expire. A published 'scored' barely
# ever moves. An 'error' said nothing about the CVE at all, so it is retried soonest.
RECHECK_HOURS: Mapping[str, int] = {
    "scored": 14 * 24,
    "unscored": 48,
    "absent": 72,
    "error": 6,
}

# Built from RECHECK_HOURS so the two cannot drift. The outcome names are a closed set the database
# already constrains, and only the hours are bound as parameters, so this is not a place user input
# reaches. An outcome with no interval falls to the ELSE and is treated as due now, which is the
# safe direction: a new outcome would be re-checked too eagerly rather than never again.
_RECHECK_CASE = (
    "case k.outcome "
    + " ".join(f"when '{name}' then :h_{name}" for name in RECHECK_HOURS)
    + " else 0 end"
)


def cves_due_for_cvss(conn: Connection, *, now: datetime, limit: int) -> list[str]:
    """The next CVEs to look up, never-checked ones first, capped at `limit`.

    The cap is the point. Resolving CVSS costs one request per CVE, so an uncapped sync would fire
    several thousand requests at the register the first time it ran — both rude and the quickest
    way to be rate-limited into looking like an outage. A bounded batch per run spreads the
    backfill over a few days and then settles into whatever `RECHECK_HOURS` asks for.

    Never-checked CVEs sort first, because they have no recorded answer at all while a re-check is
    only confirming one. Among those, the CVEs on the most recently seen events go first — and that
    tie-break is the whole difference between a useful first pass and a useless one. Ordering by
    `cve_id` instead, as the obvious reading of "stable order" suggests, sorts `CVE-2002-…` ahead of
    `CVE-2026-…`: the first live run here resolved 400 CVEs between CVE-2002-0367 and CVE-2018-19953
    and left every CVE on the front page unscored. Only 73 distinct CVEs belonged to events first
    seen in the preceding week, so in event order the entire visible site is banded inside the first
    fifth of one batch and the decade-old backlog fills in behind it.
    """
    sql = (
        "select c.cve_id from cves c "
        "left join cve_cvss_checks k on k.cve_id = c.cve_id "
        "left join ("
        "  select ec.cve_id, max(e.first_seen) as newest_event from event_cves ec "
        "  join events e on e.event_id = ec.event_id group by ec.cve_id"
        ") m on m.cve_id = c.cve_id "
        "where k.checked_at is null or k.checked_at < cast(:now as timestamptz) - "
        f"make_interval(hours => {_RECHECK_CASE}) "
        "order by k.checked_at nulls first, m.newest_event desc nulls last, c.cve_id limit :limit"
    )
    params: dict[str, object] = {"now": now, "limit": limit}
    params.update({f"h_{name}": hours for name, hours in RECHECK_HOURS.items()})
    return [r[0] for r in conn.execute(text(sql), params)]


def events_for_cves(conn: Connection, cve_ids: Sequence[str]) -> list[str]:
    """Every event mentioning any of these CVEs.

    Used to turn "these CVEs changed" into "these events need rescoring". Returns [] for an empty
    input rather than issuing a query whose `= any('{}')` would match nothing anyway.
    """
    if not cve_ids:
        return []
    rows = conn.execute(
        text(
            "select distinct event_id from event_cves "
            "where cve_id = any(cast(:ids as text[])) order by event_id"
        ),
        {"ids": sorted(set(cve_ids))},
    )
    return [r[0] for r in rows]


def set_event_severity(conn: Connection) -> list[str]:
    """Band every event from the worst CVSS among its CVEs, returning the ids that changed.

    "Worst" rather than "first" or "mean": an advisory that mentions five CVEs is as serious as the
    most serious thing in it, and a reader scanning a severity column is asking what the biggest
    problem is. The score that won is recorded through `severity_from_cvss`, so the published
    `severity_source` names the rung of PLAN.md §2.5's chain the winning number came from rather
    than the rung of some other CVE in the same advisory.

    Events with no scored CVE are left exactly as they are. That is the honest-data rule applied to
    an UPDATE: a missing lookup is not evidence, so it must not overwrite a severity that a vendor
    rating or a later Stage 2 estimate put there. The column already defaults to 'unknown', so an
    event nothing has established a severity for still reads as unknown without this function
    asserting it.
    """
    rows = conn.execute(
        text(
            "with newest as ("
            "  select distinct on (cve_id) cve_id, score, source from cve_scores "
            "  where kind = 'cvss' order by cve_id, observed_at desc, id desc"
            "), worst as ("
            "  select distinct on (ec.event_id) ec.event_id, n.score, n.source "
            "  from event_cves ec join newest n on n.cve_id = ec.cve_id "
            "  order by ec.event_id, n.score desc, n.cve_id"
            ") "
            "select e.event_id, e.severity, e.severity_source, w.score, w.source "
            "from events e join worst w on w.event_id = e.event_id"
        )
    ).all()

    changed: list[str] = []
    for event_id, stored_severity, stored_source, score, source in rows:
        # `cve_scores.source` is nullable, and SeveritySource has a member for exactly this case, so
        # a row with no recorded provider becomes unknown provenance rather than a failed parse.
        winner = CvssScore(score=score, source=source or SeveritySource.UNKNOWN.value)
        severity, severity_source = severity_from_cvss(winner)
        if severity is Severity.UNKNOWN:
            # Unreachable while `cve_scores` constrains a cvss score to [0, 10], which is what
            # `severity_for_score` rejects. Kept because the alternative if that ever changes is
            # writing `unknown` over a severity something else established.
            continue
        if (stored_severity, stored_source) == (severity.value, severity_source.value):
            continue
        conn.execute(
            text(
                "update events set severity = :severity, severity_source = :source, "
                "updated_at = now() where event_id = :event_id"
            ),
            {
                "event_id": event_id,
                "severity": severity.value,
                "source": severity_source.value,
            },
        )
        changed.append(event_id)
    return changed
