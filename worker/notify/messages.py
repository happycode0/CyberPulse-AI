"""The words of each notification, as plain text, from data the worker already holds.

These are pure functions, so every message can be read in a test without a database or a bot.
"""

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from worker.db.digest import ESCALATE_AU_RELEVANCE
from worker.db.discovery import Activation
from worker.db.incidents import Incident
from worker.db.notifications import AlertEvent, UpdateEntry
from worker.db.scout import Proposal, ScanNotice
from worker.watchdog.checks import KIND_GUIDE, span

SYDNEY = ZoneInfo("Australia/Sydney")
DIGEST_TOP = 5
DIGEST_WATCH = 8
DIGEST_SOURCES = 10
TITLE_MAX_CHARS = 160
_SPACE = re.compile(r"\s+")


def event_url(site_url: str, event_id: str) -> str:
    return f"{site_url.rstrip('/')}/event.html?id={event_id}"


def _sydney(at: datetime) -> datetime:
    return at.astimezone(SYDNEY)


def _day(at: datetime) -> str:
    local = _sydney(at)
    return f"{local:%a} {local.day} {local:%b %Y}"


def _title(title: str) -> str:
    title = _SPACE.sub(" ", title).strip()
    return title if len(title) <= TITLE_MAX_CHARS else title[: TITLE_MAX_CHARS - 1] + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _au(relevance: float | None, direct: bool) -> str:
    parts = []
    if relevance is not None:
        parts.append(f"AU {relevance:.2f}")
    if direct:
        parts.append("reported in Australia")
    return ", ".join(parts)


def _event_lines(row: dict[str, Any], site_url: str, prefix: str) -> list[str]:
    au = _au(row["au_relevance"], row["au_directly_reported"])
    head = f"{prefix}{row['severity'].upper()} · {_title(row['title'])}"
    indent = " " * len(prefix)
    return [head + (f" ({au})" if au else ""), indent + event_url(site_url, row["event_id"])]


def daily_digest(digest: dict[str, Any], *, site_url: str) -> str:
    """The morning digest, from `worker.db.digest.load_digest`."""
    window = digest["window"]
    events = digest["events"]
    lines = [
        f"CyberPulse-AI · daily digest · {_day(window['to'])}",
        f"The {window['hours']} hours to {_sydney(window['to']):%H:%M} Sydney time.",
        "",
        "EVENTS",
        (
            f"{events['new']} new, {events['updated']} updated, {events['merged']} merged; "
            f"{events['standing']} standing."
        ),
    ]
    if events["new_archived_on_arrival"]:
        lines.append(
            f"{events['new_archived_on_arrival']} of the new were a feed's back catalogue, "
            "archived on arrival."
        )

    lines += ["", "MOST PROMINENT NOW"]
    top = digest["top"][:DIGEST_TOP]
    for i, row in enumerate(top, 1):
        lines += _event_lines(row, site_url, f"{i}. ")
    if not top:
        lines.append("Nothing is prominent enough for the live page.")

    watch = digest["escalation_candidates"]
    lines += ["", f"TO WATCH (critical, or Australian relevance {ESCALATE_AU_RELEVANCE}+)"]
    for row in watch[:DIGEST_WATCH]:
        lines += _event_lines(row, site_url, "- ")
    if len(watch) > DIGEST_WATCH:
        lines.append(f"… and {len(watch) - DIGEST_WATCH} more on the site.")
    if not watch:
        lines.append("Nothing new or updated.")

    lines += ["", "SOURCES"]
    changes = digest["source_health_changes"]
    for change in changes[:DIGEST_SOURCES]:
        lines.append(f"- {change['source_id']}: {change['was'] or 'new'} → {change['now']}")
    if len(changes) > DIGEST_SOURCES:
        lines.append(f"… and {len(changes) - DIGEST_SOURCES} more.")
    if not changes:
        lines.append("No source changed status.")

    lines += ["", "COLLECTION"]
    runs = digest["runs"]
    for lane, lane_runs in runs.items():
        line = (
            f"- {lane}: {_plural(lane_runs['runs'], 'run')}, "
            f"{_plural(lane_runs['new_events'], 'new event')}, "
            f"{_plural(lane_runs['errors'], 'error')}"
        )
        if lane_runs["unfinished"]:
            line += f", {lane_runs['unfinished']} unfinished"
        lines.append(line)
    if not runs:
        lines.append("⚠ No collection ran in the window.")

    cost = digest["cost_this_month"]
    spent = f"${cost['cost_usd']:.2f}" if cost["cost_usd"] is not None else "an unknown amount"
    lines += [
        "",
        f"AI THIS MONTH ({cost['month']})",
        f"{_plural(cost['calls'], 'call')}, {spent}.",
        "",
        site_url,
    ]
    return "\n".join(lines)


