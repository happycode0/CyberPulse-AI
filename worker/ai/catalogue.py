"""OpenRouter's model list, and what changed in it since the last scan (PLAN.md §7.6).

RIPPERDOC's daily scan reads the public list (`MODELS_URL`, no key, so it can neither spend nor
expose one) and compares it with what the scans have seen before:

- `new`: a model never listed before. A new `:free` model that can do the work is the one to
  look at first, because the ladder prefers a free model that passes.
- `withdrawn` and `returned`: a model gone from the list, and one back on it.
- `price`: the headline price moved. The headline is one route's price, not the guard's
  (worker/ai/ladder.py checks every route), so this is a note for RIPPERDOC, not a verdict.
- `capability`: the model gained or lost tools or structured outputs.
- `expiring`: OpenRouter announced a withdrawal date, or moved it.

Everything in this module is pure. worker/ai/scout.py runs the scan and worker/db/scout.py
stores it. A model's name is the only text from the list that is kept, and it is clipped:
the list is not ours, and RIPPERDOC reads what the scan stored.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx

from worker.ai.ladder import (
    REQUIRED_PARAMETERS,
    CatalogueUnavailable,
    Tier,
    VerifiedLadder,
    per_mtok,
)

# Public: no key is sent.
MODELS_URL = "https://openrouter.ai/api/v1/models"

# A list this much shorter than the models the last scan saw is a broken answer, not a day on
# which half of OpenRouter was withdrawn. Nothing is recorded from it.
MIN_LISTED_SHARE = 0.5

NAME_MAX_CHARS = 120

ChangeKind = Literal[
    "new", "withdrawn", "returned", "price", "capability", "expiring", "dropped", "restored"
]


@dataclass(frozen=True)
class ListedModel:
    slug: str
    name: str
    prompt_per_mtok: Decimal | None  # US$ per million tokens; None when it is not a fixed price
    completion_per_mtok: Decimal | None
    capable: bool  # text out, and both tools and structured outputs among its parameters
    expiration: date | None
    intelligence_index: Decimal | None  # Artificial Analysis', where the list carries it
    created: datetime | None

    @property
    def free(self) -> bool:
        return self.slug.endswith(":free")


@dataclass(frozen=True)
class Known:
    """A model as the scans have seen it (worker/db/scout.py `load_catalogue`)."""

    slug: str
    prompt_per_mtok: Decimal | None
    completion_per_mtok: Decimal | None
    capable: bool
    expiration: date | None
    withdrawn: bool


@dataclass(frozen=True)
class Change:
    slug: str
    kind: ChangeKind
    detail: dict[str, Any]


def money(value: Decimal | None) -> float | None:
    """A price as JSON can hold it, to a millionth of a dollar per million tokens."""
    return None if value is None else round(float(value), 6)


def _name(value: Any, slug: str) -> str:
    name = " ".join(value.split()) if isinstance(value, str) else ""
    name = "".join(ch for ch in name if ch.isprintable())
    return (name or slug)[:NAME_MAX_CHARS]


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _index(entry: Mapping[str, Any]) -> Decimal | None:
    benchmarks = entry.get("benchmarks")
    analysis = benchmarks.get("artificial_analysis") if isinstance(benchmarks, Mapping) else None
    value = analysis.get("intelligence_index") if isinstance(analysis, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        index = Decimal(str(value))
    except InvalidOperation:
        return None
    return index if index.is_finite() else None


def _created(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _capable(entry: Mapping[str, Any]) -> bool:
    params = entry.get("supported_parameters")
    if not isinstance(params, list) or not REQUIRED_PARAMETERS <= set(params):
        return False
    architecture = entry.get("architecture")
    outputs = architecture.get("output_modalities") if isinstance(architecture, Mapping) else None
    return not isinstance(outputs, list) or "text" in outputs


def parse_listing(payload: Any) -> list[ListedModel]:
    """The models in `/api/v1/models`, in slug order. An entry without an author/model id is
    skipped. Raises `CatalogueUnavailable` when the answer is not a list of models at all."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list):
        raise CatalogueUnavailable("the model list is not in the expected shape")
    models: dict[str, ListedModel] = {}
    for entry in data:
        if not isinstance(entry, Mapping):
            continue
        slug = entry.get("id")
        if not isinstance(slug, str) or "/" not in slug or len(slug) > 200 or slug in models:
            continue
        pricing = entry.get("pricing") if isinstance(entry.get("pricing"), Mapping) else {}
        models[slug] = ListedModel(
            slug=slug,
            name=_name(entry.get("name"), slug),
            prompt_per_mtok=per_mtok(pricing.get("prompt")),
            completion_per_mtok=per_mtok(pricing.get("completion")),
            capable=_capable(entry),
            expiration=_date(entry.get("expiration_date")),
            intelligence_index=_index(entry),
            created=_created(entry.get("created")),
        )
    if not models:
        raise CatalogueUnavailable("the model list is empty")
    return [models[s] for s in sorted(models)]


