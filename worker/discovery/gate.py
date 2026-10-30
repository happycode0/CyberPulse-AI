"""SERAPH's gate (PLAN.md section 6): what a found feed must show, probe after probe, before the
worker collects it. Pure: the probes themselves are in worker/discovery/run.py.

A host is found by the nightly search or proposed by TACHIKOMA. The worker then looks for its
feed, and probes the feed once each gate pass. A probe is healthy when the feed has enough recent
items, enough of them on CyberPulse's beat, both as a count and as a share (a whole-site news feed
with two security stories a day among a hundred is not a security source), and not too many the
worker already has from another source. `probes_to_activate` healthy probes in a row activate it;
`failures_to_reject` failed ones in a row reject it for good. One good fetch never activates
anything.

An activated source is collected in the NORMAL lane as `community`, the least trusted evidence
class a real source can have, so on its own it can never confirm an event. At most `max_active`
are collected at once. When that many are and a candidate is ready, the weakest of them, those
other sources seldom corroborate, make room for it (`weakest`); a ready candidate that finds no
room keeps its place and is probed once a day (`ready_reprobe_hours`) while it waits.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from worker.ai.tasks import _URL, _clean
from worker.discovery.guard import Refused, check_url
from worker.models import Lane, NormalisedItem, SourceConfig
from worker.pipeline.followup import _BARE_LINK
from worker.pipeline.reputation import CORROBORATION_WINDOW, Reputation
from worker.publish.validate import scan_text_for_secrets

DEFAULT_DISCOVERY_PATH = Path(__file__).resolve().parents[2] / "config" / "discovery.yaml"

# What marks a source as one the gate activated: it is fetched through the guard, never
# collected from the YAML, and counted against `max_active`.
DISCOVERED_CLASS = "discovered"
DISCOVERED_CATEGORY = "community"
DISCOVERED_PRIORITY = 5
SOURCE_ID_PREFIX = "found_"

NAME_MAX_CHARS = 80
REASON_CHARS = (20, 280)
SAMPLE_TITLES = 3
SAMPLE_TITLE_MAX_CHARS = 120
MAX_EXAMPLES = 3
EVIDENCE_KEPT = 5

# Feed types a home page advertises that parse_feed reads, and where feeds usually are when it
# advertises none.
FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml"})
USUAL_FEED_PATHS = ("/feed/", "/rss", "/feed.xml", "/atom.xml", "/index.xml", "/rss.xml")
ADVERTISED_KEPT = 3


class SearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credits_per_day: int = Field(ge=0, le=100)
    results_per_query: int = Field(ge=1, le=20)
    time_range: Literal["day", "week", "month", "year"]
    queries: list[str] = Field(min_length=1, max_length=100)
    skip_hosts: list[str] = Field(default_factory=list)


class PruneConfig(BaseModel):
    """Which discovered sources make room when `max_active` is reached and a candidate is ready
    (`weakest`)."""

    model_config = ConfigDict(extra="forbid")

    per_pass: int = Field(ge=0, le=10)  # 0: none is ever retired to make room
    min_days: int = Field(ge=7, le=365)
    corroboration_below: float = Field(ge=0.0, le=1.0)


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probes_to_activate: int = Field(ge=2, le=50)
    failures_to_reject: int = Field(ge=1, le=20)
    recent_days: int = Field(ge=1, le=90)
    min_recent_items: int = Field(ge=1, le=100)
    min_on_beat: int = Field(ge=1, le=100)
    min_on_beat_share: float = Field(ge=0.0, le=1.0)
    max_already_collected: float = Field(ge=0.0, le=1.0)
    probes_per_pass: int = Field(ge=1, le=50)
    homes_per_pass: int = Field(ge=0, le=50)
    max_open: int = Field(ge=1, le=200)
    max_active: int = Field(ge=0, le=200)
    # At least one gate pass: below that it would be every pass, as for a candidate not ready.
    ready_reprobe_hours: int = Field(ge=4, le=168)
    prune: PruneConfig
    forget_after_days: int = Field(ge=1, le=3650)
    beat: list[str] = Field(min_length=1)
    beat_words: list[str] = Field(default_factory=list)

    def beat_re(self) -> re.Pattern[str]:
        return beat_pattern(self.beat, self.beat_words)


class DiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: SearchConfig
    gate: GateConfig

    @classmethod
    def load(cls, path: Path | None = None) -> "DiscoveryConfig":
        with open(path or DEFAULT_DISCOVERY_PATH) as f:
            return cls.model_validate(yaml.safe_load(f))


# --- Hosts ---------------------------------------------------------------------------------------


def host_of(url: str) -> str:
    """The host a URL is on, in lower case and without a leading "www.": one site, one row."""
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    return host.removeprefix("www.")


def same_site(a: str, b: str) -> bool:
    """Whether two hosts are one site: the same, or one under the other (blog.x.com and x.com)."""
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def skipped(host: str, skip_hosts: Iterable[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in skip_hosts)


def source_id_for(host: str) -> str:
    return SOURCE_ID_PREFIX + re.sub(r"[^a-z0-9]+", "_", host.lower()).strip("_")[:60]


def region_for(host: str) -> str:
    """"au" for an Australian domain. worker/pipeline/au.py counts a source's region as where
    its story was reported, and a .au site reports from Australia."""
    return "au" if host.endswith(".au") else "global"


# --- Home pages ----------------------------------------------------------------------------------


class _Head(HTMLParser):
    """A page's title and the feeds its <link> tags advertise."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.feeds: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and not self.title:
            self._in_title = True
        if tag != "link":
            return
        values = {k.lower(): (v or "") for k, v in attrs}
        rel = values.get("rel", "").lower().split()
        advertised = "alternate" in rel and values.get("type", "").lower().strip() in FEED_TYPES
        if advertised and (href := values.get("href", "").strip()):
            self.feeds.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


