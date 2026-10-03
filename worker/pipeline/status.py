"""Event status, from what an event's record says (PLAN.md section 4, DECKARD; Stage 5).

    new         one report, nothing more yet
    active      an independent source has confirmed it
    developing  something material has changed since it was first reported
    monitoring  confirmed or changed, then quiet for `monitoring_after_days`
    contained   a patch or mitigation is out, and nothing material has happened since
    resolved    contained and quiet for `resolved_after_days`

The status is worked out afresh from the record every run, never stepped on from the last one,
so a missed or repeated run changes nothing, and a material change after the fix reopens the
event. Quiet counts from `last_material_update`, which another outlet repeating the story never
moves. Only archiving and merging set `archived` (worker/db/archive.py, worker/db/merge.py);
nothing here touches an archived event.

What has changed is read from the timeline: every entry but a source's first report (the
`NEW_FACT` an event opens with) and a repeat (`NEW_EVIDENCE`). A `NEW_FACT` from DECKARD (its
sources start with `AGENT_SOURCE`) is a change. A single report nobody has confirmed or added to
stays `new` until it fades and is archived: there is nothing to follow up.

Everything here is pure; worker/db/followup.py reads the record and writes the status.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, model_validator

from worker.models import EventStatus, MaterialChange, Severity

DEFAULT_FOLLOWUP_PATH = Path(__file__).resolve().parents[2] / "config" / "followup.yaml"

# The first source on a timeline entry DECKARD wrote; the second is the link it rests on.
AGENT_SOURCE = "deckard"

FIX_TYPES = frozenset({MaterialChange.NEW_PATCH, MaterialChange.NEW_MITIGATION})
NOT_CHANGES = frozenset(
    {MaterialChange.NEW_FACT, MaterialChange.NEW_EVIDENCE, MaterialChange.NO_MATERIAL_CHANGE}
)
# The statuses DECKARD follows up (PLAN.md: it owns every developing or monitoring event).
TRACKED = (EventStatus.DEVELOPING, EventStatus.MONITORING)


class StatusRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monitoring_after_days: PositiveFloat
    resolved_after_days: PositiveFloat


class FollowupRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    every_hours: dict[EventStatus, dict[Severity, PositiveFloat]]
    batch: int = Field(ge=1, le=50)
    final_summary: list[Severity]

    @model_validator(mode="after")
    def _tracked_only(self) -> Self:
        other = sorted(s.value for s in set(self.every_hours) - set(TRACKED))
        if other:
            raise ValueError(f"every_hours may name developing and monitoring only, not {other}")
        return self

    def cadence(self) -> list[tuple[EventStatus, Severity, float]]:
        """(status, severity, hours) for every pair that is followed up."""
        return [
            (status, severity, hours)
            for status, by_severity in sorted(self.every_hours.items())
            for severity, hours in sorted(by_severity.items())
        ]


class FollowupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: StatusRule
    followup: FollowupRule

    @classmethod
    def load(cls, path: Path | None = None) -> "FollowupConfig":
        with open(path or DEFAULT_FOLLOWUP_PATH) as f:
            return cls.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class StatusFacts:
    """What the status rests on, for one standing event."""

    event_id: str
    status: EventStatus
    first_seen: datetime
    last_material_update: datetime | None
    confirmed: bool  # an independent source has reported it
    fixed_at: datetime | None  # the latest patch or mitigation on its timeline
    changed_at: datetime | None  # the latest other change on its timeline


@dataclass(frozen=True)
class Transition:
    event_id: str
    was: EventStatus
    now: EventStatus


def derive_status(facts: StatusFacts, rule: StatusRule, *, now: datetime) -> EventStatus:
    quiet = now - (facts.last_material_update or facts.first_seen)
    settled = quiet >= timedelta(days=rule.monitoring_after_days)
    if facts.fixed_at is not None:
        if quiet >= timedelta(days=rule.resolved_after_days):
            return EventStatus.RESOLVED
        reopened = facts.changed_at is not None and facts.changed_at > facts.fixed_at
        return EventStatus.DEVELOPING if reopened and not settled else EventStatus.CONTAINED
    if facts.changed_at is not None:
        return EventStatus.MONITORING if settled else EventStatus.DEVELOPING
    if facts.confirmed:
        return EventStatus.MONITORING if settled else EventStatus.ACTIVE
    return EventStatus.NEW


def transitions(
    facts: list[StatusFacts], rule: StatusRule, *, now: datetime
) -> list[Transition]:
    """The events whose status is not what their record says, and what it should be."""
    out = []
    for f in facts:
        if f.status is EventStatus.ARCHIVED:
            continue
        status = derive_status(f, rule, now=now)
        if status is not f.status:
            out.append(Transition(f.event_id, f.status, status))
    return out
