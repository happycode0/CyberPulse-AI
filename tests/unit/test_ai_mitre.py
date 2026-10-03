"""MITRE technique suggestions (worker/ai/mitre.py): who gets one, which techniques the model is
shown, what of its answer is kept, and what a pass spends on. No network and no database."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr

from worker.ai import mitre as mitre_mod
from worker.ai.budget import Governor, KeyStatus
from worker.ai.client import Completion
from worker.ai.enrich import Done
from worker.ai.ladder import Ladder, Tier, VerifiedLadder
from worker.ai.mitre import (
    MAX_CANDIDATES,
    MAX_SUGGESTIONS,
    MitreAnswer,
    MitreSummary,
    mitre_task,
    parse_mitre,
    shortlist,
    suggest_techniques,
    wants_mitre,
)
from worker.ai.tasks import Rejected, Subject, TaskName
from worker.db.mitre import CurrentCatalogue
from worker.groundtruth.mitre import Technique
from worker.models import AiSubdomain, Event, EvidenceClass, Severity, SourceRef
from worker.settings import Settings

T0 = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
SOURCE_TEXT = (
    "Attackers exploited a flaw in the SecureGate VPN portal and left a web shell on the "
    "appliance before moving to file servers."
)

ATTACK = CurrentCatalogue(
    "ATT&CK v19.2",
    (
        Technique("T1190", "Exploit Public-Facing Application", ("initial-access",), None),
        Technique("T1133", "External Remote Services", ("initial-access", "persistence"), None),
        Technique("T1078", "Valid Accounts", ("initial-access",), None),
        Technique("T1505", "Server Software Component", ("persistence",), None),
        Technique("T1505.003", "Web Shell", ("persistence",), "T1505"),
        Technique("T1566", "Phishing", ("initial-access",), None),
        Technique("T1566.002", "Spearphishing Link", ("initial-access",), "T1566"),
        Technique("T1486", "Data Encrypted for Impact", ("impact",), None),
        Technique("T1498", "Network Denial of Service", ("impact",), None),
    ),
)
ATLAS = CurrentCatalogue(
    "ATLAS 2026.09",
    (
        Technique("AML.T0051", "LLM Prompt Injection", ("AML.TA0005",), None),
        Technique("AML.T0054", "LLM Jailbreak", ("AML.TA0012",), None),
        Technique("AML.T0099", "Poison Training Data", ("AML.TA0003",), None),
    ),
)


def subject(event_id="evt-2026-000001", source_text=SOURCE_TEXT, **changes) -> Subject:
    fields = {
        "event_id": event_id,
        "first_seen": T0,
        "last_seen": T0,
        "title": "SecureGate VPN flaw exploited",
        "summary": source_text,
        "categories": ["vulnerability"],
        "sources": [
            SourceRef(
                source_id="vendor_acme",
                url="https://acme.example/advisory",
                evidence_class=EvidenceClass.VENDOR,
            ),
            SourceRef(
                source_id="news_wire",
                url="https://news.example/story",
                evidence_class=EvidenceClass.NEWS,
            ),
        ],
    }
    return Subject(event=Event(**(fields | changes)), source_text=source_text)


# ─── Who gets a suggestion, and from what ─────────────────────────────────────────────────────────


def test_attack_stories_get_a_suggestion_and_policy_stories_do_not():
    assert wants_mitre(subject())
    assert not wants_mitre(subject(categories=["policy", "regulation"]))
    assert wants_mitre(subject(categories=["policy"], ai_subdomain=AiSubdomain.AI_SECURITY))


def test_the_shortlist_starts_with_the_categorys_techniques_then_names_the_record_shares():
    ids = [c.technique.id for c in shortlist(subject(), ATTACK, ATLAS)]
    # Hints in their own order, skipping those the loaded release lacks (T1203, T1068, ...).
    assert ids[:2] == ["T1190", "T1133"]
    # "web shell" appears in the record; nothing about phishing or encryption does.
    assert "T1505.003" in ids and "T1566.002" not in ids and "T1486" not in ids
    # Not an AI story, so no ATLAS.
    assert not any(i.startswith("AML.") for i in ids)


def test_a_sub_technique_is_shown_under_its_parents_name():
    by_id = {c.technique.id: c for c in shortlist(subject(), ATTACK, None)}
    assert by_id["T1505.003"].technique.name == "Server Software Component: Web Shell"
    assert by_id["T1505.003"].dataset_version == "ATT&CK v19.2"


def test_an_ai_story_is_shown_atlas_first():
    s = subject(
        categories=["ai-security"],
        ai_subdomain=AiSubdomain.AI_SECURITY,
        source_text="A prompt injection in the assistant leaked customer records.",
    )
    ids = [c.technique.id for c in shortlist(s, ATTACK, ATLAS)]
    assert ids[:2] == ["AML.T0051", "AML.T0054"]
    assert "AML.T0099" not in ids  # neither a hint nor named by the record


def test_the_shortlist_is_capped(monkeypatch):
    monkeypatch.setattr(mitre_mod, "MAX_CANDIDATES", 2)
    assert len(shortlist(subject(), ATTACK, ATLAS)) == 2
    assert MAX_CANDIDATES >= 20


def test_with_no_catalogue_there_is_nothing_to_choose_from():
    assert shortlist(subject(), None, None) == []


# ─── The call ─────────────────────────────────────────────────────────────────────────────────────


def test_the_task_is_tier_2_and_its_schema_allows_only_the_shortlist():
    candidates = shortlist(subject(), ATTACK, None)
    task = mitre_task(candidates)
    assert (task.name, task.tier, task.stage, task.editorial) == (
        TaskName.MITRE,
        Tier.STRONG,
        "enrich.mitre",
        True,
    )
    enum = task.schema["properties"]["techniques"]["items"]["properties"]["id"]["enum"]
    assert enum == [c.technique.id for c in candidates]
    assert "T1505.003: Server Software Component: Web Shell [persistence]" in task.instructions


def answer(*picks):
    return {"techniques": [{"id": i, "confidence": c, "basis": b} for i, c, b in picks]}


def candidates_by_id():
    return {c.technique.id: c for c in shortlist(subject(), ATTACK, None)}


def test_a_good_answer_is_kept_best_first_with_the_events_sources_as_evidence():
    data = answer(
        ("T1505.003", 0.7, "The intruders planted a web shell on the device."),
        ("T1190", 0.9, "The VPN portal flaw was the way in."),
    )
    result = parse_mitre(data, subject(), candidates_by_id())
    assert [s.technique.id for s, _ in result.picks] == ["T1190", "T1505.003"]
    first, basis = result.picks[0]
    assert (first.dataset_version, first.confidence) == ("ATT&CK v19.2", 0.9)
    assert first.evidence == ("vendor_acme", "news_wire")
    assert basis == "The VPN portal flaw was the way in."


def test_weak_and_repeated_picks_are_dropped_and_at_most_three_kept():
    data = answer(
        ("T1190", 0.9, "The VPN portal flaw was the way in."),
        ("T1190", 0.8, "Said twice."),
        ("T1133", 0.6, "The VPN is a remote service."),
        ("T1505.003", 0.7, "A web shell was planted."),
        ("T1505", 0.55, "Server software was changed."),
        ("T1133", 0.2, "Possible in general."),
    )
    result = parse_mitre(data, subject(), candidates_by_id())
    assert len(result.picks) == MAX_SUGGESTIONS
    assert [s.technique.id for s, _ in result.picks] == ["T1190", "T1505.003", "T1133"]
    assert result.dropped == 1


def test_an_empty_answer_is_a_valid_answer():
    assert parse_mitre(answer(), subject(), candidates_by_id()) == MitreAnswer(())


def test_a_technique_outside_the_shortlist_is_rejected():
    with pytest.raises(Rejected):
        parse_mitre(answer(("T1486", 0.9, "Files were encrypted.")), subject(), candidates_by_id())


def test_a_basis_with_a_url_is_rejected():
    data = answer(("T1190", 0.9, "See https://evil.example for the details."))
    with pytest.raises(Rejected, match="URL"):
        parse_mitre(data, subject(), candidates_by_id())


def test_a_weak_pick_is_not_checked_because_it_is_not_kept():
    data = answer(("T1190", 0.3, "See https://evil.example for the details."))
    assert parse_mitre(data, subject(), candidates_by_id()).picks == ()


# ─── One pass ─────────────────────────────────────────────────────────────────────────────────────

KEY = "fake-key-TESTONLY"
LADDER = Ladder.model_validate(
    {
        "tiers": {
            "tier0_free": ["vendor-a/one:free", "vendor-a/paid"],
            "tier1_cheap": ["vendor-b/cheap"],
            "tier2_strong": ["vendor-b/strong"],
            "code": ["vendor-c/code"],
            "audit": ["vendor-d/audit"],
        }
    }
)
VERIFIED = VerifiedLadder(ladder=LADDER, checked_at=T0)
GOOD = answer(("T1190", 0.9, "The VPN portal flaw was the way in."))


def governor(usage_monthly="0", budget="20") -> Governor:
    g = Governor(VERIFIED, Decimal(budget), now=lambda: T0)
    g.observe(
        KeyStatus(
            limit=Decimal(20),
            limit_remaining=Decimal(20) - Decimal(usage_monthly),
            limit_reset="monthly",
            usage_daily=Decimal(0),
            usage_monthly=Decimal(usage_monthly),
            free_requests_remaining=988,
        )
    )
    return g


class FakeLayer:
    def __init__(self, g):
        self.governor = g
        self.verified = VERIFIED
        self.settings = Settings(
            _env_file=None,
            database_url="sqlite://",
            openrouter_api_key=SecretStr(KEY),
            ai_monthly_budget_usd=Decimal(20),
        )
        self.readied = 0

    async def ready(self, http):
        self.readied += 1
        return self.governor


class FakeClient:
    def __init__(self, data=GOOD):
        self.data = data
        self.calls = []

    async def complete(self, tier, *, schema_name, attribution, models, **_):
        self.calls.append((tier, schema_name, attribution.stage, attribution.event_id))
        return Completion(
            data=self.data,
            model="vendor-b/strong",
            upstream="provider",
            tokens_in=100,
            tokens_out=50,
            cost_usd=Decimal("0.001"),
            generation_id="gen-1",
        )


@pytest.fixture
def pass_(monkeypatch):
    state = {"attack": ATTACK, "atlas": ATLAS, "subjects": [], "client": FakeClient()}
    log = []

    def load(engine, now, limit):
        log.append(("load", limit))
        return state["attack"], state["atlas"], state["subjects"]

    def write(engine, subj, outcome, now):
        log.append(("write", subj.event.event_id, type(outcome).__name__))
        if isinstance(outcome, Done):
            log.append(("picks", [s.technique.id for s, _ in outcome.result.picks]))

    monkeypatch.setattr(mitre_mod, "_load", load)
    monkeypatch.setattr(mitre_mod, "_write", write)
    monkeypatch.setattr(mitre_mod, "OpenRouterClient", lambda *a: state["client"])
    return state, log


async def test_a_pass_suggests_for_attack_stories_on_tier_2(pass_):
    state, log = pass_
    state["subjects"] = [subject("evt-2026-000010"), subject("evt-2026-000020")]
    summary = await suggest_techniques(engine="engine", layer=FakeLayer(governor()), batch=4)
    assert log[0] == ("load", 4 * mitre_mod.CANDIDATE_FACTOR)
    assert sorted(e for e in log if e[0] == "write") == [
        ("write", "evt-2026-000010", "Done"),
        ("write", "evt-2026-000020", "Done"),
    ]
    assert ("picks", ["T1190"]) in log
    tier, schema_name, stage, _ = state["client"].calls[0]
    assert (tier, schema_name, stage) == (Tier.STRONG, "enrich_mitre", "enrich.mitre")
    assert summary.done == 2 and summary.suggested == 2 and summary.changed_anything
    assert summary.catalogues == ["ATT&CK v19.2", "ATLAS 2026.09"]


async def test_events_that_are_not_about_an_attack_are_skipped(pass_):
    state, log = pass_
    state["subjects"] = [subject(categories=["policy"])]
    summary = await suggest_techniques(engine="engine", layer=FakeLayer(governor()))
    assert summary.events == 0 and not [e for e in log if e[0] == "write"]


async def test_a_batch_takes_the_first_events_only(pass_):
    state, log = pass_
    state["subjects"] = [subject(f"evt-2026-{i:06d}") for i in range(1, 6)]
    summary = await suggest_techniques(engine="engine", layer=FakeLayer(governor()), batch=2)
    assert summary.events == 2
    assert {e[1] for e in log if e[0] == "write"} == {"evt-2026-000001", "evt-2026-000002"}


async def test_with_no_attack_release_loaded_nothing_is_asked(pass_):
    state, _ = pass_
    state["attack"] = None
    state["subjects"] = [subject()]
    layer = FakeLayer(governor())
    summary = await suggest_techniques(engine="engine", layer=layer)
    assert layer.readied == 0 and summary == MitreSummary(catalogues=["ATLAS 2026.09"])


async def test_mode_off_asks_nothing(pass_):
    state, _ = pass_
    state["subjects"] = [subject()]
    summary = await suggest_techniques(engine="engine", layer=FakeLayer(governor(budget="0")))
    assert summary.mode == "off" and state["client"].calls == []


async def test_when_money_is_short_tier_2_work_waits_rather_than_going_to_tier_0(pass_):
    state, _ = pass_
    state["subjects"] = [subject(severity=Severity.CRITICAL)]
    g = governor(usage_monthly="17")  # MINIMAL: tier 0 only, and this is editorial work
    summary = await suggest_techniques(engine="engine", layer=FakeLayer(g))
    assert summary.mode == "minimal" and summary.blocked == 1 and summary.events == 0
    assert state["client"].calls == []


async def test_an_answer_that_fails_its_checks_twice_is_recorded_as_failed(pass_):
    state, log = pass_
    state["subjects"] = [subject()]
    state["client"] = FakeClient(answer(("T1190", 0.9, "See https://evil.example now.")))
    summary = await suggest_techniques(engine="engine", layer=FakeLayer(governor()))
    assert ("write", "evt-2026-000001", "Failed") in log
    assert summary.failed == 1 and not summary.changed_anything


async def test_a_zero_batch_does_nothing(pass_):
    _, log = pass_
    assert await suggest_techniques(engine="engine", layer=FakeLayer(governor()), batch=0) == (
        MitreSummary()
    )
    assert log == []
