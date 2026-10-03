"""RIPPERDOC's scan and gauntlet as the scheduler runs them, with OpenRouter and the database
faked: what is stored, what is tried, and what stops a run."""

import asyncio
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from worker.ai import scout
from worker.ai.budget import BudgetUnreadable, KeyStatus
from worker.ai.catalogue import Known, parse_listing
from worker.ai.client import BudgetExhausted, CallFailed, Completion, InvalidOutput
from worker.ai.gauntlet import (
    INCUMBENT_REUSE,
    MONTHLY_CAP_USD,
    TIER_TASK,
    Attempt,
    Listed,
    measure,
)
from worker.ai.golden import digest, pin
from worker.ai.ladder import (
    Breach,
    CatalogueUnavailable,
    Ladder,
    LadderRejected,
    Tier,
    VerifiedCandidate,
    VerifiedLadder,
    prune,
)
from worker.ai.tasks import SourceFacts, Subject, TaskName
from worker.models import CveRef, Event, Severity, SeveritySource
from worker.settings import Settings

NOW = datetime(2026, 10, 4, 3, 40, tzinfo=UTC)  # a Sunday
KEY = "fake-key-TESTONLY"
LADDER = Ladder.load()
FREE, CHEAP, STRONG = (LADDER.tiers[t][0] for t in (Tier.FREE, Tier.CHEAP, Tier.STRONG))
SOURCE_TEXT = (
    "A heap overflow in the Acme SecureGate management interface lets an unauthenticated "
    "attacker run code as root. Acme has released fixed builds."
)


def settings(key=KEY, budget="20"):
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        openrouter_api_key=SecretStr(key) if key else None,
        ai_monthly_budget_usd=Decimal(budget),
    )