def critical_au_alert(event: AlertEvent, *, site_url: str) -> str:
    """One event, as soon as it is seen: critical or known exploited, and Australian."""
    why = "critical" if event.severity == "critical" else "known exploited"
    au = _au(event.au_relevance, event.au_directly_reported)
    lines = [
        f"CyberPulse-AI · {why} · Australia",
        "",
        f"{event.severity.upper()} · {_title(event.title)}",
    ]
    if au:
        lines.append(au)
    if event.kev_cves:
        lines.append(f"Known exploited (CISA KEV): {', '.join(event.kev_cves[:5])}")
    seen = _sydney(event.first_seen)
    lines += [
        f"First seen {seen:%H:%M} Sydney time, {_day(event.first_seen)} · {event.status}",
        "",
        event_url(site_url, event.event_id),
    ]
    return "\n".join(lines)


CHANGE_WORDS = {
    "NEW_FACT": "New",
    "NEW_CVE": "New CVE",
    "NEW_EXPLOIT": "Exploit published",
    "EXPLOIT_CONFIRMED": "Exploitation confirmed",
    "NEW_ACTOR": "Actor named",
    "NEW_TARGET": "New targets",
    "NEW_GEOGRAPHY": "New region",
    "NEW_AU_EXPOSURE": "Australian exposure",
    "NEW_IMPACT": "Impact",
    "NEW_PATCH": "Patch out",
    "NEW_MITIGATION": "Mitigation out",
    "CORRECTION": "Correction",
}


def developing_update(entry: UpdateEntry, *, site_url: str) -> str:
    """One change to an event that matters to Australia, as the worker records it."""
    au = _au(entry.au_relevance, entry.au_directly_reported)
    lines = [
        "CyberPulse-AI · update · Australia",
        "",
        f"{entry.severity.upper()} · {_title(entry.title)}",
    ]
    if au:
        lines.append(au)
    what = CHANGE_WORDS.get(entry.type, entry.type.replace("_", " ").capitalize())
    lines += ["", f"{what}: {_SPACE.sub(' ', entry.summary).strip()}"]
    if entry.link:
        lines.append(f"Source: {entry.link}")
    at = _sydney(entry.created_at)
    lines += [
        f"Status {entry.status} · {at:%H:%M} Sydney time, {_day(entry.created_at)}",
        "",
        event_url(site_url, entry.event_id),
    ]
    return "\n".join(lines)


def source_activated(source: Activation, *, site_url: str) -> str:
    """A feed SERAPH's gate let in, collected from the next NORMAL run."""
    by = "the nightly search" if source.found_by == "search" else "TACHIKOMA"
    where = " · Australia" if source.region == "au" else ""
    lines = [
        "CyberPulse-AI · new source",
        "",
        f"{_title(source.name)} · {source.host}{where}",
        (f"Found by {by}; passed SERAPH's gate with {_plural(source.passes, 'healthy probe')} "
         "in a row."),
    ]
    last = source.last_result or {}
    if isinstance(last.get("recent"), int) and isinstance(last.get("on_beat"), int):
        lines.append(
            f"Last probe: {_plural(last['recent'], 'recent item')}, {last['on_beat']} on the beat."
        )
    lines += [
        ("Collected from the next normal run as community evidence, which never confirms an "
         "event on its own."),
        "",
        site_url,
    ]
    return "\n".join(lines)


# ─── Models and money (since RIPPERDOC was retired, 2026-10-20) ───────────────────────────────────
#
# RIPPERDOC passed these on from the worker's own figures. The worker now says them itself, in
# its own words: the price guard's reasons, the gauntlet's proposal and the ledger's sums.

COST_ROWS = 6