@dataclass(frozen=True)
class HomePage:
    title: str | None
    feeds: list[str]  # advertised first, then the usual places, each on the same site


def read_home(page_url: str, html: bytes) -> HomePage:
    head = _Head()
    try:
        head.feed(html.decode("utf-8", errors="replace"))
        head.close()
    except Exception:  # noqa: BLE001, S110 - a page too broken to read is a page with no feeds
        pass
    host = host_of(page_url)
    found: list[str] = []
    advertised = [urljoin(page_url, href) for href in head.feeds][:ADVERTISED_KEPT]
    usual = [urljoin(page_url, path) for path in USUAL_FEED_PATHS]
    for url in advertised + usual:
        try:
            url = check_url(url)
        except Refused:
            continue
        if same_site(host_of(url), host) and url not in found:
            found.append(url)
    return HomePage(clean_name(head.title), found)


def clean_name(value: object) -> str | None:
    """A source name from a feed's or page's title: one line, no link, at most 80 characters."""
    if not isinstance(value, str):
        return None
    name = _clean(value)
    if not name or _URL.search(name) or scan_text_for_secrets(name):
        return None
    return name if len(name) <= NAME_MAX_CHARS else name[: NAME_MAX_CHARS - 1] + "…"


# --- Probes --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """What one fetch of a candidate's feed showed."""

    items: int
    recent: int
    on_beat: int
    already_collected: int
    newest_age_days: float | None
    samples: list[str] = field(default_factory=list)  # recent titles on the beat
    failures: list[str] = field(default_factory=list)  # why it is not healthy

    @property
    def healthy(self) -> bool:
        return not self.failures

    def as_json(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "items": self.items,
            "recent": self.recent,
            "on_beat": self.on_beat,
            "already_collected": self.already_collected,
            "newest_age_days": self.newest_age_days,
            "samples": self.samples,
            "failures": self.failures,
        }


def beat_pattern(beat: Sequence[str], words: Sequence[str] = ()) -> re.Pattern[str]:
    """`beat` matches at the start of a word, in any case ("vulnerab" finds "Vulnerabilities").
    `words` match whole and in their own case, for short acronyms that would otherwise be the
    start of ordinary words ("AI" is not "aid", "RAT" is not "rate")."""
    terms = sorted({t.strip() for t in beat if t.strip()}, key=len, reverse=True)
    pattern = r"\b(?i:" + "|".join(map(re.escape, terms)) + ")"
    whole = sorted({w.strip() for w in words if w.strip()}, key=len, reverse=True)
    if whole:
        pattern += r"|\b(?:" + "|".join(map(re.escape, whole)) + r")\b"
    return re.compile(pattern)


