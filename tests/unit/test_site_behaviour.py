"""The site's view logic, run for real: hud.js imported into node against synthetic events.

The static tests read the source; these call the exported functions that decide what a reader
sees: the default scope, what AUSTRALIA / GLOBAL / ALL and CYBER / AI each list, what KEY ONLY
keeps, where an old #sec-* bookmark lands, how far a link to a story the scope hides widens it,
when a header menu closes itself, and how the crew's org chart and schedules read. No browser and
no published data are needed, only node; the module guards its DOM entry point.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HUD = ROOT / "site" / "assets" / "hud.js"
ORG = ROOT / "tests" / "unit" / "fixtures" / "site_org.json"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _event(n, *, au=None, domains=None, beat=None, categories=(), severity="medium", **extra):
    event = {
        "event_id": f"evt-2026-{n:06d}",
        "title": f"Event {n}",
        "status": "new",
        "severity": severity,
        "categories": list(categories),
        "domains": list(domains) if domains is not None else [],
        "au": au or {"directly_reported_in_au": False, "relevance": 0.1},
        "sources": [],
        "cves": [],
        "entities": {},
    }
    if beat is not None:
        event["beat"] = beat
    event.update(extra)
    return event


AU_DIRECT = {"directly_reported_in_au": True, "relevance": 0.9}
AU_RELEVANT = {"directly_reported_in_au": False, "relevance": 0.5}


def _importance(tier, score, *reasons):
    return {"version": "1", "score": score, "tier": tier, "reasons": list(reasons)}


# Nine stories: three in Australia, six elsewhere, on every beat, with and without a published
# `beat` (a snapshot from before the AI desk has none, and the oldest have no domains either).
# Two are KEY (one each side of the scope), one NOTABLE, one ROUTINE, two carry a malformed score
# and the rest predate the score entirely.
EVENTS = [
    _event(1, au=AU_DIRECT, domains=["cybersecurity"], beat="cyber", categories=["vulnerability"],
           severity="critical",
           importance=_importance("key", 82, "Australian government target", "reported by ABC.")),
    _event(2, au=AU_RELEVANT, domains=["ai"], beat="ai", categories=["ai-governance"],
           severity="unknown", ai_significance="major", importance="key"),
    _event(3, domains=["cybersecurity", "ai"], beat="both", categories=["ai-security"],
           severity="high", ai_significance="notable", importance=_importance("key", 71.6)),
    _event(4, domains=["cybersecurity"], categories=["research"],
           importance=_importance("notable", 140, "  several sources  ", "", 3)),
    _event(5, domains=["business"], beat="other", severity="low",
           importance=_importance("routine", 12, "one source")),
    _event(6, domains=["ai"], categories=["ai-research"], severity="unknown",
           importance=_importance("urgent", 99, "not a tier")),
    _event(7, ai_subdomain="AI_INDUSTRY", severity="unknown"),
    _event(8, au=AU_RELEVANT, ai_subdomain="AI_SECURITY"),
    _event(9),
]

SCRIPT = """
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const [hudPath, eventsPath, orgPath] = process.argv.slice(2);
const m = await import(pathToFileURL(hudPath).href);
const events = JSON.parse(fs.readFileSync(eventsPath, 'utf8'));
const org = JSON.parse(fs.readFileSync(orgPath, 'utf8'));
const after = (open, event) => m.dropdownAfter(open, event);
const model = m.orgModel(org);
const ids = (list) => list.map((e) => e.event_id);
const fresh = m.freshState();
const count = (over) => m.filterEvents(events, { ...fresh, ...over }).length;
const out = {
  fresh,
  scope: Object.fromEntries(['au', 'global', 'all'].map((s) => [s, count({ scope: s })])),
  beat: {
    all_ai: count({ scope: 'all', beat: ['ai'] }),
    all_cyber: count({ scope: 'all', beat: ['cyber'] }),
    all_both: count({ scope: 'all', beat: ['ai', 'cyber'] }),
    all_none: count({ scope: 'all', beat: [] }),
    au_ai: count({ scope: 'au', beat: ['ai'] }),
    au_cyber: count({ scope: 'au', beat: ['cyber'] }),
  },
  beatOf: Object.fromEntries(events.map((e) => [e.event_id, m.beatOf(e)])),
  presets: Object.fromEntries(
    Object.entries(m.presetCounts(events, fresh)).map(([k, v]) => [k, [v.count, v.hidden]]),
  ),
  policyResearch: ids(m.filterEvents(events, m.PRESETS.find((p) => p.id === 'policy-research')
    .apply({ ...fresh, scope: 'all' }))),
  legacy: Object.fromEntries(
    Object.keys(m.LEGACY_ANCHORS).map((a) => [a, m.parseLocation('', `#${a}`)]),
  ),
  legacyUrls: Object.fromEntries(
    Object.keys(m.LEGACY_ANCHORS).map((a) => [a, m.toSearch(m.parseLocation('', `#${a}`))]),
  ),
  anchorWithView: m.parseLocation('?view=dashboard&scope=au', '#sec-global-cyber'),
  roundTrip: m.parseLocation(m.toSearch(m.freshState({
    view: 'events', scope: 'global', beat: ['ai'], feed: ['vulnerabilities'],
    tags: { severity: ['critical', 'high'] }, event: 'evt-2026-000001',
  }))),
  widen: m.sectionFor(events[3], m.freshState({ scope: 'au', tags: { severity: ['medium'] } })),
  widenDropsTag: m.sectionFor(events[3], m.freshState({
    scope: 'au', beat: ['ai'], tags: { severity: ['medium'], category: ['vulnerability'] },
  })),
  inScopeStays: m.sectionFor(events[0], m.freshState({ scope: 'au' })),
  movedAnchor: m.parseLocation('?view=crew&scope=global&beat=ai', '#sec-system'),
  movedSources: m.parseLocation('?view=dashboard&scope=au', '#source-health'),
  key: {
    importance: Object.fromEntries(events.map((e) => [e.event_id, m.importanceOf(e)])),
    isKey: ids(events.filter((e) => m.isKey(e))),
    scope: Object.fromEntries(['au', 'global', 'all'].map((s) => [s, count({ scope: s, key: true })])),
    withTag: count({ scope: 'all', key: true, tags: { severity: ['high'] } }),
    presets: Object.fromEntries(
      Object.entries(m.presetCounts(events, { ...fresh, key: true })).map(([k, v]) => [k, [v.count, v.hidden]]),
    ),
    facet: Object.fromEntries(m.buildFacets(events).get('importance')),
    byTag: ids(m.filterEvents(events, { ...fresh, scope: 'all', tags: { importance: ['notable', 'routine'] } })),
    url: m.toSearch(m.freshState({ view: 'events', key: true })),
    read: m.parseLocation('?view=events&scope=au&key=1').key,
    readJunk: m.parseLocation('?view=events&scope=au&key=yes').key,
    lifted: m.sectionFor(events[3], m.freshState({ scope: 'au', key: true })),
    kept: m.sectionFor(events[0], m.freshState({ scope: 'au', key: true })),
  },
  dropdown: {
    opens: after(null, { type: 'open', id: 'feed-menu' }),
    swaps: after('feed-menu', { type: 'open', id: 'filter-more' }),
    closesItself: after('feed-menu', { type: 'close', id: 'feed-menu' }),
    staleClose: after('filter-more', { type: 'close', id: 'feed-menu' }),
    clickInside: after('feed-menu', { type: 'click', inside: 'feed-menu' }),
    clickOtherMenu: after('feed-menu', { type: 'click', inside: 'filter-more' }),
    clickOutside: after('feed-menu', { type: 'click', inside: null }),
    clickWhenShut: after(null, { type: 'click', inside: null }),
    escape: after('filter-more', { type: 'key', key: 'Escape' }),
    escapeWhenShut: after(null, { type: 'key', key: 'Escape' }),
    otherKey: after('feed-menu', { type: 'key', key: 'ArrowDown' }),
    applyMoved: after('feed-menu', { type: 'apply', id: 'feed-menu', moved: true }),
    applyStayed: after('filter-more', { type: 'apply', id: 'filter-more', moved: false }),
    unknown: after('feed-menu', { type: 'scroll' }),
  },
  cron: Object.fromEntries(org.routines.map((r) => [r.cron, m.describeCron(r.cron, r.timezone)])),
  cronOther: {
    weekdays: m.describeCron('0 9 * * 1-5', 'UTC'),
    hourly: m.describeCron('0 * * * *', 'Australia/Sydney'),
    pastTheHour: m.describeCron('15 * * * *', null),
    months: m.describeCron('0 9 1 1,7 *', 'Australia/Sydney'),
    monthly: m.describeCron('0 9 1 * *', 'Australia/Sydney'),
    sundays: m.describeCron('0 5 * * 0', 'Australia/Sydney'),
    everyMinute: m.describeCron('* * * * *', 'UTC'),
    bothDays: m.describeCron('0 9 1 * 1', 'UTC'),
    named: m.describeCron('0 9 * * MON', 'UTC'),
    short: m.describeCron('0 9 * *', 'UTC'),
    junk: m.describeCron(null),
  },
  org: model && {
    company: model.company.name,
    top: model.top?.callsign,
    topManager: model.top?.manager,
    teams: model.teams.map((t) => [t.name, t.members.map((a) => a.callsign)]),
    managers: Object.fromEntries(model.agents.map((a) => [a.callsign, a.manager])),
    routines: Object.fromEntries(model.agents.map((a) => [a.callsign, a.routines])),
    personas: model.agents.filter((a) => a.persona).length,
    unlisted: model.unlisted,
    schedules: model.routines.map((r) => r.schedule),
  },
  orgEmpty: [m.orgModel(null), m.orgModel({}), m.orgModel({ agents: [{ title: 'no callsign' }] })],
  orgPartial: (() => {
    const partial = m.orgModel({ agents: [{ callsign: 'DECKARD', reports_to: 'MORPHEUS' }] });
    return { top: partial.top.callsign, teams: partial.teams, unlisted: partial.unlisted.length };
  })(),
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    work = tmp_path_factory.mktemp("site-behaviour")
    (work / "events.json").write_text(json.dumps(EVENTS), encoding="utf-8")
    (work / "probe.mjs").write_text(SCRIPT, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(work / "probe.mjs"), str(HUD), str(work / "events.json"), str(ORG)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_default_scope_is_australia(site):
    assert site["fresh"]["scope"] == "au"
    assert site["fresh"]["view"] == "dashboard"
    assert site["legacy"]["sec-overview"]["scope"] == "all"  # an old link keeps its meaning


def test_australia_and_global_partition_all(site):
    scope = site["scope"]
    assert scope == {"au": 3, "global": 6, "all": 9}
    assert scope["au"] + scope["global"] == scope["all"], "a story fell between the scopes"


def test_the_beat_changes_the_counts(site):
    beat = site["beat"]
    # A story on both desks answers to either beat; one on neither answers only to no beat.
    assert beat["all_ai"] == 5
    assert beat["all_cyber"] == 5
    assert beat["all_both"] == 8
    assert beat["all_none"] == 9
    assert beat["au_ai"] == 2 and beat["au_cyber"] == 2


def test_the_beat_falls_back_to_domains_when_it_is_not_published(site):
    assert site["beatOf"] == {
        "evt-2026-000001": "cyber",
        "evt-2026-000002": "ai",
        "evt-2026-000003": "both",
        "evt-2026-000004": "cyber",  # domains only
        "evt-2026-000005": "other",
        "evt-2026-000006": "ai",  # domains only
        "evt-2026-000007": "ai",  # no domains: AI_INDUSTRY has no security angle
        "evt-2026-000008": "both",  # no domains: any other AI subdomain is on both desks
        "evt-2026-000009": "cyber",  # no domains, no subdomain: a cyber source
    }


def test_the_sidebar_counts_follow_the_scope_and_offer_the_rest(site):
    presets = site["presets"]
    assert presets["australia-now"] == [3, 6]
    assert presets["global-cyber"] == [6, 3]
    # From the default AUSTRALIA scope: e2 and e8 are on the AI desk here, and three more at ALL.
    assert presets["ai-cyber"] == [2, 3]


def test_policy_and_research_takes_both_desks(site):
    assert sorted(site["policyResearch"]) == ["evt-2026-000002", "evt-2026-000004", "evt-2026-000006"]


def test_an_old_global_cyber_bookmark_opens_events_on_global(site):
    hit = site["legacy"]["sec-global-cyber"]
    assert (hit["view"], hit["scope"], hit["legacy"]) == ("events", "global", True)
    # A URL this version wrote says what it wants; its fragment is not an old anchor.
    assert site["anchorWithView"]["view"] == "dashboard"
    assert site["anchorWithView"]["scope"] == "au"
    assert "legacy" not in site["anchorWithView"]


LEGACY = {
    "sec-australia-now": "?view=events&scope=au",
    "sec-global-cyber": "?view=events&scope=global",
    "sec-ai-cyber": "?view=events&scope=all&beat=ai",
    "sec-overview": "?view=dashboard&scope=all",
    "sec-trends": "?view=dashboard&scope=all#sec-trends",
    "sec-active-exploitation": "?view=events&scope=all&feed=active-exploitation",
    "sec-developing-events": "?view=events&scope=all&feed=developing-events",
    "sec-emerging-threats": "?view=events&scope=all&feed=emerging-threats",
    "sec-threat-actors": "?view=events&scope=all&feed=threat-actors",
    "sec-vulnerabilities": "?view=events&scope=all&feed=vulnerabilities",
    "sec-research": "?view=events&scope=all&feed=research",
    "sec-policy-regulation": "?view=events&scope=all&feed=policy-regulation",
    "sec-world-map": "?view=dashboard&scope=all",
    "sec-the-crew": "?view=crew&scope=au",
    "sec-system": "?view=system&scope=au",
    "sec-sources": "?view=sources&scope=au",
    "sec-source-health": "?view=sources&scope=au",
    "source-health": "?view=sources&scope=au",
}


def test_every_old_anchor_resolves_to_a_view_and_scope(site):
    assert site["legacyUrls"] == LEGACY


def test_an_anchor_for_a_block_that_became_a_view_opens_that_view(site):
    # ?view=crew&scope=au#sec-system is what the crew link wrote while SYSTEM was a block of the
    # crew view. The view moves to the one that holds it now; what the reader chose stays.
    moved = site["movedAnchor"]
    assert (moved["view"], moved["scope"], moved["beat"], moved["legacy"]) == ("system", "global", ["ai"], True)
    assert moved["anchor"] is None
    assert (site["movedSources"]["view"], site["movedSources"]["scope"]) == ("sources", "au")


def test_the_url_round_trips_the_whole_state(site):
    assert site["roundTrip"] == {
        "view": "events", "scope": "global", "beat": ["ai"], "feed": ["vulnerabilities"],
        "key": False, "tags": {"severity": ["critical", "high"]}, "event": "evt-2026-000001",
        "anchor": None,
    }


def test_importance_is_read_strictly_and_never_invented(site):
    imp = site["key"]["importance"]
    assert imp["evt-2026-000001"] == {
        "tier": "key", "score": 82, "reasons": ["Australian government target", "reported by ABC"],
    }
    # A score is rounded and clamped to 0-100; blank and non-string reasons are dropped.
    assert imp["evt-2026-000003"] == {"tier": "key", "score": 72, "reasons": []}
    assert imp["evt-2026-000004"] == {"tier": "notable", "score": 100, "reasons": ["several sources"]}
    assert imp["evt-2026-000005"]["tier"] == "routine"
    # Not an object, an unknown tier, or no score at all: unrated, which is not ROUTINE.
    for unrated in ("evt-2026-000002", "evt-2026-000006", "evt-2026-000007", "evt-2026-000009"):
        assert imp[unrated] is None, unrated
    assert site["key"]["isKey"] == ["evt-2026-000001", "evt-2026-000003"]


def test_key_only_narrows_every_count_and_survives_the_url(site):
    key = site["key"]
    assert key["scope"] == {"au": 1, "global": 1, "all": 2}
    assert key["withTag"] == 1  # KEY ONLY and a tag combine with AND
    # The FEEDS counts are counts of what each feed would list, so KEY ONLY narrows them too.
    assert key["presets"]["australia-now"] == [1, 1]
    assert key["presets"]["ai-cyber"] == [0, 1]
    assert key["url"] == "?view=events&scope=au&key=1"
    assert key["read"] is True and key["readJunk"] is False


def test_the_importance_facet_counts_only_rated_stories(site):
    assert site["key"]["facet"] == {"key": 2, "notable": 1, "routine": 1}
    assert site["key"]["byTag"] == ["evt-2026-000004", "evt-2026-000005"]


def test_a_link_to_a_story_key_only_hides_lifts_key_only(site):
    lifted = site["key"]["lifted"]
    assert (lifted["key"], lifted["scope"], lifted["event"]) == (False, "all", "evt-2026-000004")
    assert site["key"]["kept"]["key"] is True


def test_a_header_menu_closes_itself(site):
    d = site["dropdown"]
    shut = {"open": None, "focus": None}
    assert d["opens"] == {"open": "feed-menu", "focus": None}
    # Only one at a time: opening TAG FILTERS shuts FEEDS.
    assert d["swaps"] == {"open": "filter-more", "focus": None}
    assert d["closesItself"] == shut
    assert d["staleClose"] == {"open": "filter-more", "focus": None}
    # A click inside the open menu leaves it; anywhere else, another menu included, shuts it.
    assert d["clickInside"] == {"open": "feed-menu", "focus": None}
    assert d["clickOtherMenu"] == shut and d["clickOutside"] == shut and d["clickWhenShut"] == shut
    # Escape shuts it and hands focus back to its summary, so a keyboard reader is not stranded.
    assert d["escape"] == {"open": None, "focus": "filter-more"}
    assert d["escapeWhenShut"] == shut
    assert d["otherKey"] == {"open": "feed-menu", "focus": None}
    # A choice shuts it. One that moved the view leaves focus to the view; one that did not (a tag
    # toggled where the reader already is) returns it to the menu it came from.
    assert d["applyMoved"] == shut
    assert d["applyStayed"] == {"open": None, "focus": "filter-more"}
    assert d["unknown"] == {"open": "feed-menu", "focus": None}


def test_every_crew_schedule_reads_in_plain_english(site):
    assert site["cron"] == {
        "30 7 * * *": "Daily at 07:30 Sydney time",
        "0 6,14,22 * * *": "Daily at 06:00, 14:00 and 22:00 Sydney time",
        "0 7 * * *": "Daily at 07:00 Sydney time",
        "0 3-21/6 * * *": "Daily at 03:00, 09:00, 15:00 and 21:00 Sydney time",
        "30 3 * * *": "Daily at 03:30 Sydney time",
    }


def test_a_schedule_it_cannot_say_correctly_is_left_as_the_cron(site):
    other = site["cronOther"]
    assert other["weekdays"] == "Weekdays at 09:00 UTC"
    assert other["hourly"] == "Every hour on the hour"
    assert other["pastTheHour"] == "Every hour at 15 past"
    assert other["months"] == "Monthly on the 1st in January and July at 09:00 Sydney time"
    # RIPPERDOC's routines had these shapes until it was retired on 2026-10-20.
    assert other["monthly"] == "Monthly on the 1st at 09:00 Sydney time"
    assert other["sundays"] == "Sundays at 05:00 Sydney time"
    # Every minute, both day fields (cron ORs them), names, the wrong field count, nothing.
    for shape in ("everyMinute", "bothDays", "named", "short", "junk"):
        assert other[shape] is None, shape


def test_the_org_chart_puts_the_ceo_on_top_and_reports_below(site):
    org = site["org"]
    assert org["company"] == "CyberPulse"
    assert (org["top"], org["topManager"]) == ("MORPHEUS", None)
    # Teams in the order the file names them; within one, a manager before those reporting in.
    assert org["teams"] == [
        ["Operations", ["TELETRAAN", "SERAPH", "WHEELJACK"]],
        ["Editorial", ["DECKARD", "VOIGHT"]],
        ["Sources", ["TACHIKOMA"]],
    ]
    assert org["managers"]["SERAPH"] == "TELETRAAN" and org["managers"]["DECKARD"] == "MORPHEUS"
    assert org["routines"] == {
        "MORPHEUS": 1, "TELETRAAN": 0, "SERAPH": 0, "DECKARD": 2, "VOIGHT": 1, "TACHIKOMA": 1,
        "WHEELJACK": 0,
    }
    # Every agent in the file has a persona on the page and every persona is in the file.
    assert org["personas"] == 7 and org["unlisted"] == []
    assert all(org["schedules"]), "a crew cron the page cannot put into words"


def test_an_unpublished_org_chart_is_nothing_not_an_empty_company(site):
    assert site["orgEmpty"] == [None, None, None]
    # A file naming one agent whose manager is absent still draws: that agent is the top, and the
    # page says which of its own crew the file does not list.
    partial = site["orgPartial"]
    assert partial["top"] == "DECKARD" and partial["teams"] == []
    assert partial["unlisted"] == 6


# renderOrg drawn into a small fake DOM that, like a browser's, writes a null it is given as
# the text "null".
DRAW_ORG = r"""
const [hudPath, orgPath] = process.argv.slice(2);
// Imported before there is a document, so the page's own entry point stays out of it.
const m = await import(hudPath);
class FakeNode {}
class FakeText extends FakeNode {
  constructor(t) { super(); this.data = String(t); }
  get textContent() { return this.data; }
}
class FakeEl extends FakeNode {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.childNodes = []; this.dataset = {}; }
  setAttribute() {}
  set className(v) {}
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) { this.childNodes = [new FakeText(v)]; }
  append(...nodes) { for (const n of nodes) this.childNodes.push(n instanceof FakeNode ? n : new FakeText(n)); }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
}
const hosts = {};
globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (t) => new FakeEl(t),
  createElementNS: (_, t) => new FakeEl(t),
  createTextNode: (t) => new FakeText(t),
  getElementById: (id) => (hosts[id] ||= new FakeEl('div')),
};
const { readFileSync } = await import('node:fs');
m.renderOrg(JSON.parse(readFileSync(orgPath, 'utf8')));
const text = (id) => (hosts[id] ? hosts[id].textContent : null);
process.stdout.write(JSON.stringify({
  chart: text('org-chart'), routines: text('org-routines'), facts: text('org-facts'),
}));
"""


@pytest.mark.parametrize("org", [ORG, ROOT / "site" / "assets" / "org.json"], ids=["fixture", "published"])
def test_the_org_chart_draws_no_stray_null(org, tmp_path):
    (tmp_path / "draw.mjs").write_text(DRAW_ORG, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(tmp_path / "draw.mjs"), str(HUD), str(org)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    drawn = json.loads(done.stdout)
    assert "MORPHEUS" in drawn["chart"] and "Daily editorial" in drawn["routines"]
    for part, text in drawn.items():
        assert "null" not in text and "undefined" not in text, part


def test_a_routine_the_worker_fires_says_so_not_custom_schedule(tmp_path):
    """Follow-up, Source discovery and Incident are webhook routines (2026-10-30): no cron, run
    only when the worker has work for them. The page used to call them "Custom schedule"."""
    org = json.loads(ORG.read_text(encoding="utf-8"))
    org["routines"] = [
        {"name": "Daily editorial", "assignee": "MORPHEUS", "cron": "30 7 * * *",
         "timezone": "Australia/Sydney", "trigger": "schedule", "summary": "s"},
        {"name": "Follow-up", "assignee": "DECKARD", "cron": None,
         "timezone": "Australia/Sydney", "trigger": "webhook", "summary": "w"},
    ]
    (tmp_path / "org.json").write_text(json.dumps(org), encoding="utf-8")
    (tmp_path / "draw.mjs").write_text(DRAW_ORG, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(tmp_path / "draw.mjs"), str(HUD), str(tmp_path / "org.json")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    routines = json.loads(done.stdout)["routines"]
    assert "Daily at 07:30 Sydney time" in routines
    assert "Only when there is work" in routines and "fired by the worker" in routines
    assert "Custom schedule" not in routines and "—" not in routines


def test_a_story_outside_the_scope_widens_it_and_keeps_the_tags(site):
    widen = site["widen"]
    assert widen["view"] == "events" and widen["scope"] == "all"
    assert widen["event"] == "evt-2026-000004"
    assert widen["tags"] == {"severity": ["medium"]}
    # Only what is in the way is lifted: the AI beat and the category tag hide it, the
    # severity tag does not.
    dropped = site["widenDropsTag"]
    assert dropped["scope"] == "all" and dropped["beat"] == []
    assert dropped["tags"] == {"severity": ["medium"]}
    # A story the scope already shows does not move the reader anywhere.
    assert site["inScopeStays"]["scope"] == "au"


# The CREW view drawn from crew.json, org.json and system-status.json: what each card says of an
# agent the worker cannot see, what it runs on, and the run table.
DRAW_CREW = r"""
const [hudPath, fixtures, orgPath] = process.argv.slice(2);
const m = await import(hudPath);
class FakeNode {}
class FakeText extends FakeNode {
  constructor(t) { super(); this.data = String(t); }
  get textContent() { return this.data; }
}
class FakeEl extends FakeNode {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.childNodes = []; this.dataset = {}; this.attrs = {}; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  set className(v) { this.attrs.class = v; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) { this.childNodes = [new FakeText(v)]; }
  append(...nodes) { for (const n of nodes) this.childNodes.push(n instanceof FakeNode ? n : new FakeText(n)); }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
}
const hosts = {};
globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (t) => new FakeEl(t),
  createElementNS: (_, t) => new FakeEl(t),
  createTextNode: (t) => new FakeText(t),
  getElementById: (id) => (hosts[id] ||= new FakeEl('div')),
};
const { readFileSync } = await import('node:fs');
const read = (p) => JSON.parse(readFileSync(p, 'utf8'));
const crew = read(`${fixtures}/next/crew.json`);
const status = read(`${fixtures}/next/system-status.json`);
const feed = read(`${fixtures}/next/live.json`);
const org = read(orgPath);
const NOW = Date.parse('2026-10-04T10:30:00Z');
const cards = () => hosts['crew-list'].childNodes.map((li) => li.textContent);
m.renderCrew(crew, org, status.ai, NOW);
const withOrg = { cards: cards(), summary: hosts['crew-summary'].textContent };
m.renderCrew(crew, null, null, NOW);
const withoutOrg = { cards: cards(), summary: hosts['crew-summary'].textContent };
m.renderCrewRun(status, feed);
const run = hosts['crew-run'].textContent;
// An older snapshot whose last_run is still going: never "finished null".
m.renderCrewRun({ last_run: { run_id: 'r', lane: 'normal', started_at: '2026-10-28T08:00:00Z', finished_at: null } }, feed);
const unfinished = hosts['crew-run'].textContent;
const mixroute = { agents: [{ callsign: 'DECKARD', adapter: 'model', model: 'mixroute/deepseek-v4-flash', max_daily_runs: 8 }] };
process.stdout.write(JSON.stringify({
  withOrg, withoutOrg, run, unfinished,
  seen: [
    m.seenState(null, NOW),
    m.seenState({ callsign: 'TELETRAAN', what: 'incident verdict', last_at: null, this_month: 0 }, NOW),
    m.seenState({ callsign: 'DECKARD', what: 'follow-up report', last_at: '2026-10-04T09:12:00Z', this_month: 4 }, NOW),
    m.seenState({ callsign: 'TACHIKOMA', what: 'source proposal', last_at: '2026-09-06T03:30:00Z', this_month: 0 }, NOW),
  ],
  runtime: [
    m.cardRuntime({ callsign: 'DECKARD', runtime: 'fallback' }, mixroute),
    m.cardRuntime({ callsign: 'SERAPH', runtime: 'fallback' }, { agents: [{ callsign: 'SERAPH', adapter: 'http' }] }),
    m.cardRuntime({ callsign: 'VOIGHT', runtime: 'fallback' }, mixroute),
  ],
}));
"""


@pytest.fixture(scope="module")
def crew_view(tmp_path_factory):
    work = tmp_path_factory.mktemp("crew")
    (work / "draw.mjs").write_text(DRAW_CREW, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(work / "draw.mjs"), str(HUD), str(ROOT / "tests" / "fixtures" / "site"),
         str(ORG)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_crew_card_says_only_what_of_the_agents_work_reached_the_worker(crew_view):
    none, never, recent, old = crew_view["seen"]
    assert none["label"] == "RUNS IN PAPERCLIP"
    assert "none of its work reaches the worker" in none["text"]
    assert never == {"status": "not_seen", "led": "idle", "label": "NOT SEEN YET",
                     "text": "Works in Paperclip. No incident verdict has reached the worker yet."}
    assert recent["led"] == "ok" and recent["label"] == "LAST SEEN 4 OCT"
    assert recent["text"].endswith(" · 4 this month.")
    # A month-old proposal is not "active": the card says when, and warns.
    assert old["led"] == "warn" and old["label"] == "LAST SEEN 6 SEPT"
    text = " ".join(crew_view["withOrg"]["cards"])
    assert "Last source proposal the worker received" in text
    assert "NOT SEEN YET" in text, "TELETRAAN: no verdict has reached the worker"
    for word in ("null", "undefined", "NaN"):
        assert word not in text


def test_a_crew_card_says_what_the_agent_runs_on_from_the_org_chart(crew_view):
    assert crew_view["runtime"] == ["LLM · deepseek-v4-flash · UP TO 8/DAY", "CODE · NO MODEL", "fallback"]
    # Without org.json, the roster's own line stands in, and no hard-coded tool is claimed.
    text = " ".join(crew_view["withoutOrg"]["cards"])
    assert "Tavily" not in text and "Strands" not in text


def test_the_crew_summary_tells_the_two_budgets_apart(crew_view):
    summary = crew_view["withOrg"]["summary"]
    assert "the pipeline's own AI is capped at US$12.00 a month" in summary
    assert "monthly budget in Paperclip" in summary
    assert "capped" not in crew_view["withoutOrg"]["summary"]


def test_the_run_table_credits_the_workers_figures_to_seraph(crew_view):
    run = crew_view["run"]
    assert "Last completed run: FAST lane, finished" in run
    assert "Still going: NORMAL lane since" in run
    # The fixture's run recorded one error, and the snapshot holds ten stories: SERAPH's figures.
    assert "1 error recorded; 10 events published in this snapshot" in run
    unfinished = crew_view["unfinished"]
    assert "null" not in unfinished and "No completed run has been published (still going: NORMAL lane since" in unfinished
