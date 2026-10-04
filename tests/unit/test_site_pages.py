"""The SYSTEM and SOURCES pages, run for real: pages.js imported into node.

The pure helpers (cron to text, attention, grouping, sorting, tiers, deep links, the header
label and LED) are called directly. The two renderers are run against a small fake document
that records every element, attribute and listener, so the tests can read what a visitor would
see without a browser. Both shapes of the published files are covered: the old one the site
serves today (tests/fixtures/site/old, trimmed from the data branch) and the new one with the
schedule, source reputation and attention, discovery candidates and event importance
(tests/fixtures/site/new).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGES = ROOT / "site" / "assets" / "pages.js"
CSS = ROOT / "site" / "assets" / "pages.css"
FIXTURES = ROOT / "tests" / "fixtures" / "site"
NODE = shutil.which("node")

NOW = "2026-10-04T10:30:00Z"

CRONS = [
    ("0 * * * *", "UTC", "every hour on the hour"),
    ("0 */4 * * *", "UTC", "every 4 hours"),
    ("25 */6 * * *", "UTC", "every 6 hours at :25"),
    ("5,35 * * * *", "UTC", "twice an hour at :05 and :35"),
    ("3,18,48 * * * *", "UTC", "three times an hour at :03, :18 and :48"),
    ("10 7,8,9 * * *", "Australia/Sydney", "daily 07:10, 08:10 and 09:10 Sydney"),
    ("10 3 * * *", "Australia/Sydney", "daily 03:10 Sydney"),
    ("50 3-23/4 * * *", "UTC", "every 4 hours at :50, from 03:50 UTC"),
    ("40 3 * * sun", "Australia/Sydney", "Sundays 03:40 Sydney"),
    ("2-57/5 * * * *", None, "every 5 minutes from :02"),
    ("*/15 * * * *", None, "every 15 minutes"),
    ("0 9 * * 1-5", "UTC", "weekdays 09:00 UTC"),
    ("0 0 1 * *", "UTC", "monthly on the 1st at 00:00 UTC"),
    ("30 6 * * 1,4", "Europe/London", "Mondays and Thursdays 06:30 London"),
    ("@hourly", None, "every hour on the hour"),
    ("0 9-17 * * *", "UTC", "custom schedule"),
    ("0 0 * 1 *", "UTC", "custom schedule"),
    ("61 * * * *", "UTC", "custom schedule"),
    ("* * *", "UTC", "custom schedule"),
    ("", "UTC", "custom schedule"),
    (None, None, "custom schedule"),
]

SCRIPT = r"""
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const [pagesPath, fixtures, cronsJson, NOW] = process.argv.slice(2);
const m = await import(pathToFileURL(pagesPath).href);
const load = (shape, name) => JSON.parse(fs.readFileSync(path.join(fixtures, shape, name), 'utf8'));
const shapes = {};
for (const shape of ['old', 'new']) {
  shapes[shape] = {
    status: load(shape, 'system-status.json'), health: load(shape, 'source-health.json'),
    live: load(shape, 'live.json'), crew: load(shape, 'crew.json'),
  };
}
const out = { exports: Object.keys(m).sort() };

// Without a document the renderers still answer, and never touch the host.
const touched = [];
const trap = new Proxy({}, { get(_, key) { touched.push(String(key)); return () => {}; } });
out.noDom = {
  system: m.renderSystemPage(trap, { ...shapes.new, now: NOW }),
  sources: m.renderSourcesPage(trap, { ...shapes.new, now: NOW }),
  touched,
};

// A fake document: enough of the DOM for pages.js, and loud about anything it must not use.
class FakeText {
  constructor(t) { this.nodeType = 3; this.data = String(t); }
  get textContent() { return this.data; }
}
class FakeEl {
  constructor(tag, ns = 'html') {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this.namespaceURI = ns;
    this.attrs = {}; this.childNodes = []; this.listeners = {};
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.hasOwn(this.attrs, k) ? this.attrs[k] : null; }
  hasAttribute(k) { return Object.hasOwn(this.attrs, k); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) { this.childNodes = [new FakeText(v)]; }
  append(...nodes) {
    for (const n of nodes) this.childNodes.push(n !== null && typeof n === 'object' ? n : new FakeText(n));
  }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { for (const fn of this.listeners.click || []) fn({ type: 'click', target: this }); }
  get style() { throw new Error('pages.js touched .style'); }
  set innerHTML(v) { throw new Error('pages.js set innerHTML'); }
  get innerHTML() { throw new Error('pages.js read innerHTML'); }
  set className(v) { this.attrs.class = String(v); }
}
globalThis.document = {
  createElement: (t) => new FakeEl(t),
  createElementNS: (ns, t) => new FakeEl(t, ns),
  createTextNode: (t) => new FakeText(t),
};

const walk = (node, fn) => { fn(node); for (const c of node.childNodes || []) walk(c, fn); };
const all = (root, pred) => { const r = []; walk(root, (n) => { if (n.nodeType === 1 && pred(n)) r.push(n); }); return r; };
const hasClass = (n, c) => (n.attrs.class || '').split(/\s+/).includes(c);
const byClass = (root, c) => all(root, (n) => hasClass(n, c));
const text = (n) => (n ? n.textContent : null);

