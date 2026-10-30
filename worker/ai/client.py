"""The OpenRouter client: the one way the worker calls a model (PLAN.md §7.2).

Every request carries the same discipline, so no caller can leave part of it out:

- strict JSON-schema output, and our own check of the answer against that schema whatever
  `strict` promised;
- `require_parameters` and `sort: "price"`, so only providers that honour the schema are
  eligible and the cheapest of them is tried first;
- `max_price` at the ceiling the ladder guard checked against. The guard proves each model has
  a route under the ceiling; this keeps every call on one, since `sort` is a preference and
  falls through to dearer routes when the cheap ones are busy;
- the tier's whole chain as `models`, recording the model that answered, which is the one billed;
- an explicit `max_tokens`, no tools (§2.10), and the attribution headers;
- a ledger row for every billed call, including one whose answer was unusable.

It takes a `VerifiedLadder`, never a bare `Ladder`, so a ladder the guard has not passed cannot
reach OpenRouter. The gauntlet calls models outside the ladder through `trial`, which takes a
`VerifiedCandidate` for the same reason.

`MixRouteClient` is the same discipline on MixRoute (worker/ai/provider.py), where the gateway
does less of it. A request names one model, so the client walks the chain itself, moving on when
a model is busy, unavailable or slow. There is no `provider` object to cap the price with: the
guard's reviewed prices are the cap, and each call is charged at them, since MixRoute reports
tokens but no cost. A 403 or 402 means the key's quota is spent, which no other model would
change.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import jsonschema
from pydantic import SecretStr

from worker.ai.ladder import (
    OUTPUT_CEILING_USD_PER_MTOK,
    TIER2_QUANTIZATIONS,
    MixRouteModel,
    Tier,
    VerifiedCandidate,
    VerifiedLadder,
)
from worker.db.ledger import LedgerEntry
from worker.publish.validate import scan_text_for_secrets
from worker.settings import Settings, get_settings

logger = logging.getLogger(__name__)

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# The cost_ledger `provider`: whose API billed the call.
PROVIDER = "openrouter"
MIXROUTE = "mixroute"

# MixRoute statuses after which the next model in the chain is tried: busy (429), down or unknown
# to this channel (500, 502, 503) and slow (408, 504). No answer at all counts too. So does a
# request one model refuses (400, 404, 422): a parameter in its reviewed extras it does not take,
# an id this channel lacks, a schema it will not follow. A refused key (401) is the same for every
# model, so it is raised at once.
MIXROUTE_FALL_THROUGH = frozenset({400, 404, 408, 422, 429, 500, 502, 503, 504})

# What a spent MixRoute key says, as `BudgetExhausted.limit_source`: 403 "Token Group Disabled",
# usually the key's quota (https://mixroute.ai/docs/faq/error-code.md).
MIXROUTE_KEY_QUOTA = "mixroute_key_quota"

# OpenAI's rule for json_schema names, which OpenRouter passes on to providers that apply it.
_SCHEMA_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# Error text from OpenRouter is logged, so it is kept short and checked for credentials first.
_ERROR_TEXT_LIMIT = 200


@dataclass(frozen=True)
class Attribution:
    """Who is spending, and on what. Every call needs one, so every ledger row can be traced."""

    agent: str
    stage: str
    event_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class Completion:
    data: Any  # the parsed answer, already checked against the schema
    model: str | None  # the model that answered, which is the one billed
    upstream: str | None  # the provider OpenRouter routed the call to
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal | None
    generation_id: str | None
    duration_ms: int | None = None


class CallFailed(Exception):
    """The call produced nothing usable.

    `status` is the HTTP status, or None when there was no response at all. `retry_after` is in
    seconds, set only when OpenRouter sent a Retry-After.
    """

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class BudgetExhausted(CallFailed):
    """402. `limit_source` says which limit was hit (PLAN.md §2.8).

    `openrouter_in_flight_budget` is transient: requests already running have reserved the
    remaining budget, and Retry-After says when to try again. `openrouter_key_limit` means the
    key's own limit is spent and `openrouter_credits` that the account balance is; neither clears
    by waiting, so paid calls stop until a person acts.
    """

    def __init__(self, message: str, *, limit_source: str | None, retry_after: float | None):
        super().__init__(message, status=402, retry_after=retry_after)
        self.limit_source = limit_source

    @property
    def transient(self) -> bool:
        return self.limit_source == "openrouter_in_flight_budget"


class RateLimited(CallFailed):
    """429 after the whole chain. On tier 0 that is usually a free model's daily cap, which
    backing off does not clear (§7.4), so the caller moves up a tier rather than retrying."""


class InvalidOutput(CallFailed):
    """OpenRouter answered and billed, but the answer is unusable. Its ledger row is written.

    `refused` is set when the model declined rather than failed: a refusal, or a content filter
    cutting the answer off. The gauntlet counts those apart (§7.6).
    """

    def __init__(
        self,
        message: str,
        *,
        model: str | None,
        refused: bool = False,
        cost_usd: Decimal | None = None,
        duration_ms: int | None = None,
    ):
        super().__init__(message, status=200)
        self.model = model
        self.refused = refused
        self.cost_usd = cost_usd
        self.duration_ms = duration_ms


class _Unusable(Exception):
    def __init__(self, reason: str, *, refused: bool = False):
        super().__init__(reason)
        self.refused = refused


def _is_object(node: Mapping[str, Any]) -> bool:
    kind = node.get("type")
    return kind == "object" or (isinstance(kind, list) and "object" in kind)


def check_strict_schema(schema: Mapping[str, Any]) -> None:
    """Refuse a schema that strict structured outputs cannot take, before it costs a request.

    Strict mode needs an object at the root and, on every object, all properties required and no
    others allowed. A provider that enforces that rejects the request; one that does not may treat
    the schema as a hint, which is the worse failure because it looks like it worked. Raises
    `ValueError`, since a bad schema is a bug in the caller rather than anything OpenRouter did.
    """
    jsonschema.Draft202012Validator.check_schema(schema)
    if not _is_object(schema):
        raise ValueError("#: a strict schema must be an object at the root")

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            if _is_object(node):
                if node.get("additionalProperties") is not False:
                    raise ValueError(f"{path}: strict schemas need additionalProperties: false")
                if set(node.get("required", ())) != set(node.get("properties", {})):
                    raise ValueError(f"{path}: strict schemas must require every property")
            for key, child in node.items():
                walk(child, f"{path}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}/{index}")

    walk(schema, "#")


def request_body(
    tier: Tier,
    chain: Sequence[str],
    *,
    schema_name: str,
    schema: Mapping[str, Any],
    messages: Sequence[Mapping[str, str]],
    max_tokens: int,
) -> dict[str, Any]:
    provider: dict[str, Any] = {
        "require_parameters": True,
        "sort": "price",
        # US$ per million tokens, as a string (OpenRouter's ProviderPreferences.max_price).
        "max_price": {"completion": str(OUTPUT_CEILING_USD_PER_MTOK)},
    }
    if tier is Tier.STRONG:
        provider["quantizations"] = sorted(TIER2_QUANTIZATIONS)
    return {
        "models": list(chain),
        "messages": [dict(m) for m in messages],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
        "provider": provider,
        "max_tokens": max_tokens,
    }


def mixroute_request_body(
    model: MixRouteModel,
    *,
    schema_name: str,
    schema: Mapping[str, Any],
    messages: Sequence[Mapping[str, str]],
    max_tokens: int,
) -> dict[str, Any]:
    """One request to one MixRoute model: its id as written, the strict schema, and the model's
    reviewed extras (which may not override any of these)."""
    return {
        **model.request,
        "model": model.id,
        "messages": [dict(m) for m in messages],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
        model.max_tokens_field: max_tokens,
    }


def _usd(value: Any) -> Decimal | None:
    """`usage.cost` as a Decimal, or None if OpenRouter did not report a usable figure."""
    if value is None or isinstance(value, bool):
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _retry_after(header: str | None) -> float | None:
    """Retry-After in seconds. It may be a number of seconds or an HTTP date."""
    if not header:
        return None
    header = header.strip()
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _safe_message(value: Any, fallback: str) -> str:
    """OpenRouter's error text, fit to log: short, and withheld outright if it looks like it
    holds a credential. Scanned before it is cut, so a cut cannot hide half a key."""
    if not isinstance(value, str) or not value.strip():
        return fallback
    if scan_text_for_secrets(value):
        return f"{fallback} (error text withheld: it looked like it held a credential)"
    return value.strip()[:_ERROR_TEXT_LIMIT]


def _failure(status: int, error: Any, retry_after: float | None) -> CallFailed:
    error = error if isinstance(error, Mapping) else {}
    message = _safe_message(error.get("message"), f"HTTP {status}")
    if status == 402:
        metadata = error.get("metadata")
        source = metadata.get("limit_source") if isinstance(metadata, Mapping) else None
        return BudgetExhausted(message, limit_source=_text(source), retry_after=retry_after)
    if status == 429:
        return RateLimited(message, status=429, retry_after=retry_after)
    return CallFailed(message, status=status, retry_after=retry_after)


def _mixroute_failure(status: int, error: Any, retry_after: float | None) -> CallFailed:
    if status in (402, 403):
        details = error if isinstance(error, Mapping) else {}
        message = _safe_message(details.get("message"), f"HTTP {status}")
        return BudgetExhausted(message, limit_source=MIXROUTE_KEY_QUOTA, retry_after=retry_after)
    return _failure(status, error, retry_after)


def _cached_tokens(usage: Mapping[str, Any]) -> int:
    """Cached prompt tokens, where the answer says (OpenAI's `prompt_tokens_details`)."""
    details = usage.get("prompt_tokens_details")
    return _count(details.get("cached_tokens")) if isinstance(details, Mapping) else 0


def _answer(choice: Mapping[str, Any], validator: jsonschema.Draft202012Validator) -> Any:
    """The parsed answer, or `_Unusable` saying why there is none. Never quotes the output."""
    message = choice.get("message")
    message = message if isinstance(message, Mapping) else {}
    if message.get("refusal"):
        raise _Unusable("the model refused", refused=True)
    finish = choice.get("finish_reason")
    if finish == "content_filter":
        raise _Unusable("finish_reason 'content_filter'", refused=True)
    if finish not in (None, "stop"):
        # "length" is the common one: the answer was cut off at max_tokens.
        raise _Unusable(f"finish_reason {finish!r}")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _Unusable("no content")
    try:
        data = json.loads(content)
    except ValueError:
        raise _Unusable("not JSON") from None
    error = jsonschema.exceptions.best_match(validator.iter_errors(data))
    if error is not None:
        raise _Unusable(f"fails the schema at {error.json_path} ({error.validator})")
    return data


def _prepare(
    schema_name: str, schema: Mapping[str, Any], max_tokens: int
) -> jsonschema.Draft202012Validator:
    """Refuse a request that is wrong before it is sent, and return the answer's validator."""
    if not _SCHEMA_NAME.match(schema_name):
        raise ValueError(f"schema name {schema_name!r} must match {_SCHEMA_NAME.pattern}")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    check_strict_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _usage(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    usage = payload.get("usage")
    return usage if isinstance(usage, Mapping) else {}


class OpenRouterClient:
    """Calls one tier of a verified ladder. `record` gets every billed call's ledger entry; in
    production it is `functools.partial(worker.db.ledger.record_to, engine)`.

    A ledger write that fails propagates: a call that cannot be accounted for is one the worker
    should stop making, even though it has already been paid for.
    """

    provider = PROVIDER
    label = "OpenRouter"

    def __init__(
        self,
        verified: VerifiedLadder,
        api_key: SecretStr,
        http: httpx.AsyncClient,
        record: Callable[[LedgerEntry], None],
        settings: Settings | None = None,
    ):
        settings = settings or get_settings()
        self._verified = verified
        self._api_key = api_key
        self._http = http
        self._record = record
        self._referer = settings.openrouter_app_url
        self._title = settings.openrouter_app_title
        self._user_agent = settings.user_agent

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "HTTP-Referer": self._referer,
            "X-OpenRouter-Title": self._title,
            # Only takes effect on the request that first creates the app on OpenRouter, and
            # cannot be changed afterwards, so it goes on every call from the very first.
            "X-OpenRouter-App-Visibility": "hidden",
            "User-Agent": self._user_agent,
        }

    async def complete(
        self,
        tier: Tier,
        *,
        schema_name: str,
        schema: Mapping[str, Any],
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        attribution: Attribution,
        models: Sequence[str] | None = None,
    ) -> Completion:
        """One structured call on `tier`'s chain.

        `models` narrows the chain, as the budget's `Route.models` does when only free models
        may be used. It can drop models but never add or reorder them, so nothing the guard has
        not passed is called.

        Raises `ValueError` for a request that is wrong before it is sent, and a `CallFailed`
        for one that was sent and gave nothing usable: `BudgetExhausted` (402), `RateLimited`
        (429), `InvalidOutput` (billed but unusable), or `CallFailed` itself for anything else.
        """
        chain = self._verified.ladder.tiers[tier]
        if models is not None:
            narrowed = tuple(models)
            if not narrowed or narrowed != tuple(m for m in chain if m in narrowed):
                raise ValueError(f"models must be a non-empty part of the {tier.value} chain")
            chain = narrowed
        return await self._call(
            tier,
            chain,
            schema_name=schema_name,
            schema=schema,
            messages=messages,
            max_tokens=max_tokens,
            attribution=attribution,
        )

    async def trial(
        self,
        candidate: VerifiedCandidate,
        *,
        schema_name: str,
        schema: Mapping[str, Any],
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        attribution: Attribution,
    ) -> Completion:
        """One structured call to a single model, for the gauntlet (§7.6).

        The model need not be in the ladder, but it must have passed the guard for this tier,
        and the request carries the tier's whole discipline: the price cap, `require_parameters`
        and, in tier 2, the pinned quantisations. No fallback chain, because the gauntlet is
        measuring this model and no other. Raises as `complete` does.
        """
        return await self._call(
            candidate.tier,
            (candidate.slug,),
            schema_name=schema_name,
            schema=schema,
            messages=messages,
            max_tokens=max_tokens,
            attribution=attribution,
        )

    async def _call(
        self,
        tier: Tier,
        chain: Sequence[str],
        *,
        schema_name: str,
        schema: Mapping[str, Any],
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        attribution: Attribution,
    ) -> Completion:
        validator = _prepare(schema_name, schema, max_tokens)
        body = request_body(
            tier,
            chain,
            schema_name=schema_name,
            schema=schema,
            messages=messages,
            max_tokens=max_tokens,
        )
        payload, duration_ms = await self._post(CHAT_URL, body)
        usage = _usage(payload)
        return await self._settle(
            tier,
            chain[0],
            payload,
            validator,
            attribution,
            duration_ms,
            model=_text(payload.get("model")),
            upstream=_text(payload.get("provider")),
            cost_usd=_usd(usage.get("cost")),
        )

    async def _post(
        self, url: str, body: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], int]:
        """Send one request. Returns the answer and how long it took, or raises a `CallFailed`
        for anything that is not a 200 carrying choices."""
        started = time.monotonic()
        try:
            r = await self._http.post(url, json=body, headers=self._headers(), timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise CallFailed(f"{type(exc).__name__} calling {self.label}") from exc
        duration_ms = round((time.monotonic() - started) * 1000)

        retry_after = _retry_after(r.headers.get("retry-after"))
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if r.status_code != 200:
            error = payload.get("error") if isinstance(payload, Mapping) else None
            raise self._failure(r.status_code, error, retry_after)
        if not isinstance(payload, Mapping):
            raise CallFailed(f"{self.label} returned a 200 that is not a JSON object", status=200)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            # A 200 carrying an error object instead of an answer: an upstream failure after
            # the gateway had already accepted the request.
            error = payload.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            status = code if isinstance(code, int) and not isinstance(code, bool) else 200
            raise self._failure(status, error, retry_after)
        return payload, duration_ms

    def _failure(self, status: int, error: Any, retry_after: float | None) -> CallFailed:
        return _failure(status, error, retry_after)

    async def _settle(
        self,
        tier: Tier,
        requested: str,
        payload: Mapping[str, Any],
        validator: jsonschema.Draft202012Validator,
        attribution: Attribution,
        duration_ms: int,
        *,
        model: str | None,
        upstream: str | None,
        cost_usd: Decimal | None,
    ) -> Completion:
        """Check the answer, write its ledger row and return it, or raise `InvalidOutput` once
        the row is written."""
        usage = _usage(payload)
        refused = False
        try:
            data, outcome, reason = _answer(payload["choices"][0], validator), "ok", None
        except _Unusable as exc:
            data, outcome, reason, refused = None, "invalid_output", str(exc), exc.refused

        entry = LedgerEntry(
            provider=self.provider,
            stage=attribution.stage,
            outcome=outcome,
            agent=attribution.agent,
            event_id=attribution.event_id,
            run_id=attribution.run_id,
            tier=tier.value,
            requested_model=requested,
            model=model,
            upstream=upstream,
            generation_id=_text(payload.get("id")),
            tokens_in=_count(usage.get("prompt_tokens")),
            tokens_out=_count(usage.get("completion_tokens")),
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )
        await asyncio.to_thread(self._record, entry)
        logger.info(
            "ai %s/%s on %s: %s, %s in / %s out, $%s",
            attribution.agent,
            attribution.stage,
            model,
            outcome,
            entry.tokens_in,
            entry.tokens_out,
            entry.cost_usd if entry.cost_usd is not None else "unreported",
        )
        if reason is not None:
            raise InvalidOutput(
                f"{model}: {reason}",
                model=model,
                refused=refused,
                cost_usd=entry.cost_usd,
                duration_ms=duration_ms,
            )
        return Completion(
            data=data,
            model=model,
            upstream=entry.upstream,
            tokens_in=entry.tokens_in,
            tokens_out=entry.tokens_out,
            cost_usd=entry.cost_usd,
            generation_id=entry.generation_id,
            duration_ms=duration_ms,
        )


class MixRouteClient(OpenRouterClient):
    """`OpenRouterClient`'s discipline on MixRoute. The ladder must have passed MixRoute's guard,
    which carries the prices each call is charged at."""

    provider = MIXROUTE
    label = "MixRoute"

    def __init__(
        self,
        verified: VerifiedLadder,
        api_key: SecretStr,
        http: httpx.AsyncClient,
        record: Callable[[LedgerEntry], None],
        settings: Settings | None = None,
    ):
        if verified.prices is None:
            raise ValueError("the ladder was not checked against MixRoute, so it has no prices")
        settings = settings or get_settings()
        super().__init__(verified, api_key, http, record, settings)
        self._prices = verified.prices
        self._url = f"{settings.mix_router_url.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "User-Agent": self._user_agent,
        }

    def _failure(self, status: int, error: Any, retry_after: float | None) -> CallFailed:
        return _mixroute_failure(status, error, retry_after)

    async def _call(
        self,
        tier: Tier,
        chain: Sequence[str],
        *,
        schema_name: str,
        schema: Mapping[str, Any],
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        attribution: Attribution,
    ) -> Completion:
        """Each model in turn until one answers. Raises the last failure when none does, and at
        once for a failure no other model would change: a spent quota or a refused key."""
        validator = _prepare(schema_name, schema, max_tokens)
        last: CallFailed | None = None
        for slug in chain:
            model = self._prices.models[slug]
            body = mixroute_request_body(
                model,
                schema_name=schema_name,
                schema=schema,
                messages=messages,
                max_tokens=max_tokens,
            )
            try:
                payload, duration_ms = await self._post(self._url, body)
            except BudgetExhausted:
                raise
            except CallFailed as exc:
                if exc.status is not None and exc.status not in MIXROUTE_FALL_THROUGH:
                    raise
                logger.info("ai %s on MixRoute failed (%s); trying the next model", slug, exc)
                last = exc
                continue
            usage = _usage(payload)
            cost = model.cost(
                _count(usage.get("prompt_tokens")),
                _count(usage.get("completion_tokens")),
                _cached_tokens(usage),
            )
            # The configured slug, not MixRoute's echo of the id: the ladder, the task states
            # and the gauntlet all name models by slug.
            return await self._settle(
                tier,
                chain[0],
                payload,
                validator,
                attribution,
                duration_ms,
                model=slug,
                upstream=None,
                cost_usd=cost,
            )
        assert last is not None  # the chain is never empty
        raise last
