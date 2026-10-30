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

A model that fails is dropped from its chain, for as long as it fails (§7.6: "immediately drop
any that drifted above the ceiling"). Going down a chain that was already signed off is not a new
decision (§7.7), and one withdrawn model should not switch off every tier. What is left must still
be a ladder `Ladder` accepts — no empty tier, tier 0 still ending in a paid model — or the whole
ladder is rejected. Failing closed here means the AI layer stays off, not that the worker stops:
see worker/ai. The daily model scan (worker/ai/catalogue.py) records every drop and return.

On MixRoute (AI_PROVIDER=mixroute, worker/ai/provider.py) there are no routes to read: its
`GET /v1/models` lists ids and nothing else. The ladder and every model's price come from
`config/models.mixroute.yaml` instead, and a model passes if MixRoute lists its id for our key
and its reviewed output price, at the dearest step a worker prompt can reach, is within the same
ceiling. There is no quantisation to pin, so tier 2 has no extra check there.
"""

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from worker.settings import Settings, get_settings

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
    # Batch is asynchronous, which cannot keep up with the FAST collection lane (§7.1).
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
        # Two-person rule (§7.1): TELETRAAN never reviews WHEELJACK's code on the same vendor's
        # model. Checked down both whole chains, because a fallback is a model that does get used.
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
    """Configured models break the guard, and what is left without them is not a usable ladder,
    so none of them may be called. `remainder` says what is wrong with what is left."""

    def __init__(self, breaches: list[Breach], remainder: str | None = None):
        self.breaches = breaches
        self.remainder = remainder
        message = "; ".join(str(b) for b in breaches)
        if remainder:
            message += f" (without them, {remainder})"
        super().__init__(message)


class CatalogueUnavailable(LadderUnusable):
    """OpenRouter's catalogue could not be read. Nothing was checked, so this fails closed too."""


@dataclass(frozen=True)
class VerifiedLadder:
    """A ladder that passed the guard, and when.

    Only `verify_ladder` makes one. Code that calls a model takes this rather than a `Ladder`,
    so a ladder nobody has checked cannot reach OpenRouter. `ladder` is what may be called: the
    configured ladder without the models in `dropped`.
    """

    ladder: Ladder
    checked_at: datetime
    dropped: tuple[Breach, ...] = ()
    # Set when MixRoute's guard passed it: the reviewed prices it was checked against, which the
    # client charges each call at and the budget reads `free` from.
    prices: "MixRouteConfig | None" = None


@dataclass(frozen=True)
class VerifiedCandidate:
    """A model outside the ladder that passed the guard for one tier, and when.

    Only `verify_candidate` makes one, and `OpenRouterClient.trial` takes nothing else, so the
    gauntlet can call a challenger without the guard being skipped for it.
    """

    tier: Tier
    slug: str
    checked_at: datetime


def per_mtok(price: Any) -> Decimal | None:
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
    if per_mtok(pricing.get("prompt")) is None:
        return None
    return per_mtok(pricing.get("completion"))


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


def prune(ladder: Ladder, breaches: Iterable[Breach]) -> Ladder:
    """The ladder without the models that breached, in the tiers they breached in.

    Rebuilt through `Ladder`'s own validation, so every rule it enforces holds for what is left.
    Raises `LadderRejected` when one does not.
    """
    breaches = list(breaches)
    if not breaches:
        return ladder
    out = {(b.tier, b.slug) for b in breaches}
    tiers = {t: tuple(s for s in chain if (t, s) not in out) for t, chain in ladder.tiers.items()}
    try:
        return Ladder.model_validate({"tiers": tiers})
    except ValidationError as exc:
        reason = str(exc.errors()[0]["msg"]).removeprefix("Value error, ")
        raise LadderRejected(breaches, reason) from None


async def _routes(
    client: httpx.AsyncClient | None, slugs: Iterable[str]
) -> dict[str, list[Mapping[str, Any]] | None]:
    if client is not None:
        return await fetch_routes(client, slugs)
    async with httpx.AsyncClient(headers={"User-Agent": get_settings().user_agent}) as owned:
        return await fetch_routes(owned, slugs)


async def verify_ladder(
    ladder: Ladder | None = None, client: httpx.AsyncClient | None = None
) -> VerifiedLadder:
    """Check every configured model against OpenRouter's live routes, and drop the ones that fail.

    Raises a `LadderUnusable`: `LadderInvalid` if the file cannot be loaded, `LadderRejected` when
    what is left after the drops is not a usable ladder, `CatalogueUnavailable` when the check
    could not be made. Whichever it is, the caller leaves the AI layer off.
    """
    if ladder is None:
        try:
            ladder = Ladder.load()
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise LadderInvalid(f"{DEFAULT_LADDER_PATH.name}: {exc}") from exc
    breaches = check_ladder(ladder, await _routes(client, ladder.slugs()))
    return VerifiedLadder(
        ladder=prune(ladder, breaches), checked_at=datetime.now(UTC), dropped=tuple(breaches)
    )


async def verify_candidate(
    slug: str, tier: Tier, client: httpx.AsyncClient | None = None
) -> VerifiedCandidate:
    """Put one model outside the ladder through the same guard, for one tier.

    Raises `LadderRejected` when it fails and `CatalogueUnavailable` when it could not be checked.
    A slug the ladder itself would refuse (a router, a batch variant) is rejected without a fetch.
    """
    try:
        _check_slug(tier, slug)
    except ValueError as exc:
        reason = str(exc).removeprefix(f"{tier.value}: ")
        raise LadderRejected([Breach(tier, slug, reason)]) from None
    routes = await _routes(client, [slug])
    reason = breach_reason(routes.get(slug), tier)
    if reason is not None:
        raise LadderRejected([Breach(tier, slug, reason)])
    return VerifiedCandidate(tier=tier, slug=slug, checked_at=datetime.now(UTC))


# ─── MixRoute ─────────────────────────────────────────────────────────────────────────────────────

# Read instead of models.yaml when AI_PROVIDER=mixroute.
MIXROUTE_LADDER_PATH = DEFAULT_LADDER_PATH.with_name("models.mixroute.yaml")

# The longest prompt the worker sends, with room to spare: the MITRE task's, with its candidate
# list, is the largest (worker/ai/mitre.py). A dearer price step that only starts past this can
# never be charged for a worker call, so the guard does not hold it against the model.
MAX_PROMPT_TOKENS = 32_000

_NO_PRICE = f"no price in {MIXROUTE_LADDER_PATH.name}"

# What a model's extras may not set: the client owns these on every request.
_RESERVED_FIELDS = frozenset(
    {"model", "messages", "response_format", "max_tokens", "max_completion_tokens", "stream",
     "tools", "tool_choice"}
)


class PriceStep(BaseModel):
    """A dearer rate that applies once the prompt is longer than `prompt_tokens`."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    prompt_tokens: int = Field(gt=0)
    input: Decimal = Field(alias="in", ge=0)
    output: Decimal = Field(alias="out", ge=0)
    cache_read: Decimal | None = Field(default=None, ge=0)


class MixRouteModel(BaseModel):
    """One model in config/models.mixroute.yaml: its MixRoute id and its reviewed prices, in US$
    per million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str = Field(min_length=1)  # sent as the request's `model`, exactly as written
    input: Decimal = Field(alias="in", ge=0)
    output: Decimal = Field(alias="out", ge=0)
    cache_read: Decimal | None = Field(default=None, ge=0)  # None: cached input costs `input`
    free: bool = False
    above: tuple[PriceStep, ...] = ()
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    request: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.free and (self.input or self.output or any(s.input or s.output for s in self.above)):
            raise ValueError(f"{self.id} is marked free but has a price")
        steps = [s.prompt_tokens for s in self.above]
        if steps != sorted(set(steps)):
            raise ValueError(f"{self.id}: price steps must rise")
        reserved = sorted(set(self.request) & _RESERVED_FIELDS)
        if reserved:
            raise ValueError(f"{self.id}: request extras may not set {reserved}")
        return self

    def rates(self, prompt_tokens: int) -> tuple[Decimal, Decimal, Decimal | None]:
        """Input, output and cache-read prices for a prompt of this many tokens."""
        price = (self.input, self.output, self.cache_read)
        for step in self.above:
            if prompt_tokens > step.prompt_tokens:
                price = (step.input, step.output, step.cache_read)
        return price

    def worst_output(self, max_prompt_tokens: int = MAX_PROMPT_TOKENS) -> Decimal:
        """The dearest output price a prompt of up to `max_prompt_tokens` can be charged."""
        reachable = (s.output for s in self.above if s.prompt_tokens < max_prompt_tokens)
        return max([self.output, *reachable])

    def cost(self, tokens_in: int, tokens_out: int, cached: int) -> Decimal:
        """What one call cost at these prices. `cached` input is charged at `cache_read` where one
        is listed and at the input price otherwise: an overestimate, the safe direction."""
        price_in, price_out, price_cached = self.rates(tokens_in)
        cached = min(max(cached, 0), tokens_in)
        if price_cached is None:
            price_cached = price_in
        total = (tokens_in - cached) * price_in + cached * price_cached + tokens_out * price_out
        return total / _PER_MTOK


class MixRouteConfig(BaseModel):
    """config/models.mixroute.yaml: each tier's chain, and the reviewed price of every model.

    The chains are held to everything `Ladder` enforces. On top of that every model a tier names
    must be priced, and tier 0 must still end in a paid model, which here means one not marked
    `free`. Priced models no tier names are the gauntlet's challengers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tiers: dict[Tier, tuple[str, ...]]
    models: dict[str, MixRouteModel]

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        ladder = Ladder.model_validate({"tiers": self.tiers})
        for slug in self.models:
            author, _, name = slug.partition("/")
            if not author or not name:
                raise ValueError(f"{slug!r} is not a vendor/id slug")
        missing = sorted(ladder.slugs() - set(self.models))
        if missing:
            raise ValueError(f"no price for {', '.join(missing)}")
        if self.models[ladder.tiers[Tier.FREE][-1]].free:
            raise ValueError("tier0_free must end in a paid model")
        return self

    @property
    def ladder(self) -> Ladder:
        return Ladder.model_validate({"tiers": self.tiers})

    @classmethod
    def load(cls, path: Path = MIXROUTE_LADDER_PATH) -> "MixRouteConfig":
        with open(path) as f:
            return cls.model_validate(yaml.safe_load(f))


def is_free(slug: str, prices: MixRouteConfig | None = None) -> bool:
    """Whether a model costs nothing: what the price list says on MixRoute, the `:free` suffix
    on OpenRouter."""
    if prices is not None and slug in prices.models:
        return prices.models[slug].free
    return slug.endswith(":free")


def mixroute_breach(model: MixRouteModel | None, listed: frozenset[str]) -> str | None:
    """Why a model may not be used on MixRoute, or None if it may."""
    if model is None:
        return _NO_PRICE
    if model.id not in listed:
        return "not on MixRoute for this key (withdrawn, or the id is wrong)"
    worst = model.worst_output()
    if worst > OUTPUT_CEILING_USD_PER_MTOK:
        return (
            f"configured at ${worst:.2f}/M output, "
            f"over the ${OUTPUT_CEILING_USD_PER_MTOK}/M ceiling"
        )
    return None


async def fetch_mixroute_ids(
    client: httpx.AsyncClient, settings: Settings, timeout: float = 30.0
) -> frozenset[str]:
    """The model ids MixRoute serves our key, from `GET /v1/models`.

    The one check that needs the key: MixRoute has no public model list. It costs nothing. Any
    answer that is not a readable, non-empty list raises `CatalogueUnavailable`.
    """
    key = settings.mix_router_key
    if key is None or not key.get_secret_value():
        raise CatalogueUnavailable("MIX_ROUTER_KEY is not set, so MixRoute's model list is unread")
    url = f"{settings.mix_router_url.rstrip('/')}/models"
    headers = {
        "Authorization": f"Bearer {key.get_secret_value()}",
        "User-Agent": settings.user_agent,
    }
    try:
        r = await client.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise CatalogueUnavailable(f"MixRoute's model list: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise CatalogueUnavailable(f"MixRoute's model list: HTTP {r.status_code}")
    try:
        ids = frozenset(m["id"] for m in r.json()["data"] if isinstance(m["id"], str))
    except (ValueError, KeyError, TypeError) as exc:
        raise CatalogueUnavailable("MixRoute's model list: unexpected response") from exc
    if not ids:
        raise CatalogueUnavailable("MixRoute's model list is empty")
    return ids


async def _mixroute_ids(client: httpx.AsyncClient | None, settings: Settings) -> frozenset[str]:
    if client is not None:
        return await fetch_mixroute_ids(client, settings)
    async with httpx.AsyncClient() as owned:
        return await fetch_mixroute_ids(owned, settings)


def _load_mixroute(config: MixRouteConfig | None) -> MixRouteConfig:
    if config is not None:
        return config
    try:
        return MixRouteConfig.load()
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise LadderInvalid(f"{MIXROUTE_LADDER_PATH.name}: {exc}") from exc


async def verify_mixroute_ladder(
    config: MixRouteConfig | None = None,
    client: httpx.AsyncClient | None = None,
    *,
    settings: Settings | None = None,
) -> VerifiedLadder:
    """`verify_ladder` for MixRoute: drop each model MixRoute does not list or that is priced over
    the ceiling. Raises the same `LadderUnusable`s, and the caller leaves the AI layer off."""
    settings = settings or get_settings()
    config = _load_mixroute(config)
    listed = await _mixroute_ids(client, settings)
    ladder = config.ladder
    breaches = [
        Breach(tier, slug, reason)
        for tier, chain in ladder.tiers.items()
        for slug in chain
        if (reason := mixroute_breach(config.models.get(slug), listed)) is not None
    ]
    return VerifiedLadder(
        ladder=prune(ladder, breaches),
        checked_at=datetime.now(UTC),
        dropped=tuple(breaches),
        prices=config,
    )


async def verify_mixroute_candidate(
    slug: str,
    tier: Tier,
    config: MixRouteConfig | None = None,
    client: httpx.AsyncClient | None = None,
    *,
    settings: Settings | None = None,
) -> VerifiedCandidate:
    """`verify_candidate` for MixRoute. A model with no reviewed price is rejected without asking
    MixRoute anything: there would be nothing to charge its calls at."""
    try:
        _check_slug(tier, slug)
    except ValueError as exc:
        reason = str(exc).removeprefix(f"{tier.value}: ")
        raise LadderRejected([Breach(tier, slug, reason)]) from None
    config = _load_mixroute(config)
    model = config.models.get(slug)
    if model is None:
        raise LadderRejected([Breach(tier, slug, _NO_PRICE)])
    reason = mixroute_breach(model, await _mixroute_ids(client, settings or get_settings()))
    if reason is not None:
        raise LadderRejected([Breach(tier, slug, reason)])
    return VerifiedCandidate(tier=tier, slug=slug, checked_at=datetime.now(UTC))
