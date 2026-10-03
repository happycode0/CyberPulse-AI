"""The worker's ops API: how Paperclip's agents see the worker (PLAN.md section 9, Stage 4).

It listens on port 8700 inside the worker's container. Only the compose network can reach it:
docker-compose.yml publishes no port for the worker, and Docker-published ports bypass the host
firewall, so none ever should be. There are two kinds of endpoint.

- **Wakes**, `POST /ops/agents/<callsign>/wake`, are called by the five `http` agents (ROGUE,
  LIBRARIAN, SERAPH, PROWL, LINK). The worker already runs each of their jobs on its own
  schedule, so a wake runs nothing. It answers 200 when that job is working and 503 when it is
  not, and Paperclip records the agent's run as succeeded or failed. A wake changes nothing and
  says only whether a job is healthy, so it needs no token, and the agents' configuration (a
  package in a public repository) carries none.
- **Reads**, `GET /ops/...`, are for the AI agents: MORPHEUS's digest, ZION's escalations. They
  need the bearer token in `CYBERPULSE_OPS_TOKEN`. Each runs in a read-only transaction with a
  statement timeout.

Every response passes the publisher's secret scan, or is withheld. Paperclip's `http` adapter
adds `paperclipRuntimeTools` to a wake's body, with a bearer token for Paperclip's own API in
it. The body is read for `job` and the run's id, and is never logged or stored.

The standard library's HTTP server is enough for a handful of requests an hour from one
neighbour, and adds no dependency to the worker image.
"""

import asyncio
import hmac
import json
import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from worker.ai.budget import BudgetUnreadable, KeyStatus, assess, fetch_key_status
from worker.db.digest import (
    ESCALATE_AU_RELEVANCE,
    EVENT_COLUMNS,
    event_row,
    ledger_totals,
    load_digest,
    month_bounds,
    truncate,
    usd,
)
from worker.db.events import load_events
from worker.db.jobs import JobRun, load_latest_completed_job, load_latest_job
from worker.db.sources import load_lifecycle_states, load_registry_rows
from worker.models import EventStatus, LifecycleState, Severity
from worker.publish.build import _source_health_payload
from worker.publish.claims import with_fact_claims
from worker.publish.validate import scan_for_secrets, scan_text_for_secrets
from worker.settings import Settings

logger = logging.getLogger(__name__)

# The http agents' URL is http://worker:8700/ops/agents/<callsign>/wake
# (OPS_WAKE_URL in ops/build-paperclip-package.py).
PORT = 8700

# Each http agent's job, as its payload template names it (docs/wiki/stage-4b-the-crew.md).
WAKE_JOBS: dict[str, str] = {
    "rogue": "cost-reconcile",
    "librarian": "groundtruth",
    "seraph": "source-verify",
    "prowl": "correlation-report",
    "link": "publish",
}

# A collection runs every 15 minutes and publishes after it, so 30 minutes is one missed run.
COLLECTION_FRESH = timedelta(minutes=30)
# The ground-truth sync runs every 6 hours and takes about a minute.
GROUNDTRUTH_FRESH = timedelta(hours=7)

MAX_BODY_BYTES = 1 << 20  # a wake's body is well under 10 KB
MAX_CONCURRENT = 4
SOCKET_TIMEOUT_SECONDS = 15
BODY_DEADLINE_SECONDS = 15
STATEMENT_TIMEOUT = "15s"
# token_urlsafe(32) gives 43 characters; anything this short was not generated for the job.
MIN_TOKEN_LENGTH = 32
KEY_STATUS_TTL_SECONDS = 300
KEY_STATUS_TIMEOUT_SECONDS = 15.0

DIGEST_HOURS = (24, 1, 168)  # default, lowest, highest
EVENTS_LIMIT = (50, 1, 200)
RUNS_LIMIT = (20, 1, 100)
ERRORS_PER_RUN = 5
QUERY_MAX_CHARS = 100

_WAKE_PATH = re.compile(r"/ops/agents/(?P<slug>[a-z0-9-]{1,40})/wake")
_EVENT_PATH = re.compile(r"/ops/events/(?P<event_id>[^/]{1,40})")
_EVENT_ID = re.compile(r"evt-\d{4}-\d{6}")
_RUN_REF = re.compile(r"[A-Za-z0-9._:-]{1,100}")
_MONTH = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")
_DIGITS = re.compile(r"\d{1,9}")
_LOGGABLE = re.compile(r"[^A-Za-z0-9/_.-]")

