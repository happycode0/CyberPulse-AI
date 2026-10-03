"""The AI budget: how much AI the worker may use right now (PLAN.md §7.4, §2.8).

The mode is set before the spend, from OpenRouter's own account of the key (`GET /api/v1/key`),
and tightened at once by what a failed call says. Four things push it down and nothing else
lifts it:

- a reading: the share of the month's budget left, as the table in §7.4 lays out;
- age: paid calls need a reading under `MAX_READING_AGE` old, so a poll that keeps failing
  degrades to free models instead of spending blind;
- a 402: `limit_source` says which limit, and each is handled as §2.8 says;
- `force`: an operator can make the worker stricter than the budget requires, never looser.

Degrading is the safe direction, so every unclear case goes that way.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

import httpx
from pydantic import SecretStr

from worker.ai.client import BudgetExhausted, CallFailed, RateLimited
from worker.ai.ladder import Ladder, Tier, VerifiedLadder
from worker.models import Severity

logger = logging.getLogger(__name__)

# Authenticated with the ordinary key: /credits would need a management key (§2.8).
KEY_URL = "https://openrouter.ai/api/v1/key"

# Lanes poll before each cycle and run every 15 minutes, so this tolerates one missed poll.
MAX_READING_AGE = timedelta(minutes=30)

# How long paid calls wait after a 402 or 429 that sent no Retry-After.
DEFAULT_PAUSE = timedelta(seconds=60)

_FULL_ABOVE = Decimal("0.50")
_MINIMAL_BELOW = Decimal("0.20")


class Mode(StrEnum):
    FULL = "full"  # over 50% of the month's budget left: the whole ladder
    CONSERVE = "conserve"  # 20 to 50%: tier 2 only for critical and KEV-linked events
    MINIMAL = "minimal"  # under 20%: tier 0 only, and only for the events that matter most
    FREE_ONLY = "free_only"  # paid tiers unavailable: :free models only, same events as MINIMAL
    OFF = "off"  # no model calls at all


_ORDER = tuple(Mode)


def stricter(*modes: Mode) -> Mode:
    return max(modes, key=_ORDER.index)


class BudgetUnreadable(Exception):
    """`GET /api/v1/key` gave no usable answer. Nothing is known, so nothing paid is licensed."""


@dataclass(frozen=True)
class KeyStatus:
    """The parts of `GET /api/v1/key` the budget uses.

    `label` is deliberately not kept: OpenRouter fills it with a fragment of the key itself.
    """

    limit: Decimal | None  # None: the key has no credit limit
    limit_remaining: Decimal | None
    limit_reset: str | None
    usage_daily: Decimal
    usage_monthly: Decimal
    free_requests_remaining: int | None


def _money(value: Any, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise BudgetUnreadable("a credit figure is missing or not a number")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise BudgetUnreadable("a credit figure is not a number") from None
    if not amount.is_finite():
        raise BudgetUnreadable("a credit figure is not finite")
    return amount


def parse_key_status(payload: Any) -> KeyStatus:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise BudgetUnreadable("no data object")
    free = data.get("free_model_daily_requests")
    remaining = free.get("remaining") if isinstance(free, dict) else None
    reset = data.get("limit_reset")
    return KeyStatus(
        limit=_money(data.get("limit"), optional=True),
        limit_remaining=_money(data.get("limit_remaining"), optional=True),
        limit_reset=reset if isinstance(reset, str) else None,
        usage_daily=_money(data.get("usage_daily")),
        usage_monthly=_money(data.get("usage_monthly")),
        free_requests_remaining=(
            remaining if isinstance(remaining, int) and not isinstance(remaining, bool) else None
        ),
    )


async def fetch_key_status(
    http: httpx.AsyncClient, api_key: SecretStr, user_agent: str, timeout: float = 30.0
) -> KeyStatus:
    try:
        r = await http.get(
            KEY_URL,
            headers={
                "Authorization": f"Bearer {api_key.get_secret_value()}",
                "User-Agent": user_agent,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise BudgetUnreadable(f"{type(exc).__name__} reading the key's budget") from exc
    if r.status_code in (401, 403):
        raise BudgetUnreadable(f"HTTP {r.status_code}: OpenRouter did not accept the key")
    if r.status_code != 200:
        raise BudgetUnreadable(f"HTTP {r.status_code}")
    try:
        payload = r.json()
    except ValueError:
        raise BudgetUnreadable("the response is not JSON") from None
    return parse_key_status(payload)


@dataclass(frozen=True)
class Reading:
    mode: Mode
    remaining_usd: Decimal
    share_left: Decimal
    reason: str


def assess(status: KeyStatus, monthly_budget: Decimal) -> Reading:
    """The mode this reading allows, by the share of the month's budget left (§7.4).

    What is left is the smaller of the budget minus this month's usage and what the key's own
    limit has left. The share is of the monthly budget, so a key limit below the budget makes the
    worker degrade early: the safe direction.
    """
    if monthly_budget <= 0:
        return Reading(Mode.OFF, Decimal(0), Decimal(0), "no monthly AI budget is set")
    left = monthly_budget - status.usage_monthly
    if status.limit_remaining is not None:
        left = min(left, status.limit_remaining)
    left = max(left, Decimal(0))
    share = left / monthly_budget
    if left == 0:
        mode, why = Mode.FREE_ONLY, "budget exhausted"
    elif share < _MINIMAL_BELOW:
        mode, why = Mode.MINIMAL, "under 20% of the budget left"
    elif share <= _FULL_ABOVE:
        mode, why = Mode.CONSERVE, "50% or less of the budget left"
    else:
        mode, why = Mode.FULL, "over 50% of the budget left"
    return Reading(mode, left, share, f"{why}: ${left:.2f} of ${monthly_budget:.2f}")


@dataclass(frozen=True)
class Work:
    """What a caller wants to spend on: the tier the task asks for, and the event it is for."""

    tier: Tier
    severity: Severity
    kev: bool = False
    developing: bool = False

    @property
    def matters_most(self) -> bool:
        """Critical, high, KEV-linked or developing: what MINIMAL still enriches (§7.4)."""
        return self.severity in (Severity.CRITICAL, Severity.HIGH) or self.kev or self.developing


@dataclass(frozen=True)
class Route:
    """The tier to call and the part of its chain that may be used."""

    tier: Tier
    models: tuple[str, ...]


def _free(slug: str) -> bool:
    return slug.endswith(":free")


def plan(mode: Mode, work: Work, ladder: Ladder, *, free_cap_hit: bool = False) -> Route | None:
    """Where `work` may go in `mode`, or None if it waits (the event is `pending_enrichment`)."""
    if mode is Mode.OFF:
        return None
    tier = work.tier
    if mode in (Mode.MINIMAL, Mode.FREE_ONLY):
        # The code and audit tiers have no free equivalent, so they wait rather than degrade.
        if not work.matters_most or tier in (Tier.CODE, Tier.AUDIT):
            return None
        tier = Tier.FREE
    elif mode is Mode.CONSERVE and tier is Tier.STRONG:
        if work.severity is not Severity.CRITICAL and not work.kev:
            tier = Tier.CHEAP
    if tier is Tier.FREE and free_cap_hit and mode in (Mode.FULL, Mode.CONSERVE):
        # A free model's daily cap does not clear with backoff, so step up a tier (§7.4).
        tier = Tier.CHEAP
    models = ladder.tiers[tier]
    if free_cap_hit:
        models = tuple(m for m in models if not _free(m))
    if mode is Mode.FREE_ONLY:
        models = tuple(m for m in models if _free(m))
    return Route(tier, tuple(models)) if models else None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Governor:
    """The budget state between polls, and the one place a call asks where it may go.

    Built once per worker with the ladder that passed the guard. `observe` takes each poll;
    `on_failure` takes each failed call; `route` answers each request.
    """

    def __init__(
        self,
        verified: VerifiedLadder,
        monthly_budget: Decimal,
        *,
        force: Mode | None = None,
        now: Callable[[], datetime] = _utcnow,
    ):
        self._ladder = verified.ladder
        self._budget = monthly_budget
        self._force = force or Mode.FULL
        self._now = now
        self._reading: Reading | None = None
        self._read_at: datetime | None = None
        self._paid_paused_until: datetime | None = None
        self._key_limit_hit = False
        self._free_cap_hit = False
        self.stopped: str | None = None  # set by a 402 on credits; only a restart clears it

    @property
    def reading(self) -> Reading | None:
        return self._reading

    def adopt(self, verified: VerifiedLadder) -> None:
        """Route on a ladder that passed the guard again. The budget state carries over."""
        self._ladder = verified.ladder

    def mode(self) -> tuple[Mode, str]:
        """The mode now, and the strictest reason for it."""
        now = self._now()
        if self.stopped:
            return Mode.OFF, self.stopped
        if self._reading is None:
            candidates = [(Mode.FREE_ONLY, "no budget reading yet")]
        elif now - self._read_at > MAX_READING_AGE:
            candidates = [(Mode.FREE_ONLY, "the last budget reading is stale")]
        else:
            candidates = [(self._reading.mode, self._reading.reason)]
        if self._key_limit_hit:
            candidates.append((Mode.FREE_ONLY, "OpenRouter says the key's limit is spent"))
        if self._paid_paused_until is not None and now < self._paid_paused_until:
            candidates.append((Mode.FREE_ONLY, "paid calls paused until Retry-After"))
        candidates.append((self._force, "forced by the operator"))
        return max(candidates, key=lambda c: _ORDER.index(c[0]))

    def observe(self, status: KeyStatus) -> Reading:
        """Take a poll. A fresh reading supersedes a key-limit 402, since it reports that limit."""
        self._reading = assess(status, self._budget)
        self._read_at = self._now()
        self._key_limit_hit = False
        if status.free_requests_remaining is not None:
            self._free_cap_hit = status.free_requests_remaining <= 0
        return self._reading

    def on_failure(self, exc: CallFailed, route: Route) -> None:
        """Tighten at once on what a failed call says (§2.8, §7.4). Others change nothing."""
        now = self._now()
        pause = timedelta(seconds=exc.retry_after) if exc.retry_after is not None else DEFAULT_PAUSE
        if isinstance(exc, BudgetExhausted):
            if exc.transient:
                self._paid_paused_until = now + pause
            elif exc.limit_source == "openrouter_credits":
                self.stopped = "the OpenRouter account is out of credits: a person has to top it up"
                logger.error("AI stopped: %s", self.stopped)
            else:
                # openrouter_key_limit, or a 402 that did not say which limit: paid calls stop
                # until a poll shows money left.
                self._key_limit_hit = True
        elif isinstance(exc, RateLimited):
            if any(_free(m) for m in route.models):
                self._free_cap_hit = True
            else:
                self._paid_paused_until = now + pause

    def route(self, work: Work) -> Route | None:
        mode, _ = self.mode()
        return plan(mode, work, self._ladder, free_cap_hit=self._free_cap_hit)
