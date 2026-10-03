"""What DECKARD may write: a follow-up report, checked before any of it is stored.

DECKARD reports through the ops API (`POST /ops/followup/<task>`, worker/ops_api.py). It reads
the open web, which can carry text written to steer it, so a report is checked as if hostile:

- A task of kind `check` is answered `no_change`, or `changed` with one to `MAX_CHANGES` changes.
  A change is one of `AGENT_TYPES`, a short plain-text summary and the https link it rests on.
- A change that happens once to an event (`ONCE_TYPES`: exploitation confirmed, a patch, ...) is
  refused if the timeline has one already, as a register's is (worker/db/material.py). So is a
  summary the timeline already has, and anything past `MAX_AGENT_ENTRIES` on one event.
- A task of kind `final_summary` is answered `summary` with the closing summary of the case.
- Text is plain: control and format characters are removed, and it may hold no URL, no CVE the
  event does not name, and nothing shaped like a credential.
- A link is https to a named host, with no user name or password. Its query and fragment are
  dropped: a query string can carry a token, and the page is the evidence, not the tracking.

A report with the wrong shape is rejected whole. A well-formed change that fails a check is
refused on its own, with the reason, and the rest are kept.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from worker.ai.tasks import _CVE, _DASH, _URL, _clean, sentences
from worker.models import MaterialChange
from worker.publish.validate import scan_text_for_secrets

MAX_CHANGES = 3
MAX_AGENT_ENTRIES = 8
SUMMARY_CHARS = (20, 280)
SUMMARY_SENTENCES = 2
FINAL_CHARS = (40, 600)
FINAL_SENTENCES = 4
URL_MAX_CHARS = 300
# How far before the event's first report a change may be dated (a patch can predate the news).
EARLIEST_BEFORE_FIRST_SEEN = timedelta(days=30)

AGENT_TYPES = frozenset({
    MaterialChange.NEW_FACT,
    MaterialChange.NEW_EXPLOIT,
    MaterialChange.EXPLOIT_CONFIRMED,
    MaterialChange.NEW_ACTOR,
    MaterialChange.NEW_TARGET,
    MaterialChange.NEW_GEOGRAPHY,
    MaterialChange.NEW_AU_EXPOSURE,
    MaterialChange.NEW_IMPACT,
    MaterialChange.NEW_PATCH,
    MaterialChange.NEW_MITIGATION,
    MaterialChange.CORRECTION,
})
ONCE_TYPES = frozenset({
    MaterialChange.NEW_EXPLOIT,
    MaterialChange.EXPLOIT_CONFIRMED,
    MaterialChange.NEW_AU_EXPOSURE,
    MaterialChange.NEW_PATCH,
    MaterialChange.NEW_MITIGATION,
})

Outcome = Literal["no_change", "changed", "summary"]
OUTCOMES: dict[str, tuple[Outcome, ...]] = {
    "check": ("no_change", "changed"),
    "final_summary": ("summary",),
}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A host name, ending in a name rather than a number: no IP addresses.
_HOST = re.compile(r"([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]([a-z0-9-]*[a-z0-9])?")
# A link written without its scheme, such as example.org/page.
_BARE_LINK = re.compile(r"\b[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}/", re.IGNORECASE)


class ReportRejected(Exception):
    """The report as a whole cannot be used: the message says why."""


@dataclass(frozen=True)
class EventContext:
    """What the checks need to know about the event."""

    cves: frozenset[str]
    types: frozenset[MaterialChange]  # on its timeline already
    summaries: frozenset[str]  # its timeline's summaries, casefolded
    agent_entries: int
    first_seen: date


@dataclass(frozen=True)
class Change:
    type: MaterialChange
    summary: str
    url: str
    happened_on: date | None  # when it happened, if DECKARD said


@dataclass(frozen=True)
class Refusal:
    index: int
    reason: str


@dataclass(frozen=True)
class Report:
    outcome: Outcome
    changes: tuple[Change, ...] = ()
    refused: tuple[Refusal, ...] = ()
    summary: str | None = None


def _fields(value: Any, what: str, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReportRejected(f"{what} must be a JSON object")
    missing = sorted(required - set(value))
    if missing:
        raise ReportRejected(f"{what} needs {', '.join(missing)}")
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise ReportRejected(f"{what} has an unknown field {unknown[0][:40]!r}")
    return value


def _string(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ReportRejected(f"{what} must be a string")
    return value


def _prose(value: str, what: str, event: EventContext, chars: tuple[int, int], most: int) -> str:
    """`value` as plain text, or ValueError saying why it cannot be used."""
    text = _clean(value)
    low, high = chars
    if not low <= len(text) <= high:
        raise ValueError(f"{what} must be {low} to {high} characters")
    if sentences(text) > most:
        raise ValueError(f"{what} must be at most {most} sentences")
    if _URL.search(text) or _BARE_LINK.search(text):
        raise ValueError(f"{what} may not contain a URL; the link goes in url")
    named = {re.sub(_DASH, "-", m).upper() for m in _CVE.findall(text)}
    if named - event.cves:
        raise ValueError(f"{what} names a CVE the event does not")
    if scan_text_for_secrets(text):
        raise ValueError(f"{what} contains something shaped like a credential")
    return text


def clean_url(value: str) -> str:
    """`value` as https://host/path, or ValueError."""
    if len(value) > URL_MAX_CHARS or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError(f"url must be at most {URL_MAX_CHARS} characters, with no spaces")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("url is not a URL") from None
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme != "https" or not _HOST.fullmatch(host):
        raise ValueError("url must be https:// and a host name")
    if parts.username is not None or parts.password is not None or port not in (None, 443):
        raise ValueError("url may not carry a user name, password or port")
    url = f"https://{host}{parts.path or '/'}"
    if scan_text_for_secrets(url):
        raise ValueError("url contains something shaped like a credential")
    return url


