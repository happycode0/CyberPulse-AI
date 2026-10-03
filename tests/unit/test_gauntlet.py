import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from worker.ai.gauntlet import (
    CHEAPER_BY,
    EXPIRY_MARGIN,
    Attempt,
    Listed,
    Measured,
    challengers,
    decide,
    estimate,
    failures,
    measure,
    proposal_body,
    proposal_payload,
    proposal_title,
    score,
)
from worker.ai.ladder import Tier
from worker.ai.tasks import Brief, SeverityJudgment, Triage
from worker.models import Severity

NOW = datetime(2026, 10, 4, 3, 40, tzinfo=UTC)
LABELS = {
    "severity": "high",
    "triage": {"domain": "cybersecurity", "categories": ["vulnerability", "zero-day"]},
    "au_desk": False,
}


def triage(domains=("cybersecurity",), categories=("vulnerability",)):
    return Triage(domains, categories, None, (), (), (), (), (), ())


def brief(au_relevance):
    return Brief("A summary.", None, au_relevance, (), ())


def judgment(severity, confidence=0.9):
    return SeverityJudgment(severity, confidence, "Because.")


# ─── Scoring one answer ───────────────────────────────────────────────────────────────────────────


def test_triage_scores_the_domain_and_the_category():
    assert score(triage(), LABELS) == 1.0
    assert score(triage(categories=("ransomware",)), LABELS) == 0.5
    assert score(triage(domains=("ai",), categories=("ransomware",)), LABELS) == 0.0


def test_the_brief_is_scored_on_the_au_desk_only_where_there_is_a_label():
    assert score(brief(0.1), LABELS) == 1.0
    assert score(brief(0.8), LABELS) == 0.0
    assert score(brief(0.8), {**LABELS, "au_desk": True}) == 1.0
    assert score(brief(0.8), {**LABELS, "au_desk": None}) is None


def test_severity_scores_exact_and_adjacent_levels():
    assert score(judgment(Severity.HIGH), LABELS) == 1.0
    assert score(judgment(Severity.CRITICAL), LABELS) == 0.5
    assert score(judgment(Severity.MEDIUM), LABELS) == 0.5
    assert score(judgment(Severity.LOW), LABELS) == 0.0


def test_a_severity_production_would_not_publish_scores_nothing():
    assert score(judgment(Severity.UNKNOWN), LABELS) == 0.0
    assert score(judgment(Severity.HIGH, confidence=0.1), LABELS) == 0.0


# ─── One model's figures ──────────────────────────────────────────────────────────────────────────


def attempts(
    ok=20,
    rejected=0,
    invalid=0,
    refused=0,
    error=0,
    skipped=0,
    ms=1000,
    cost="0.001",
    agreement=1.0,
):
    out = [Attempt(f"e{i}", "ok", agreement, ms, Decimal(cost)) for i in range(ok)]
    out += [Attempt("r", "rejected", None, ms, Decimal(cost)) for _ in range(rejected)]
    out += [Attempt("i", "invalid", None, ms, Decimal(cost)) for _ in range(invalid)]
    out += [Attempt("f", "refused", None, ms, Decimal(cost)) for _ in range(refused)]
    out += [Attempt("x", "error") for _ in range(error)]
    out += [Attempt("s", "skipped") for _ in range(skipped)]
    return out


def measured(slug="vendor/model", tier=Tier.CHEAP, incumbent=False, note=None, **counts):
    return measure(
        tier,
        slug,
        attempts(**counts),
        incumbent=incumbent,
        measured_at=NOW,
        golden_digest="abc123def456",
        note=note,
    )


def test_the_figures_of_a_run():
    m = measured(ok=17, rejected=1, invalid=1, refused=1, error=1, ms=2000, agreement=0.5)
    assert (m.attempts, m.answered, m.compliant, m.passed, m.refused) == (21, 20, 18, 17, 1)
    assert m.compliance == round(18 / 19, 4) and m.refusal_rate == 0.05
    assert m.checks == round(17 / 18, 4) and m.agreement == 0.5 and m.labelled == 17
    assert m.p95_ms == 2000 and m.spent_usd == 0.02 and m.cost_per_event == 0.001
    assert m.task == "brief" and m.complete


def test_p95_is_the_slow_tail():
    m = measure(
        Tier.FREE,
        "v/m:free",
        [Attempt(f"e{i}", "ok", 1.0, (i + 1) * 1000, None) for i in range(20)],
        incumbent=True,
        measured_at=NOW,
        golden_digest="d",
    )
    assert m.p95_ms == 19_000 and m.spent_usd == 0 and m.cost_per_event == 0