def ladder_changed(notice: ScanNotice) -> str:
    """What one night's model scan took out of the ladder or let back in. A drop is the price
    guard's verdict on every route (worker/ai/ladder.py), never a headline price."""
    dropped = [c for c in notice.changes if c.kind == "dropped"]
    restored = [c for c in notice.changes if c.kind == "restored"]
    at = _sydney(notice.scanned_at)
    lines = [
        "CyberPulse-AI · model ladder",
        f"The model scan at {at:%H:%M} Sydney time, {_day(notice.scanned_at)}.",
    ]
    if dropped:
        lines += ["", "DROPPED: no route for it passes the price guard"]
        lines += [f"- {c.tier} · {c.slug}: {c.reason or 'no reason recorded'}" for c in dropped]
    if restored:
        lines += ["", "BACK: it passes the guard again"]
        lines += [f"- {c.tier} · {c.slug}" for c in restored]
    lines.append("")
    if not notice.usable:
        lines.append(
            "What is left is not a usable ladder, so AI enrichment is off until "
            "config/models.yaml (config/models.mixroute.yaml on MixRoute) is fixed. "
            "Collection and publishing carry on without it."
        )
    elif dropped:
        lines.append(
            "Enrichment already goes down each tier's chain without them, which needs no "
            "approval. If a tier's default is gone for good, edit config/models.yaml "
            "(config/models.mixroute.yaml on MixRoute)."
        )
    else:
        lines.append("Enrichment can use them again from its next pass.")
    if dropped:
        lines.append(
            "The Paperclip agents' models are set on each agent's page, not by this ladder: if "
            "an agent runs a dropped model, change it there."
        )
    return "\n".join(lines)


def model_proposal(proposal: Proposal) -> str:
    """A model the weekly gauntlet proposes, as it proposed it. The body is the worker's own
    words and figures (worker/ai/gauntlet.py `proposal_body`), and the owner decides."""
    return "\n".join(
        ["CyberPulse-AI · model proposal", "", _title(proposal.title), "", proposal.body.strip()]
    )


def _money(value: float | None) -> str:
    if value is None:
        return "an unknown amount"
    return f"${value:,.2f}" if value >= 1 else f"${value:.4f}"


def _cost_rows(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        f"- {r['key']}: {_plural(r['calls'], 'call')}, {_money(r['cost_usd'])}"
        for r in rows[:COST_ROWS]
    ]
    if len(rows) > COST_ROWS:
        lines.append(f"… and {len(rows) - COST_ROWS} more.")
    return lines


def monthly_cost(report: dict[str, Any]) -> str:
    """Last month's cost ledger, from `worker.db.digest.cost_report`: what `GET /ops/cost`
    answers for that month."""
    month = datetime.strptime(report["month"], "%Y-%m")
    ledger = report["ledger"]
    lines = [
        f"CyberPulse-AI · AI cost · {month:%B %Y}",
        (
            "The worker's cost ledger: every model call it made in the month (UTC), as "
            "OpenRouter billed it, or on MixRoute, its tokens at the reviewed prices."
        ),
        "",
        "TOTAL",
    ]
    if not ledger["calls"]:
        lines.append("No model calls were recorded.")
    else:
        lines.append(f"{_plural(ledger['calls'], 'call')}, {_money(ledger['cost_usd'])}.")
        if ledger["unknown_cost_calls"]:
            lines.append(
                f"{_plural(ledger['unknown_cost_calls'], 'call')} with no billed cost, so the "
                "total is a floor."
            )
        lines += [
            f"{_plural(r['calls'], 'call')} ended {str(r['key']).replace('_', ' ')}."
            for r in report["by_outcome"]
            if r["key"] != "ok"
        ]
        lines += ["", "BY STAGE", *_cost_rows(report["by_stage"])]
        lines += ["", "BY MODEL", *_cost_rows(report["by_model"])]
    lines += [
        "",
        (
            "The Paperclip agents' own runs are not in the ledger: their spend is on "
            "Paperclip's Budgets page and the AI provider's usage page."
        ),
    ]
    return "\n".join(lines)


# ─── The watchdog (Stage 6) ───────────────────────────────────────────────────────────────────────


def _incident_head(incident: Incident) -> list[str]:
    subject = f" · {incident.subject}" if incident.subject else ""
    return [f"INC-{incident.id} · {incident.kind}{subject}", _title(incident.title)]


def _diagnosis_lines(diagnosis: dict[str, Any] | None) -> list[str]:
    """The worker's first diagnosis of an incident (worker/ai/diagnosis.py), labelled as AI."""
    if not diagnosis:
        return []
    whose = "the board" if diagnosis.get("owner") == "board" else "a code change"
    return [
        "",
        f"Likely cause (AI): {diagnosis['likely_cause']}",
        f"Suggested fix: {diagnosis['suggested_fix']}",
        f"Whose: {whose}",
    ]