def _happened_on(value: Any, event: EventContext, today: date) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise ValueError("date is not a real date") from None
    if day > today:
        raise ValueError("date is in the future")
    if day < event.first_seen - EARLIEST_BEFORE_FIRST_SEEN:
        raise ValueError("date is long before the event was first reported")
    return day


def _change(
    raw: dict[str, Any], event: EventContext, today: date, taken: set[MaterialChange],
    seen: set[str],
) -> Change:
    try:
        kind = MaterialChange(raw["type"])
    except ValueError:
        raise ValueError(f"type must be one of {', '.join(sorted(AGENT_TYPES))}") from None
    if kind not in AGENT_TYPES:
        raise ValueError(f"type must be one of {', '.join(sorted(AGENT_TYPES))}")
    if kind in ONCE_TYPES and (kind in event.types or kind in taken):
        raise ValueError(f"the event already has {kind.value} on its timeline")
    summary = _prose(raw["summary"], "summary", event, SUMMARY_CHARS, SUMMARY_SENTENCES)
    if summary.casefold() in event.summaries or summary.casefold() in seen:
        raise ValueError("the timeline already says this")
    return Change(
        kind, summary, clean_url(raw["url"]), _happened_on(raw.get("date"), event, today)
    )


def check_report(body: Any, *, kind: str, event: EventContext, today: date) -> Report:
    """The report DECKARD sent for a task of `kind`, checked. Raises ReportRejected."""
    body = _fields(body, "the report", {"outcome"}, {"changes", "summary"})
    outcome = body["outcome"]
    allowed = OUTCOMES[kind]
    if outcome not in allowed:
        raise ReportRejected(f"a {kind} task is answered with outcome {' or '.join(allowed)}")

    if outcome == "no_change":
        if body.get("changes") or body.get("summary") is not None:
            raise ReportRejected("no_change carries no changes and no summary")
        return Report("no_change")

    if outcome == "summary":
        if "changes" in body:
            raise ReportRejected("a summary report carries no changes")
        text = _string(body.get("summary"), "summary")
        try:
            return Report("summary", summary=_prose(text, "summary", event, FINAL_CHARS,
                                                    FINAL_SENTENCES))
        except ValueError as exc:
            raise ReportRejected(str(exc)) from None

    if "summary" in body:
        raise ReportRejected("a changed report puts each summary in its change")
    changes = body.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        raise ReportRejected(f"changed needs a list of 1 to {MAX_CHANGES} changes")
    raws = [
        _fields(c, f"change {i}", {"type", "summary", "url"}, {"date"})
        for i, c in enumerate(changes)
    ]
    for i, raw in enumerate(raws):
        for name in ("type", "summary", "url"):
            _string(raw[name], f"change {i} {name}")

    kept: list[Change] = []
    refused: list[Refusal] = []
    for i, raw in enumerate(raws):
        if event.agent_entries + len(kept) >= MAX_AGENT_ENTRIES:
            refused.append(Refusal(i, f"the event has {MAX_AGENT_ENTRIES} entries from DECKARD"))
            continue
        try:
            change = _change(
                raw, event, today, {c.type for c in kept}, {c.summary.casefold() for c in kept}
            )
        except ValueError as exc:
            refused.append(Refusal(i, str(exc)))
            continue
        kept.append(change)
    return Report("changed", tuple(kept), tuple(refused))
