"""RIPPERDOC's weekly gauntlet: how each model does on the golden set, and when to propose a
change to the ladder (PLAN.md §7.6, §7.7).

Each tier is judged on the task it does in production: tier 0 on triage, tier 1 on the brief,
tier 2 on the severity judgment. The code and audit tiers do no enrichment, so there is
nothing here to judge them on. The incumbent (the tier's default as the guard leaves it) and up
to `CHALLENGERS_PER_TIER` challengers from the catalogue each answer every golden event, and
every call goes through the price guard and the tier's request discipline
(worker/ai/client.py `trial`).

Per model, the five dimensions of §7.6:

- schema compliance: answers that matched the schema, of those that were not refusals;
- refusals: answers the model declined to give, or a filter cut off;
- agreement with the golden labels (worker/ai/golden.py), and how many answers passed the
  checks production applies (`checks`);
- p95 latency;
- cost per event, as OpenRouter billed it.

A challenger is proposed only when it clears every gate and, against an incumbent that also
clears them, agrees about as well and is better on the order §7.6 sets: in tier 0, where both
are free, it agrees clearly more; in tiers 1 and 2 it is clearly cheaper per event. Against an
incumbent that fails a gate, the best challenger that clears them is proposed. A proposal is a
row for RIPPERDOC to raise, and promoting it is MORPHEUS's and ROGUE's decision (§7.7): nothing
here changes config/models.yaml.

The adoption veto in §7.6 needs OpenRouter's authenticated datasets, so it is not checked
here; every proposal says so.

Everything here is pure; worker/ai/scout.py runs the gauntlet and worker/db/scout.py stores it.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, get_args

from worker.ai.ladder import OUTPUT_CEILING_USD_PER_MTOK, Tier
from worker.ai.tasks import (
    AU_DESK_RELEVANCE,
    BRIEF,
    SEVERITY,
    TRIAGE,
    Brief,
    Result,
    SeverityJudgment,
    Task,
    Triage,
)
from worker.models import Severity, beat_of

AGENT = "ripperdoc"
STAGE = "gauntlet"

# Owner policy (§7.6): the gauntlet costs at most this much a month, whatever it finds.
MONTHLY_CAP_USD = Decimal("0.25")
# And at most this much in one run, so one week cannot spend the month's.
RUN_CAP_USD = Decimal("0.08")

CHALLENGERS_PER_TIER = 2
# Challengers put through the guard per tier to find that many that pass it.
MAX_VERIFIED_PER_TIER = 6
# An incumbent's result is reused for this long, on the same golden set.
INCUMBENT_REUSE = timedelta(days=28)
# A model listed for the first time this recently goes ahead of the others.
NEW_MODEL_WINDOW = timedelta(days=14)
# A model OpenRouter withdraws within this long is not worth the change.
EXPIRY_MARGIN = timedelta(days=60)
# Free requests left for the day after the gauntlet's own, for enrichment.
FREE_REQUEST_MARGIN = 100
CONCURRENCY = 3

MIN_COMPLIANCE = 0.95
MAX_REFUSALS = 0.05
MIN_CHECKS = 0.90
MAX_ERRORS = 0.10
MAX_P95_MS = 60_000
# Agreement within this of the incumbent's is as good as it.
AGREEMENT_TOLERANCE = 0.05
# What a replacement for an incumbent that fails a gate must agree to, at least.
AGREEMENT_FLOOR = 0.6
# A paid challenger must cost at least this much less per event to be worth the change.
CHEAPER_BY = 0.2
# Fewer golden events than this with one AU desk answer, and agreement on the brief says little.
MIN_AU_LABELS = 5

# The tokens a call is assumed to use, to estimate a model's cost before running it.
EST_TOKENS_IN = 2000
EST_TOKENS_OUT = 1500

TIER_TASK: dict[Tier, Task] = {Tier.FREE: TRIAGE, Tier.CHEAP: BRIEF, Tier.STRONG: SEVERITY}

Outcome = Literal["ok", "rejected", "invalid", "refused", "error", "skipped"]


# ─── Scoring one answer ───────────────────────────────────────────────────────────────────────────

_LEVELS = (Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)


def judged_on(task: Task, labels: Mapping[str, Any]) -> bool:
    """Whether an event is worth a call for this task. An AI story the owner labelled has no
    severity label (worker/ai/golden.py), so the severity task is not tried on it."""
    return task is not SEVERITY or "severity" in labels


def score(result: Result, labels: Mapping[str, Any]) -> float | None:
    """How far one answer agrees with the golden labels, from 0 to 1. None when the event has no
    label for this task, so it is not counted."""
    if isinstance(result, Triage):
        want = labels["triage"]
        if "beat" in want:  # an AI story the owner labelled: the beat, and how much it matters
            points = [beat_of(result.domains).value == want["beat"]]
            if "ai_significance" in want:
                got = result.ai_significance
                points.append(got is not None and got.value == want["ai_significance"])
            return sum(points) / len(points)
        return (
            (want["domain"] in result.domains)
            + any(c in want["categories"] for c in result.categories)
        ) / 2
    if isinstance(result, Brief):
        desk = labels.get("au_desk")
        if desk is None:
            return None
        return float((result.au_relevance >= AU_DESK_RELEVANCE) == desk)
    if isinstance(result, SeverityJudgment):
        if "severity" not in labels:
            return None
        expected = Severity(labels["severity"])
        # Production publishes only a usable judgment; anything else leaves the event unknown.
        if not result.usable or expected not in _LEVELS:
            return 0.0
        gap = abs(_LEVELS.index(result.severity) - _LEVELS.index(expected))
        return {0: 1.0, 1: 0.5}.get(gap, 0.0)
    raise TypeError(type(result).__name__)


@dataclass(frozen=True)
class Attempt:
    event_id: str
    outcome: Outcome
    score: float | None = None
    duration_ms: int | None = None
    cost_usd: Decimal | None = None


# ─── One model's figures ──────────────────────────────────────────────────────────────────────────


def _p95(values: Sequence[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _share(n: int, of: int) -> float | None:
    return round(n / of, 4) if of else None


@dataclass(frozen=True)
class Measured:
    """One model on one task, over the golden set. Stored in `gauntlet_runs.results`."""

    tier: str
    slug: str
    task: str
    incumbent: bool
    measured_at: str  # ISO 8601
    golden_digest: str
    attempts: int
    errors: int
    skipped: int
    answered: int
    compliant: int  # matched the schema
    refused: int
    passed: int  # and passed production's checks
    labelled: int
    agreement: float | None
    p95_ms: int | None
    spent_usd: float
    cost_per_event: float | None
    note: str | None = None
    # Carried over from an earlier run on the same set (`INCUMBENT_REUSE`), not measured again.
    reused: bool = field(default=False, compare=False)

    @property
    def free(self) -> bool:
        return self.slug.endswith(":free")

    @property
    def compliance(self) -> float | None:
        return _share(self.compliant, self.answered - self.refused)

    @property
    def refusal_rate(self) -> float | None:
        return _share(self.refused, self.answered)

    @property
    def checks(self) -> float | None:
        return _share(self.passed, self.compliant)

    @property
    def complete(self) -> bool:
        return (
            self.attempts > 0
            and not self.skipped
            and self.errors <= self.attempts * MAX_ERRORS
            and self.note is None
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any], *, reused: bool = False) -> "Measured":
        """`reused` is the caller's to say: a stored result was measured when it was stored."""
        names = {f for f in cls.__dataclass_fields__ if f != "reused"}
        return cls(**{k: v for k, v in data.items() if k in names}, reused=reused)