def assess_probe(
    items: Sequence[NormalisedItem],
    collected: Iterable[str],
    *,
    now: datetime,
    gate: GateConfig,
) -> Probe:
    """`collected` is the url hashes among `items` the worker already has from any source.

    An item with no date of its own is not recent: normalise() dates it when it was fetched, which
    would make a feed of old pages look live.
    """
    known = set(collected)
    window = timedelta(days=gate.recent_days)
    dated = [i for i in items if i.published is not None and not i.published_is_estimated]
    recent = [i for i in dated if now - window <= i.published <= now + timedelta(days=1)]
    on_beat_re = gate.beat_re()
    on_beat = [i for i in recent if i.cves or on_beat_re.search(f"{i.title} {i.summary or ''}")]
    already = sum(i.url_hash in known for i in recent)
    newest = max((i.published for i in dated), default=None)

    failures = []
    if not items:
        failures.append("the feed has no items")
    if len(recent) < gate.min_recent_items:
        failures.append(
            f"{len(recent)} items dated in the last {gate.recent_days} days; "
            f"{gate.min_recent_items} needed"
        )
    if len(on_beat) < gate.min_on_beat:
        failures.append(f"{len(on_beat)} recent items on the beat; {gate.min_on_beat} needed")
    elif len(on_beat) < gate.min_on_beat_share * len(recent):
        failures.append(
            f"{len(on_beat)} of its {len(recent)} recent items are on the beat; at least "
            f"{gate.min_on_beat_share:.0%} must be"
        )
    if recent and already / len(recent) > gate.max_already_collected:
        failures.append(
            f"{already} of its {len(recent)} recent items were already collected from another "
            f"source; at most {gate.max_already_collected:.0%} may be"
        )
    return Probe(
        items=len(items),
        recent=len(recent),
        on_beat=len(on_beat),
        already_collected=already,
        newest_age_days=(
            round((now - newest).total_seconds() / 86400, 1) if newest is not None else None
        ),
        samples=[_sample(i.title) for i in on_beat[:SAMPLE_TITLES]],
        failures=failures,
    )


def _sample(title: str) -> str:
    text = _clean(title)
    return text if len(text) <= SAMPLE_TITLE_MAX_CHARS else text[: SAMPLE_TITLE_MAX_CHARS - 1] + "…"


# --- Activation ----------------------------------------------------------------------------------


def discovered_source(
    *, host: str, feed_url: str, name: str | None, found_by: str, now: datetime
) -> SourceConfig:
    """The registry row an activated candidate becomes."""
    return SourceConfig(
        id=source_id_for(host),
        name=name or host,
        type="rss",
        region=region_for(host),
        category=DISCOVERED_CATEGORY,
        source_class=DISCOVERED_CLASS,
        priority=DISCOVERED_PRIORITY,
        lane=Lane.NORMAL,
        enabled=True,
        url=feed_url,
        parser="rss",
        expected_frequency="weekly",
        notes=(
            f"Found by {'the nightly search' if found_by == 'search' else 'TACHIKOMA'}; "
            f"activated by SERAPH's gate on {now:%Y-%m-%d}. Collected through the URL guard."
        ),
    )


# --- Making room ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Collected:
    """An active source as the pruning sees it: its reputation is the one source-health.json
    publishes (worker/pipeline/reputation.py)."""

    source_id: str
    source_class: str
    first_checked_at: datetime | None  # its first health check: when collection began
    reputation: Reputation


