"""Source reputation: how far a source's reports can be trusted, as a whole number 0-100
(docs/wiki/importance-and-reputation.md).

Three parts, weighted:

- **Standing** (60%): what kind of voice the source is, set by hand in config/sources.yaml
  (`Standing`). An authoritative source starts at 90, an established one at 75, a specialist
  at 60 and a community one at 40.
- **Uptime** (15%): the share of its recent health checks (the source-health.json history) that
  came back `ok`.
- **Corroboration** (25%): of the events it reported that were first seen in the last 90 days,
  the share another independent lineage also reported (worker/pipeline/lineage.py). Its own
  feeds, and copies of its headlines, never corroborate it.

A part with too little behind it is left out and the weights of the rest renormalised, and
`basis` says so: a source with no health checks yet has no uptime, and one with fewer than
`CORROBORATION_MIN_EVENTS` events in the window has no corroboration. The scores are published
in source-health.json and feed each event's importance (worker/pipeline/importance.py).

Everything here is pure: worker/publish/build.py gathers the inputs at publish time.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from worker.models import HealthStatus, SourceHealth, Standing

# Bump it with any change below that can move a score.
REPUTATION_VERSION = "1"

STANDING_BASE: dict[Standing, int] = {
    Standing.AUTHORITATIVE: 90,
    Standing.ESTABLISHED: 75,
    Standing.SPECIALIST: 60,
    Standing.COMMUNITY: 40,
}
STANDING_WEIGHT = 0.60
UPTIME_WEIGHT = 0.15
CORROBORATION_WEIGHT = 0.25
CORROBORATION_WINDOW = timedelta(days=90)
CORROBORATION_MIN_EVENTS = 5
# A source with no standing in the registry (one the discovery gate added) counts as this.
DEFAULT_STANDING = Standing.COMMUNITY


@dataclass(frozen=True)
class Corroboration:
    """A source's events first seen in the window, and how many of them another independent
    lineage also reported (worker/db/sources.py `load_corroboration`)."""

    events: int
    corroborated: int


@dataclass(frozen=True)
class Reputation:
    score: int
    standing: Standing
    uptime: float | None
    corroboration: float | None
    events_90d: int
    basis: str

    def public(self) -> dict[str, Any]:
        """source-health.json's `reputation`."""
        return {
            "version": REPUTATION_VERSION,
            "score": self.score,
            "standing": self.standing.value,
            "uptime": self.uptime,
            "corroboration": self.corroboration,
            "events_90d": self.events_90d,
            "basis": self.basis,
        }


def _round(value: float) -> int:
    """Half up, not to even: a score of 62.5 is 63 whatever the digit before it."""
    return math.floor(value + 0.5)


def assess_reputation(
    standing: Standing | None,
    history: Sequence[SourceHealth],
    corroboration: Corroboration | None,
) -> Reputation:
    standing = standing or DEFAULT_STANDING
    events = corroboration.events if corroboration else 0
    parts: list[tuple[float, float]] = [(STANDING_WEIGHT, STANDING_BASE[standing])]
    used, missing = ["standing"], []

    uptime = None
    # A check of a disabled source says nothing about the feed (worker/pipeline/health.py).
    checks = [h for h in history if h.status is not HealthStatus.DISABLED]
    if checks:
        n = len(checks)
        uptime = round(sum(h.status is HealthStatus.OK for h in checks) / n, 3)
        parts.append((UPTIME_WEIGHT, 100 * uptime))
        used.append(f"uptime over the last {n} checks" if n > 1 else "uptime over the last check")
    else:
        missing.append("no health checks yet")

    share = None
    if corroboration is not None and events >= CORROBORATION_MIN_EVENTS:
        share = round(corroboration.corroborated / events, 3)
        parts.append((CORROBORATION_WEIGHT, 100 * share))
        used.append(f"corroboration of {events} events in 90 days")
    else:
        missing.append(
            f"{events} event{'' if events == 1 else 's'} in 90 days, too few to judge "
            f"corroboration (needs {CORROBORATION_MIN_EVENTS})"
        )

    total = sum(w for w, _ in parts)
    score = _round(sum(w * v for w, v in parts) / total)
    basis = _joined(used)
    if missing:
        basis += "; left out: " + "; ".join(missing)
    return Reputation(score, standing, uptime, share, events, basis)


def _joined(things: list[str]) -> str:
    if len(things) == 1:
        return things[0] + " only"
    return ", ".join(things[:-1]) + " and " + things[-1]