def measure(
    tier: Tier,
    slug: str,
    attempts: Sequence[Attempt],
    *,
    incumbent: bool,
    measured_at: datetime,
    golden_digest: str,
    note: str | None = None,
) -> Measured:
    counts = {o: sum(1 for a in attempts if a.outcome == o) for o in get_args(Outcome)}
    answered = counts["ok"] + counts["rejected"] + counts["invalid"] + counts["refused"]
    scores = [a.score for a in attempts if a.outcome == "ok" and a.score is not None]
    spent = sum((a.cost_usd for a in attempts if a.cost_usd is not None), Decimal(0))
    return Measured(
        tier=tier.value,
        slug=slug,
        task=TIER_TASK[tier].name.value,
        incumbent=incumbent,
        measured_at=measured_at.isoformat(),
        golden_digest=golden_digest,
        attempts=len(attempts),
        errors=counts["error"],
        skipped=counts["skipped"],
        answered=answered,
        compliant=counts["ok"] + counts["rejected"],
        refused=counts["refused"],
        passed=counts["ok"],
        labelled=len(scores),
        agreement=round(sum(scores) / len(scores), 4) if scores else None,
        p95_ms=_p95(
            [a.duration_ms for a in attempts if a.outcome != "error" and a.duration_ms is not None]
        ),
        spent_usd=round(float(spent), 8),
        cost_per_event=round(float(spent) / answered, 8) if answered else None,
        note=note,
    )