def test_a_clean_model_clears_every_gate():
    assert failures(measured()) == []


@pytest.mark.parametrize(
    "counts, word",
    [
        ({"ok": 18, "invalid": 2}, "schema compliance"),
        ({"ok": 18, "refused": 2}, "refused"),
        ({"ok": 17, "rejected": 3}, "passed production's checks"),
        ({"ok": 20, "ms": 90_000}, "p95 latency"),
        ({"ok": 15, "error": 5}, "incomplete: 5 of 20 calls failed"),
        ({"ok": 15, "skipped": 1}, "incomplete: 1 of 16 events not tried"),
    ],
)
def test_each_gate(counts, word):
    assert any(word in f for f in failures(measured(**counts)))


def test_a_run_cut_short_says_why():
    m = measured(note="OpenRouter turned a call away for budget (HTTP 402)")
    assert not m.complete and failures(m) == [f"incomplete: {m.note}"]


def test_a_result_survives_storage_and_says_when_it_is_reused():
    m = measured()
    data = dataclasses.replace(m, reused=True).to_json()
    assert data["reused"] is True
    assert Measured.from_json(data) == m and not Measured.from_json(data).reused
    assert Measured.from_json({**data, "added_later": 1}, reused=True).reused


# ─── Deciding ─────────────────────────────────────────────────────────────────────────────────────


def test_nothing_is_proposed_without_a_complete_incumbent():
    challenger = measured("v/cheap", cost="0.0001")
    assert decide(Tier.CHEAP, None, [challenger]) is None
    assert decide(Tier.CHEAP, measured(incumbent=True, error=5), [challenger]) is None


def test_a_clearly_cheaper_challenger_that_agrees_as_well_is_proposed():
    incumbent = measured("v/now", incumbent=True, cost="0.001", agreement=0.8)
    cheaper = measured("v/cheap", cost=str(0.001 * (1 - CHEAPER_BY) * 0.9), agreement=0.78)
    d = decide(Tier.CHEAP, incumbent, [cheaper])
    assert d is not None and d.challenger.slug == "v/cheap" and "cheaper per event" in d.why


def test_a_little_cheaper_or_agreeing_less_is_not_enough():
    incumbent = measured("v/now", incumbent=True, cost="0.001", agreement=0.8)
    slightly = measured("v/slight", cost="0.0009", agreement=0.8)
    worse = measured("v/worse", cost="0.0001", agreement=0.7)
    assert decide(Tier.CHEAP, incumbent, [slightly, worse]) is None


def test_a_free_challenger_beats_a_paid_incumbent_that_agrees_no_better():
    incumbent = measured("v/paid", tier=Tier.FREE, incumbent=True, agreement=0.9)
    free = measured("v/new:free", tier=Tier.FREE, cost="0", agreement=0.87)
    d = decide(Tier.FREE, incumbent, [free])
    assert d is not None and d.why == "free, and agrees as well"


def test_between_free_models_only_clearly_better_agreement_counts():
    incumbent = measured("v/now:free", tier=Tier.FREE, incumbent=True, cost="0", agreement=0.8)
    same = measured("v/same:free", tier=Tier.FREE, cost="0", agreement=0.84)
    better = measured("v/better:free", tier=Tier.FREE, cost="0", agreement=0.9)
    assert decide(Tier.FREE, incumbent, [same]) is None
    d = decide(Tier.FREE, incumbent, [same, better])
    assert d is not None and d.challenger.slug == "v/better:free" and "agrees more" in d.why


def test_a_challenger_that_fails_a_gate_is_never_proposed():
    incumbent = measured("v/now", incumbent=True, cost="0.001")
    flaky = measured("v/flaky", cost="0.0001", ok=18, invalid=2)
    assert decide(Tier.CHEAP, incumbent, [flaky]) is None


def test_an_incumbent_that_fails_a_gate_is_replaced_by_the_best_that_clears_them():
    incumbent = measured("v/now", incumbent=True, ok=15, refused=5)
    pricey = measured("v/pricey", cost="0.002", agreement=0.9)
    cheap = measured("v/cheap", cost="0.0005", agreement=0.7)
    poor = measured("v/poor", cost="0.0001", agreement=0.5)
    d = decide(Tier.CHEAP, incumbent, [pricey, cheap, poor])
    assert d is not None and d.challenger.slug == "v/cheap"
    assert d.why.startswith("the incumbent fails a gate: refused")