def weakest(
    sources: Iterable[Collected], *, need: int, prune: PruneConfig, now: datetime
) -> list[Collected]:
    """The discovered sources to retire so `need` ready candidates can be collected: at most
    `prune.per_pass`, lowest reputation first, each collected for at least `prune.min_days`
    and with less than `prune.corroboration_below` of its events corroborated.

    A source with too few events to judge (no corroboration) is kept: it is quiet, not shown to
    be weak, and one that stops answering is retired as silent anyway. A curated source is
    never one: only the gate's own finds make room for its next.
    """
    cutoff = now - timedelta(days=prune.min_days)
    weak = [
        s for s in sources
        if s.source_class == DISCOVERED_CLASS
        and s.first_checked_at is not None
        and s.first_checked_at <= cutoff
        and s.reputation.corroboration is not None
        and s.reputation.corroboration < prune.corroboration_below
    ]
    weak.sort(key=lambda s: (s.reputation.score, s.reputation.corroboration, s.source_id))
    return weak[: max(min(need, prune.per_pass), 0)]


def weak_reason(source: Collected, *, now: datetime) -> str:
    """Why `weakest` chose it, for the candidate's `last_error` and the log."""
    rep = source.reputation
    days = (now - source.first_checked_at).days if source.first_checked_at else 0
    return (
        f"retired for a ready candidate: corroboration {rep.corroboration or 0:.0%} of "
        f"{rep.events_90d} events in {CORROBORATION_WINDOW.days} days, reputation {rep.score}, "
        f"after {days} days collected"
    )


# --- TACHIKOMA's proposals -----------------------------------------------------------------------


class ProposalRejected(ValueError):
    """A proposal the worker will not queue, and why, for TACHIKOMA to fix."""


@dataclass(frozen=True)
class Proposal:
    host: str
    url: str
    is_home: bool  # the URL is the site's home page, so its feed is still to find
    name: str | None
    reason: str
    examples: list[str]


PROPOSAL_FIELDS = frozenset({"url", "name", "reason", "examples"})


def check_proposal(body: object, *, skip_hosts: Iterable[str] = ()) -> Proposal:
    """TACHIKOMA's proposal, checked as if hostile: its text came from the open web."""
    if not isinstance(body, Mapping):
        raise ProposalRejected("send a JSON object")
    if unknown := sorted(set(body) - PROPOSAL_FIELDS):
        raise ProposalRejected(
            f"unknown field {unknown[0]!r}; a proposal has {', '.join(sorted(PROPOSAL_FIELDS))}"
        )
    for name in ("url", "reason"):
        if name not in body:
            raise ProposalRejected(f"a proposal needs {name}")
    try:
        url = check_url(body["url"])
    except Refused as exc:
        raise ProposalRejected(f"url: {exc}") from None
    host = host_of(url)
    if skipped(host, skip_hosts):
        raise ProposalRejected(f"{host} is a platform, not a source; propose the site itself")
    path = urlsplit(url).path

    reason = body["reason"]
    if not isinstance(reason, str):
        raise ProposalRejected("reason must be a string")
    reason = _clean(reason)
    low, high = REASON_CHARS
    if not low <= len(reason) <= high:
        raise ProposalRejected(f"reason is {low} to {high} characters")
    if _URL.search(reason) or _BARE_LINK.search(reason):
        raise ProposalRejected("reason may not contain a URL; examples carry the links")

    name = None
    if body.get("name") is not None:
        name = clean_name(body["name"])
        if name is None:
            raise ProposalRejected(f"name is one line of at most {NAME_MAX_CHARS} characters")

    raw_examples = body.get("examples", [])
    if not isinstance(raw_examples, list) or len(raw_examples) > MAX_EXAMPLES:
        raise ProposalRejected(f"examples is a list of at most {MAX_EXAMPLES} links")
    examples = []
    for i, example in enumerate(raw_examples):
        try:
            link = check_url(example)
        except Refused as exc:
            raise ProposalRejected(f"examples[{i}]: {exc}") from None
        if not same_site(host_of(link), host):
            raise ProposalRejected(f"examples[{i}] is not on {host}")
        examples.append(link)

    if scan_text_for_secrets(" ".join([url, reason, name or "", *examples])):
        raise ProposalRejected("the proposal looks like it carries a credential")
    return Proposal(host, url, path in ("", "/"), name, reason, examples)