def incident_opened(incident: Incident, *, crew: bool) -> str:
    """An incident the watchdog opened, or opened again, at high or critical."""
    again = ""
    if incident.reopened:
        again = f" · reopened ({_plural(incident.reopened, 'time')})"
    opened = _sydney(incident.last_seen or incident.opened_at)
    lines = [
        f"CyberPulse-AI · incident · {incident.severity.upper()}",
        "",
        *_incident_head(incident),
        f"Seen {opened:%H:%M} Sydney time, {_day(opened)}{again}",
        "",
        KIND_GUIDE.get(incident.kind, ""),
    ]
    lines += _diagnosis_lines(incident.diagnosis)
    if crew:
        lines += ["", "The crew has been told: TELETRAAN reads it from GET /ops/incidents."]
    if incident.fix_failures:
        lines.append(f"Fixes that failed TELETRAAN's tests so far: {incident.fix_failures}.")
    return "\n".join(lines)


def incident_breaker(incident: Incident) -> str:
    """The breaker tripped: the crew stops, and a person decides."""
    return "\n".join(
        [
            "CyberPulse-AI · incident · needs a human",
            "",
            *_incident_head(incident),
            "",
            (
                f"{incident.fix_failures} fix{'' if incident.fix_failures == 1 else 'es'} "
                "failed TELETRAAN's tests, so the crew has "
                "stopped work on it and the ops API takes no more verdicts. A person decides "
                "what happens next."
            ),
        ]
    )


def incident_resolved(incident: Incident) -> str:
    """A high or critical incident the watchdog no longer sees."""
    opened = incident.opened_at
    resolved = incident.resolved_at or incident.last_seen or opened
    return "\n".join(
        [
            "CyberPulse-AI · incident resolved",
            "",
            *_incident_head(incident),
            (
                f"Open from {_sydney(opened):%H:%M} {_day(opened)} to "
                f"{_sydney(resolved):%H:%M} {_day(resolved)} Sydney time "
                f"({span(resolved - opened)})."
            ),
        ]
    )


def database_unreachable(since: datetime) -> str:
    return "\n".join(
        [
            "CyberPulse-AI · system failure",
            "",
            (
                f"The worker has not reached its database since {_sydney(since):%H:%M} Sydney "
                f"time, {_day(since)}. Nothing is collected or published until it does, and "
                "the watchdog cannot record incidents."
            ),
        ]
    )


def database_back(since: datetime, now: datetime) -> str:
    return "\n".join(
        [
            "CyberPulse-AI · system recovered",
            "",
            (
                f"The worker reaches its database again, after {span(now - since)} without it "
                f"(from {_sydney(since):%H:%M} Sydney time)."
            ),
        ]
    )


_GAP_KIND = {"au_sector": "Australian {topic} sector", "category": "{topic}"}


def daily_report(
    report: dict[str, Any], titles: dict[str, tuple[str, str]], *, site_url: str
) -> str:
    """The worker's daily intelligence report (worker/ai/report.py). `titles` holds each named
    event's severity and title as published; an item whose event is not in it is left out."""
    day = datetime(report["report_date"].year, report["report_date"].month,
                   report["report_date"].day, 12, tzinfo=SYDNEY)
    lines = [f"CyberPulse-AI · daily intelligence report · {_day(day)}", "", report["summary"]]
    items = [i for i in report["items"] if i["event_id"] in titles]
    if items:
        lines += ["", "TODAY"]
        for n, item in enumerate(items, 1):
            severity, title = titles[item["event_id"]]
            lines += [
                f"{n}. {severity.upper()} · {_title(title)}",
                f"   {item['why']}",
                f"   {event_url(site_url, item['event_id'])}",
            ]
    if report["gaps"]:
        lines += ["", "QUIET"]
        for gap in report["gaps"]:
            name = _GAP_KIND.get(gap["kind"], "{topic}").format(topic=gap["topic"])
            lines.append(f"- {name}: nothing new for {gap['quiet_days']:.0f} days")
    lines += [
        "",
        f"Written by AI ({report['model'] or 'a model'}) from the worker's own records. Every "
        "event it names is checked; the judgement is the model's.",
    ]
    return "\n".join(lines)
