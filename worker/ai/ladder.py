"""The model ladder and the price-ceiling guard (PLAN.md §7.1, §7.2).

`config/models.yaml` names the models; this module decides whether the AI layer may use them. A
model passes only if OpenRouter currently lists at least one *route* for it (one provider serving
it) that offers both `tools` and `structured_outputs` at no more than
`OUTPUT_CEILING_USD_PER_MTOK`. In tier 2 the route must also be at one of `TIER2_QUANTIZATIONS`,
because tier 2 requests pin those.

Routes rather than the model's headline price, because the headline is one route's price and not
necessarily one a request could use. Measured 2026-10-03: `deepseek/deepseek-v4-flash-0731` is
listed at $1.28/M output, which is the price of its cheapest-input route. That route has no
structured outputs, so `require_parameters` would never send a request there; the routes that do
have them cost $0.18 to $1.60. Checking the headline would reject a model with a dozen usable
routes, and would equally pass one whose only capable route is over the ceiling. The guard proves
a usable route exists. Keeping each call on such a route is the request's own price cap (§7.2).

Failing closed here means the AI layer stays off, not that the worker stops: see worker/ai.
"""

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from worker.settings import get_settings

# Owner policy, 2026-09-30 (PLAN.md §7.1). A constant rather than a setting on purpose: raising it
# should be a reviewed commit with a PLAN.md entry, not a line edited in one machine's .env.
OUTPUT_CEILING_USD_PER_MTOK = Decimal("1.00")

# §7.1 filters every candidate on both. Enrichment calls send no tools, but the agent tiers do,
# and one ladder serves both.
REQUIRED_PARAMETERS = frozenset({"tools", "structured_outputs"})

# What tier 2 requests pin (§7.2). A tier 2 route at any other quantisation is one they cannot use.
TIER2_QUANTIZATIONS = frozenset({"bf16", "fp8", "unknown"})

# Public: no key is sent, so this check can neither spend credit nor expose the key.
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{slug}/endpoints"

DEFAULT_LADDER_PATH = Path(__file__).resolve().parents[2] / "config" / "models.yaml"

_PER_MTOK = Decimal(1_000_000)


class Tier(StrEnum):
    FREE = "tier0_free"
    CHEAP = "tier1_cheap"
    STRONG = "tier2_strong"
    CODE = "code"
    AUDIT = "audit"


def vendor(slug: str) -> str:
    return slug.split("/", 1)[0]


def _check_slug(tier: Tier, slug: str) -> None:
    author, _, name = slug.partition("/")
    if not author or not name:
        raise ValueError(f"{tier.value}: {slug!r} is not an author/model slug")
    # A router picks the model per request, so neither its price nor its capabilities can be
    # checked in advance. That covers openrouter/free as well as openrouter/auto.
    if author == "openrouter":
        raise ValueError(f"{tier.value}: {slug} is a router, not a model")
    # Batch is asynchronous, which cannot serve a 15-minute collection lane (§7.1).
    if slug.endswith(":batch"):
        raise ValueError(f"{tier.value}: {slug} is a batch variant")


class Ladder(BaseModel):
    """`config/models.yaml`: each tier's fallback chain, default first.

    Everything here is checked without the network, so a malformed file is rejected whatever
    OpenRouter says.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tiers: dict[Tier, tuple[str, ...]]

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        missing = set(Tier) - set(self.tiers)
        if missing:
            raise ValueError(f"tiers missing: {sorted(t.value for t in missing)}")
        for tier, chain in self.tiers.items():
            if not chain:
                raise ValueError(f"{tier.value} is empty")
            if len(set(chain)) != len(chain):
                raise ValueError(f"{tier.value} names a model twice")
            for slug in chain:
                _check_slug(tier, slug)
        # :free variants are withdrawn without notice (§7.1). A chain that is free all the way
        # down stops working the day its last one goes, so the last link must be paid for.
        if self.tiers[Tier.FREE][-1].endswith(":free"):
            raise ValueError("tier0_free must end in a paid model")
        # Two-person rule (§7.1): TRON never reviews WHEELJACK's code on the same vendor's model.
        # Checked down both whole chains, because a fallback is a model that does get used.
        shared = {vendor(s) for s in self.tiers[Tier.CODE]} & {
            vendor(s) for s in self.tiers[Tier.AUDIT]
        }
        if shared:
            raise ValueError(f"code and audit share a vendor: {sorted(shared)}")
        return self

    def slugs(self) -> set[str]:
        return {slug for chain in self.tiers.values() for slug in chain}

    @classmethod
    def load(cls, path: Path = DEFAULT_LADDER_PATH) -> "Ladder":
        with open(path) as f:
            return cls.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class Breach:
    tier: Tier
    slug: str
    reason: str

    def __str__(self) -> str:
        return f"{self.tier.value}: {self.slug}: {self.reason}"


class LadderUnusable(Exception):
    """The ladder did not pass, for whatever reason. Every subclass means the AI layer stays off."""


class LadderInvalid(LadderUnusable):
    """`config/models.yaml` is missing or malformed, so there was nothing to check."""


class LadderRejected(LadderUnusable):
    """At least one configured model breaks the guard, so none of them may be called."""

    def __init__(self, breaches: list[Breach]):
        self.breaches = breaches
        super().__init__("; ".join(str(b) for b in breaches))


class CatalogueUnavailable(LadderUnusable):
    """OpenRouter's catalogue could not be read. Nothing was checked, so this fails closed too."""


