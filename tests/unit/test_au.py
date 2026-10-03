"""The AU engine (worker/pipeline/au.py): which facts set a floor, what the reasons say, how the
model's reading combines with them, and that the config's words are the pipeline's own."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from worker.ai.tasks import AU_DESK_RELEVANCE, SECTORS
from worker.pipeline.assemble import AU_SOURCE_REASON
from worker.pipeline.au import AuConfig, AuFacts, AuSource, ModelReading, assess_au
from worker.pipeline.run import DEFAULT_SCORING_PATH
from worker.pipeline.score import ScoringConfig

CONFIG = ScoringConfig.load(DEFAULT_SCORING_PATH).au
SOURCES_PATH = Path(__file__).parents[2] / "config" / "sources.yaml"

ACSC = AuSource("acsc_alerts", "ACSC Alerts", "au")
ITNEWS = AuSource("itnews_security", "ITNews Security", "au")
ACSM = AuSource("acsm", "Australian Cybersecurity Magazine", "au")
WIRE = AuSource("bleepingcomputer", "BleepingComputer", "global")
NO_READING = ModelReading()


def assess(*texts, sources=(WIRE,), model=NO_READING):
    return assess_au(AuFacts(texts=texts, sources=tuple(sources), model=model), CONFIG)


# ─── Facts ────────────────────────────────────────────────────────────────────────────────────────


def test_nothing_australian_and_no_model_reading_is_unrated_not_zero():
    au = assess("Chrome zero-day patched", "Google fixed a flaw in V8.")
    assert au.relevance is None and au.reasons == [] and not au.directly_reported_in_au


def test_an_australian_outlet_alone_is_a_low_floor_with_the_pipelines_reason():
    au = assess("Chrome zero-day patched", sources=[ITNEWS])
    assert au.relevance == CONFIG.floors.outlet
    assert au.reasons == [AU_SOURCE_REASON] and au.directly_reported_in_au


def test_an_authority_puts_the_event_on_the_desk_and_names_itself():
    au = assess("Critical Fortinet flaw", sources=[ACSC, ITNEWS])
    assert au.relevance == CONFIG.floors.authority >= AU_DESK_RELEVANCE
    assert au.reasons == ["published by an Australian government authority (ACSC Alerts)"]
    assert au.directly_reported_in_au


def test_a_named_organisation_brings_its_sectors():
    au = assess("Medibank and Optus named in new leak claims")
    assert au.relevance == CONFIG.floors.organisation
    assert au.reasons == ["names Australian organisations (Medibank, Optus)"]
    assert au.sectors == ["health", "insurance", "telecommunications"]
    assert au.soci_asset_classes == [
        "health care and medical",
        "financial services and markets",
        "communications",
    ]


@pytest.mark.parametrize(
    "text, named",
    [
        ("eSafety orders AI companion apps to verify ages", "eSafety"),
        ("DISR consults on mandatory guardrails for high-risk AI", "DISR"),
        (
            "The Department of Industry, Science and Resources updates its AI standard",
            "Department of Industry, Science and Resources",
        ),
        ("National AI Centre publishes guidance for small businesses", "National AI Centre"),
        ("Data61 researchers show a new attack on code models", "Data61"),
        ("CSIRO releases an open dataset for training", "CSIRO"),
    ],
)
def test_the_australian_ai_bodies_put_a_story_on_the_desk(text, named):
    au = assess(text)
    assert au.reasons == [f"names an Australian organisation ({named})"]
    assert au.relevance == CONFIG.floors.organisation >= AU_DESK_RELEVANCE
    assert au.sectors == ["government"]


def test_naming_australia_or_a_place_or_a_au_address_is_a_place_floor():
    assert assess("Authorities in Australia arrest two").reasons == ["names Australia"]
    assert assess("Sydney council hit by ransomware").reasons == [
        "names an Australian place (Sydney)"
    ]
    au = assess("Phishing kit spoofs mygov.au pages", "See https://www.cyber.gov.au/ for more.")
    assert au.reasons == ["names an Australian (.au) web address (mygov.au, www.cyber.gov.au)"]
    assert au.relevance == CONFIG.floors.place


def test_matching_is_whole_words_and_case_sensitive():
    # "unable" holds "nab", "Austrian" is not "Australian", "colesterol" is not Coles, and an
    # Australian superannuation fund's name is not the word "Australian".
    au = assess("Austrian bank unable to process payments; colesterol app leaks data")
    assert au.relevance is None
    au = assess("AustralianSuper members targeted")
    assert au.reasons == ["names an Australian organisation (AustralianSuper)"]
    assert assess("The domain cobalt.beau.example and luau scripts").relevance is None


def test_feed_boilerplate_and_the_events_own_outlet_name_say_nothing():
    au = assess(
        "Kubernetes operators betray your security posture",
        "The post Kubernetes operators betray you appeared first on Australian Security Magazine.",
        "Australian Cybersecurity Magazine reports on a Kubernetes flaw.",
        sources=[ACSM],
    )
    assert au.reasons == [AU_SOURCE_REASON] and au.relevance == CONFIG.floors.outlet


def test_the_highest_floor_wins_and_every_fact_gives_its_reason_in_order():
    au = assess("Optus outage hits Melbourne", sources=[ACSC, ITNEWS])
    assert au.relevance == max(CONFIG.floors.organisation, CONFIG.floors.authority)
    assert au.reasons == [
        "published by an Australian government authority (ACSC Alerts)",
        "names an Australian organisation (Optus)",
        "names an Australian place (Melbourne)",
    ]


# ─── The model's reading ──────────────────────────────────────────────────────────────────────────


def test_the_model_may_lift_the_number_but_never_lower_it():
    lifted = assess(
        "Chrome zero-day",
        sources=[ITNEWS],
        model=ModelReading(0.8, ("Australian banks run the affected build",), ("finance",)),
    )
    assert lifted.relevance == 0.8
    assert lifted.reasons == [AU_SOURCE_REASON, "Australian banks run the affected build"]
    assert lifted.sectors == ["finance"]
    assert lifted.soci_asset_classes == ["financial services and markets"]
    lowered = assess("Optus outage", model=ModelReading(0.1, (), ()))
    assert lowered.relevance == CONFIG.floors.organisation


def test_a_model_reading_alone_still_counts_and_cannot_claim_the_pipelines_reason():
    au = assess(
        "Chrome zero-day",
        model=ModelReading(0.2, (AU_SOURCE_REASON.upper(), "Widely used in Australia"), ()),
    )
    assert au.relevance == 0.2 and au.reasons == ["Widely used in Australia"]


def test_reasons_are_unique_and_capped():
    model = ModelReading(0.9, tuple(f"Reason {i}" for i in range(10)) + ("Reason 1",), ())
    au = assess("Optus outage", sources=[ITNEWS], model=model)
    assert len(au.reasons) == CONFIG.max_reasons == len(set(au.reasons))
    assert au.reasons[:2] == ["names an Australian organisation (Optus)", AU_SOURCE_REASON]


def test_soci_sectors_wait_for_desk_level_relevance():
    au = assess("Hospital ransomware in Ohio", model=ModelReading(0.1, (), ("health",)))
    assert au.sectors == ["health"] and au.soci_asset_classes == []


# ─── The config ───────────────────────────────────────────────────────────────────────────────────


def test_the_configs_sectors_are_the_ones_triage_and_the_brief_use():
    assert {s for ss in CONFIG.organisations.values() for s in ss} <= set(SECTORS)
    assert set(CONFIG.soci_sectors) <= set(SECTORS)
    assert CONFIG.soci_min_relevance == AU_DESK_RELEVANCE


def test_every_authority_is_an_australian_source_in_the_registry():
    sources = {s["id"]: s for s in yaml.safe_load(SOURCES_PATH.read_text())["sources"]}
    for source_id in CONFIG.authorities:
        assert sources[source_id]["region"] == "au", source_id


def test_a_name_with_stray_spaces_is_refused():
    data = CONFIG.model_dump()
    data["places"] = [*data["places"], " Sydney"]
    with pytest.raises(ValidationError, match="usable name"):
        AuConfig.model_validate(data)