def failures(m: Measured) -> list[str]:
    """Each gate the model fails, in words. Empty means it clears them all."""
    if not m.complete:
        why = m.note or (
            f"{m.skipped} of {m.attempts} events not tried"
            if m.skipped
            else f"{m.errors} of {m.attempts} calls failed"
        )
        return [f"incomplete: {why}"]
    out = []
    if m.compliance is None or m.compliance < MIN_COMPLIANCE:
        out.append(f"schema compliance {_pct(m.compliance)} (needs {MIN_COMPLIANCE:.0%})")
    if m.refusal_rate is not None and m.refusal_rate > MAX_REFUSALS:
        out.append(f"refused {_pct(m.refusal_rate)} (at most {MAX_REFUSALS:.0%})")
    if m.checks is None or m.checks < MIN_CHECKS:
        out.append(f"passed production's checks {_pct(m.checks)} (needs {MIN_CHECKS:.0%})")
    if m.agreement is None:
        out.append("no answer could be scored against the labels")
    if m.p95_ms is not None and m.p95_ms > MAX_P95_MS:
        out.append(f"p95 latency {m.p95_ms / 1000:.0f} s (at most {MAX_P95_MS // 1000} s)")
    return out


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


# ─── Deciding ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Decision:
    tier: Tier
    challenger: Measured
    incumbent: Measured
    why: str


def _preference(m: Measured) -> tuple[Any, ...]:
    """§7.6's order: free first, then the cheapest per event, then the one that agrees most."""
    return (not m.free, m.cost_per_event or 0.0, -(m.agreement or 0.0), m.slug)


def decide(
    tier: Tier, incumbent: Measured | None, challengers: Iterable[Measured]
) -> Decision | None:
    """The challenger to propose for `tier`, or None to keep the incumbent.

    Nothing is proposed without a complete incumbent result to compare against: a run cut short
    by an outage says nothing about the incumbent.
    """
    if incumbent is None or not incumbent.complete:
        return None
    passing = [c for c in challengers if not failures(c) and c.agreement is not None]
    against = failures(incumbent)
    if against:
        pool = [c for c in passing if c.agreement >= AGREEMENT_FLOOR]
        if not pool:
            return None
        best = min(pool, key=_preference)
        return Decision(tier, best, incumbent, f"the incumbent fails a gate: {against[0]}")

    assert incumbent.agreement is not None
    reasons: dict[str, str] = {}
    for c in passing:
        if c.agreement < incumbent.agreement - AGREEMENT_TOLERANCE:
            continue
        if c.free and not incumbent.free:
            reasons[c.slug] = "free, and agrees as well"
        elif c.free == incumbent.free and c.agreement > incumbent.agreement + AGREEMENT_TOLERANCE:
            reasons[c.slug] = (
                f"agrees more ({_pct(c.agreement)} against {_pct(incumbent.agreement)})"
            )
        elif (
            not c.free
            and not incumbent.free
            and c.cost_per_event is not None
            and incumbent.cost_per_event is not None
            and c.cost_per_event < incumbent.cost_per_event * (1 - CHEAPER_BY)
        ):
            reasons[c.slug] = (
                f"cheaper per event (${c.cost_per_event:.6f} against "
                f"${incumbent.cost_per_event:.6f}) and agrees as well"
            )
    pool = [c for c in passing if c.slug in reasons]
    if not pool:
        return None
    best = min(pool, key=_preference)
    return Decision(tier, best, incumbent, reasons[best.slug])


# ─── Choosing challengers ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Listed:
    """A catalogue row, as far as choosing a challenger needs it (worker/db/scout.py)."""

    slug: str
    first_seen: datetime
    prompt_per_mtok: Decimal | None
    completion_per_mtok: Decimal | None
    capable: bool
    expiration: date | None
    intelligence_index: Decimal | None
    withdrawn: bool

    @property
    def free(self) -> bool:
        return self.slug.endswith(":free")


def challengers(
    rows: Iterable[Listed], tier: Tier, chain: Iterable[str], now: datetime
) -> list[Listed]:
    """Models worth trying in `tier`, best first: the ones listed recently, then by Artificial
    Analysis' index, then the cheapest.

    Tier 0 takes free models only, and tiers 1 and 2 paid ones only: editorial work never runs
    on a free model (§8). The headline price is only a first cut; the guard checks the routes.
    """
    chain = set(chain)
    out = []
    for r in rows:
        if r.withdrawn or not r.capable or r.slug in chain:
            continue
        if r.slug.startswith("openrouter/") or r.slug.endswith(":batch") or "/" not in r.slug:
            continue
        if r.free != (tier is Tier.FREE):
            continue
        if r.prompt_per_mtok is None or r.completion_per_mtok is None:
            continue
        if r.completion_per_mtok > OUTPUT_CEILING_USD_PER_MTOK:
            continue
        if r.expiration is not None and r.expiration <= (now + EXPIRY_MARGIN).date():
            continue
        out.append(r)

    def key(r: Listed) -> tuple[Any, ...]:
        recent = now - r.first_seen <= NEW_MODEL_WINDOW
        index = -(r.intelligence_index if r.intelligence_index is not None else Decimal(-1))
        return (not recent, index, r.completion_per_mtok, r.slug)

    return sorted(out, key=key)