class FakeDb:
    """worker/db/scout.py in memory, keeping what the scout stored."""

    def __init__(self):
        self.catalogue: dict[str, Known] = {}
        self.last_ladder = None
        self.candidates: list[tuple[str, str]] = []
        self.golden = []
        self.listed: list[Listed] = []
        self.spent = Decimal(0)
        self.previous = []
        self.open: set[str] = set()
        self.calls: list[tuple[str, tuple, dict]] = []

    def called(self, name):
        return [(a, k) for n, a, k in self.calls if n == name]

    def _note(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def load_catalogue(self, conn):
        return self.catalogue

    def store_listing(self, conn, listing, changes, now):
        self._note("store_listing", listing, list(changes), now)

    def last_scan_ladder(self, conn):
        return self.last_ladder

    def insert_changes(self, conn, changes, now):
        self._note("insert_changes", list(changes), now)

    def record_scan(self, conn, **kwargs):
        self._note("record_scan", **kwargs)

    def golden_candidates(self, conn):
        return self.candidates

    def load_golden(self, conn):
        return self.golden

    def golden_reviewed(self, conn):
        return 0

    def replace_golden(self, conn, golden, now):
        self.golden = list(golden)
        self._note("replace_golden", list(golden), now)

    def load_listed(self, conn):
        return self.listed

    def gauntlet_spend(self, conn, start, end):
        return self.spent

    def recent_results(self, conn, golden_digest, since):
        self._note("recent_results", golden_digest, since)
        return self.previous

    def record_gauntlet(self, conn, **kwargs):
        self._note("record_gauntlet", **kwargs)
        return 7

    def propose(self, conn, *, title, body, payload, now):
        self._note("propose", title=title, body=body, payload=payload, now=now)
        if title in self.open:
            return None
        self.open.add(title)
        return len(self.open)


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(scout, "db", fake)
    monkeypatch.setattr(scout, "_in", lambda engine, fn, /, *a, **k: fn("conn", *a, **k))
    return fake


def _give(value):
    if isinstance(value, BaseException):
        raise value
    return value


# ─── The daily scan ───────────────────────────────────────────────────────────────────────────────


def entry(slug, prompt="0.0000001", completion="0.0000004"):
    if slug.endswith(":free"):
        prompt = completion = "0"
    return {
        "id": slug,
        "name": slug,
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["tools", "structured_outputs"],
    }


def listing(*extra):
    return parse_listing({"data": [entry(s) for s in sorted(LADDER.slugs()) + list(extra)]})


def known(models):
    return {
        m.slug: Known(m.slug, m.prompt_per_mtok, m.completion_per_mtok, m.capable, None, False)
        for m in models
    }


class Scan:
    def __init__(self):
        self.listing = listing("v/other")
        self.verify = lambda ladder: VerifiedLadder(ladder, NOW)


@pytest.fixture
def scan(fake_db, monkeypatch):
    state = Scan()

    async def fetch_listing(http):
        return _give(state.listing)

    async def verify_ladder(ladder, client):
        return state.verify(ladder) if callable(state.verify) else _give(state.verify)

    monkeypatch.setattr(scout, "fetch_listing", fetch_listing)
    monkeypatch.setattr(scout, "verify_ladder", verify_ladder)
    return state


async def scan_models():
    return await scout.scan_models(engine=object(), settings=settings(), now=NOW)


def recorded_scan(fake_db):
    [(_, kwargs)] = fake_db.called("record_scan")
    return kwargs


async def test_the_first_scan_stores_the_list_and_the_ladder_as_checked(scan, fake_db):
    summary = await scan_models()
    [(args, _)] = fake_db.called("store_listing")
    assert args == (scan.listing, [], NOW)
    rec = recorded_scan(fake_db)
    assert (rec["models_listed"], rec["changes"], rec["error"]) == (len(scan.listing), 0, None)
    assert rec["ladder"][Tier.CHEAP.value]["effective"] == list(LADDER.tiers[Tier.CHEAP])
    assert summary.verified is not None and not summary.ladder_failed
    assert not summary.changed_anything and summary.dropped == []


async def test_a_model_leaving_the_ladder_and_a_new_free_one_are_noted(scan, fake_db, caplog):
    fake_db.catalogue = known(scan.listing)
    scan.listing = listing("v/other", "v/new:free")
    breach = Breach(Tier.CHEAP, CHEAP, "over the ceiling")
    scan.verify = lambda ladder: VerifiedLadder(prune(ladder, [breach]), NOW, (breach,))

    with caplog.at_level(logging.INFO, logger="worker.ai.scout"):
        summary = await scan_models()

    assert summary.new_free == ["v/new:free"]
    assert summary.dropped == [f"tier1_cheap: {CHEAP}: over the ceiling"]
    assert summary.changes == Counter({"new": 1, "dropped": 1}) and summary.changed_anything
    assert CHEAP not in summary.verified.ladder.tiers[Tier.CHEAP]
    [((drops, _), _)] = fake_db.called("insert_changes")
    assert [(c.slug, c.kind) for c in drops] == [(CHEAP, "dropped")]
    assert recorded_scan(fake_db)["changes"] == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("new free model v/new:free" in w for w in warnings)
    assert any(f"{CHEAP} left tier1_cheap: over the ceiling" in w for w in warnings)


async def test_a_model_still_dropped_is_not_noted_again(scan, fake_db):
    breach = Breach(Tier.CHEAP, CHEAP, "over the ceiling")
    scan.verify = lambda ladder: VerifiedLadder(prune(ladder, [breach]), NOW, (breach,))
    await scan_models()
    fake_db.last_ladder = recorded_scan(fake_db)["ladder"]
    fake_db.calls.clear()

    summary = await scan_models()
    [((drops, _), _)] = fake_db.called("insert_changes")
    assert drops == [] and not summary.changed_anything
    assert summary.dropped == [f"tier1_cheap: {CHEAP}: over the ceiling"]


async def test_a_ladder_that_does_not_pass_is_failed_and_recorded(scan, fake_db):
    breach = Breach(Tier.STRONG, STRONG, "no capable route")
    scan.verify = LadderRejected([breach], "tier2_strong would be empty")
    summary = await scan_models()
    assert summary.ladder_failed and summary.verified is None
    assert summary.errors[0].startswith("the ladder did not pass: ")
    assert summary.dropped == [str(breach)]
    rec = recorded_scan(fake_db)
    assert all(t["effective"] is None for t in rec["ladder"].values())
    assert "tier2_strong would be empty" in rec["error"]


async def test_a_list_that_cannot_be_read_is_an_error_and_the_ladder_is_still_checked(
    scan, fake_db
):
    scan.listing = CatalogueUnavailable("HTTP 503")
    summary = await scan_models()
    assert not fake_db.called("store_listing")
    assert summary.models_listed is None and summary.verified is not None
    rec = recorded_scan(fake_db)
    assert rec["models_listed"] is None and "HTTP 503" in rec["error"]
    assert rec["ladder"] is not None


async def test_a_ladder_that_could_not_be_checked_is_not_a_failure(scan, fake_db):
    scan.verify = CatalogueUnavailable("timed out")
    summary = await scan_models()
    assert not summary.ladder_failed and summary.verified is None
    assert summary.errors == ["the ladder could not be checked: timed out"]
    rec = recorded_scan(fake_db)
    assert rec["ladder"] is None
    [((drops, _), _)] = fake_db.called("insert_changes")
    assert drops == []


async def test_a_ladder_file_that_cannot_be_read_fails(scan, fake_db, monkeypatch):
    def broken(cls):
        raise OSError("No such file")

    monkeypatch.setattr(scout.Ladder, "load", classmethod(broken))
    summary = await scan_models()
    assert summary.ladder_failed and summary.verified is None
    assert summary.errors == ["models.yaml: No such file"]
    assert recorded_scan(fake_db)["ladder"] is None


# ─── The golden set ───────────────────────────────────────────────────────────────────────────────


def subject(n: int) -> Subject:
    t0 = datetime(2026, 9, 20, 14, 30, tzinfo=UTC)
    return Subject(
        event=Event(
            event_id=f"evt-2026-{n:06d}",
            first_seen=t0,
            last_seen=t0,
            title="Acme SecureGate heap overflow",
            summary=SOURCE_TEXT,
            severity=Severity.CRITICAL,
            severity_source=SeveritySource.CNA,
            cves=[CveRef(id=f"CVE-2026-{4000 + n}")],
        ),
        source_text=SOURCE_TEXT,
        headlines=("Acme patches SecureGate flaw",),
        sources=(SourceFacts("CISA", "us", "advisory"),),
    )


SUBJECTS = {s.event.event_id: s for s in map(subject, range(1, 5))}
GOLDEN = [pin(s) for s in SUBJECTS.values()]


@pytest.fixture
def subjects(monkeypatch):
    monkeypatch.setattr(scout, "load_subjects", lambda conn, ids: [SUBJECTS[i] for i in ids])


def test_pinning_replaces_the_set_with_the_candidates_chosen(fake_db, subjects):
    fake_db.candidates = [(i, "critical") for i in SUBJECTS]
    golden = scout.pin_golden_set(engine=object(), now=NOW)
    assert golden == GOLDEN
    assert fake_db.called("replace_golden") == [((GOLDEN, NOW), {})]


# ─── The gauntlet ─────────────────────────────────────────────────────────────────────────────────

ANSWERS = {
    TaskName.TRIAGE: {
        "domains": ["cybersecurity"],
        "categories": ["vulnerability"],
        "ai_subdomain": None,
        "entities": {
            "actors": [],
            "organisations": ["Acme"],
            "products": [],
            "countries": [],
            "industries": [],
        },
        "tags": ["rce"],
    },
    TaskName.BRIEF: {
        "summary": "Acme has fixed a flaw that lets anyone reach its gateways take them over.",
        "why_it_matters": None,
        "au": {"relevance": 0.1, "reasons": [], "sectors": []},
    },
    TaskName.SEVERITY: {
        "severity": "critical",
        "confidence": 0.9,
        "rationale": "Unauthenticated code execution on an exposed gateway.",
    },
}


def row(slug, prompt="0.02", completion="0.08"):
    return Listed(
        slug,
        NOW - timedelta(days=100),
        Decimal(prompt),
        Decimal(completion),
        True,
        None,
        None,
        False,
    )


class Gauntlet:
    """OpenRouter as the gauntlet sees it: the key, the guard and `trial`."""

    def __init__(self):
        self.status = KeyStatus(None, None, None, Decimal(0), Decimal(0), None)
        self.ladder = VerifiedLadder(LADDER, NOW)
        self.rejected: set[str] = set()  # slugs the guard turns away
        self.unchecked: set[str] = set()  # slugs the guard cannot check
        self.raises: dict[str, Exception] = {}  # what trial raises, per slug
        self.answers: dict[str, dict] = {}  # answers per slug, where not ANSWERS
        self.costs: dict[str, Decimal] = {"v/cheap": Decimal("0.00005")}
        self.calls: list[tuple] = []

    def slugs_called(self):
        return Counter(c[0] for c in self.calls)

    async def trial(self, candidate, *, schema_name, schema, messages, max_tokens, attribution):
        task = TIER_TASK[candidate.tier].name
        self.calls.append((candidate.slug, task, messages, attribution))
        if candidate.slug in self.raises:
            raise self.raises[candidate.slug]
        free = candidate.slug.endswith(":free")
        cost = Decimal(0) if free else self.costs.get(candidate.slug, Decimal("0.0005"))
        data = self.answers.get(candidate.slug, ANSWERS)[task]
        return Completion(data, candidate.slug, "Provider", 2000, 500, cost, "gen-1", 1200)


@pytest.fixture
def gauntlet(fake_db, monkeypatch):
    g = Gauntlet()
    fake_db.golden = list(GOLDEN)
    fake_db.listed = [row("v/new:free", "0", "0"), row("v/cheap")]

    async def fetch_key_status(http, api_key, user_agent):
        assert api_key.get_secret_value() == KEY
        return _give(g.status)

    async def verify_ladder(ladder=None, client=None):
        return _give(g.ladder)

    async def verify_candidate(slug, tier, client=None):
        if slug in g.unchecked:
            raise CatalogueUnavailable("timed out")
        if slug in g.rejected:
            raise LadderRejected([Breach(tier, slug, "no capable route")])
        return VerifiedCandidate(tier, slug, NOW)

    monkeypatch.setattr(scout, "fetch_key_status", fetch_key_status)
    monkeypatch.setattr(scout, "verify_ladder", verify_ladder)
    monkeypatch.setattr(scout, "verify_candidate", verify_candidate)
    monkeypatch.setattr(scout, "OpenRouterClient", lambda *args: g)
    return g


async def run_gauntlet(**kwargs):
    return await scout.run_gauntlet(engine=object(), settings=settings(**kwargs), now=NOW)


def recorded_run(fake_db):
    [(_, kwargs)] = fake_db.called("record_gauntlet")
    return kwargs


async def test_a_full_run_tries_every_tier_and_proposes_what_is_better(gauntlet, fake_db):
    summary = await run_gauntlet()

    assert gauntlet.slugs_called() == {FREE: 4, "v/new:free": 4, CHEAP: 4, STRONG: 4, "v/cheap": 8}
    assert [(m.tier, m.slug, m.incumbent) for m in summary.measured] == [
        ("tier0_free", FREE, True),
        ("tier0_free", "v/new:free", False),
        ("tier1_cheap", CHEAP, True),
        ("tier1_cheap", "v/cheap", False),
        ("tier2_strong", STRONG, True),
        ("tier2_strong", "v/cheap", False),
    ]
    assert all(m.complete and m.agreement == 1.0 for m in summary.measured)
    # Two free models that agree as well: nothing to change in tier 0.
    assert summary.proposals == [
        "[MODEL] Proposal: tier1_cheap -> v/cheap",
        "[MODEL] Proposal: tier2_strong -> v/cheap",
    ]
    assert summary.spent_usd == Decimal("0.0044") and summary.notes == []
    assert summary.run_id == 7 and summary.changed_anything

    run = recorded_run(fake_db)
    assert run["golden_digest"] == digest(GOLDEN) and run["spent_usd"] == Decimal("0.0044")
    assert run["results"] == summary.measured and run["note"] is None
    assert [k["payload"]["gauntlet_run"] for _, k in fake_db.called("propose")] == [7, 7]
    assert fake_db.called("recent_results") == [((digest(GOLDEN), NOW - INCUMBENT_REUSE), {})]


async def test_every_call_is_ripperdocs_and_severity_is_judged_blind(gauntlet, fake_db):
    await run_gauntlet()
    assert {(a.agent, a.stage, a.event_id) for *_, a in gauntlet.calls} == {
        ("ripperdoc", "gauntlet", None)
    }
    shown = {
        task: json.loads(msgs[-1]["content"])["official_severity"]
        for _, task, msgs, _ in gauntlet.calls
    }
    assert shown == {
        TaskName.TRIAGE: "critical",
        TaskName.BRIEF: "critical",
        TaskName.SEVERITY: "unknown",
    }


async def test_a_proposal_already_open_is_not_raised_again(gauntlet, fake_db):
    fake_db.open = {"[MODEL] Proposal: tier1_cheap -> v/cheap"}
    summary = await run_gauntlet()
    assert summary.proposals == ["[MODEL] Proposal: tier2_strong -> v/cheap"]


async def test_a_recent_incumbent_result_is_reused_rather_than_paid_for_again(gauntlet, fake_db):
    earlier = measure(
        Tier.CHEAP,
        CHEAP,
        [Attempt(g.event_id, "ok", 1.0, 1000, Decimal("0.0005")) for g in GOLDEN],
        incumbent=True,
        measured_at=NOW - timedelta(days=7),
        golden_digest=digest(GOLDEN),
    )
    fake_db.previous = [earlier]
    summary = await run_gauntlet()
    assert CHEAP not in gauntlet.slugs_called()
    [reused] = [m for m in summary.measured if m.slug == CHEAP]
    assert reused.reused and reused == earlier
    assert "[MODEL] Proposal: tier1_cheap -> v/cheap" in summary.proposals


async def test_with_the_months_cap_spent_only_free_models_are_tried(gauntlet, fake_db):
    fake_db.spent = MONTHLY_CAP_USD
    summary = await run_gauntlet()
    assert set(gauntlet.slugs_called()) == {FREE, "v/new:free"}
    assert summary.notes == ["paid tiers not tried: the month's $0.25 for the gauntlet is spent"]
    assert summary.spent_usd == 0 and summary.proposals == []
    assert recorded_run(fake_db)["note"] == summary.notes[0]


async def test_a_model_that_should_cost_more_than_is_left_is_not_tried(gauntlet, fake_db):
    fake_db.spent = MONTHLY_CAP_USD - Decimal("0.01")
    summary = await run_gauntlet()
    # No incumbent result to compare against, so its challengers are not paid for either.
    assert set(gauntlet.slugs_called()) == {FREE, "v/new:free"}
    assert summary.notes == [
        f"tier1_cheap: {CHEAP} not tried: it should cost about $0.0140, and $0.0100 is left",
        f"tier2_strong: {STRONG} not tried: it should cost about $0.0140, and $0.0100 is left",
    ]


async def test_a_402_stops_the_run_and_nothing_is_proposed_from_it(gauntlet, fake_db):
    gauntlet.raises[CHEAP] = BudgetExhausted(
        "key limit", limit_source="openrouter_key_limit", retry_after=None
    )
    summary = await run_gauntlet()
    assert gauntlet.slugs_called()[CHEAP] == 1  # the others were skipped once it came back
    assert STRONG not in gauntlet.slugs_called() and "v/cheap" not in gauntlet.slugs_called()
    [cut] = [m for m in summary.measured if m.slug == CHEAP]
    assert not cut.complete and (cut.errors, cut.skipped) == (1, 3)
    assert summary.proposals == []
    assert summary.notes == ["OpenRouter turned a call away for budget (HTTP 402)"]


async def test_too_few_free_requests_left_leaves_tier_0_out(gauntlet, fake_db):
    gauntlet.status = KeyStatus(None, None, None, Decimal(0), Decimal(0), 10)
    summary = await run_gauntlet()
    assert FREE not in gauntlet.slugs_called() and "v/new:free" not in gauntlet.slugs_called()
    assert summary.notes == ["tier0_free not tried: 10 free requests left today"]
    assert len(summary.proposals) == 2


async def test_an_incumbent_the_guard_turns_away_leaves_its_tier_out(gauntlet, fake_db):
    gauntlet.rejected = {CHEAP}
    summary = await run_gauntlet()
    assert CHEAP not in gauntlet.slugs_called() and gauntlet.slugs_called()["v/cheap"] == 4
    assert summary.notes == [
        f"tier1_cheap: {CHEAP} not tried: tier1_cheap: {CHEAP}: no capable route"
    ]
    assert summary.proposals == ["[MODEL] Proposal: tier2_strong -> v/cheap"]


async def test_a_challenger_the_guard_turns_away_makes_room_for_the_next(gauntlet, fake_db):
    fake_db.listed.append(row("v/second", "0.03", "0.09"))
    gauntlet.rejected = {"v/cheap"}
    gauntlet.costs["v/second"] = Decimal("0.00006")
    summary = await run_gauntlet()
    assert "v/cheap" not in gauntlet.slugs_called()
    assert gauntlet.slugs_called()["v/second"] == 8 and len(summary.proposals) == 2


async def test_challengers_that_cannot_be_checked_keep_the_incumbents_result(gauntlet, fake_db):
    gauntlet.unchecked = {"v/new:free", "v/cheap"}
    summary = await run_gauntlet()
    assert [m.slug for m in summary.measured] == [FREE, CHEAP, STRONG]
    assert summary.notes == [
        f"{t}: challengers not checked: timed out"
        for t in ("tier0_free", "tier1_cheap", "tier2_strong")
    ]
    assert summary.proposals == []


async def test_a_challenger_whose_answers_fail_productions_checks_is_not_proposed(
    gauntlet, fake_db
):
    # On the Australian desk without a reason: parse_brief turns it away.
    gauntlet.answers["v/cheap"] = {
        **ANSWERS,
        TaskName.BRIEF: {
            **ANSWERS[TaskName.BRIEF],
            "au": {"relevance": 0.8, "reasons": [], "sectors": []},
        },
    }
    summary = await run_gauntlet()
    [m] = [m for m in summary.measured if m.slug == "v/cheap" and m.tier == "tier1_cheap"]
    assert (m.compliant, m.passed) == (4, 0)
    assert summary.proposals == ["[MODEL] Proposal: tier2_strong -> v/cheap"]


@pytest.mark.parametrize(
    "raised, outcome, cost",
    [
        (InvalidOutput("filtered", model=None, refused=True, duration_ms=800), "refused", None),
        (InvalidOutput("not json", model=None, cost_usd=Decimal("0.0001")), "invalid", "0.0001"),
        (CallFailed("bad gateway", status=502), "error", None),
    ],
)
async def test_what_a_failed_call_counts_as(raised, outcome, cost):
    g = Gauntlet()
    g.raises["v/m"] = raised
    purse = scout._Purse(Decimal("0.01"))
    attempt = await scout._attempt(
        g, VerifiedCandidate(Tier.CHEAP, "v/m", NOW), GOLDEN[0], asyncio.Semaphore(1), purse
    )
    assert attempt.outcome == outcome
    assert purse.spent == (Decimal(cost) if cost else 0)


async def test_a_paid_model_is_not_called_once_the_purse_is_empty():
    g = Gauntlet()
    purse = scout._Purse(Decimal(0))
    gate = asyncio.Semaphore(1)
    paid = await scout._attempt(
        g, VerifiedCandidate(Tier.CHEAP, "v/m", NOW), GOLDEN[0], gate, purse
    )
    free = await scout._attempt(
        g, VerifiedCandidate(Tier.FREE, "v/m:free", NOW), GOLDEN[0], gate, purse
    )
    assert (paid.outcome, free.outcome) == ("skipped", "ok")
    assert [c[0] for c in g.calls] == ["v/m:free"]


async def test_the_golden_set_is_pinned_the_first_time_none_is_found(gauntlet, fake_db, subjects):
    fake_db.golden = []
    fake_db.candidates = [(i, "critical") for i in SUBJECTS]
    summary = await run_gauntlet()
    assert fake_db.called("replace_golden") == [((GOLDEN, NOW), {})]
    assert summary.skipped is None and summary.measured


@pytest.mark.parametrize(
    "setup, kwargs, why",
    [
        (lambda g, db: None, {"key": None}, "OPENROUTER_API_KEY is not set"),
        (
            lambda g, db: (setattr(db, "golden", []), setattr(db, "candidates", [])),
            {},
            "no event qualifies for the golden set yet",
        ),
        (lambda g, db: setattr(db, "listed", []), {}, "the model scan has not run yet"),
        (
            lambda g, db: setattr(g, "status", BudgetUnreadable("HTTP 500")),
            {},
            "no budget reading: HTTP 500",
        ),
        (
            lambda g, db: None,
            {"budget": "0"},
            "the budget mode is off: no monthly AI budget is set",
        ),
        (
            lambda g, db: setattr(g, "ladder", CatalogueUnavailable("timed out")),
            {},
            "the ladder did not pass: timed out",
        ),
    ],
)
async def test_what_skips_a_run_records_nothing(gauntlet, fake_db, subjects, setup, kwargs, why):
    setup(gauntlet, fake_db)
    summary = await run_gauntlet(**kwargs)
    assert summary.skipped == why
    assert gauntlet.calls == [] and not fake_db.called("record_gauntlet")
    assert not summary.changed_anything