_WITHHELD = "response withheld: it failed the secret scan"


class ClientError(Exception):
    """A request this API will not answer as asked: 400 with the message."""


@dataclass(frozen=True)
class Reply:
    status: int
    body: dict[str, Any]
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Response:
    """A reply rendered to JSON and passed by the secret scan."""

    status: int
    data: bytes
    headers: tuple[tuple[str, str], ...] = ()

    def json(self) -> Any:
        return json.loads(self.data)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    summary: dict[str, Any] = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"{type(value).__name__} is not JSON")


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _error(status: int, message: str, headers: tuple[tuple[str, str], ...] = ()) -> Reply:
    return Reply(status, {"error": message}, headers)


def _loggable(target: str) -> str:
    """The path alone, safe to log: no query string, nothing but plain path characters."""
    return _LOGGABLE.sub("?", target.split("?", 1)[0])[:80]


# --- Query parameters ------------------------------------------------------------------------


def _one(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise ClientError(f"{name} is given more than once")
    return values[0] or None


def _int(query: Mapping[str, list[str]], name: str, bounds: tuple[int, int, int]) -> int:
    default, low, high = bounds
    raw = _one(query, name)
    if raw is None:
        return default
    if not _DIGITS.fullmatch(raw):
        raise ClientError(f"{name} must be a whole number")
    value = int(raw)
    if not low <= value <= high:
        raise ClientError(f"{name} must be between {low} and {high}")
    return value


def _only(query: Mapping[str, list[str]], *allowed: str) -> None:
    unknown = sorted(set(query) - set(allowed))
    if unknown:
        accepted = ", ".join(allowed) or "none"
        raise ClientError(f"unknown parameter {unknown[0][:40]!r}; this endpoint takes: {accepted}")


def _since(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise ClientError("since must be an ISO 8601 time, such as 2026-10-03T00:00:00Z") from None
    if value.tzinfo is None:
        raise ClientError("since must carry a time zone, such as Z")
    return value


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --- Rows to JSON ------------------------------------------------------------------------------


def _job_row(run: JobRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "completed": run.completed,
        "errors": run.errors,
        "changed": run.changed,
    }


def _run_row(row: Mapping[str, Any]) -> dict[str, Any]:
    errors = list(row["errors"] or [])
    out = {k: row[k] for k in row.keys() if k != "errors"}  # noqa: SIM118 - a RowMapping
    out["error_count"] = len(errors)
    out["errors"] = [truncate(e) for e in errors[:ERRORS_PER_RUN]]
    return out


# --- The API ------------------------------------------------------------------------------------


def openrouter_key_reader(settings: Settings) -> Callable[[], KeyStatus]:
    """What `GET /api/v1/key` says about the worker's OpenRouter key, read on request."""

    def read() -> KeyStatus:
        key = settings.openrouter_api_key
        if key is None or not key.get_secret_value():
            raise BudgetUnreadable("no OpenRouter key is set on this host")

        async def fetch() -> KeyStatus:
            async with httpx.AsyncClient() as http:
                return await fetch_key_status(
                    http, key, settings.user_agent, timeout=KEY_STATUS_TIMEOUT_SECONDS
                )

        return asyncio.run(fetch())

    return read


class OpsApi:
    """The endpoints, with no sockets: `handle` takes a request and returns the response."""

    def __init__(
        self,
        *,
        engine: Engine,
        token: SecretStr | None,
        data_dir: Path,
        key_status: Callable[[], KeyStatus],
        monthly_budget: Decimal,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        value = token.get_secret_value() if token is not None else ""
        self._token = value.encode() if len(value) >= MIN_TOKEN_LENGTH else None
        self._engine = engine
        self._data_dir = data_dir
        self._read_key = key_status
        self._budget = monthly_budget
        self._clock = clock
        self._key_lock = threading.Lock()
        self._key_cache: tuple[float, KeyStatus | BudgetUnreadable] | None = None
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)

    def __repr__(self) -> str:
        return f"OpsApi(reads_open={self.reads_open})"

    @property
    def reads_open(self) -> bool:
        return self._token is not None

    # --- Dispatch ---

    def handle(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes = b""
    ) -> Response:
        try:
            reply = self._route(method, target, headers, body)
        except ClientError as exc:
            reply = _error(400, str(exc))
        except SQLAlchemyError:
            logger.exception("ops %s %s: the database could not be read", method, _loggable(target))
            reply = _error(503, "the database could not be read")
        except Exception:
            logger.exception("ops %s %s failed", method, _loggable(target))
            reply = _error(500, "internal error; the worker's log has the details")
        return self.render(reply, target)

    def render(self, reply: Reply, target: str = "") -> Response:
        """JSON, or the withheld notice if the secret scan finds anything in it."""
        try:
            data = json.dumps(
                reply.body, default=_json_default, ensure_ascii=False, separators=(",", ":")
            )
            findings = scan_for_secrets(json.loads(data)) + scan_text_for_secrets(data)
        except Exception as exc:  # noqa: BLE001 - an unscanned response is never sent
            findings = [f"the scan could not run ({type(exc).__name__})"]
        if findings:
            # Findings name where and what matched, never the value, so they are safe to log.
            logger.error("ops %s: %s; %s", _loggable(target), _WITHHELD, "; ".join(findings))
            return Response(500, json.dumps({"error": _WITHHELD}).encode())
        return Response(reply.status, data.encode(), reply.headers)

    def _route(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> Reply:
        parts = urlsplit(target)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=20)

        if path == "/ops/health":
            if method != "GET":
                return _error(405, "use GET", (("Allow", "GET"),))
            return Reply(200, {"ok": True})

        if wake := _WAKE_PATH.fullmatch(path):
            if method != "POST":
                return _error(405, "use POST", (("Allow", "POST"),))
            return self._wake(wake["slug"], body)

        if path != "/ops" and not path.startswith("/ops/"):
            return _error(404, "not found; the ops API is under /ops")
        if method != "GET":
            return _error(405, "the read endpoints take GET", (("Allow", "GET"),))
        if (refused := self._authorise(headers)) is not None:
            return refused

        if event := _EVENT_PATH.fullmatch(path):
            _only(query)
            return self._event(event["event_id"])
        reads: dict[str, Callable[[Mapping[str, list[str]]], Reply]] = {
            "/ops": self._index,
            "/ops/digest": self._digest,
            "/ops/events": self._events,
            "/ops/sources": self._sources,
            "/ops/runs": self._runs,
            "/ops/cost": self._cost,
            "/ops/jobs": self._jobs,
        }
        read = reads.get(path)
        if read is None:
            return _error(404, "no such endpoint; GET /ops lists them")
        return read(query)

    def _authorise(self, headers: Mapping[str, str]) -> Reply | None:
        if self._token is None:
            return _error(
                503,
                "the read endpoints are closed: CYBERPULSE_OPS_TOKEN is not set on the worker, "
                f"or is shorter than {MIN_TOKEN_LENGTH} characters",
            )
        scheme, _, given = (headers.get("Authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode(), self._token
        ):
            return _error(
                401,
                "send Authorization: Bearer $CYBERPULSE_OPS_TOKEN",
                (("WWW-Authenticate", 'Bearer realm="cyberpulse-ops"'),),
            )
        return None

    @contextmanager
    def _read(self) -> Iterator[Connection]:
        """A connection in a read-only transaction that is always rolled back."""
        with self._engine.connect() as conn:
            transaction = conn.begin()
            try:
                conn.execute(text("set transaction read only"))
                conn.execute(text(f"set local statement_timeout = '{STATEMENT_TIMEOUT}'"))
                yield conn
            finally:
                transaction.rollback()

    # --- Wakes ---

    def _wake(self, slug: str, body: bytes) -> Reply:
        job = WAKE_JOBS.get(slug)
        if job is None:
            return _error(404, f"no http agent is called {slug}; they are {', '.join(WAKE_JOBS)}")
        try:
            payload = json.loads(body) if body.strip() else {}
        except (ValueError, UnicodeDecodeError, RecursionError):
            return _error(400, "the body is not JSON")
        if not isinstance(payload, dict):
            return _error(400, "the body is not a JSON object")
        if payload.get("job") != job:
            return _error(400, f'{slug} runs one job: send {{"job": "{job}"}}')
        run_id = payload.get("runId")
        run_ref = run_id if isinstance(run_id, str) and _RUN_REF.fullmatch(run_id) else "-"

        verdict = self.verdict(job)
        logger.info("wake %s (%s), run %s: %s", slug, job, run_ref, "ok" if verdict.ok else "failing")
        return Reply(
            200 if verdict.ok else 503,
            {
                "agent": slug,
                "job": job,
                "ok": verdict.ok,
                "checked_at": self._clock(),
                "summary": verdict.summary,
            },
        )

    def verdict(self, job: str) -> Verdict:
        """Whether `job` is working, and what was looked at to decide."""
        checks: dict[str, Callable[[], Verdict]] = {
            "cost-reconcile": self._cost_verdict,
            "groundtruth": self._groundtruth_verdict,
            "source-verify": self._sources_verdict,
            "correlation-report": self._correlation_verdict,
            "publish": self._publish_verdict,
        }
        return checks[job]()

    def _fresh(self, at: datetime | None, within: timedelta, what: str) -> Verdict:
        if at is None:
            return Verdict(False, {"reason": f"no {what} is recorded"})
        age = self._clock() - at
        ok = age <= within
        summary: dict[str, Any] = {"last": at, "age_minutes": _minutes(age)}
        if not ok:
            summary["reason"] = (
                f"the last {what} was {_minutes(age)} minutes ago; "
                f"one is expected within {_minutes(within)}"
            )
        return Verdict(ok, summary)

    def _groundtruth_verdict(self) -> Verdict:
        with self._read() as conn:
            done = load_latest_completed_job(conn, "groundtruth")
            latest = load_latest_job(conn, "groundtruth")
        verdict = self._fresh(
            done.finished_at if done else None, GROUNDTRUTH_FRESH, "completed ground-truth pass"
        )
        verdict.summary["last_pass"] = _job_row(latest)
        if done is not None:
            verdict.summary["last_completed_errors"] = done.errors
        return verdict

    def _sources_verdict(self) -> Verdict:
        with self._read() as conn:
            last = conn.execute(text("select max(checked_at) from source_health")).scalar_one()
            latest = dict(
                conn.execute(
                    text(
                        "select distinct on (h.source_id) h.source_id, h.status "
                        "from source_health h join source_registry r on r.id = h.source_id "
                        "where r.enabled order by h.source_id, h.checked_at desc, h.id desc"
                    )
                ).all()
            )
            registry = load_registry_rows(conn)
            states = load_lifecycle_states(conn)
        enabled = [r for r in registry if r.enabled]
        lifecycle = Counter(
            (states.get(r.id) or LifecycleState.ACTIVE).value for r in enabled
        )
        verdict = self._fresh(last, COLLECTION_FRESH, "source health check")
        verdict.summary.update(
            sources_enabled=len(enabled),
            latest_status=dict(Counter(latest.get(r.id, "no_data") for r in enabled)),
            lifecycle=dict(lifecycle),
            not_ok=sorted(r.id for r in enabled if latest.get(r.id, "no_data") != "ok"),
        )
        return verdict

    def _correlation_verdict(self) -> Verdict:
        now = self._clock()
        with self._read() as conn:
            run = (
                conn.execute(
                    text(
                        "select run_id, lane, started_at, finished_at, new_events, "
                        "updated_events, duplicates, archived_events, errors from runs "
                        "where finished_at is not null order by finished_at desc, run_id limit 1"
                    )
                )
                .mappings()
                .first()
            )
            merged, standing = conn.execute(
                text(
                    "select count(*) filter (where merged_into is not null "
                    "and updated_at >= :since), "
                    "count(*) filter (where merged_into is null and status <> 'archived') "
                    "from events"
                ),
                {"since": now - timedelta(hours=24)},
            ).one()
        verdict = self._fresh(run["finished_at"] if run else None, COLLECTION_FRESH, "collection")
        verdict.summary.update(
            last_run=_run_row(run) if run else None,
            merged_last_24h=merged,
            standing_events=standing,
        )
        return verdict

    def _publish_verdict(self) -> Verdict:
        path = self._data_dir / "system-status.json"
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
            generated = datetime.fromisoformat(status["generated_at"])
            if generated.tzinfo is None:
                raise ValueError("no time zone")
        except FileNotFoundError:
            return Verdict(False, {"reason": "nothing has been published on this host"})
        except (OSError, ValueError, KeyError, TypeError):
            return Verdict(False, {"reason": "system-status.json cannot be read"})
        verdict = self._fresh(generated, COLLECTION_FRESH, "publish")
        counts = status.get("counts") if isinstance(status.get("counts"), dict) else {}
        verdict.summary.update(
            last_completed_collection=status.get("last_completed_collection"),
            events_published=counts.get("events_published"),
        )
        return verdict

    def _cost_verdict(self) -> Verdict:
        start, end = month_bounds(self._clock())
        with self._read() as conn:
            ledger = ledger_totals(conn, start, end)
        summary: dict[str, Any] = {"month": f"{start:%Y-%m}", "ledger": ledger}
        reasons = []
        if ledger["unknown_cost_calls"]:
            reasons.append(f"{ledger['unknown_cost_calls']} calls this month have no billed cost")
        key = self._key_summary(ledger["cost_usd"])
        summary["key"] = key
        if "unreadable" in key:
            reasons.append(f"the key's budget cannot be read: {key['unreadable']}")
        if reasons:
            summary["reason"] = "; ".join(reasons)
        return Verdict(not reasons, summary)

    # --- Money ---

    def _key_status(self) -> KeyStatus:
        """The key's status, read at most every 5 minutes, however often agents ask."""
        with self._key_lock:
            now = time.monotonic()
            if self._key_cache is None or now - self._key_cache[0] >= KEY_STATUS_TTL_SECONDS:
                try:
                    self._key_cache = (now, self._read_key())
                except BudgetUnreadable as exc:
                    self._key_cache = (now, exc)
            reading = self._key_cache[1]
        if isinstance(reading, BudgetUnreadable):
            raise reading
        return reading

    def _key_summary(self, ledger_usd: float | None) -> dict[str, Any]:
        """Key usage against the ledger. The gap is spend on the same key the ledger never
        sees: the Paperclip agents' own model calls."""
        try:
            status = self._key_status()
        except BudgetUnreadable as exc:
            return {"unreadable": str(exc)}
        reading = assess(status, self._budget)
        used = usd(status.usage_monthly) or 0.0
        return {
            "usage_month_usd": used,
            "usage_today_usd": usd(status.usage_daily),
            "limit_usd": usd(status.limit),
            "limit_remaining_usd": usd(status.limit_remaining),
            "monthly_budget_usd": usd(self._budget),
            "mode": reading.mode.value,
            "remaining_usd": usd(reading.remaining_usd),
            "outside_ledger_usd": round(max(used - (ledger_usd or 0.0), 0.0), 6),
        }

    # --- Reads ---

    def _index(self, query: Mapping[str, list[str]]) -> Reply:
        _only(query)
        return Reply(
            200,
            {
                "reads": {
                    "/ops/digest?hours=24": "what changed in the window: events, the most "
                    "prominent, escalation candidates, source-health changes, runs, cost",
                    "/ops/events?status=live&severity=&since=&q=&limit=50": "events, most "
                    "prominent first; status is live, archived, all or one status",
                    "/ops/events/<event_id>": "one event as the site publishes it",
                    "/ops/sources": "source health, as source-health.json",
                    "/ops/runs?limit=20": "the latest collection runs",
                    "/ops/cost?month=YYYY-MM": "the cost ledger by stage, model and agent, "
                    "and the key's own usage",
                    "/ops/jobs": "every http agent's verdict, and the worker's latest passes",
                },
                "wakes": {
                    f"POST /ops/agents/{slug}/wake": job for slug, job in WAKE_JOBS.items()
                },
                "note": "Everything here is read-only. Open escalations are Paperclip issues, "
                "which this API does not see.",
            },
        )

    def _digest(self, query: Mapping[str, list[str]]) -> Reply:
        _only(query, "hours")
        hours = _int(query, "hours", DIGEST_HOURS)
        with self._read() as conn:
            digest = load_digest(conn, now=self._clock(), hours=hours)
        return Reply(
            200,
            {
                **digest,
                "notes": [
                    (
                        "Escalation candidates are critical, or Australian relevance of "
                        f"{ESCALATE_AU_RELEVANCE} or more, and new or updated in the window."
                    ),
                    "New events archived on arrival are a feed's back catalogue, taken in once.",
                    "Open escalations are Paperclip issues, which this API does not see.",
                ],
            },
        )

    def _events(self, query: Mapping[str, list[str]]) -> Reply:
        _only(query, "status", "severity", "since", "q", "limit")
        clauses = ["e.merged_into is null"]
        params: dict[str, Any] = {"limit": _int(query, "limit", EVENTS_LIMIT)}
        status = _one(query, "status") or "live"
        if status == "live":
            clauses.append("e.status <> 'archived'")
        elif status != "all":
            if status not in set(EventStatus):
                raise ClientError("status must be live, archived, all or an event status")
            clauses.append("e.status = :status")
            params["status"] = status
        if (severity := _one(query, "severity")) is not None:
            if severity not in set(Severity):
                raise ClientError(f"severity must be one of {', '.join(Severity)}")
            clauses.append("e.severity = :severity")
            params["severity"] = severity
        if (since := _one(query, "since")) is not None:
            clauses.append("(e.first_seen >= :since or e.last_material_update >= :since)")
            params["since"] = _since(since)
        if (term := _one(query, "q")) is not None:
            if len(term) > QUERY_MAX_CHARS:
                raise ClientError(f"q is at most {QUERY_MAX_CHARS} characters")
            clauses.append("e.title ilike :q escape '\\'")
            params["q"] = _like(term)
        with self._read() as conn:
            rows = conn.execute(
                text(
                    f"select {EVENT_COLUMNS} from events e where {' and '.join(clauses)} "
                    "order by e.prominence desc nulls last, "
                    "e.last_material_update desc nulls last, e.event_id limit :limit"
                ),
                params,
            ).mappings()
            events = [event_row(r) for r in rows]
        return Reply(200, {"count": len(events), "events": events})

    def _event(self, event_id: str) -> Reply:
        if not _EVENT_ID.fullmatch(event_id):
            raise ClientError("an event id looks like evt-2026-001234")
        with self._read() as conn:
            row = conn.execute(
                text("select merged_into from events where event_id = :id"), {"id": event_id}
            ).first()
            if row is None:
                return _error(404, f"no event {event_id}")
            [event] = load_events(conn, [event_id])
            public = with_fact_claims(event).model_dump_public()
        return Reply(200, {"event": public, "merged_into": row.merged_into})

    def _sources(self, query: Mapping[str, list[str]]) -> Reply:
        _only(query)
        with self._read() as conn:
            payload = _source_health_payload(conn, _iso(self._clock()))
        return Reply(200, payload)

    def _runs(self, query: Mapping[str, list[str]]) -> Reply:
        _only(query, "limit")
        limit = _int(query, "limit", RUNS_LIMIT)
        with self._read() as conn:
            rows = conn.execute(
                text(
                    "select run_id, lane, started_at, finished_at, sources_ok, sources_failed, "
                    "sources_stale, items_fetched, new_events, updated_events, duplicates, "
                    "archived_events, errors from runs "
                    "order by started_at desc, run_id limit :limit"
                ),
                {"limit": limit},
            ).mappings()
            runs = [_run_row(r) for r in rows]
        return Reply(200, {"count": len(runs), "runs": runs})

    def _cost(self, query: Mapping[str, list[str]]) -> Reply:
        _only(query, "month")
        now = self._clock()
        month = _one(query, "month")
        if month is None:
            start, end = month_bounds(now)
        elif match := _MONTH.fullmatch(month):
            start, end = month_bounds(
                datetime(int(match[1]), int(match[2]), 1, tzinfo=UTC)
            )
        else:
            raise ClientError("month looks like 2026-10")

        def grouped(conn: Connection, column: str) -> list[dict[str, Any]]:
            rows = conn.execute(
                text(
                    f"select {column} as key, count(*) as calls, "
                    "coalesce(sum(cost_usd), 0) as cost_usd, "
                    "count(*) filter (where cost_usd is null) as unknown_cost_calls, "
                    "coalesce(sum(tokens_in), 0) as tokens_in, "
                    "coalesce(sum(tokens_out), 0) as tokens_out "
                    "from cost_ledger where ts >= :start and ts < :end "
                    "group by 1 order by cost_usd desc, calls desc, key"
                ),
                {"start": start, "end": end},
            ).mappings()
            return [
                {**dict(r), "cost_usd": usd(r["cost_usd"]), "tokens_in": int(r["tokens_in"]),
                 "tokens_out": int(r["tokens_out"])}
                for r in rows
            ]

        # Column expressions are fixed here, never taken from the request.
        with self._read() as conn:
            totals = ledger_totals(conn, start, end)
            by_stage = grouped(conn, "stage")
            by_model = grouped(conn, "coalesce(model, requested_model, 'unknown')")
            by_agent = grouped(conn, "coalesce(agent, 'none')")
            by_outcome = grouped(conn, "outcome")
        body: dict[str, Any] = {
            "month": f"{start:%Y-%m}",
            "ledger": totals,
            "by_stage": by_stage,
            "by_model": by_model,
            "by_agent": by_agent,
            "by_outcome": by_outcome,
        }
        if start <= now < end:
            body["key"] = self._key_summary(totals["cost_usd"])
        return Reply(200, body)

    def _jobs(self, query: Mapping[str, list[str]]) -> Reply:
        _only(query)
        wakes = {}
        for slug, job in WAKE_JOBS.items():
            verdict = self.verdict(job)
            wakes[slug] = {"job": job, "ok": verdict.ok, "summary": verdict.summary}
        with self._read() as conn:
            passes = {
                job: _job_row(load_latest_job(conn, job)) for job in ("groundtruth", "enrichment")
            }
        return Reply(200, {"checked_at": self._clock(), "wakes": wakes, "passes": passes})


# --- HTTP ----------------------------------------------------------------------------------------


class _BodyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Handler(BaseHTTPRequestHandler):
    server: "OpsServer"
    # One request per connection: nothing here is worth keeping a socket open for.
    protocol_version = "HTTP/1.0"
    server_version = "cyberpulse-ops"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_SECONDS

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format: str, *args: Any) -> None:
        """Silenced: each request is logged once, without its query string, by `_serve`."""

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def do_PUT(self) -> None:
        self._serve("PUT")

    def do_PATCH(self) -> None:
        self._serve("PATCH")

    def do_DELETE(self) -> None:
        self._serve("DELETE")

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise _BodyError(411, "send a Content-Length; a chunked body is not read")
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        if not _DIGITS.fullmatch(raw.strip()):
            raise _BodyError(400, "Content-Length is not a number")
        left = int(raw)
        if left > MAX_BODY_BYTES:
            raise _BodyError(413, f"the body is over {MAX_BODY_BYTES} bytes")
        deadline = time.monotonic() + BODY_DEADLINE_SECONDS
        chunks = []
        try:
            while left > 0:
                if time.monotonic() > deadline:
                    raise _BodyError(408, "the body took too long to arrive")
                chunk = self.rfile.read1(min(left, 65_536))
                if not chunk:
                    raise _BodyError(400, "the body ended before its Content-Length")
                chunks.append(chunk)
                left -= len(chunk)
        except TimeoutError:
            raise _BodyError(408, "the body took too long to arrive") from None
        return b"".join(chunks)

    def _serve(self, method: str) -> None:
        api = self.server.api
        if not api.slots.acquire(blocking=False):
            response = api.render(_error(503, "busy: too many requests at once; try again"))
        else:
            try:
                try:
                    body = self._read_body()
                except _BodyError as exc:
                    response = api.render(_error(exc.status, str(exc)))
                else:
                    response = api.handle(method, self.path, self.headers, body)
            finally:
                api.slots.release()
        self._send(response)
        if response.status != 200 or _loggable(self.path) != "/ops/health":
            # A passing healthcheck, every minute, would bury everything else.
            logger.info("ops %s %s %d", method, _loggable(self.path), response.status)

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response.data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.data)


class OpsServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address: tuple[str, int], api: OpsApi) -> None:
        self.api = api
        super().__init__(address, _Handler)


def start(settings: Settings, engine: Engine) -> OpsServer | None:
    """Serve the ops API from a background thread; None if it cannot listen.

    Not listening is logged and survived: collection and publishing never depend on Paperclip,
    so they must not depend on the API it calls either.
    """
    api = OpsApi(
        engine=engine,
        token=settings.cyberpulse_ops_token,
        data_dir=settings.data_dir,
        key_status=openrouter_key_reader(settings),
        monthly_budget=settings.ai_monthly_budget_usd,
    )
    try:
        server = OpsServer((settings.ops_api_host, settings.ops_api_port), api)
    except OSError as exc:
        logger.error(
            "ops API not started: cannot listen on port %d (%s); collection carries on",
            settings.ops_api_port,
            exc.strerror or type(exc).__name__,
        )
        return None
    threading.Thread(target=server.serve_forever, name="ops-api", daemon=True).start()
    logger.info(
        "ops API listening on port %d; read endpoints %s",
        server.server_address[1],
        "need the token" if api.reads_open
        else f"closed (CYBERPULSE_OPS_TOKEN unset or under {MIN_TOKEN_LENGTH} characters)",
    )
    return server


def stop(server: OpsServer) -> None:
    server.shutdown()
    server.server_close()
