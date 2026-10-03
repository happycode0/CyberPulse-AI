"""How one watchdog pass moves the incidents: a plan, worked out from the incidents as they are
and the pass's findings. worker/db/incidents.py applies it, in one transaction.

- A finding with no incident opens one, or reopens the same fault's if it resolved within
  REOPEN_WITHIN. Reopening keeps its count of failed fixes and its breaker, so a fault that
  flaps cannot clear them.
- A finding with an open incident updates it.
- An open incident whose kind this pass covered, and which it did not see, starts clearing. It
  resolves once it has stayed clear for RESOLVE_AFTER: a fault is not over at its first quiet
  pass. One seen again before then stops clearing.
- An open incident whose kind this pass could not check is left as it is.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from worker.watchdog.checks import Finding

RESOLVE_AFTER = timedelta(minutes=15)
REOPEN_WITHIN = timedelta(hours=6)


@dataclass(frozen=True)
class Known:
    """An incident, as far as the plan needs it."""

    id: int
    kind: str
    subject: str
    resolved: bool
    clear_since: datetime | None = None
    resolved_at: datetime | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.subject


@dataclass
class Plan:
    insert: list[Finding] = field(default_factory=list)
    reopen: list[tuple[int, Finding]] = field(default_factory=list)
    update: list[tuple[int, Finding]] = field(default_factory=list)
    clearing: list[int] = field(default_factory=list)
    resolve: list[int] = field(default_factory=list)


def plan(
    known: Iterable[Known],
    findings: Sequence[Finding],
    covered: frozenset[str],
    now: datetime,
) -> Plan:
    """`known`: every unresolved incident, and those resolved in the last REOPEN_WITHIN."""
    unresolved: dict[tuple[str, str], Known] = {}
    recent: dict[tuple[str, str], Known] = {}
    for k in known:
        if not k.resolved:
            unresolved[k.key] = k
        elif k.resolved_at is not None and now - k.resolved_at <= REOPEN_WITHIN:
            last = recent.get(k.key)
            if last is None or (last.resolved_at or now) < k.resolved_at:
                recent[k.key] = k
    out = Plan()
    seen: set[tuple[str, str]] = set()
    for f in findings:
        if f.key in seen:
            continue
        seen.add(f.key)
        if f.key in unresolved:
            out.update.append((unresolved[f.key].id, f))
        elif f.key in recent:
            out.reopen.append((recent[f.key].id, f))
        else:
            out.insert.append(f)
    for key, k in unresolved.items():
        if key in seen or k.kind not in covered:
            continue
        if k.clear_since is None:
            out.clearing.append(k.id)
        elif now - k.clear_since >= RESOLVE_AFTER:
            out.resolve.append(k.id)
    return out
