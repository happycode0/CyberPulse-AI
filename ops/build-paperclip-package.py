#!/usr/bin/env python3
"""Build the CyberPulse company package for Paperclip's Import page.

Reads the crew (house rules, prompts, models, budgets) from docs/wiki/stage-4b-the-crew.md and
the routines and mission from docs/wiki/stage-4a-paperclip-setup.md, so the wiki stays the one
source of truth. Writes a zip that Company Settings -> Import accepts (agentcompanies/v1 plus
the paperclip/v1 .paperclip.yaml extension, release 2026.1001.0).

A card's `### Skill `<name>`` sections (DECKARD's beats) become package skills,
`skills/<name>/SKILL.md`, attached to that agent by name, so its own prompt stays short.

The package carries no secret values. It declares one secret input, each AI agent's
MIXROUTE_API_KEY: the import asks for it and Paperclip stores it as a company secret, bound to
that agent alone, so the key never sits in the server's environment (CYB-36). The crew's models
run on MixRoute through a custom OpenCode provider, which the opencode_local adapter reads from
the agent's PAPERCLIP_OPENCODE_PROVIDERS (release 2026.1001.0); Paperclip has no MixRoute
connection of its own. Things the importer cannot carry are done after the import: agent budget
policies, the company budget, and the webhook triggers' secrets
(docs/wiki/stage-4a-paperclip-setup.md).

With `--org-json PATH` it writes the public org chart for the site's crew page instead
(site/assets/org.json): the same pages, with each card's Summary row and team, and no prompts,
payload templates or internal addresses. tests/unit/test_paperclip_package.py fails until the
committed file is rebuilt after a wiki change.

    python3 ops/build-paperclip-package.py [output.zip]
    python3 ops/build-paperclip-package.py --org-json site/assets/org.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CREW_PAGE = REPO / "docs/wiki/stage-4b-the-crew.md"
SETUP_PAGE = REPO / "docs/wiki/stage-4a-paperclip-setup.md"
ROOT = "cyberpulse"
PROJECT = "cyberpulse-ai"
TIMEZONE = "Australia/Sydney"
OPS_WAKE_URL = "http://worker:8700/ops/agents/{slug}/wake"

# Structure the wiki states in prose and tables. Titles, models, budgets, daily-run caps,
# prompts and http payloads are read from the crew page and checked against this.
CREW = [
    # slug, role, reports to, adapter, icon
    ("morpheus", "ceo", None, "opencode_local", "crown"),
    ("teletraan", "devops", "morpheus", "opencode_local", "radar"),
    ("seraph", "devops", "teletraan", "http", "fingerprint"),
    ("deckard", "researcher", "morpheus", "opencode_local", "search"),
    ("voight", "qa", "morpheus", "opencode_local", "eye"),
    ("tachikoma", "researcher", "morpheus", "opencode_local", "telescope"),
    ("wheeljack", "engineer", "teletraan", "opencode_local", "wrench"),
]
# Callsigns that are gone, and may not come back by accident: the 16-agent crew's, retired when
# the 8 took over their work, which the owner terminates in Paperclip
# (docs/wiki/stage-4a-paperclip-setup.md), and RIPPERDOC, retired on 2026-10-20 when the worker
# began sending its notices itself, which the owner pauses (docs/runbooks/owner-checklist.md).
# A retired agent's card keeps its number on the crew page but not the `N. NAME — Title`
# heading, so it is not read as a card; a full card for one is refused.
RETIRED = (
    "zion", "blaster", "wintermute", "tron", "rogue", "prowl", "librarian", "link", "ripperdoc",
)
# The company budget the owner sets after the import (stage-4a step 6): the crew's US$8 of the
# owner's US$20 a month, the worker having the other US$12. The agents' own budgets must add up
# to less, so one agent's overrun hits its own budget before the company's.
COMPANY_BUDGET_CENTS = 800

# MORPHEUS keeps its appearance. Since 2026-10-30 it no longer hires or keeps memory files, so
# those two skills are gone: each one put its text in every run's context.
MORPHEUS_SKILLS = [
    "paperclipai/paperclip/paperclip",
    "paperclipai/paperclip/paperclip-board",
    "paperclipai/paperclip/paperclip-converting-plans-to-tasks",
]
MORPHEUS_APPEARANCE = {"characterVersion": "cap-v1", "paletteId": "arctic-blue", "schemaVersion": 1}
AGENT_SKILLS = ["paperclipai/paperclip/paperclip"]

# The crew's models on MixRoute, and whose each one is. A gateway's model id names no vendor, so
# the two-person rule needs this map; a model not in it is refused until someone says whose it
# is. Ids are MixRoute's, exactly (mixroute.ai/docs/model-api/<vendor>/<id>.md).
MODEL_VENDORS = {
    "deepseek-v4-flash": "deepseek",
    "glm-4-flash": "zhipu",
    "glm-5.3-flash": "zhipu",
}
# OpenCode's small model, for its helper calls (session titles, summaries), never an agent's own
# work. MixRoute's free model (owner, 2026-10-30); it returns at most 4,095 tokens a request.
SMALL_MODEL = "mixroute/glm-4-flash"
MIXROUTE_BASE_URL = "https://api.mixroute.ai/v1"
# OpenCode's custom-provider shape (MixRoute's OpenCode guide). The adapter bakes the key into
# the run's opencode.json from the agent's own MIXROUTE_API_KEY, a Paperclip secret.
MIXROUTE_PROVIDER = {
    "mixroute": {
        "npm": "@ai-sdk/openai-compatible",
        "name": "MixRoute",
        "options": {"baseURL": MIXROUTE_BASE_URL, "apiKey": "{env:MIXROUTE_API_KEY}"},
        "models": {model: {"name": model} for model in sorted(MODEL_VENDORS)},
    },
}
MIXROUTE_KEY_INPUT = {
    "kind": "secret",
    "requirement": "optional",
    "description": "The crew's own MixRoute API key, with a monthly quota set in MixRoute's console. "
    "Paperclip stores it as a secret for this agent alone.",
}

# Issue text for each routine (stage-4a step 7, second table).
ROUTINE_TEXT = {
    "Daily editorial": ("[EDITORIAL] Daily — {{date}}",
                        "Run the daily editorial: overnight digest and its escalation candidates, gaps, VOIGHT verdicts, daily report."),
    "Follow-up": ("[FOLLOW-UP] Sweep", "Work the follow-up queue: report on each task due."),
    "Source discovery": ("[DISCOVERY] Sites waiting for a feed",
                         "Read the worker's finds that have no feed yet and propose their feeds, plus any open [GAP] topics."),
    "Incident": ("[INCIDENT] Watchdog",
                 "The watchdog opened an incident: diagnose it and hand the fix to its owner."),
}
# A fire while the routine's issue is still open: an incident joins it, the rest skip.
ROUTINE_CONCURRENCY = {"Incident": "coalesce_if_active"}


def fail(message: str) -> None:
    sys.exit(f"build-paperclip-package: {message}")


def text_blocks(section: str) -> list[str]:
    return re.findall(r"```text\n(.*?)\n```", section, flags=re.S)


def sections(page: str, level: str) -> dict[str, str]:
    """Map each heading's text to the body below it, up to the next heading of that level."""
    parts = re.split(rf"^{level} (.+)$", page, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def row(section: str, label: str) -> str | None:
    match = re.search(rf"^\| {re.escape(label)} \| (.+?) \|$", section, flags=re.M)
    return match.group(1) if match else None


def read_skills(section: str, owner: str) -> list[dict]:
    """A card's `### Skill `<name>`` sections: each a Description row and one text block."""
    skills = []
    for heading, body in sections(section, "###").items():
        match = re.fullmatch(r"Skill `([a-z0-9]+(?:-[a-z0-9]+)*)`", heading)
        if not match:
            fail(f"{owner}: unexpected sub-heading {heading!r} in its card")
        description = row(body, "Description")
        blocks = text_blocks(body)
        if not description or len(blocks) != 1:
            fail(f"{owner}: skill {match.group(1)} needs a Description row and one text block")
        skills.append({"name": match.group(1), "description": description, "text": blocks[0]})
    return skills


def read_teams(page: str) -> dict[str, str]:
    """Each card's team, from the `# <Name> team` heading above it."""
    teams = {}
    for heading, body in sections(page, "#").items():
        team = re.match(r"([A-Z][a-z]+) team\b", heading)
        if team:
            for name in re.findall(r"^## \d+\. ([A-Z]+) — ", body, flags=re.M):
                teams[name] = team.group(1)
    return teams


def read_crew() -> tuple[str, dict[str, dict]]:
    page = CREW_PAGE.read_text(encoding="utf-8")
    teams = read_teams(page)
    cards = sections(page, "##")
    house = next((body for heading, body in cards.items() if heading.startswith("House rules")), None)
    if house is None or len(text_blocks(house)) != 1:
        fail("house rules block not found")
    house_rules = text_blocks(house)[0]

    crew: dict[str, dict] = {}
    for heading, body in cards.items():
        match = re.match(r"\d+\. ([A-Z]+) — ", heading)
        if not match:
            continue
        name = match.group(1)
        title_cell = row(body, "Name / Title") or row(body, "Name / Title · Role")
        title = re.search(r"`[A-Z]+` / `([^`]+)`", title_cell or "")
        if not title:
            fail(f"{name}: no title row")
        summary = row(body, "Summary")
        if not summary or name not in teams:
            fail(f"{name}: needs a Summary row and a team heading above its card")
        card = {"name": name, "title": title.group(1), "team": teams[name], "summary": summary}
        # The card's own text stops at its first sub-heading; any after it are its skills.
        own, _, rest = body.partition("\n### ")
        card["skills"] = read_skills("### " + rest, name) if rest else []
        blocks = text_blocks(own)
        if blocks:
            card["prompt"] = blocks[0]
            model = re.search(r"`(mixroute/[^`]+)`", row(body, "Adapter · Model") or "")
            money = re.match(r"US\$(\d+\.\d\d) · (\d+)$", row(body, "Budget · Max daily runs") or "")
            if not model or not money:
                fail(f"{name}: model or budget row not found")
            card["model"] = model.group(1)
            card["budget_cents"] = round(float(money.group(1)) * 100)
            card["max_daily_runs"] = int(money.group(2))
        else:
            payload = re.match(r"`(\{.+?\})`", row(body, "Payload template") or "")
            if not payload:
                fail(f"{name}: no payload template")
            card["payload"] = json.loads(payload.group(1))
            card["does"] = row(body, "Does")
            card["never"] = row(body, "Never")
            card["wakes"] = row(body, "Wakes on")
        crew[name.lower()] = card
    return house_rules, crew


def read_setup() -> tuple[str, list[dict]]:
    page = SETUP_PAGE.read_text(encoding="utf-8")
    mission = next((b for b in text_blocks(page) if b.startswith("MISSION\n")), None)
    if mission is None:
        fail("mission block not found")
    first_paragraph = mission.split("\n\n")[0].split("\n", 1)[1]
    description = " ".join(line.strip() for line in first_paragraph.splitlines())

    step = next((body for heading, body in sections(page, "##").items() if "Create the routines" in heading), None)
    if step is None:
        fail("routines step not found")
    routines = []
    for name, assignee, trigger, switch_on, summary in re.findall(
        r"^\| ([A-Za-z -]+) \| ([A-Z]+) \| `([^`]+)`[^|]* \| (now|Stage \d) \| ([^|]+?) \|$", step,
        flags=re.M,
    ):
        if name not in ROUTINE_TEXT:
            fail(f"routine {name!r} has no issue text")
        # A trigger is a cron line in Australia/Sydney, or `webhook`: the worker fires it.
        routines.append({"name": name, "assignee": assignee.lower(), "trigger": trigger,
                         "cron": None if trigger == "webhook" else trigger,
                         "active": switch_on == "now", "starts": switch_on, "summary": summary})
    if len(routines) != len(ROUTINE_TEXT):
        fail(f"found {len(routines)} routines, expected {len(ROUTINE_TEXT)}")
    return description, routines


def scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def yaml_lines(value, indent: int = 0) -> list[str]:
    """Same shape as Paperclip's renderYamlBlock: two-space indent, JSON-quoted scalars."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, (dict, list)):
                lines.append(f"{pad}-")
                lines.extend(yaml_lines(entry, indent + 1))
            else:
                lines.append(f"{pad}- {scalar(entry)}")
        return lines
    for key, entry in value.items():
        if isinstance(entry, (dict, list)):
            lines.append(f"{pad}{key}:")
            lines.extend(yaml_lines(entry, indent + 1))
        else:
            lines.append(f"{pad}{key}: {scalar(entry)}")
    return lines


def markdown(frontmatter: dict, body: str) -> str:
    return "---\n" + "\n".join(yaml_lines(frontmatter)) + "\n---\n\n" + (body.strip() + "\n" if body else "")


def http_instructions(card: dict) -> str:
    return "\n".join([
        f"{card['name']} is an http agent. It spends no tokens: Paperclip calls the CyberPulse",
        "worker, which does the job in Python. This text is for the board and the other agents;",
        "no model reads it.",
        "",
        f"Wakes on: {card['wakes']}",
        f"Does: {card['does']}",
        f"Never: {card['never']}",
    ]).replace("**", "")


def vendor(model: str) -> str:
    """Whose model `mixroute/<id>` is, from MODEL_VENDORS."""
    owner = MODEL_VENDORS.get(model.removeprefix("mixroute/"))
    if owner is None:
        fail(f"no vendor for {model}: add it to MODEL_VENDORS")
    return owner


def read_wiki() -> tuple[str, dict[str, dict], str, list[dict]]:
    """Both pages, checked against CREW and each other."""
    house_rules, cards = read_crew()
    description, routines = read_setup()
    if sorted(cards) != sorted(slug for slug, *_ in CREW):
        fail(f"crew page agents {sorted(cards)} do not match CREW")
    if set(cards) & set(RETIRED):
        fail(f"retired agents on the crew page: {sorted(set(cards) & set(RETIRED))}")
    strangers = {routine["assignee"] for routine in routines} - set(cards)
    if strangers:
        fail(f"routines assigned to agents not on the crew page: {sorted(strangers)}")
    budgets = sum(card.get("budget_cents", 0) for card in cards.values())
    if budgets >= COMPANY_BUDGET_CENTS:
        fail(f"the agents' budgets add up to {budgets} cents, not under the company's {COMPANY_BUDGET_CENTS}")
    # TELETRAAN checks WHEELJACK's code: the two-person rule needs two vendors.
    vendors = {slug: vendor(cards[slug]["model"]) for slug in ("teletraan", "wheeljack")}
    for card in cards.values():
        if "model" in card:
            vendor(card["model"])
    if vendors["teletraan"] == vendors["wheeljack"]:
        fail(f"TELETRAAN and WHEELJACK both run a {vendors['wheeljack']} model; they must differ")
    return house_rules, cards, description, routines


def build(output: Path) -> None:
    house_rules, cards, description, routines = read_wiki()
    files: dict[str, str] = {}
    agents_ext: dict[str, dict] = {}
    for slug, role, manager, adapter, icon in CREW:
        card = cards[slug]
        frontmatter = {"name": card["name"], "title": card["title"]}
        if manager:
            frontmatter["reportsTo"] = manager
        ext: dict = {"role": role, "icon": icon}
        if adapter == "opencode_local":
            if "prompt" not in card:
                fail(f"{slug}: AI agent without a prompt")
            frontmatter["skills"] = (MORPHEUS_SKILLS if slug == "morpheus" else AGENT_SKILLS) + [
                skill["name"] for skill in card["skills"]
            ]
            for skill in card["skills"]:
                path = f"skills/{skill['name']}/SKILL.md"
                if path in files:
                    fail(f"skill {skill['name']} is defined twice")
                files[path] = markdown({"name": skill["name"], "description": skill["description"]},
                                       skill["text"])
            body = house_rules + "\n\n" + card["prompt"]
            ext["adapter"] = {"type": adapter, "config": {
                "model": card["model"],
                "dangerouslySkipPermissions": True,
                "graceSec": 20,
                "timeoutSec": 0,
                "env": {
                    "PAPERCLIP_OPENCODE_PROVIDERS": {
                        "type": "plain", "value": json.dumps(MIXROUTE_PROVIDER, sort_keys=True),
                    },
                    # OpenCode's helper calls (session titles) on the agent's own model: its
                    # default is a model the gateway may not serve, which aborts the run.
                    "PAPERCLIP_OPENCODE_SMALL_MODEL": {"type": "plain", "value": SMALL_MODEL},
                },
            }}
            ext["inputs"] = {"env": {"MIXROUTE_API_KEY": MIXROUTE_KEY_INPUT}}
            # No aiConnection: Paperclip's are Claude, OpenAI, OpenRouter and Grok, and the
            # OpenRouter one matches only openrouter/ models.
            ext["runtime"] = {
                "heartbeat": {
                    "enabled": False,
                    "intervalSec": 0,
                    "wakeOnDemand": True,
                    "maxDailyRuns": card["max_daily_runs"],
                    "maxConcurrentRuns": 1,
                    "cooldownSec": 10,
                },
            }
            ext["budgetMonthlyCents"] = card["budget_cents"]
        else:
            if "payload" not in card:
                fail(f"{slug}: http agent without a payload template")
            if card["skills"]:
                fail(f"{slug}: an http agent runs no model, so it takes no skills")
            body = http_instructions(card)
            ext["capabilities"] = card["does"].replace("**", "")
            ext["adapter"] = {"type": adapter, "config": {
                "url": OPS_WAKE_URL.format(slug=slug),
                "method": "POST",
                "timeoutMs": 120000,
                "payloadTemplate": card["payload"],
            }}
            ext["runtime"] = {"heartbeat": {
                "enabled": False, "intervalSec": 0, "wakeOnDemand": True, "maxConcurrentRuns": 1,
            }}
            ext["budgetMonthlyCents"] = 0
        if slug == "morpheus":
            ext["permissions"] = {"canCreateAgents": False, "canCreateSkills": False}
            ext["permissionGrants"] = [{"permissionKey": "tasks:assign"}]
            ext["appearance"] = MORPHEUS_APPEARANCE
        else:
            ext["permissions"] = {"canCreateAgents": False, "canCreateSkills": False}
        agents_ext[slug] = ext
        files[f"agents/{slug}/AGENTS.md"] = markdown(frontmatter, body)

    routines_ext: dict[str, dict] = {}
    for routine in routines:
        slug = re.sub(r"[^a-z0-9]+", "-", routine["name"].lower()).strip("-")
        title, body = ROUTINE_TEXT[routine["name"]]
        files[f"tasks/{slug}/TASK.md"] = markdown(
            {"name": title, "assignee": routine["assignee"], "project": PROJECT, "recurring": True}, body
        )
        if routine["cron"] is None:
            # Paperclip makes the trigger's secret; the owner rotates it once to read it and
            # gives it, with the trigger's URL, to the worker (stage 4a step 7).
            trigger = {"kind": "webhook", "label": routine["name"], "enabled": True,
                       "signingMode": "bearer", "replayWindowSec": 300}
        else:
            trigger = {"kind": "schedule", "label": routine["name"], "enabled": True,
                       "cronExpression": routine["cron"], "timezone": TIMEZONE}
        routines_ext[slug] = {
            "status": "active" if routine["active"] else "paused",
            "priority": "medium",
            "concurrencyPolicy": ROUTINE_CONCURRENCY.get(routine["name"], "skip_if_active"),
            "catchUpPolicy": "skip_missed",
            "triggers": [trigger],
        }

    files["COMPANY.md"] = markdown(
        {"name": "CyberPulse", "description": description, "schema": "agentcompanies/v1", "slug": "cyberpulse"}, ""
    )
    files[f"projects/{PROJECT}/PROJECT.md"] = markdown({"name": "CyberPulse-AI"}, "")
    files[".paperclip.yaml"] = "\n".join(yaml_lines({
        "schema": "paperclip/v1",
        "agents": agents_ext,
        "company": {"requireBoardApprovalForNewAgents": True, "feedbackDataSharingEnabled": False},
        "projects": {PROJECT: {"status": "planned"}},
        "routines": routines_ext,
        "schemaVersion": 7,
        "sidebar": {"agents": [slug for slug, *_ in CREW], "projects": [PROJECT]},
    })) + "\n"
    files["README.md"] = readme(cards, routines)

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.writestr(f"{ROOT}/{path}", files[path])
    print(f"wrote {output} ({len(CREW)} agents, {len(routines)} routines, {len(files)} files)")


def readme(cards: dict[str, dict], routines: list[dict]) -> str:
    lines = [
        "# CyberPulse",
        "",
        "Built by `ops/build-paperclip-package.py` from the CyberPulse-AI wiki (stage 4a and 4b).",
        "Contains no secret values. Import it into the existing company with **Start imported",
        "agents and routines paused** ticked.",
        "",
        "| Agent | Title | Reports to | Adapter | Model | Budget / month |",
        "|---|---|---|---|---|---|",
    ]
    for slug, _, manager, adapter, _ in CREW:
        card = cards[slug]
        money = f"US${card.get('budget_cents', 0) / 100:.2f}"
        reports = cards[manager]["name"] if manager else "—"
        lines.append(f"| {card['name']} | {card['title']} | {reports} | `{adapter}` | {card.get('model', '—')} | {money} |")
    lines += ["", "| Routine | Agent | Trigger (cron in Australia/Sydney) |", "|---|---|---|"]
    for routine in routines:
        lines.append(f"| {routine['name']} | {cards[routine['assignee']]['name']} | `{routine['trigger']}` |")
    return "\n".join(lines) + "\n"


# Anything that looks like an address or an agent's working instructions; none belongs on the
# public org chart.
NOT_PUBLIC = re.compile(r"https?://|worker:|\$CYBERPULSE|/ops/|\{\{|AUTHORIZATION|YOU OWN", re.I)


def org_chart() -> dict:
    """The public org chart, for the site's crew page (site/assets/org.json): who each agent is,
    what it costs and does, and the routines. Never a prompt, a payload template, an internal
    address or a secret."""
    _, cards, description, routines = read_wiki()
    agents = []
    for slug, role, manager, adapter, _ in CREW:
        card = cards[slug]
        agents.append({
            "callsign": card["name"],
            "title": card["title"],
            "role": role,
            "reports_to": cards[manager]["name"] if manager else None,
            "team": card["team"],
            "adapter": "model" if adapter == "opencode_local" else "http",
            "model": card.get("model"),
            "budget_usd": card.get("budget_cents", 0) / 100,
            "max_daily_runs": card.get("max_daily_runs"),
            "summary": card["summary"],
        })
    org = {
        "company": {
            "name": "CyberPulse",
            "mission": description,
            "budget_usd": COMPANY_BUDGET_CENTS / 100,
        },
        "agents": agents,
        "routines": [
            {
                "name": routine["name"],
                "assignee": cards[routine["assignee"]]["name"],
                "trigger": "webhook" if routine["cron"] is None else "schedule",
                "cron": routine["cron"],
                "timezone": TIMEZONE,
                "summary": routine["summary"],
                "starts": routine["starts"],
            }
            for routine in routines
        ],
    }
    leak = NOT_PUBLIC.search(json.dumps(org))
    if leak:
        fail(f"the org chart would publish {leak.group(0)!r}; reword the wiki")
    return org


def org_json() -> str:
    return json.dumps(org_chart(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_org_json(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(org_json(), encoding="utf-8")
    print(f"wrote {output} ({len(CREW)} agents)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the Paperclip package from the wiki.")
    parser.add_argument("output", nargs="?", type=Path, help="the zip (build/cyberpulse-crew.zip)")
    parser.add_argument("--org-json", type=Path, metavar="PATH",
                        help="write the public org chart here instead (site/assets/org.json)")
    args = parser.parse_args()
    if args.org_json:
        write_org_json(args.org_json)
    if args.output or not args.org_json:
        build(args.output or REPO / "build/cyberpulse-crew.zip")
