"""Trends: what is being reported more than it was, measured (PLAN.md §7.3, §9 Stage 3).

A model may interpret a trend but never generate a statistic, so every number here is a count
of stored reports. Pure: worker/db/trends.py loads the inputs and the publisher writes the
result as `trends.json`.

- **A report** is one independent voice on an event (worker/pipeline/lineage.py), dated by its
  publication time, else when it was fetched. A copy of a wire story is not a second report.
- **A topic** is a vendor, actor, malware family, kind of threat or AI subject
  (config/trends.yaml). A report counts towards it when its own headline names one of its
  terms. A CVE counts every report on an event that names it.
- **Only reports from after collection began count.** A feed's first fetch lists its backlog,
  a sample of earlier days rather than all of them: counted, it would make everything look as
  if it were rising.
- **Velocity** compares the last 24 hours with the per-day rate over the up to six days before
  them: `ratio = (recent + 1) / (baseline per day + 1)`. The ones keep a single report on a
  quiet topic from reading as a surge. With under two days of baseline nothing is called
  rising or falling, only counted (`warming_up`).
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_TRENDS_PATH = Path(__file__).resolve().parents[2] / "config" / "trends.yaml"

RECENT = timedelta(hours=24)
BASELINE = timedelta(days=6)
MIN_BASELINE = timedelta(hours=48)
# A topic needs this many reports in the last 24 hours to be called rising or new.
MIN_REPORTS = 3
RISING_RATIO = 2.0
# Below this per-day baseline a quiet day is not a fall.
MIN_FALLING_BASELINE = 1.0
ACTIVITY_DAYS = 14
MAX_TOPICS = 30
MAX_CVES = 15
EVENTS_PER_TREND = 3

State = Literal["warming_up", "new", "rising", "steady", "falling"]
Coverage = Literal["full", "partial", "none"]

_POSSESSIVE = re.compile(r"['’]s\b")
_PUNCTUATION = re.compile(r"[^\w\s]")


def words(text: str) -> tuple[str, ...]:
    """A headline's words as topics are matched: lowercase, possessives and punctuation gone
    ("Fortinet's" is "fortinet", "D-Link" is "dlink")."""
    return tuple(_PUNCTUATION.sub("", _POSSESSIVE.sub("", text.casefold())).split())


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    label: str = Field(min_length=1)
    kind: Literal["vendor", "actor", "malware", "threat", "ai"]
    terms: tuple[str, ...] = Field(min_length=1)

    @field_validator("terms")
    @classmethod
    def _terms_are_words(cls, terms: tuple[str, ...]) -> tuple[str, ...]:
        for term in terms:
            if " ".join(words(term)) != term:
                raise ValueError(
                    f"term {term!r} is not written as a headline reads ({words(term)})"
                )
        return terms


class TrendsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    topics: tuple[Topic, ...]

    @model_validator(mode="after")
    def _unique(self) -> "TrendsConfig":
        keys = [t.key for t in self.topics]
        if len(set(keys)) != len(keys):
            raise ValueError("topic keys must be unique")
        owner: dict[str, str] = {}
        for t in self.topics:
            for term in t.terms:
                if owner.setdefault(term, t.key) != t.key:
                    raise ValueError(f"term {term!r} belongs to both {owner[term]} and {t.key}")
        return self


def load_trends_config(path: Path = DEFAULT_TRENDS_PATH) -> TrendsConfig:
    return TrendsConfig.model_validate(yaml.safe_load(path.read_text()))


class Matcher:
    """Which topics a headline names, by whole words in a row."""

    def __init__(self, topics: Iterable[Topic]) -> None:
        self._by_first: dict[str, list[tuple[tuple[str, ...], str]]] = defaultdict(list)
        for topic in topics:
            for term in topic.terms:
                seq = words(term)
                self._by_first[seq[0]].append((seq, topic.key))

    def topics(self, headline: str) -> set[str]:
        ws = words(headline)
        found: set[str] = set()
        for i, w in enumerate(ws):
            for seq, key in self._by_first.get(w, ()):
                if ws[i : i + len(seq)] == seq:
                    found.add(key)
        return found


@dataclass(frozen=True)
class Report:
    event_id: str
    headline: str
    at: datetime


@dataclass(frozen=True)
class Story:
    event_id: str
    first_seen: datetime
    prominence: float | None
    critical_or_high: bool
    au: bool
    cves: tuple[str, ...] = ()
    kev_cves: frozenset[str] = frozenset()
    ai: bool = False  # on the AI desk: its beat is `ai` or `both` (docs/wiki/ai-news-beat.md)


@dataclass(frozen=True)
class Windows:
    """The two windows every trend is measured over, clipped to when collection began."""

    now: datetime
    collecting_since: datetime | None

    @property
    def recent_start(self) -> datetime:
        return self._clip(self.now - RECENT)

    @property
    def baseline_start(self) -> datetime:
        return self._clip(self.now - RECENT - BASELINE)

    @property
    def baseline_end(self) -> datetime:
        return self._clip(self.now - RECENT)

    @property
    def baseline_hours(self) -> float:
        return (self.baseline_end - self.baseline_start).total_seconds() / 3600

    @property
    def warming_up(self) -> bool:
        return self.baseline_end - self.baseline_start < MIN_BASELINE

    def _clip(self, t: datetime) -> datetime:
        if self.collecting_since is None:
            return self.now
        return min(max(t, self.collecting_since), self.now)

    def collected(self, at: datetime) -> bool:
        """Whether `at` falls between when collection began and now."""
        return self.collecting_since is not None and self.collecting_since <= at <= self.now

    def where(self, at: datetime) -> Literal["recent", "baseline"] | None:
        if self.recent_start <= at <= self.now:
            return "recent"
        if self.baseline_start <= at < self.baseline_end:
            return "baseline"
        return None


@dataclass
class _Tally:
    recent: int = 0
    baseline: int = 0
    events: set[str] = field(default_factory=set)

    def add(self, where: Literal["recent", "baseline"], event_id: str) -> None:
        if where == "recent":
            self.recent += 1
        else:
            self.baseline += 1
        self.events.add(event_id)


def _state(t: _Tally, per_day: float, ratio: float, windows: Windows) -> State:
    if windows.warming_up:
        return "warming_up"
    if t.recent >= MIN_REPORTS and t.baseline == 0:
        return "new"
    if t.recent >= MIN_REPORTS and ratio >= RISING_RATIO:
        return "rising"
    if per_day >= MIN_FALLING_BASELINE and ratio <= 1 / RISING_RATIO:
        return "falling"
    return "steady"


def _measure(t: _Tally, windows: Windows) -> tuple[float, float, State]:
    days = windows.baseline_hours / 24
    per_day = t.baseline / days if days > 0 else 0.0
    ratio = (t.recent + 1) / (per_day + 1)
    return round(per_day, 2), round(ratio, 2), _state(t, per_day, ratio, windows)


def _top_events(ids: Iterable[str], stories: dict[str, Story]) -> list[str]:
    def rank(eid: str) -> tuple[float, str]:
        p = stories[eid].prominence if eid in stories else None
        return (-(p or 0.0), eid)

    return sorted(ids, key=rank)[:EVENTS_PER_TREND]


def _day_coverage(day: date, windows: Windows) -> Coverage:
    start = datetime.combine(day, time(), tzinfo=UTC)
    end = start + timedelta(days=1)
    if windows.collecting_since is None or end <= windows.collecting_since:
        return "none"
    if start >= windows.collecting_since and end <= windows.now:
        return "full"
    return "partial"


def compute_trends(
    reports: Sequence[Report],
    stories: Sequence[Story],
    kev_added: dict[date, int],
    config: TrendsConfig,
    *,
    now: datetime,
    collecting_since: datetime | None,
) -> dict:
    """The `trends.json` payload, less its envelope (worker/publish/build.py adds that)."""
    windows = Windows(now=now, collecting_since=collecting_since)
    by_id = {s.event_id: s for s in stories}
    matcher = Matcher(config.topics)

    topics: dict[str, _Tally] = defaultdict(_Tally)
    cves: dict[str, _Tally] = defaultdict(_Tally)
    reports_by_day: dict[date, int] = defaultdict(int)
    for r in reports:
        if not windows.collected(r.at):
            continue
        reports_by_day[r.at.astimezone(UTC).date()] += 1
        where = windows.where(r.at)
        if where is None:
            continue
        for key in matcher.topics(r.headline):
            topics[key].add(where, r.event_id)
        story = by_id.get(r.event_id)
        for cve_id in story.cves if story else ():
            cves[cve_id].add(where, r.event_id)

    labels = {t.key: t for t in config.topics}
    topic_rows = []
    for key, t in topics.items():
        per_day, ratio, state = _measure(t, windows)
        topic_rows.append(
            {
                "key": key,
                "label": labels[key].label,
                "kind": labels[key].kind,
                "recent": t.recent,
                "baseline": t.baseline,
                "baseline_per_day": per_day,
                "ratio": ratio,
                "state": state,
                "stories": len(t.events),
                "event_ids": _top_events(t.events, by_id),
            }
        )
    topic_rows.sort(key=lambda r: (-r["recent"], -r["ratio"], -r["baseline"], r["key"]))

    kev = {c for s in stories for c in s.kev_cves}
    cve_rows = []
    for cve_id, t in cves.items():
        per_day, ratio, state = _measure(t, windows)
        cve_rows.append(
            {
                "cve_id": cve_id,
                "recent": t.recent,
                "week": t.recent + t.baseline,
                "ratio": ratio,
                "state": state,
                "stories": len(t.events),
                "kev": cve_id in kev,
                "event_ids": _top_events(t.events, by_id),
            }
        )
    cve_rows.sort(key=lambda r: (-r["week"], -r["recent"], r["cve_id"]))

    today = now.astimezone(UTC).date()
    # A story's first_seen is when its first report was published, so a backlog would fill the
    # days before collection began: only stories first seen since then count. KEV additions are
    # CISA's own dates over its whole catalogue, so they count on every day.
    first_seen: dict[date, list[Story]] = defaultdict(list)
    for s in stories:
        if windows.collected(s.first_seen):
            first_seen[s.first_seen.astimezone(UTC).date()].append(s)
    activity = []
    for n in range(ACTIVITY_DAYS - 1, -1, -1):
        day = today - timedelta(days=n)
        on_day = first_seen.get(day, [])
        activity.append(
            {
                "date": day.isoformat(),
                "coverage": _day_coverage(day, windows),
                "stories": len(on_day),
                "reports": reports_by_day.get(day, 0),
                "kev_added": kev_added.get(day, 0),
                "critical_high": sum(s.critical_or_high for s in on_day),
                "au_stories": sum(s.au for s in on_day),
                "ai_stories": sum(s.ai for s in on_day),
            }
        )

    return {
        "trends_version": config.version,
        "coverage": {
            "collecting_since": collecting_since.isoformat() if collecting_since else None,
            "recent_hours": RECENT.total_seconds() / 3600,
            "baseline_hours": round(windows.baseline_hours, 1),
            "baseline_hours_wanted": BASELINE.total_seconds() / 3600,
            "baseline_hours_needed": MIN_BASELINE.total_seconds() / 3600,
            "warming_up": windows.warming_up,
        },
        "activity": activity,
        "topics": topic_rows[:MAX_TOPICS],
        "cves": cve_rows[:MAX_CVES],
    }
