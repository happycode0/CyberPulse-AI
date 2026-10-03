"""The two discovery passes the scheduler runs (worker/scheduler.py).

- **The nightly search**, at 03:10 Sydney time: Tavily searches on CyberPulse's beat, within the
  day's credits. Each result's host is recorded as found, with the result's title and link as
  evidence, unless it is registered already or a platform (`skip_hosts`). Nothing is fetched.
- **SERAPH's gate**, every 4 hours: look for the feed of the hosts found, probe each candidate's
  feed once, activate those with enough healthy probes in a row (worker/discovery/gate.py), and
  retire activated sources that have gone silent. Every request goes through the URL guard
  (worker/discovery/guard.py), and a probe's items are counted, never stored.

Neither pass is fatal to the schedule: a failure is logged, and the next pass starts again.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import feedparser
import httpx
from sqlalchemy import Engine

from worker.collectors.feed import parse_feed
from worker.db import discovery as db
from worker.db.ingest import known_url_hashes
from worker.db.session import get_engine
from worker.discovery import tavily
from worker.discovery.gate import (
    DiscoveryConfig,
    GateConfig,
    Probe,
    assess_probe,
    clean_name,
    discovered_source,
    host_of,
    read_home,
    same_site,
    skipped,
)
from worker.discovery.guard import Refused, Resolver, _resolve, check_url, get, request_headers
from worker.models import NormalisedItem
from worker.pipeline.followup import clean_url
from worker.pipeline.normalise import normalise
from worker.publish.validate import scan_text_for_secrets
from worker.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# A home page and every place its feed might be, together.
HOME_DEADLINE_SECONDS = 120.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _why(exc: BaseException) -> str:
    if isinstance(exc, Refused):
        return f"refused: {exc}"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "no response in time"
    return type(exc).__name__


# --- The nightly search --------------------------------------------------------------------------


@dataclass
class SearchSummary:
    searches: int = 0
    hits: int = 0
    new_hosts: int = 0
    credits: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: str | None = None  # why nothing was searched


def _registered(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return db.registered_hosts(conn)


def _spent(engine: Engine, since: datetime) -> int:
    with engine.connect() as conn:
        return db.credits_used(conn, since=since)


def _store_search(
    engine: Engine,
    *,
    query: str,
    found: tavily.Searched | None,
    error: str | None,
    evidence: list[tuple[str, dict]],
    now: datetime,
) -> int:
    with engine.begin() as conn:
        db.record_search(
            conn,
            query=query,
            results=len(found.hits) if found else 0,
            credits=found.credits if found else None,
            error=error,
            now=now,
        )
        return sum(db.add_found(conn, host=host, evidence=e, now=now) for host, e in evidence)


def _evidence(hit: tavily.Hit, query: str, now: datetime) -> dict | None:
    """What a hit says about its host: its link without the query, its title and date."""
    try:
        link = clean_url(hit.url)
    except ValueError:
        return None
    if scan_text_for_secrets(link):  # one flagged link would withhold all of GET /ops/candidates
        return None
    title = hit.title if hit.title and not scan_text_for_secrets(hit.title) else None
    return {"query": query, "url": link, "title": title, "published": hit.published,
            "at": now.isoformat()}


async def run_search(
    *,
    engine: Engine | None = None,
    settings: Settings | None = None,
    config: DiscoveryConfig | None = None,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SearchSummary:
    settings = settings or get_settings()
    key = settings.tavily_api_key
    if key is None or not key.get_secret_value().strip():
        return SearchSummary(skipped="no TAVILY_API_KEY on the worker")
    config = config or DiscoveryConfig.load()
    engine = engine or get_engine()
    now = now or _utcnow()
    search = config.search

    spent = await asyncio.to_thread(_spent, engine, now - timedelta(days=1))
    allowance = search.credits_per_day - spent
    if allowance <= 0:
        return SearchSummary(skipped=f"{spent} credits spent in the last 24 hours")
    # Start one query further on each day, so a list longer than the allowance is all used.
    start = now.toordinal() % len(search.queries)
    queries = (search.queries[start:] + search.queries[:start])[:allowance]
    registered = await asyncio.to_thread(_registered, engine)

    summary = SearchSummary()
    async with httpx.AsyncClient(transport=transport) as client:
        for query in queries:
            summary.searches += 1
            try:
                found = await tavily.search(
                    client, key, query, max_results=search.results_per_query,
                    time_range=search.time_range,
                )
            except tavily.SearchFailed as exc:
                summary.errors.append(f"{query}: {exc}")
                await asyncio.to_thread(
                    _store_search, engine, query=query, found=None, error=str(exc), evidence=[],
                    now=now,
                )
                if exc.stop:
                    logger.warning("discovery: %s; no more searches tonight", exc)
                    break
                continue

            evidence = []
            for hit in found.hits:
                try:
                    host = host_of(check_url(hit.url))
                except Refused:
                    continue
                if skipped(host, search.skip_hosts) or any(
                    same_site(host, r) for r in registered
                ):
                    continue
                if (e := _evidence(hit, query, now)) is not None:
                    evidence.append((host, e))
            summary.hits += len(found.hits)
            summary.credits += found.credits if found.credits is not None else 1
            summary.new_hosts += await asyncio.to_thread(
                _store_search, engine, query=query, found=found, error=None, evidence=evidence,
                now=now,
            )

    logger.info(
        "discovery: %d searches, %d results, %d new hosts, %d credits%s",
        summary.searches, summary.hits, summary.new_hosts, summary.credits,
        f", {len(summary.errors)} failed" if summary.errors else "",
    )
    return summary


# --- SERAPH's gate -------------------------------------------------------------------------------


@dataclass
class GateSummary:
    forgotten: int = 0
    retired: list[str] = field(default_factory=list)
    homes: int = 0
    feeds_found: int = 0
    probes: int = 0
    healthy: int = 0
    rejected: int = 0
    activated: list[str] = field(default_factory=list)
    waiting: int = 0  # ready, but the active cap is reached
    errors: list[str] = field(default_factory=list)

    @property
    def changed_anything(self) -> bool:
        return bool(self.activated or self.retired)


def _feed_title(body: bytes) -> str | None:
    try:
        return clean_name(feedparser.parse(body).feed.get("title"))
    except Exception:  # noqa: BLE001 - no title is a title to make up
        return None


async def find_feed(
    client: httpx.AsyncClient, host: str, *, now: datetime, resolve: Resolver = _resolve
) -> tuple[str, str | None]:
    """The feed `host` publishes, and a name for it; Refused when none is found."""
    headers = request_headers()
    async with asyncio.timeout(HOME_DEADLINE_SECONDS):
        home = await get(client, f"https://{host}/", headers=headers, resolve=resolve)
        if not 200 <= home.status_code < 300:
            raise Refused(f"the home page answered HTTP {home.status_code}")
        page = read_home(home.url, home.body)
        probe_source = discovered_source(
            host=host, feed_url=home.url, name=None, found_by="search", now=now
        )
        for url in page.feeds:
            if scan_text_for_secrets(url):
                continue
            try:
                feed = await get(client, url, headers=headers, resolve=resolve)
            except (Refused, httpx.HTTPError):
                continue
            if not 200 <= feed.status_code < 300 or not same_site(host_of(feed.url), host):
                continue
            if parse_feed(probe_source, feed.body):
                return feed.url, _feed_title(feed.body) or page.title
    raise Refused("no feed on the home page or in the usual places")


def _known(engine: Engine, items: list[NormalisedItem]) -> set[str]:
    with engine.connect() as conn:
        return set(known_url_hashes(conn, [i.url_hash for i in items]))


async def probe(
    client: httpx.AsyncClient,
    engine: Engine,
    candidate: db.Candidate,
    *,
    gate: GateConfig,
    now: datetime,
    resolve: Resolver = _resolve,
) -> Probe:
    """One fetch of a candidate's feed, assessed. Refused, or httpx's error, when it fails."""
    got = await get(client, candidate.feed_url or "", headers=request_headers(), resolve=resolve)
    if not 200 <= got.status_code < 300:
        raise Refused(f"the feed answered HTTP {got.status_code}")
    if not same_site(host_of(got.url), candidate.host):
        raise Refused("the feed now redirects to another site")
    source = discovered_source(
        host=candidate.host, feed_url=got.url, name=candidate.name,
        found_by=candidate.found_by, now=now,
    )
    items = []
    for raw in parse_feed(source, got.body):
        try:
            items.append(normalise(raw, now=now))
        except Exception:  # noqa: BLE001, S112 - an item that will not normalise is not counted
            continue
    collected = await asyncio.to_thread(_known, engine, items)
    return assess_probe(items, collected, now=now, gate=gate)


def _in(engine: Engine, fn, /, **kwargs):
    with engine.begin() as conn:
        return fn(conn, **kwargs)


async def run_gate(
    *,
    engine: Engine | None = None,
    config: DiscoveryConfig | None = None,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Resolver = _resolve,
) -> GateSummary:
    config = config or DiscoveryConfig.load()
    gate = config.gate
    engine = engine or get_engine()
    now = now or _utcnow()
    summary = GateSummary()

    summary.forgotten = await asyncio.to_thread(
        _in, engine, db.forget_unfed, now=now, gate=gate
    )
    summary.retired = await asyncio.to_thread(_in, engine, db.retire_silent, now=now)
    for source_id in summary.retired:
        logger.warning("discovery: retired %s; no healthy fetch in 21 days", source_id)

    async with httpx.AsyncClient(transport=transport) as client:
        homes = await asyncio.to_thread(
            _in, engine, db.homes_due, limit=gate.homes_per_pass, now=now, gate=gate
        )
        for home in homes:
            summary.homes += 1
            try:
                feed_url, name = await find_feed(client, home.host, now=now, resolve=resolve)
                error = None
            except (Refused, httpx.HTTPError, TimeoutError) as exc:
                feed_url, name, error = None, None, _why(exc)
            state = await asyncio.to_thread(
                _in, engine, db.record_home, candidate_id=home.id, feed_url=feed_url, name=name,
                error=error, gate=gate, now=now,
            )
            summary.feeds_found += feed_url is not None
            summary.rejected += state == "rejected"

        due = await asyncio.to_thread(
            _in, engine, db.probes_due, limit=gate.probes_per_pass, now=now
        )
        for candidate in due:
            summary.probes += 1
            result: Probe | None = None
            error = None
            try:
                result = await probe(client, engine, candidate, gate=gate, now=now, resolve=resolve)
            except (Refused, httpx.HTTPError, TimeoutError) as exc:
                error = _why(exc)
            state, _ = await asyncio.to_thread(
                _in, engine, db.record_probe, candidate_id=candidate.id, probe=result,
                error=error, gate=gate, now=now,
            )
            summary.healthy += result is not None and result.healthy
            summary.rejected += state == "rejected"

    summary.activated, summary.waiting = await asyncio.to_thread(
        _activate_ready, engine, gate, now
    )
    logger.info(
        "source gate: %d homes read (%d feeds found), %d probes (%d healthy), %d rejected, "
        "%d activated, %d retired%s",
        summary.homes, summary.feeds_found, summary.probes, summary.healthy, summary.rejected,
        len(summary.activated), len(summary.retired),
        f", {summary.waiting} ready but the cap of {gate.max_active} is reached"
        if summary.waiting else "",
    )
    return summary


def _activate_ready(engine: Engine, gate: GateConfig, now: datetime) -> tuple[list[str], int]:
    """Activate what is ready, up to `max_active` discovered sources; the rest keep testing."""
    activated: list[str] = []
    with engine.begin() as conn:
        ready = db.ready(conn, gate)
        room = max(gate.max_active - db.active_discovered(conn), 0)
        for candidate in ready[:room]:
            source = discovered_source(
                host=candidate.host, feed_url=candidate.feed_url or "", name=candidate.name,
                found_by=candidate.found_by, now=now,
            )
            if db.activate(conn, candidate, source, now=now):
                activated.append(source.id)
                logger.info("discovery: activated %s (%s)", source.id, candidate.host)
            else:
                logger.warning("discovery: %s is already a source id; %s waits", source.id,
                               candidate.host)
    return activated, max(len(ready) - room, 0)