@dataclass(frozen=True)
class VerifiedLadder:
    """A ladder that passed the guard, and when.

    Only `verify_ladder` makes one. Code that calls a model takes this rather than a `Ladder`,
    so a ladder nobody has checked cannot reach OpenRouter.
    """

    ladder: Ladder
    checked_at: datetime


def _per_mtok(price: Any) -> Decimal | None:
    """OpenRouter's per-token price string as US$ per million tokens, or None if it is not one.

    A negative price is how the catalogue marks a router whose cost depends on the model it picks,
    so it is not a fixed price at all.
    """
    try:
        value = Decimal(str(price))
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return value * _PER_MTOK


def _output_price(route: Mapping[str, Any]) -> Decimal | None:
    """The route's output price, provided both of its prices are fixed."""
    pricing = route.get("pricing") or {}
    if _per_mtok(pricing.get("prompt")) is None:
        return None
    return _per_mtok(pricing.get("completion"))


def breach_reason(routes: list[Mapping[str, Any]] | None, tier: Tier) -> str | None:
    """Why no route for this model is usable in this tier, or None if one is.

    The checks run in the order a person would fix them, so the reason names the first thing
    wrong rather than the last.
    """
    if routes is None:
        return "not on OpenRouter (withdrawn, or the slug is wrong)"
    capable = [r for r in routes if REQUIRED_PARAMETERS <= set(r.get("supported_parameters") or ())]
    if not capable:
        return "no route offers both tools and structured outputs"
    if tier is Tier.STRONG:
        capable = [r for r in capable if r.get("quantization") in TIER2_QUANTIZATIONS]
        if not capable:
            return f"no capable route at {', '.join(sorted(TIER2_QUANTIZATIONS))} quantisation"
    prices = [p for r in capable if (p := _output_price(r)) is not None]
    if not prices:
        return "no capable route has a fixed price"
    cheapest = min(prices)
    if cheapest > OUTPUT_CEILING_USD_PER_MTOK:
        return (
            f"cheapest capable route is ${cheapest:.2f}/M output, "
            f"over the ${OUTPUT_CEILING_USD_PER_MTOK}/M ceiling"
        )
    return None


def check_ladder(
    ladder: Ladder, routes: Mapping[str, list[Mapping[str, Any]] | None]
) -> list[Breach]:
    """Every model in every tier that has no usable route. Empty means the ladder passes.

    Per tier rather than per model, because the same model can pass in one tier and fail in
    another: tier 2 is the only one that pins quantisation.
    """
    breaches = []
    for tier, chain in ladder.tiers.items():
        for slug in chain:
            reason = breach_reason(routes.get(slug), tier)
            if reason is not None:
                breaches.append(Breach(tier, slug, reason))
    return breaches


async def fetch_routes(
    client: httpx.AsyncClient, slugs: Iterable[str], timeout: float = 30.0
) -> dict[str, list[Mapping[str, Any]] | None]:
    """Each model's routes from OpenRouter, or None for a model it does not list (a 404).

    Anything else that is not a readable 200 raises `CatalogueUnavailable`. That includes a 5xx
    for one model out of twelve: a model that could not be checked has not passed.
    """

    async def one(slug: str) -> tuple[str, list[Mapping[str, Any]] | None]:
        try:
            r = await client.get(ENDPOINTS_URL.format(slug=slug), timeout=timeout)
        except httpx.HTTPError as exc:
            raise CatalogueUnavailable(f"{slug}: {type(exc).__name__}") from exc
        if r.status_code == 404:
            return slug, None
        if r.status_code != 200:
            raise CatalogueUnavailable(f"{slug}: HTTP {r.status_code}")
        try:
            routes = r.json()["data"]["endpoints"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CatalogueUnavailable(f"{slug}: unexpected response") from exc
        if not isinstance(routes, list):
            raise CatalogueUnavailable(f"{slug}: unexpected response")
        return slug, routes

    results = await asyncio.gather(*(one(s) for s in sorted(set(slugs))), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return dict(results)


async def verify_ladder(
    ladder: Ladder | None = None, client: httpx.AsyncClient | None = None
) -> VerifiedLadder:
    """Check every configured model against OpenRouter's live routes.

    Raises a `LadderUnusable`: `LadderInvalid` if the file cannot be loaded, `LadderRejected` when
    any model fails, `CatalogueUnavailable` when the check could not be made. Whichever it is, the
    caller leaves the AI layer off.
    """
    if ladder is None:
        try:
            ladder = Ladder.load()
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise LadderInvalid(f"{DEFAULT_LADDER_PATH.name}: {exc}") from exc
    if client is None:
        async with httpx.AsyncClient(headers={"User-Agent": get_settings().user_agent}) as owned:
            routes = await fetch_routes(owned, ladder.slugs())
    else:
        routes = await fetch_routes(client, ladder.slugs())
    breaches = check_ladder(ladder, routes)
    if breaches:
        raise LadderRejected(breaches)
    return VerifiedLadder(ladder=ladder, checked_at=datetime.now(UTC))