# ─── Choosing challengers ─────────────────────────────────────────────────────────────────────────


def row(
    slug,
    prompt="0.1",
    completion="0.4",
    *,
    days_ago=100,
    capable=True,
    expiration=None,
    index=None,
    withdrawn=False,
):
    return Listed(
        slug,
        NOW - timedelta(days=days_ago),
        None if prompt is None else Decimal(prompt),
        None if completion is None else Decimal(completion),
        capable,
        expiration,
        None if index is None else Decimal(index),
        withdrawn,
    )


def test_tier_0_takes_free_models_and_the_others_paid_ones():
    rows = [row("v/free:free", "0", "0"), row("v/paid")]
    assert [r.slug for r in challengers(rows, Tier.FREE, [], NOW)] == ["v/free:free"]
    assert [r.slug for r in challengers(rows, Tier.STRONG, [], NOW)] == ["v/paid"]


def test_models_that_could_never_be_promoted_are_left_out():
    soon = (NOW + EXPIRY_MARGIN - timedelta(days=1)).date()
    rows = [
        row("v/in-chain"),
        row("v/withdrawn", withdrawn=True),
        row("v/incapable", capable=False),
        row("openrouter/auto"),
        row("v/model:batch"),
        row("v/no-price", None, None),
        row("v/too-dear", completion="1.5"),
        row("v/expiring", expiration=soon),
        row("v/fine", expiration=date(2027, 6, 1)),
    ]
    assert [r.slug for r in challengers(rows, Tier.CHEAP, ["v/in-chain"], NOW)] == ["v/fine"]


def test_new_models_go_first_then_the_index_then_the_price():
    rows = [
        row("v/cheap", completion="0.1"),
        row("v/smart", completion="0.9", index="60"),
        row("v/new", completion="0.9", days_ago=3),
        row("v/plain", completion="0.2"),
    ]
    order = [r.slug for r in challengers(rows, Tier.CHEAP, [], NOW)]
    assert order == ["v/new", "v/smart", "v/cheap", "v/plain"]


def test_the_estimate_is_from_the_headline_price_and_assumes_the_ceiling_when_unknown():
    assert estimate(row("v/free:free", "0", "0"), 30) == 0
    assert estimate(row("v/m", "0.5", "1.0"), 10) == Decimal("0.025")
    assert estimate(None, 10) == Decimal("0.035")


# ─── The proposal ─────────────────────────────────────────────────────────────────────────────────


def test_the_proposal_shows_both_models_and_what_was_not_checked():
    incumbent = measured("v/now", incumbent=True, cost="0.001")
    cheaper = measured("v/cheap", cost="0.0001")
    d = decide(Tier.CHEAP, incumbent, [cheaper])
    assert proposal_title(d) == "[MODEL] Proposal: tier1_cheap -> v/cheap"
    body = proposal_body(d, golden_size=30, reviewed=0, au_labels=(2, 28))
    assert (
        "| `v/now` (now) | `v/cheap` |" in body
        and "| cost per event | $0.001000 | $0.000100 |" in body
    )
    assert "adoption veto" in body and "0 of 30 have been reviewed" in body
    assert "over 2 Australian and 28 non-Australian events, too few of one kind" in body
    assert "MORPHEUS and ROGUE" in body and "config/models.yaml" in body
    payload = proposal_payload(d, gauntlet_run=7)
    assert (payload["tier"], payload["slug"], payload["replaces"], payload["gauntlet_run"]) == (
        "tier1_cheap",
        "v/cheap",
        "v/now",
        7,
    )
    assert payload["challenger"]["cost_per_event"] == 0.0001


def test_only_the_brief_proposal_says_how_many_au_labels_it_rests_on():
    def body(tier, au_labels):
        d = decide(
            tier,
            measured("v/now", tier=tier, incumbent=True, cost="0.001"),
            [measured("v/cheap", tier=tier, cost="0.0001")],
        )
        return proposal_body(d, golden_size=30, reviewed=0, au_labels=au_labels)

    enough = body(Tier.CHEAP, (6, 20))
    assert "over 6 Australian and 20 non-Australian events." in enough
    assert "too few" not in enough
    assert "AU relevance" not in body(Tier.STRONG, (0, 30))
