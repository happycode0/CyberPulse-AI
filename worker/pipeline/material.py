"""Material change: what a new report or a register says that the event did not (PLAN.md §5,
§9 Stage 3).

An event's freshness decays from `last_material_update` (worker/pipeline/score.py), so only a
material change brings a story back up; it also makes a new or archived event a developing one
(worker/db/archive.py). Another
outlet repeating it is evidence (`NEW_EVIDENCE`), not news, and anything else is
`NO_MATERIAL_CHANGE`, which is never written.
What is found here, without a model:

- `NEW_CVE`: a CVE the event did not have (worker/pipeline/assemble.py `plan_update`).
- `NEW_AU_EXPOSURE`: the first Australian source on an event first reported elsewhere.
- `EXPLOIT_CONFIRMED`, `NEW_EXPLOIT`, `NEW_PATCH`, `NEW_MITIGATION`, `CORRECTION`: a new
  report's headline says so, and nothing the event already had did (its headlines, its
  timeline, a KEV listing or a published fix on one of its CVEs).
- From the registers (worker/groundtruth/sync.py): one of the event's CVEs added to CISA's KEV
  after the event began is `EXPLOIT_CONFIRMED`; an advisory that gains a fixed version is
  `NEW_PATCH`.

`NEW_ACTOR`, `NEW_TARGET`, `NEW_GEOGRAPHY` and `NEW_IMPACT` need the entities a model reads
from the text, so they are left to the AI layer. Headlines only, never feed summaries: a
summary's boilerplate ("apply the latest security updates") would read as news on every
story. Everything here is pure; worker/db/material.py writes what the registers say.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from worker.models import Event, MaterialChange, SourceConfig, TimelineEntry


def _any(*phrases: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(phrases) + r")\b", re.IGNORECASE)


_ZERO_DAY = r"zero[- ]?days?|0[- ]?days?"

# What a headline says happened, in the order entries are written.
SIGNALS: dict[MaterialChange, re.Pattern[str]] = {
    MaterialChange.EXPLOIT_CONFIRMED: _any(
        r"actively exploited",
        r"exploited in (?:the wild|(?:real[- ]world )?attacks?)",
        r"under (?:active )?exploitation",
        r"exploitation (?:detected|observed|confirmed|attempts)",
        r"(?:attackers|hackers|threat actors) (?:are )?exploit(?:ing)?",
        r"known exploited",
        rf"exploited (?:as )?(?:a )?(?:{_ZERO_DAY})",
        rf"(?:{_ZERO_DAY}) (?:flaws? |bugs? |vulnerabilit(?:y|ies) )?(?:exploited|attacks?)",
    ),
    MaterialChange.NEW_EXPLOIT: _any(
        r"proof[- ]?of[- ]?concept",
        r"poc",
        r"exploit (?:code|released|published|available|details|chain)",
        r"public(?:ly available)? exploits?",
    ),
    MaterialChange.NEW_PATCH: _any(
        r"patch(?:es|ed)",
        r"fix(?:es|ed)",
        r"security updates?",
        r"hotfix(?:es)?",
        r"update now",
        r"patch now",
    ),
    MaterialChange.NEW_MITIGATION: _any(r"mitigat(?:e|es|ed|ion|ions)", r"workarounds?"),
    MaterialChange.CORRECTION: _any(r"correction", r"corrected", r"retract(?:s|ed|ion)?"),
}

# A headline saying the opposite ("No patch yet for ...") is not that change.
NEGATIONS: dict[MaterialChange, re.Pattern[str]] = {
    MaterialChange.EXPLOIT_CONFIRMED: _any(
        r"no (?:evidence|signs?|reports?) of (?:active )?exploitation",
        r"not (?:been |yet )?exploited",
    ),
    MaterialChange.NEW_PATCH: _any(
        r"unpatched",
        r"no (?:patch|fix)(?:es)?",
        r"(?:without|awaiting|before|lacks?) (?:a |an )?(?:official )?(?:patch|fix)(?:es)?",
        r"yet to (?:be )?(?:patch|fix)(?:ed)?",
    ),
}


def signals(headline: str | None) -> set[MaterialChange]:
    """The changes a headline reports."""
    if not headline:
        return set()
    return {
        change
        for change, pattern in SIGNALS.items()
        if pattern.search(headline)
        and not (change in NEGATIONS and NEGATIONS[change].search(headline))
    }


def known(event: Event, headlines: Iterable[str]) -> set[MaterialChange]:
    """What the event already says: its timeline, its reports' headlines and its CVEs."""
    out = {t.type for t in event.timeline}
    for h in (event.title, *headlines):
        out |= signals(h)
    if any(c.kev.listed for c in event.cves):
        out.add(MaterialChange.EXPLOIT_CONFIRMED)
    if any(p.fixed for c in event.cves for a in c.advisories for p in a.packages):
        out.add(MaterialChange.NEW_PATCH)
    return out


def report_changes(
    event: Event,
    headlines: Iterable[str],
    title: str,
    source: SourceConfig,
    *,
    when: datetime,
) -> list[TimelineEntry]:
    """What a new report on `event`, headlined `title`, changes (`NEW_CVE` aside)."""
    had = known(event, headlines)
    found = [c for c in SIGNALS if c in signals(title) - had]
    entries = [
        TimelineEntry(
            timestamp=when, type=c, summary=f"{source.name}: {title}", sources=[source.id]
        )
        for c in found
    ]
    if (
        source.region.lower() == "au"
        and not event.au.directly_reported_in_au
        and MaterialChange.NEW_AU_EXPOSURE not in had
    ):
        entries.append(
            TimelineEntry(
                timestamp=when,
                type=MaterialChange.NEW_AU_EXPOSURE,
                summary=f"First reported in Australia by {source.name}",
                sources=[source.id],
            )
        )
    return entries


@dataclass(frozen=True)
class RegisterChange:
    """A change a register reports on a CVE, for every event that names it."""

    cve_id: str
    type: MaterialChange
    # Only events first seen before this are changed: a later one began with the register
    # already saying it.
    at: datetime
    summary: str


def kev_listing(cve_id: str, date_added: date) -> RegisterChange:
    """CISA added the CVE to KEV on `date_added`."""
    return RegisterChange(
        cve_id,
        MaterialChange.EXPLOIT_CONFIRMED,
        datetime.combine(date_added, time(), UTC),
        f"CISA added {cve_id} to the Known Exploited Vulnerabilities catalogue",
    )


def fix_published(cve_id: str, advisory_id: str, *, now: datetime) -> RegisterChange:
    """An advisory on the CVE that named no fixed version now names one."""
    return RegisterChange(
        cve_id,
        MaterialChange.NEW_PATCH,
        now,
        f"A fixed version is published for {cve_id} ({advisory_id})",
    )