async def fetch_listing(client: httpx.AsyncClient, timeout: float = 60.0) -> list[ListedModel]:
    try:
        r = await client.get(MODELS_URL, timeout=timeout)
    except httpx.HTTPError as exc:
        raise CatalogueUnavailable(f"model list: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise CatalogueUnavailable(f"model list: HTTP {r.status_code}")
    try:
        payload = r.json()
    except ValueError as exc:
        raise CatalogueUnavailable("model list: not JSON") from exc
    return parse_listing(payload)


def _prices(m: ListedModel | Known) -> dict[str, float | None]:
    return {"prompt": money(m.prompt_per_mtok), "completion": money(m.completion_per_mtok)}


def diff(known: Mapping[str, Known], listing: Iterable[ListedModel]) -> list[Change]:
    """What changed from `known` to `listing`. The first scan (nothing known) is a baseline and
    changes nothing. Raises `CatalogueUnavailable` for a list implausibly shorter than the last."""
    listing = list(listing)
    if not known:
        return []
    standing = sum(1 for k in known.values() if not k.withdrawn)
    if len(listing) < standing * MIN_LISTED_SHARE:
        raise CatalogueUnavailable(
            f"the model list has {len(listing)} models where the last scan saw {standing}"
        )
    changes: list[Change] = []
    listed = {m.slug for m in listing}
    for m in listing:
        was = known.get(m.slug)
        if was is None:
            changes.append(
                Change(m.slug, "new", {"free": m.free, "capable": m.capable, **_prices(m)})
            )
            if m.expiration is not None:
                changes.append(Change(m.slug, "expiring", {"date": m.expiration.isoformat()}))
            continue
        if was.withdrawn:
            changes.append(Change(m.slug, "returned", {}))
        if (was.prompt_per_mtok, was.completion_per_mtok) != (
            m.prompt_per_mtok,
            m.completion_per_mtok,
        ):
            changes.append(Change(m.slug, "price", {"was": _prices(was), "now": _prices(m)}))
        if was.capable != m.capable:
            changes.append(Change(m.slug, "capability", {"capable": m.capable}))
        if m.expiration is not None and m.expiration != was.expiration:
            changes.append(Change(m.slug, "expiring", {"date": m.expiration.isoformat()}))
    for slug, was in sorted(known.items()):
        if slug not in listed and not was.withdrawn:
            changes.append(Change(slug, "withdrawn", {}))
    return changes


# ─── The ladder as the scan checked it ────────────────────────────────────────────────────────────


Dropped = frozenset[tuple[str, str]]  # (tier, slug)


def ladder_report(
    configured: Mapping[Tier, tuple[str, ...]],
    verified: VerifiedLadder | None,
    breaches: Iterable[Any],
) -> dict[str, Any]:
    """Per tier: the configured chain, the chain in use (None when the ladder did not pass) and
    what was dropped from it and why. `breaches` are worker/ai/ladder.py `Breach`es."""
    breaches = list(breaches)
    report: dict[str, Any] = {}
    for tier, chain in configured.items():
        report[tier.value] = {
            "configured": list(chain),
            "effective": list(verified.ladder.tiers[tier]) if verified is not None else None,
            "dropped": [{"slug": b.slug, "reason": b.reason} for b in breaches if b.tier is tier],
        }
    return report


def dropped_in(report: Mapping[str, Any] | None) -> Dropped:
    if not report:
        return frozenset()
    return frozenset(
        (tier, d["slug"]) for tier, entry in report.items() for d in entry.get("dropped") or ()
    )


def drop_changes(before: Dropped, report: Mapping[str, Any]) -> list[Change]:
    """`dropped` for each model the guard took out of a tier since the last scan, `restored`
    for each it let back in."""
    reasons = {
        (tier, d["slug"]): d["reason"]
        for tier, entry in report.items()
        for d in entry.get("dropped") or ()
    }
    now = frozenset(reasons)
    out = [
        Change(slug, "dropped", {"tier": tier, "reason": reasons[(tier, slug)]})
        for tier, slug in sorted(now - before)
    ]
    out += [Change(slug, "restored", {"tier": tier}) for tier, slug in sorted(before - now)]
    return out
