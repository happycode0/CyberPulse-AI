#!/usr/bin/env python3
"""Build the CyberPulse company package for Paperclip's Import page.

Reads the crew (house rules, prompts, models, budgets) from docs/wiki/stage-4b-the-crew.md and
the routines and mission from docs/wiki/stage-4a-paperclip-setup.md, so the wiki stays the one
source of truth. Writes a zip that Company Settings -> Import accepts (agentcompanies/v1 plus
the paperclip/v1 .paperclip.yaml extension, release 2026.1001.0).

A card's `### Skill `<name>`` sections (DECKARD's beats) become package skills,
`skills/<name>/SKILL.md`, attached to that agent by name, so its own prompt stays short.

The package carries no secret values and declares no secret inputs. Things the importer
cannot carry are done after the import: agent budget policies, the company budget, and each
agent's access to the OpenRouter connection (docs/wiki/stage-4a-paperclip-setup.md).

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
    ("ripperdoc", "cfo", "morpheus", "opencode_local", "microscope"),
    ("wheeljack", "engineer", "teletraan", "opencode_local", "wrench"),
]
# The 16-agent crew's callsigns, retired when the 8 took over their work. None may come back
# by accident: the owner terminates them in Paperclip (docs/wiki/stage-4a-paperclip-setup.md).
RETIRED = ("zion", "blaster", "wintermute", "tron", "rogue", "prowl", "librarian", "link")
# The company budget the owner sets after the import (stage-4a step 6). The agents' own budgets
# must add up to less, so one agent's overrun hits its own budget before the company's.
COMPANY_BUDGET_CENTS = 1200

# MORPHEUS keeps the skills and appearance its hire gave it; replacing it must not change them.
MORPHEUS_SKILLS = [
    "paperclipai/paperclip/paperclip",
    "paperclipai/paperclip/paperclip-board",
    "paperclipai/paperclip/paperclip-converting-plans-to-tasks",
    "paperclipai/paperclip/paperclip-create-agent",
    "paperclipai/paperclip/para-memory-files",
]
MORPHEUS_APPEARANCE = {"characterVersion": "cap-v1", "paletteId": "arctic-blue", "schemaVersion": 1}
AGENT_SKILLS = ["paperclipai/paperclip/paperclip"]

# Issue text for each routine (stage-4a step 7, second table).
ROUTINE_TEXT = {
    "Daily editorial": ("[EDITORIAL] Daily — {{date}}",
                        "Run the daily editorial: overnight digest, follow-ups, gaps, VOIGHT verdicts, daily report."),
    "Desk digest": ("[DIGEST] Desk — {{date}}",
                    "Review the candidate events in the current desk digest: Australia first, then global, then AI."),
    "Daily QA sample": ("[QA] Daily sample", "QA a sample of yesterday's published events."),
    "Follow-up": ("[FOLLOW-UP] Sweep", "Work the follow-up queue: report on each task due."),
    "Source discovery": ("[DISCOVERY] Nightly",
                         "Read the worker's discovery finds and propose sources, plus any open [GAP] topics."),
    "Model scan": ("[MODEL] Daily scan",
                   "Do the daily model scan: read /ops/models and report what changed."),
    "Model gauntlet": ("[MODEL] Weekly gauntlet",
                       "Do the weekly gauntlet: report the results and raise each new proposal."),
    "Monthly cost review": ("[COST] Monthly review",
                            "Reconcile last month from /ops/cost and /ops/jobs, and report to MORPHEUS."),
}


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
            model = re.search(r"`(openrouter/[^`]+)`", row(body, "Adapter · Model") or "")
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
    for name, assignee, cron, switch_on, summary in re.findall(
        r"^\| ([A-Za-z -]+) \| ([A-Z]+) \| `([^`]+)`[^|]* \| (now|Stage \d) \| ([^|]+?) \|$", step,
        flags=re.M,
    ):
        if name not in ROUTINE_TEXT:
            fail(f"routine {name!r} has no issue text")
        routines.append({"name": name, "assignee": assignee.lower(), "cron": cron,
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
    # TELETRAAN checks WHEELJACK's code: the two-person rule needs two vendors (config/models.yaml).
    vendors = {slug: cards[slug]["model"].split("/")[1] for slug in ("teletraan", "wheeljack")}
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
            }}
            ext["runtime"] = {
                "aiConnection": {"mode": "responsible_user", "method": "api_key", "provider": "openrouter"},
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
            ext["permissions"] = {"canCreateAgents": True, "canCreateSkills": True}
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
        routines_ext[slug] = {
            "status": "active" if routine["active"] else "paused",
            "priority": "medium",
            "concurrencyPolicy": "skip_if_active",
            "catchUpPolicy": "skip_missed",
            "triggers": [{
                "kind": "schedule",
                "label": routine["name"],
                "enabled": True,
                "cronExpression": routine["cron"],
                "timezone": TIMEZONE,
            }],
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
    lines += ["", "| Routine | Agent | Cron (Australia/Sydney) |", "|---|---|---|"]
    for routine in routines:
        lines.append(f"| {routine['name']} | {cards[routine['assignee']]['name']} | `{routine['cron']}` |")
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