def estimate(row: Listed | None, events: int) -> Decimal:
    """What running a model over `events` should cost, from its headline price. A model with no
    catalogue row is assumed to cost the ceiling."""
    if row is not None and row.free:
        return Decimal(0)
    prompt = row.prompt_per_mtok if row and row.prompt_per_mtok is not None else Decimal(1)
    out = (
        row.completion_per_mtok
        if row and row.completion_per_mtok is not None
        else OUTPUT_CEILING_USD_PER_MTOK
    )
    return events * (prompt * EST_TOKENS_IN + out * EST_TOKENS_OUT) / Decimal(1_000_000)


# ─── The proposal ─────────────────────────────────────────────────────────────────────────────────


def proposal_title(d: Decision) -> str:
    return f"[MODEL] Proposal: {d.tier.value} -> {d.challenger.slug}"


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.6f}"


def _ms(value: int | None) -> str:
    return "n/a" if value is None else f"{value / 1000:.1f} s"


def proposal_body(
    d: Decision, *, golden_size: int, reviewed: int, au_labels: tuple[int, int]
) -> str:
    """`au_labels`: how many of the set the AU desk label calls Australian, and how many not
    (worker/ai/golden.py `au_label_counts`)."""
    inc, ch = d.incumbent, d.challenger
    rows = [
        ("schema compliance", _pct(inc.compliance), _pct(ch.compliance)),
        ("refusals", _pct(inc.refusal_rate), _pct(ch.refusal_rate)),
        ("passed production's checks", _pct(inc.checks), _pct(ch.checks)),
        ("agreement with the labels", _pct(inc.agreement), _pct(ch.agreement)),
        ("events scored", str(inc.labelled), str(ch.labelled)),
        ("p95 latency", _ms(inc.p95_ms), _ms(ch.p95_ms)),
        ("cost per event", _money(inc.cost_per_event), _money(ch.cost_per_event)),
        ("measured", inc.measured_at[:10], ch.measured_at[:10]),
    ]
    against = failures(inc)
    lines = [
        (
            f"RIPPERDOC's gauntlet proposes `{ch.slug}` as the default for `{d.tier.value}`, "
            f"in place of `{inc.slug}`: {d.why}."
        ),
        "",
        (
            f"Task: {ch.task}, over the golden set of {golden_size} events "
            f"(digest `{ch.golden_digest}`)."
        ),
        "",
        f"| | `{inc.slug}` (now) | `{ch.slug}` |",
        "|---|---|---|",
        *(f"| {name} | {a} | {b} |" for name, a, b in rows),
        "",
    ]
    if against:
        lines += [f"The incumbent fails: {'; '.join(against)}.", ""]
    lines += [
        "Not checked, so check before approving:",
        "",
        (
            "- The adoption veto (§7.6): a model whose use fell 40% or more week on week. It "
            "needs OpenRouter's authenticated datasets; see https://openrouter.ai/rankings."
        ),
        (
            f"- The labels come from the registers and the source registry, or for AI stories "
            f"from the owner: {reviewed} of {golden_size} have been reviewed by a person."
        ),
    ]
    if d.tier is Tier.CHEAP:
        australian, not_australian = au_labels
        lines.append(
            f"- The brief is scored on AU relevance only, over {australian} Australian and "
            f"{not_australian} non-Australian events"
            + (
                ", too few of one kind to tell the models apart."
                if min(au_labels) < MIN_AU_LABELS
                else "."
            )
        )
    lines += [
        "",
        (
            "A promotion needs MORPHEUS and ROGUE (§7.7). Approving changes nothing by itself: "
            "the change is a reviewed edit to config/models.yaml."
        ),
    ]
    return "\n".join(lines)


def proposal_payload(d: Decision, *, gauntlet_run: int | None) -> dict[str, Any]:
    return {
        "tier": d.tier.value,
        "slug": d.challenger.slug,
        "replaces": d.incumbent.slug,
        "why": d.why,
        "gauntlet_run": gauntlet_run,
        "incumbent": d.incumbent.to_json(),
        "challenger": d.challenger.to_json(),
    }
