"""The Paperclip package builder (ops/build-paperclip-package.py): the 8-agent crew and its
routines, read from the wiki (stage 4a and 4b) and written as the zip the Import page takes."""

import importlib.util
import re
import zipfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CREW = {
    "morpheus",
    "teletraan",
    "seraph",
    "deckard",
    "voight",
    "tachikoma",
    "ripperdoc",
    "wheeljack",
}
RETIRED = {"zion", "blaster", "wintermute", "tron", "rogue", "prowl", "librarian", "link"}
BEATS = [
    "cyberpulse-au-desk",
    "cyberpulse-global-desk",
    "cyberpulse-ai-desk",
    "cyberpulse-follow-up",
]
ROUTINES = {
    # name: (assignee, cron in Australia/Sydney, switched on now)
    "Daily editorial": ("morpheus", "30 7 * * *", True),
    "Desk digest": ("deckard", "0 6,14,22 * * *", True),
    "Daily QA sample": ("voight", "0 7 * * *", False),
    "Follow-up": ("deckard", "0 3-21/6 * * *", False),
    "Source discovery": ("tachikoma", "30 3 * * *", False),  # after the worker's 03:10 search
    "Model scan": ("ripperdoc", "0 4 * * *", False),
    "Model gauntlet": ("ripperdoc", "0 5 * * 0", False),
    "Monthly cost review": ("ripperdoc", "0 9 1 * *", True),
}


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location(
        "build_paperclip_package", REPO / "ops/build-paperclip-package.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def package(builder, tmp_path_factory):
    """Every file in the built zip, by its path under the company folder."""
    output = tmp_path_factory.mktemp("package") / "crew.zip"
    builder.build(output)
    with zipfile.ZipFile(output) as archive:
        return {
            name.removeprefix(f"{builder.ROOT}/"): archive.read(name).decode("utf-8")
            for name in archive.namelist()
        }


@pytest.fixture(scope="module")
def extension(package):
    return yaml.safe_load(package[".paperclip.yaml"])


def frontmatter(text: str) -> tuple[dict, str]:
    _, head, body = text.split("---\n", 2)
    return yaml.safe_load(head), body


def agent(package, slug: str) -> tuple[dict, str]:
    return frontmatter(package[f"agents/{slug}/AGENTS.md"])


def test_read_setup_finds_the_eight_routines(builder):
    description, routines = builder.read_setup()
    assert description
    found = {r["name"]: (r["assignee"], r["cron"], r["active"]) for r in routines}
    assert found == ROUTINES
    assert set(builder.ROUTINE_TEXT) == set(ROUTINES)


def test_the_package_has_the_eight_agents_and_nothing_else(package, extension):
    agents = {path.split("/")[1] for path in package if path.startswith("agents/")}
    assert agents == CREW
    assert set(extension["agents"]) == CREW
    assert set(extension["sidebar"]["agents"]) == CREW
    assert len(package) == 24  # 3 company files, 8 agents, 1 project, 4 skills, 8 routines


def test_no_retired_agent_is_in_the_package(package, extension):
    assert not RETIRED & set(extension["agents"])
    callsigns = re.compile(r"\b(" + "|".join(name.upper() for name in RETIRED) + r")\b")
    for path, text in package.items():
        assert not callsigns.search(text), f"{path} names a retired agent"


def test_the_routines_in_the_package(package, extension):
    routines = extension["routines"]
    assert len(routines) == 8
    for slug, routine in routines.items():
        name = routine["triggers"][0]["label"]
        assignee, cron, active = ROUTINES[name]
        task, _ = frontmatter(package[f"tasks/{slug}/TASK.md"])
        assert task["assignee"] == assignee and task["recurring"] is True
        assert routine["concurrencyPolicy"] == "skip_if_active"
        assert routine["status"] == ("active" if active else "paused")
        trigger = routine["triggers"][0]
        assert trigger["cronExpression"] == cron and trigger["timezone"] == "Australia/Sydney"
    assert routines["desk-digest"]["triggers"][0]["cronExpression"] == "0 6,14,22 * * *"


def test_the_org_chart(package):
    managers = {slug: agent(package, slug)[0].get("reportsTo") for slug in CREW}
    assert managers == {
        "morpheus": None,
        "teletraan": "morpheus",
        "seraph": "teletraan",
        "deckard": "morpheus",
        "voight": "morpheus",
        "tachikoma": "morpheus",
        "ripperdoc": "morpheus",
        "wheeljack": "teletraan",
    }
    titles = {slug: agent(package, slug)[0]["title"] for slug in CREW}
    assert titles == {
        "morpheus": "CEO",
        "teletraan": "Operation",
        "seraph": "Collector",
        "deckard": "Researcher",
        "voight": "Publisher",
        "tachikoma": "Finder",
        "ripperdoc": "Cheap",
        "wheeljack": "Coder",
    }


def test_deckard_has_one_run_slot_and_its_beats_as_skills(builder, package, extension):
    deckard = extension["agents"]["deckard"]
    assert deckard["budgetMonthlyCents"] == 200
    assert deckard["runtime"]["heartbeat"]["maxConcurrentRuns"] == 1
    front, _ = agent(package, "deckard")
    assert set(BEATS) <= set(front["skills"])
    for name in BEATS:
        skill, text = frontmatter(package[f"skills/{name}/SKILL.md"])
        assert skill["name"] == name and skill["description"] and text.strip()
    # The core prompt routes to the beats; the beats' own steps live only in their skills.
    _, cards = builder.read_crew()
    prompt = cards["deckard"]["prompt"]
    assert len(prompt.splitlines()) <= 40
    assert all(name in prompt for name in BEATS)
    assert "SOCI" not in prompt and "AI_CYBER_CONVERGENCE" not in prompt


def test_the_ai_budgets_fit_inside_the_company_budget(builder, extension):
    budgets = {slug: ext["budgetMonthlyCents"] for slug, ext in extension["agents"].items()}
    assert budgets.pop("seraph") == 0
    assert len(budgets) == 7 and all(cents > 0 for cents in budgets.values())
    assert sum(budgets.values()) == 950
    assert sum(budgets.values()) < builder.COMPANY_BUDGET_CENTS == 1200


def test_teletraan_checks_wheeljack_on_another_vendors_model(extension):
    ladder = yaml.safe_load((REPO / "config/models.yaml").read_text(encoding="utf-8"))["tiers"]
    models = {
        slug: ext["adapter"]["config"].get("model") for slug, ext in extension["agents"].items()
    }
    assert models["teletraan"] == "openrouter/" + ladder["audit"][0]
    assert models["wheeljack"] == "openrouter/" + ladder["code"][0]
    code_vendors = {slug.split("/")[0] for slug in ladder["code"]}
    assert models["teletraan"].split("/")[1] not in code_vendors


def test_seraph_is_the_one_http_agent(builder, extension):
    adapters = {slug: ext["adapter"]["type"] for slug, ext in extension["agents"].items()}
    assert {slug for slug, kind in adapters.items() if kind == "http"} == {"seraph"}
    config = extension["agents"]["seraph"]["adapter"]["config"]
    assert config["payloadTemplate"] == {"job": "pipeline"}
    assert config["url"] == builder.OPS_WAKE_URL.format(slug="seraph")


def test_the_builder_refuses_budgets_that_reach_the_company_budget(builder, monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "COMPANY_BUDGET_CENTS", 950)
    with pytest.raises(SystemExit, match="budgets add up to 950"):
        builder.build(tmp_path / "crew.zip")
    assert not (tmp_path / "crew.zip").exists()


def test_the_builder_refuses_one_vendor_for_code_and_its_check(builder, monkeypatch, tmp_path):
    house_rules, cards = builder.read_crew()
    cards["teletraan"]["model"] = cards["wheeljack"]["model"]
    monkeypatch.setattr(builder, "read_crew", lambda: (house_rules, cards))
    with pytest.raises(SystemExit, match="must differ"):
        builder.build(tmp_path / "crew.zip")