function snapshot(host) {
  const attrs = new Set();
  const badAttrs = [];
  const ids = [];
  const labelledby = [];
  walk(host, (n) => {
    if (n.nodeType !== 1) return;
    for (const [k, v] of Object.entries(n.attrs)) {
      attrs.add(k);
      if (/\b(null|undefined|NaN)\b|\[object/.test(v)) badAttrs.push(`${n.tagName}[${k}]=${v}`);
      if (k === 'id') ids.push(v);
      if (k === 'aria-labelledby') labelledby.push(v);
    }
  });
  return {
    text: host.textContent,
    attrNames: [...attrs].sort(),
    badAttrs,
    ids,
    danglingLabels: labelledby.filter((id) => !ids.includes(id)),
    hrefs: all(host, (n) => n.tagName === 'A').map((a) => a.getAttribute('href')),
    h3: all(host, (n) => n.tagName === 'H3').map(text),
    svgCount: all(host, (n) => n.tagName === 'SVG').length,
    htmlSvg: all(host, (n) => ['SVG', 'RECT'].includes(n.tagName) && n.namespaceURI === 'html').length,
  };
}

// SYSTEM
out.system = {};
for (const shape of ['old', 'new']) {
  const host = new FakeEl('div');
  const data = { ...shapes[shape], now: NOW };
  const res = m.renderSystemPage(host, data);
  const first = snapshot(host);
  const res2 = m.renderSystemPage(host, data);
  const again = snapshot(host);
  out.system[shape] = {
    res, ...first,
    idempotent: JSON.stringify(first) === JSON.stringify(again) && JSON.stringify(res) === JSON.stringify(res2),
    stages: byClass(host, 'pg-stage').map((li) => [li.getAttribute('data-kind'), text(byClass(li, 'pg-stage__name')[0]), text(byClass(li, 'pg-kind')[0])]),
    scheduleRows: byClass(host, 'pg-schedule').flatMap((t) => all(t, (n) => n.tagName === 'TR').slice(1).map((tr) => tr.childNodes.map(text))),
    replay: byClass(host, 'pg-step').map((li) => li.childNodes.map(text)),
    versions: all(host, (n) => n.tagName === 'DT').map(text),
    budget: text(byClass(host, 'pg-budget')[0]),
  };
}

// SOURCES
const sections = (host) => byClass(host, 'pg-group').map((sec) => sec.getAttribute('aria-labelledby'));
const cards = (host) => Object.fromEntries(byClass(host, 'pg-group').map((sec) => [
  sec.getAttribute('aria-labelledby'), byClass(sec, 'pg-card__name').map(text),
]));
const regions = (host) => byClass(host, 'pg-card').map((c) => c.getAttribute('data-region')).filter(Boolean);
const button = (host, value) => all(host, (n) => n.tagName === 'BUTTON' && n.getAttribute('data-value') === value)[0];
const pressed = (host) => Object.fromEntries(all(host, (n) => n.tagName === 'BUTTON').map((b) => [b.getAttribute('data-value'), b.getAttribute('aria-pressed')]));
function sourcesView(host) {
  return {
    sections: sections(host),
    cards: cards(host),
    regions: regions(host),
    showing: text(byClass(host, 'pg-showing')[0]),
    pressed: pressed(host),
    notes: byClass(host, 'pg-group').flatMap((sec) => byClass(sec, 'hint').map(text)),
  };
}
out.sources = {};
for (const shape of ['old', 'new']) {
  const host = new FakeEl('div');
  const data = { ...shapes[shape], now: NOW };
  const res = m.renderSourcesPage(host, data);
  const first = snapshot(host);
  m.renderSourcesPage(host, data);
  const again = snapshot(host);
  const view = sourcesView(host);
  const r = {
    res, ...first, ...view,
    idempotent: JSON.stringify(first) === JSON.stringify(again),
    strip: byClass(host, 'pg-tile').map((t) => [text(byClass(t, 'pg-tile__label')[0]), text(byClass(t, 'pg-tile__value')[0])]),
    reasons: byClass(host, 'pg-card__reason').map(text),
    reps: byClass(host, 'pg-rep').map(text),
    meters: byClass(host, 'pg-meter').map((s) => s.getAttribute('aria-label')),
    meterFills: byClass(host, 'pg-meter__fill').map((s) => s.getAttribute('width')),
    histories: byClass(host, 'pg-history__cells').map((s) => [s.getAttribute('role'), s.getAttribute('aria-label'), s.childNodes.length]),
    cellTitles: byClass(host, 'pg-cell').slice(0, 3).map((c) => text(c)),
    tiers: text(byClass(host, 'pg-tiers__legend')[0]),
    tierBar: byClass(host, 'pg-tierbar').map((s) => s.getAttribute('aria-label')),
    top: byClass(host, 'pg-top__link').map((a) => [text(a), a.getAttribute('href')]),
    candidates: byClass(host, 'pg-card--candidate').map(text),
  };
  // The controls: each click redraws the lists and moves aria-pressed.
  button(host, 'au').click();
  r.au = sourcesView(host);
  button(host, 'name').click();
  r.auByName = sourcesView(host);
  button(host, 'global').click();
  r.global = sourcesView(host);
  button(host, 'status').click();
  r.globalByStatus = sourcesView(host);
  // A fresh render of the same host keeps the reader's choices.
  m.renderSourcesPage(host, data);
  r.rerendered = sourcesView(host);
  button(host, 'all').click();
  button(host, 'reputation').click();
  r.back = sourcesView(host);
  out.sources[shape] = r;
}

// Null and junk inputs, with a document.
const junk = {
  undef: undefined,
  nul: null,
  empty: {},
  allNull: { status: null, health: null, crew: null, live: null },
  junk: {
    status: { last_run: {}, schedule: 'x' }, health: { sources: [null, 1, 'x', {}], pipeline: 'no' },
    crew: { pipeline_ai: 'no' }, live: { events: 'nope', counts: null },
  },
  junk2: {
    status: { last_run: { sources_failed: 2, sources_ok: 0, finished_at: 'bad' }, schedule: [{ cron: 5 }, null, { cron: '* * *', kind: 'weird' }] },
    health: { sources: [{ latest: { status: 7 }, history: [null, { status: 'ok', checked_at: 'never' }], reputation: { score: 'x' }, attention: { kind: 'weird' } }], pipeline: [null, {}] },
    crew: { pipeline_ai: { calls: 'many', cost_usd: null } },
    live: { events: [null, { importance: { score: 'high' } }, { importance: { tier: 'key' }, event_id: 'evt-1' }] },
  },
};
out.junk = {};
for (const [name, data] of Object.entries(junk)) {
  const sys = new FakeEl('div');
  const src = new FakeEl('div');
  const a = m.renderSystemPage(sys, data);
  const b = m.renderSourcesPage(src, data);
  const sa = snapshot(sys);
  const sb = snapshot(src);
  out.junk[name] = { system: a, sources: b, systemText: sa.text, sourcesText: sb.text, badAttrs: [...sa.badAttrs, ...sb.badAttrs] };
}
out.nullHost = [m.renderSystemPage(null, { ...shapes.new, now: NOW }), m.renderSourcesPage(undefined, { ...shapes.new, now: NOW })];

// Pure helpers.
const crons = JSON.parse(cronsJson);
out.cron = crons.map(([c, tz]) => m.cronToText(c, tz));
out.tz = ['UTC', null, 'Australia/Sydney', 'Europe/London', 'America/New_York'].map((t) => m.tzLabel(t));
const byId = (list) => Object.fromEntries(list.map((s) => [s.source_id, s]));
out.attention = {};
out.classify = {};
out.lastGood = {};
for (const shape of ['old', 'new']) {
  out.attention[shape] = Object.fromEntries(shapes[shape].health.sources.map((s) => [s.source_id, m.attentionOf(s)]));
  out.classify[shape] = m.classifySources(shapes[shape].health).counts;
  out.lastGood[shape] = Object.fromEntries(shapes[shape].health.sources.map((s) => [s.source_id, m.lastGoodCheck(s)]));
}
out.classifyBad = [m.classifySources(null), m.classifySources({}), m.classifySources({ sources: 'x' })];
out.attentionBad = [m.attentionOf(null), m.attentionOf('x'), m.attentionOf({ enabled: true, lifecycle_state: 'retired' })];
const newSources = shapes.new.health.sources;
const ids = (list) => list.map((s) => s.source_id);
out.sort = {
  reputation: ids(m.sortSources(newSources, 'reputation')),
  name: m.sortSources(newSources, 'name').map((s) => m.nameOf(s)),
  status: m.sortSources(newSources, 'status').map((s) => m.attentionOf(s).kind),
  input: ids(newSources),
  junk: m.sortSources(null, 'name'),
};
out.filter = {
  au: ids(m.filterSources(newSources, 'au')),
  global: ids(m.filterSources(newSources, 'global')),
  all: m.filterSources(newSources, 'all').length,
  bogus: m.filterSources(newSources, 'mars').length,
  regions: Object.fromEntries(newSources.map((s) => [s.source_id, m.regionOf(s)])),
};
out.reputation = Object.fromEntries(newSources.map((s) => [s.source_id, m.reputationOf(s)]));
out.reputationOld = m.reputationOf(shapes.old.health.sources[0]);
out.tiers = { old: m.tierCounts(shapes.old.live.events), new: m.tierCounts(shapes.new.live.events), none: m.tierCounts(null) };
out.tierOf = [
  m.tierOf({ importance: { score: 60 } }), m.tierOf({ importance: { score: 59.9 } }),
  m.tierOf({ importance: { score: 40 } }), m.tierOf({ importance: { score: 39 } }),
  m.tierOf({ importance: { tier: 'routine', score: 99 } }), m.tierOf({}), m.tierOf(null),
];
out.topKey = m.topKey(shapes.new.live.events).map((e) => e.event_id);
out.topKeyOld = m.topKey(shapes.old.live.events).length;
out.href = {
  au: m.eventHref({ event_id: 'evt-2026-000001', au: { directly_reported_in_au: true, relevance: 0.2 } }),
  auByRelevance: m.eventHref({ event_id: 'evt-2026-000002', au: { directly_reported_in_au: false, relevance: 0.5 } }),
  global: m.eventHref({ event_id: 'evt-2026-000003', au: { relevance: null } }),
  odd: m.eventHref({ event_id: 'a&b=c d' }),
  none: m.eventHref({}),
  nul: m.eventHref(null),
};
const run = (over) => ({ last_run: { finished_at: '2026-10-04T10:00:01Z', sources_ok: 10, sources_failed: 0, sources_stale: 1, ...over } });
out.systemSummary = {
  none: m.systemSummary(),
  noRun: m.systemSummary({ status: { generated_at: NOW } }),
  liveOnly: m.systemSummary({ live: { events: [] } }),
  ok: m.systemSummary({ status: run({}), now: NOW }),
  failed: m.systemSummary({ status: run({ sources_failed: 2 }), now: NOW }),
  allFailed: m.systemSummary({ status: run({ sources_ok: 0, sources_failed: 5 }), now: NOW }),
  old: m.systemSummary({ status: run({ finished_at: '2026-10-04T05:00:00Z' }), now: NOW }),
  yesterday: m.systemSummary({ status: run({ finished_at: '2026-10-03T10:00:00Z' }), now: NOW }),
  noTime: m.systemSummary({ status: run({ finished_at: null }), now: NOW }),
};
const src = (id, status, extra = {}) => ({ source_id: id, name: id, enabled: true, lifecycle_state: 'active', latest: { status, checked_at: NOW }, history: [], ...extra });
out.sourcesSummary = {
  none: m.sourcesSummary(),
  empty: m.sourcesSummary({ health: { sources: [] } }),
  healthy: m.sourcesSummary({ health: { sources: [src('a', 'ok'), src('b', 'ok'), src('c', null, { enabled: false })] } }),
  watch: m.sourcesSummary({ health: { sources: [src('a', 'stale')] } }),
  fixOne: m.sourcesSummary({ health: { sources: [src('a', 'error'), src('b', 'stale')] } }),
  fixTwo: m.sourcesSummary({ health: { sources: [src('a', 'timeout'), src('b', 'ok', { attention: { kind: 'fix', reason: 'Feed moved.' } })] } }),
  attentionWins: m.sourcesSummary({ health: { sources: [src('a', 'stale', { attention: { kind: 'coming', reason: 'Paused.' } })] } }),
};
out.fmt = {
  days: [m.fmtDays(0.01), m.fmtDays(0.5), m.fmtDays(1), m.fmtDays(2.41667), m.fmtDays(11.2), m.fmtDays(null), m.fmtDays(-1)],
  ago: [m.fmtAgo(10 * 1000), m.fmtAgo(5 * 60 * 1000), m.fmtAgo(3 * 3600 * 1000), m.fmtAgo(3 * 86400 * 1000), m.fmtAgo(NaN)],
  usd: [m.fmtUsd(0.1177), m.fmtUsd(0), m.fmtUsd(0.004), m.fmtUsd(null), m.fmtUsd('3')],
  clock: [m.sydneyClock(Date.parse('2026-10-04T10:00:00Z'), NOW), m.sydneyClock(Date.parse('2026-10-03T10:00:00Z'), NOW)],
};
out.constants = {
  stages: m.STORY_STAGES.map((s) => [s.name, s.kind, Boolean(s.side)]),
  standing: m.STANDING_POINTS,
  importance: m.IMPORTANCE_POINTS,
  reputation: m.REPUTATION_WEIGHTS.map(([n, w]) => [n, w]),
  keyAt: m.KEY_AT,
  notableAt: m.NOTABLE_AT,
  fallback: m.FALLBACK_SCHEDULE.map((r) => [r.job, r.cron, r.timezone, r.kind]),
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    if NODE is None:
        pytest.skip("node is not installed")
    work = tmp_path_factory.mktemp("site-pages")
    (work / "probe.mjs").write_text(SCRIPT, encoding="utf-8")
    crons = json.dumps([[c, tz] for c, tz, _ in CRONS])
    done = subprocess.run(
        [NODE, str(work / "probe.mjs"), str(PAGES), str(FIXTURES), crons, NOW],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _source_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _no_comments_js(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", js)


# ---------------------------------------------------------------------------------------------
# Static checks on the two new assets


def test_pages_js_builds_the_dom_without_html_injection_or_inline_styles():
    js = _source_text(PAGES)
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned
    assert not re.search(r"\.style\b", js), "pages.js must not set inline styles (CSP style-src 'self')"
    assert not re.search(r"setAttribute\(\s*['\"]style['\"]", js)
    assert not re.search(r"['\"]style['\"]\s*:", js)


def test_pages_js_fetches_nothing_and_names_no_external_origin():
    js = _source_text(PAGES)
    for banned in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "import(", "localStorage"):
        assert banned not in js, banned
    assert not re.search(r"(?m)^\s*import\s", js), "pages.js imports nothing (hud.js imports it)"
    stripped = js.replace("http://www.w3.org/2000/svg", "")
    assert not re.search(r"https?://", stripped)


def test_pages_js_exports_the_contract():
    js = _source_text(PAGES)
    assert re.search(r"export function renderSystemPage\(host, data = \{\}\)", js)
    assert re.search(r"export function renderSourcesPage\(host, data = \{\}\)", js)
    assert "const { status = null, health = null, crew = null, live = null" in js
    assert "const { health = null, live = null" in js
    assert "typeof document !== 'undefined'" in js


def test_pages_js_copy_never_says_live():
    # The site never calls itself "live"; `live` is only the snapshot's variable name.
    code = _no_comments_js(_source_text(PAGES))
    strings = re.findall(r"'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`", code)
    for single, template in strings:
        literal = re.sub(r"\$\{[^}]*\}", "", single or template)
        assert not re.search(r"\blive\b", literal, re.IGNORECASE), literal


def test_pages_css_is_self_contained_and_still():
    css = _source_text(CSS)
    assert not re.search(r"https?://", css)
    assert "@import" not in css and "url(" not in css
    assert "@keyframes" not in css and not re.search(r"(^|[\s;{])animation(-name)?\s*:", css)
    assert not re.search(r"(?<![-\w])color:\s*var\(--sev-critical\)", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    assert '.led[data-state="fail"]' in css
    classes = set(re.findall(r"\.([a-z][\w-]*)", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)))
    reused = {"led", "seg-btn"}
    assert all(c.startswith("pg-") or c in reused for c in classes), sorted(c for c in classes if not c.startswith("pg-"))


def test_pages_css_has_narrow_screen_rules():
    css = _source_text(CSS)
    assert re.search(r"@media \(max-width: 7\d\dpx\)", css)
    assert re.search(r"@media \(max-width: 5\d\dpx\)", css)
    assert "minmax(min(100%," in css


def test_fixtures_cover_both_shapes():
    for shape in ("old", "new"):
        for name in ("system-status.json", "source-health.json", "live.json", "crew.json"):
            assert (FIXTURES / shape / name).is_file(), f"{shape}/{name}"
    old_h = json.loads((FIXTURES / "old" / "source-health.json").read_text())
    new_h = json.loads((FIXTURES / "new" / "source-health.json").read_text())
    assert not any(k in s for s in old_h["sources"] for k in ("reputation", "attention", "standing", "description"))
    assert "pipeline" not in old_h and new_h["pipeline"]
    assert all("reputation" in s and "attention" in s for s in new_h["sources"])
    old_l = json.loads((FIXTURES / "old" / "live.json").read_text())
    new_l = json.loads((FIXTURES / "new" / "live.json").read_text())
    assert not any("importance" in e for e in old_l["events"])
    assert any("importance" in e for e in new_l["events"]) and any("importance" not in e for e in new_l["events"])
    assert "schedule" not in json.loads((FIXTURES / "old" / "system-status.json").read_text())
    assert json.loads((FIXTURES / "new" / "system-status.json").read_text())["schedule"]


# ---------------------------------------------------------------------------------------------
# Pure helpers


def test_exports(pages):
    for name in ("renderSystemPage", "renderSourcesPage", "cronToText", "attentionOf", "classifySources",
                 "tierCounts", "topKey", "sortSources", "filterSources", "eventHref", "systemSummary",
                 "sourcesSummary", "reputationOf", "STORY_STAGES", "FALLBACK_SCHEDULE"):
        assert name in pages["exports"], name


@pytest.mark.parametrize("i", range(len(CRONS)))
def test_cron_to_text(pages, i):
    cron, tz, want = CRONS[i]
    assert pages["cron"][i] == want, (cron, tz)


def test_timezone_labels(pages):
    assert pages["tz"] == ["UTC", "UTC", "Sydney", "London", "New York"]


def test_attention_in_the_old_shape_comes_from_lifecycle_and_status(pages):
    a = pages["attention"]["old"]
    assert a["abc_ai"]["kind"] is None
    assert a["acsc_advice"]["kind"] == "coming"
    assert a["sophos_labs"] == {"kind": "fix", "reason": "Last check TIMEOUT: ReadTimeout after 30s."}
    assert a["cisa_kev"] == {"kind": "watch", "reason": "No new item for 2.4 days."}
    assert a["arxiv_cs_cr"] == {"kind": "watch", "reason": "No check recorded yet."}
    assert a["fortinet_blog"]["kind"] == "watch"
    for ok in ("bleepingcomputer", "itnews_security", "cisa_advisories"):
        assert a[ok]["kind"] is None, ok


def test_attention_in_the_new_shape_trusts_the_published_object(pages):
    a = pages["attention"]["new"]
    assert a["sophos_labs"] == {"kind": "fix", "reason": "Timed out on the last 3 checks."}
    assert a["cisa_kev"] == {"kind": "watch", "reason": "Quieter than usual: no new entry for 2 days."}
    assert a["acsc_advice"] == {"kind": "coming", "reason": "Being tested before it is switched on."}
    # attention: null adds nothing, so a failing check still needs fixing.
    assert a["example_feed"] == {"kind": "fix", "reason": "Last check ERROR: HTTP 503 from upstream."}
    assert a["abc_ai"]["kind"] is None


def test_attention_handles_junk_and_retired(pages):
    assert pages["attentionBad"][0]["kind"] is None
    assert pages["attentionBad"][1]["kind"] is None
    assert pages["attentionBad"][2]["kind"] == "off"


def test_classify_counts(pages):
    assert pages["classify"]["old"] == {"total": 9, "healthy": 4, "fix": 1, "watch": 3, "coming": 1, "retired": 0}
    # Two discovery candidates join COMING without counting as sources.
    assert pages["classify"]["new"] == {"total": 10, "healthy": 4, "fix": 2, "watch": 3, "coming": 3, "retired": 0}
    assert pages["classifyBad"] == [None, None, None]


def test_last_good_check(pages):
    good = pages["lastGood"]["new"]
    assert good["example_feed"] == "2026-10-04T07:00:00.000Z"
    assert good["arxiv_cs_cr"] is None and good["acsc_advice"] is None
    assert good["abc_ai"].startswith("2026-10-04T08:00:00")


def test_sorting(pages):
    s = pages["sort"]
    assert s["reputation"][:5] == ["cisa_kev", "bleepingcomputer", "abc_ai", "fortinet_blog", "sophos_labs"]
    assert s["name"] == sorted(s["name"], key=str.lower)
    rank = {"fix": 0, "watch": 1, None: 2, "coming": 3, "off": 4}
    assert [rank[k] for k in s["status"]] == sorted(rank[k] for k in s["status"])
    assert s["input"][0] == "abc_ai", "sorting copies; it never reorders the published list"
    assert s["junk"] == []


def test_filtering(pages):
    f = pages["filter"]
    assert set(f["au"]) == {k for k, v in f["regions"].items() if v == "au"}
    assert set(f["global"]) == {k for k, v in f["regions"].items() if v == "global"}
    assert len(f["au"]) + len(f["global"]) == f["all"] == f["bogus"] == 10
    assert f["regions"]["cisa_kev"] == "global", "a US source is GLOBAL"


def test_reputation(pages):
    r = pages["reputation"]
    assert r["abc_ai"] == {"score": 79, "standing": "established", "uptime": 100, "corroboration": 62,
                           "events": 41, "basis": "established standing, 3 checks", "version": "1"}
    assert r["sophos_labs"]["corroboration"] is None
    assert r["arxiv_cs_cr"]["score"] is None and r["arxiv_cs_cr"]["standing"] == "specialist"
    assert r["example_feed"]["score"] is None and r["example_feed"]["standing"] is None
    assert pages["reputationOld"]["score"] is None and pages["reputationOld"]["standing"] is None


def test_tiers(pages):
    assert pages["tiers"]["old"] == {"key": 0, "notable": 0, "routine": 0, "unrated": 10, "total": 10}
    # The event with a score and no tier is placed by the score (45 is NOTABLE).
    assert pages["tiers"]["new"] == {"key": 4, "notable": 3, "routine": 2, "unrated": 1, "total": 10}
    assert pages["tiers"]["none"]["total"] == 0
    assert pages["tierOf"] == ["key", "notable", "notable", "routine", "routine", None, None]


def test_top_key_and_deep_links(pages):
    assert pages["topKey"] == ["evt-2026-001770", "evt-2026-002001", "evt-2026-004970"]
    assert pages["topKeyOld"] == 0
    h = pages["href"]
    assert h["au"] == "?view=events&scope=au&event=evt-2026-000001"
    assert h["auByRelevance"] == "?view=events&scope=au&event=evt-2026-000002"
    assert h["global"] == "?view=events&scope=all&event=evt-2026-000003"
    assert h["odd"] == "?view=events&scope=all&event=a%26b%3Dc+d"
    assert h["none"] is None and h["nul"] is None


def test_system_label_and_led(pages):
    s = pages["systemSummary"]
    assert s["none"] == {"label": "AWAITING DATA", "led": "idle"}
    assert s["noRun"] == {"label": "NO RUN YET", "led": "idle"}
    assert s["liveOnly"]["led"] == "idle"
    # 10:00 UTC on 4 October 2026 is 21:00 in Sydney (daylight saving began that morning).
    assert s["ok"] == {"label": "LAST RUN 21:00 SYDNEY", "led": "ok"}
    assert s["failed"] == {"label": "LAST RUN 21:00 SYDNEY", "led": "warn"}
    assert s["allFailed"]["led"] == "fail"
    assert s["old"] == {"label": "LAST RUN 16:00 SYDNEY", "led": "warn"}
    assert s["yesterday"] == {"label": "LAST RUN 3 OCT 20:00 SYDNEY", "led": "warn"}
    assert s["noTime"] == {"label": "LAST RUN TIME UNKNOWN", "led": "ok"}


def test_sources_label_and_led(pages):
    s = pages["sourcesSummary"]
    assert s["none"] == {"label": "AWAITING DATA", "led": "idle"}
    assert s["empty"] == {"label": "NO SOURCES LISTED", "led": "idle"}
    assert s["healthy"] == {"label": "3 SOURCES · ALL HEALTHY", "led": "ok"}
    assert s["watch"] == {"label": "1 SOURCE · 1 TO WATCH", "led": "warn"}
    assert s["fixOne"] == {"label": "2 SOURCES · 1 NEEDS FIXING", "led": "fail"}
    assert s["fixTwo"] == {"label": "2 SOURCES · 2 NEED FIXING", "led": "fail"}
    assert s["attentionWins"] == {"label": "1 SOURCE · ALL HEALTHY", "led": "ok"}


def test_formatting(pages):
    f = pages["fmt"]
    assert f["days"] == ["under an hour", "12 h", "1 day", "2.4 days", "11 days", None, None]
    assert f["ago"] == ["just now", "5 min ago", "3 h ago", "3 days ago", "just now"]
    assert f["usd"] == ["US$0.12", "US$0.00", "under US$0.01", None, None]
    assert f["clock"] == ["21:00", "3 OCT 20:00"]


def test_published_weights_match_the_brief(pages):
    c = pages["constants"]
    assert c["standing"] == {"authoritative": 90, "established": 75, "specialist": 60, "community": 40}
    assert c["reputation"] == [["Standing", 60], ["Uptime", 15], ["Corroboration", 25]]
    assert sum(w for _, w in c["reputation"]) == 100
    assert dict(c["importance"]) == {
        "Source standing": 25, "Australia": 25, "Public sector or critical infrastructure": 15,
        "Harm": 25, "Cyber and AI together": 5, "Corroboration": 10,
    }
    assert (c["keyAt"], c["notableAt"]) == (60, 40)
    assert c["fallback"] == [
        ["fast", "0 * * * *", "UTC", "code"],
        ["normal", "0 */4 * * *", "UTC", "code"],
        ["deep", "10 3 * * *", "Australia/Sydney", "mixed"],
    ]
    stages = c["stages"]
    assert 9 <= len(stages) <= 11
    assert all(kind in ("code", "ai") for _, kind, _ in stages)
    assert [n for n, k, side in stages if k == "ai"] == [n for n, k, side in stages if side]
    assert any(n == "SCORE" and k == "code" for n, k, _ in stages)


# ---------------------------------------------------------------------------------------------
# Rendering


BAD_WORDS = re.compile(r"\b(null|undefined|NaN)\b|\[object")


def _clean(snapshot):
    assert not BAD_WORDS.search(snapshot["text"]), BAD_WORDS.search(snapshot["text"]).group(0)
    assert not re.search(r"\blive\b", snapshot["text"], re.IGNORECASE)
    assert snapshot["badAttrs"] == []
    assert "style" not in snapshot["attrNames"]
    assert not any(a.startswith("on") for a in snapshot["attrNames"])
    assert len(snapshot["ids"]) == len(set(snapshot["ids"])), "duplicate ids"
    assert snapshot["danglingLabels"] == []
    assert snapshot["htmlSvg"] == 0, "SVG must be created in the SVG namespace"
    for href in snapshot["hrefs"]:
        assert href.startswith("?view=events&") or href.startswith("https://"), href


def test_without_a_document_the_renderers_answer_and_touch_nothing(pages):
    assert pages["noDom"]["system"] == {"label": "LAST RUN 21:00 SYDNEY", "led": "ok"}
    assert pages["noDom"]["sources"] == {"label": "10 SOURCES · 2 NEED FIXING", "led": "fail"}
    assert pages["noDom"]["touched"] == []
    assert pages["nullHost"][0]["led"] == "ok" and pages["nullHost"][1]["led"] == "fail"


@pytest.mark.parametrize("shape", ["old", "new"])
def test_system_page_renders_cleanly_and_idempotently(pages, shape):
    page = pages["system"][shape]
    _clean(page)
    assert page["idempotent"]
    assert page["res"] == {"label": "LAST RUN 21:00 SYDNEY", "led": "ok"}
    assert page["h3"] == ["HOW A STORY GETS HERE", "WHAT IS CODE, WHAT IS AI", "SCHEDULE", "LAST RUN", "VERSIONS"]
    assert "COLLECTION REPLAY" in page["text"]
    # Every stage carries its marker as a word, not only a colour.
    for kind, name, marker in page["stages"]:
        assert marker.endswith({"code": "CODE", "ai": "AI"}[kind]), name
    assert "US$20" in page["budget"] and "never depend" in page["budget"]


def test_system_page_old_shape_falls_back_to_the_standing_schedule(pages):
    page = pages["system"]["old"]
    assert [r[0] for r in page["scheduleRows"]] == ["FAST LANE", "NORMAL LANE", "DEEP LANE AND DISCOVERY"]
    assert page["scheduleRows"][0][1] == "every hour on the hour"
    assert page["scheduleRows"][2][1] == "daily 03:10 Sydney"
    assert "standing defaults" in page["text"]
    assert "IMPORTANCE" not in page["versions"] and "REPUTATION" not in page["versions"]
    assert {"PIPELINE", "SCHEMA", "SCORING", "ENRICHMENT"} <= set(page["versions"])


def test_system_page_new_shape_reads_the_published_schedule(pages):
    page = pages["system"]["new"]
    rows = {r[0]: r for r in page["scheduleRows"]}
    assert rows["GROUND TRUTH"][1] == "every 6 hours at :25"
    assert rows["GROUND TRUTH"][2] == "25 */6 * * * UTC"
    assert rows["MORNING DIGEST"][1] == "daily 07:10, 08:10 and 09:10 Sydney"
    assert rows["AI ENRICHMENT"][3].endswith("AI")
    assert rows["MORNING DIGEST"][3].endswith("CODE + AI")
    assert rows["ODD JOB"][1] == "custom schedule" and rows["ODD JOB"][3] == "—" and rows["ODD JOB"][4] == "—"
    assert "standing defaults" not in page["text"]
    assert {"IMPORTANCE", "REPUTATION"} <= set(page["versions"])


def test_system_page_replays_the_last_run(pages):
    replay = {step[0]: step[1:] for step in pages["system"]["old"]["replay"]}
    assert replay["SOURCES CHECKED"] == ["11", "10 ok · 0 failed · 1 stale"]
    assert replay["ITEMS FETCHED"][0] == "90"
    assert replay["DUPLICATES"] == ["90", "already held"]
    assert replay["ARCHIVED"] == ["0", "merged or faded"]
    assert replay["PUBLISHED"] == ["10", "stories, 1 awaiting AI"]
    text = pages["system"]["old"]["text"]
    assert "Pipeline AI this month: 960 model calls, US$0.12." in text
    assert "FAST LANE · finished 4 Oct 21:00 Sydney, 30 min ago · took 1.8 s" in text


@pytest.mark.parametrize("shape", ["old", "new"])
def test_sources_page_renders_cleanly_and_idempotently(pages, shape):
    page = pages["sources"][shape]
    _clean(page)
    assert page["idempotent"]
    assert page["sections"][:2] == ["pg-src-fix", "pg-src-watch"]
    assert page["sections"][2:] == ["pg-src-active", "pg-src-coming"]
    assert page["pressed"] == {"all": "true", "au": "false", "global": "false",
                               "reputation": "true", "name": "false", "status": "false"}
    for role, label, cells in page["histories"]:
        assert role == "img" and label.startswith("Last ") and cells >= 1
    assert page["h3"][-1] == "HOW WE RATE"


def test_sources_page_old_shape(pages):
    page = pages["sources"]["old"]
    assert page["res"] == {"label": "9 SOURCES · 1 NEEDS FIXING", "led": "fail"}
    assert page["strip"] == [["HEALTHY", "●4"], ["NEEDS FIXING", "■1"], ["WATCH", "▲3"], ["COMING", "○1"]]
    assert page["showing"] == "Showing 9 of 9 sources."
    assert page["reps"] and all(r.startswith("REPUTATION") and "standing unknown" in r for r in page["reps"])
    assert page["meters"] == []
    assert "not rated for importance yet" in page["text"]
    assert "No KEY stories in this snapshot." in page["text"]
    assert page["candidates"] == []
    reasons = page["reasons"]
    assert "Last check TIMEOUT: ReadTimeout after 30s. No good check on record." in reasons
    # A stale feed still answers; it is only quiet, so there is no "good check" note.
    assert "No new item for 2.4 days." in reasons
    # A source never checked says so once: no "no good check" note and no empty history line.
    assert "No check recorded yet." in reasons
    assert page["text"].count("No check") == 1
    assert "Listed, waiting to be tested and switched on." in reasons


def test_sources_page_says_when_the_last_good_check_was(pages):
    # example_feed: OK at 07:00, timed out at 08:00, failed at 09:00; the page is read at 10:30.
    reasons = pages["sources"]["new"]["reasons"]
    assert "Last check ERROR: HTTP 503 from upstream. Last good check 4 h ago." in reasons
    assert "Timed out on the last 3 checks. No good check on record." in reasons
    assert "Quieter than usual: no new entry for 2 days." in reasons


def test_sources_page_new_shape(pages):
    page = pages["sources"]["new"]
    assert page["res"] == {"label": "10 SOURCES · 2 NEED FIXING", "led": "fail"}
    assert page["strip"] == [["HEALTHY", "●4"], ["NEEDS FIXING", "■2"], ["WATCH", "▲3"], ["COMING", "○3"]]
    assert set(page["cards"]["pg-src-fix"]) == {"Example Security Feed", next(n for n in page["cards"]["pg-src-fix"] if "Sophos" in n)}
    assert page["svgCount"] > 10, "history strips, meters and the tier bar are SVG"
    assert "Reputation 88 out of 100" in page["meters"] and "Reputation 47 out of 100" in page["meters"]
    assert all(0 <= float(w) <= 100 for w in page["meterFills"])
    assert any("uptime 100% · corroboration 62% · 41 events in 90 days" in r for r in page["reps"])
    assert any("SPECIALIST · not scored yet" in r for r in page["reps"])
    assert any("standing unknown" in r for r in page["reps"])
    assert "ABC News coverage of artificial intelligence." in page["text"]
    assert "Australian Broadcasting Corporation" in page["text"]
    assert "Research from around the world." in page["text"], "a source without a description falls back"
    assert page["tiers"] == "KEY 4 · NOTABLE 3 · ROUTINE 2 · 1 not rated"
    assert page["tierBar"][0].startswith("Importance of 9 rated stories")
    assert [href for _, href in page["top"]] == [
        "?view=events&scope=au&event=evt-2026-001770",
        "?view=events&scope=au&event=evt-2026-002001",
        "?view=events&scope=au&event=evt-2026-004970",
    ]
    assert len(page["candidates"]) == 2
    assert any("cert.example.net" in c and "CANDIDATE" in c for c in page["candidates"])
    assert any("blog.example.com" in c for c in page["candidates"])
    assert "javascript:" not in json.dumps(page["hrefs"])
    assert page["cards"]["pg-src-coming"][0]


def test_sources_controls_filter_sort_and_keep_their_state(pages):
    page = pages["sources"]["new"]
    au = page["au"]
    assert au["regions"] and set(au["regions"]) == {"au"}
    assert au["pressed"]["au"] == "true" and au["pressed"]["all"] == "false"
    assert au["showing"].startswith("Showing ") and au["showing"].endswith(", AU only.")
    assert "pg-src-fix" not in au["sections"], "no AU source needs fixing"
    assert any("choose ALL" in n for n in au["notes"]), "candidates have no region; the page says so"
    names = page["auByName"]["cards"]["pg-src-active"]
    assert len(names) >= 2 and names == sorted(names, key=str.lower)
    assert set(page["auByName"]["regions"]) == {"au"}, "sorting keeps the region filter"
    assert page["auByName"]["pressed"]["name"] == "true" and page["auByName"]["pressed"]["reputation"] == "false"
    glob = page["global"]
    assert set(glob["regions"]) == {"global"}
    assert "pg-src-fix" in glob["sections"]
    assert page["rerendered"]["pressed"]["global"] == "true" and page["rerendered"]["pressed"]["status"] == "true"
    assert set(page["rerendered"]["regions"]) == {"global"}
    back = page["back"]
    assert back["showing"] == "Showing 10 of 10 sources."
    assert back["cards"] == page["cards"]


@pytest.mark.parametrize("case", ["undef", "nul", "empty", "allNull", "junk", "junk2"])
def test_null_and_junk_inputs_never_crash(pages, case):
    r = pages["junk"][case]
    assert r["system"]["led"] in ("ok", "warn", "fail", "idle")
    assert r["sources"]["led"] in ("ok", "warn", "fail", "idle")
    assert not BAD_WORDS.search(r["systemText"]), r["systemText"]
    assert not BAD_WORDS.search(r["sourcesText"]), r["sourcesText"]
    assert r["badAttrs"] == []
    if case in ("undef", "nul", "empty", "allNull"):
        assert r["system"] == {"label": "AWAITING DATA", "led": "idle"}
        assert r["sources"] == {"label": "AWAITING DATA", "led": "idle"}
        assert "No completed run has been published yet." in r["systemText"]
        assert "Source health has not been published yet." in r["sourcesText"]
        assert "standing defaults" in r["systemText"]


def test_junk_shapes_degrade_to_sentences(pages):
    junk = pages["junk"]["junk"]
    assert junk["system"] == {"label": "LAST RUN TIME UNKNOWN", "led": "ok"}
    assert junk["sources"] == {"label": "1 SOURCE · 1 TO WATCH", "led": "warn"}
    junk2 = pages["junk"]["junk2"]
    assert junk2["system"]["led"] == "fail"
    assert "custom schedule" in junk2["systemText"]
    assert "Unnamed source" in junk2["sourcesText"]
    assert "Unnamed candidate" in junk2["sourcesText"]
