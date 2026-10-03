"""The site's view logic, run for real: hud.js imported into node against synthetic events.

The static tests read the source; these call the exported functions that decide what a reader
sees: the default scope, what AUSTRALIA / GLOBAL / ALL and CYBER / AI each list, where an old
#sec-* bookmark lands, and how far a link to a story the scope hides widens it. No browser and
no published data are needed, only node; the module guards its DOM entry point.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HUD = ROOT / "site" / "assets" / "hud.js"
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

# Nine stories: three in Australia, six elsewhere, on every beat, with and without a published
# `beat` (a snapshot from before the AI desk has none, and the oldest have no domains either).
EVENTS = [
    _event(1, au=AU_DIRECT, domains=["cybersecurity"], beat="cyber", categories=["vulnerability"],
           severity="critical"),
    _event(2, au=AU_RELEVANT, domains=["ai"], beat="ai", categories=["ai-governance"],
           severity="unknown", ai_significance="major"),
    _event(3, domains=["cybersecurity", "ai"], beat="both", categories=["ai-security"],
           severity="high", ai_significance="notable"),
    _event(4, domains=["cybersecurity"], categories=["research"]),
    _event(5, domains=["business"], beat="other", severity="low"),
    _event(6, domains=["ai"], categories=["ai-research"], severity="unknown"),
    _event(7, ai_subdomain="AI_INDUSTRY", severity="unknown"),
    _event(8, au=AU_RELEVANT, ai_subdomain="AI_SECURITY"),
    _event(9),
]

SCRIPT = """
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const [hudPath, eventsPath] = process.argv.slice(2);
const m = await import(pathToFileURL(hudPath).href);
const events = JSON.parse(fs.readFileSync(eventsPath, 'utf8'));
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
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    work = tmp_path_factory.mktemp("site-behaviour")
    (work / "events.json").write_text(json.dumps(EVENTS), encoding="utf-8")
    (work / "probe.mjs").write_text(SCRIPT, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(work / "probe.mjs"), str(HUD), str(work / "events.json")],
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
    "sec-the-crew": "?view=crew&scope=au",
    "sec-system": "?view=crew&scope=au#sec-system",
}


def test_every_old_anchor_resolves_to_a_view_and_scope(site):
    assert site["legacyUrls"] == LEGACY


def test_the_url_round_trips_the_whole_state(site):
    assert site["roundTrip"] == {
        "view": "events", "scope": "global", "beat": ["ai"], "feed": ["vulnerabilities"],
        "tags": {"severity": ["critical", "high"]}, "event": "evt-2026-000001", "anchor": None,
    }


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
