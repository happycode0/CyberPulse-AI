// CyberPulse-AI HUD. Plain ES module, no framework, no build step.
// Every string from the data files is written with textContent or setAttribute,
// never parsed as markup.

import { renderMap, syncMapSelection } from './map.js';
// The SYSTEM and SOURCES views are drawn by pages.js, which owns their markup and styles
// (pages.css). The contract is two functions that clear and refill a host and hand back the
// view head's count and LED; renderPage() below is the only caller.
import { renderSystemPage, renderSourcesPage } from './pages.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const DATA_BASES = ['data/', '../data/'];
const FX_KEY = 'cp-fx';

export const SEVERITY = {
  critical: { glyph: '◆', shape: 'diamond', label: 'CRITICAL', bars: 4 },
  high: { glyph: '▲', shape: 'triangle', label: 'HIGH', bars: 3 },
  medium: { glyph: '●', shape: 'circle', label: 'MEDIUM', bars: 2 },
  low: { glyph: '■', shape: 'square', label: 'LOW', bars: 1 },
  info: { glyph: '○', shape: 'ring', label: 'INFO', bars: 1 },
  unknown: { glyph: '◇', shape: 'open-diamond', label: 'UNRATED', bars: 0 },
};
const SEVERITY_WEIGHT = { critical: 10, high: 6, medium: 3, low: 1, info: 0.5, unknown: 1 };

// The crew roster is presentation, not authority (PLAN.md): personas are stable and
// ship with the site. Workload per agent comes from data/crew.json and is looked up by
// callsign at render time; an agent crew.json does not list shows RUNS IN PAPERCLIP, since
// every such agent runs in Paperclip's control panel, which this data file never reads.
// Each agent carries a `face`: the key of the one accessory that distinguishes its drawn
// portrait in THE CREW. Callsigns are unchanged — they are the join key for data/crew.json
// and the names PLAN.md assigns permissions and budgets to. Eight agents since October 2026:
// the eight retired callsigns' work was folded into these, so each keeps its own portrait and
// `title` is its Paperclip role. `desk` only picks the portrait's accent colour.
export const CREW = [
  { callsign: 'MORPHEUS', face: 'crown', title: 'CEO', desk: 'INTELLIGENCE', beat: 'Chief executive and chief editor', runtime: 'LLM · strong · 1×/day', quote: 'I can only show you the door.' },
  { callsign: 'DECKARD', face: 'lens', title: 'RESEARCHER', desk: 'INTELLIGENCE', beat: 'Research, context and developing events', runtime: 'LLM + Strands', quote: "The case stays open until it's patched." },
  { callsign: 'VOIGHT', face: 'scan', title: 'PUBLISHER', desk: 'INTELLIGENCE', beat: 'Editorial check and publication', runtime: 'LLM · strong · gated', quote: 'Says who?' },
  { callsign: 'TACHIKOMA', face: 'tilt', title: 'FINDER', desk: 'INTELLIGENCE', beat: 'Source discovery', runtime: 'LLM + Tavily', quote: "Ooh — what's this one?" },
  { callsign: 'WHEELJACK', face: 'helmet', title: 'CODER', desk: 'ENGINEERING', beat: 'Collectors, parsers and the platform', runtime: 'CODE agent · wakes on request', quote: '' },
  { callsign: 'TELETRAAN', face: 'dish', title: 'OPERATION', desk: 'OPERATIONS', beat: 'Watchdog, SRE and code checks', runtime: 'Deterministic + LLM triage', quote: '' },
  { callsign: 'RIPPERDOC', face: 'mirror', title: 'CHEAP', desk: 'OPERATIONS', beat: 'Cost ceiling and model scout', runtime: 'Deterministic scan · daily', quote: 'Better chrome just came in.' },
  { callsign: 'SERAPH', face: 'shield', title: 'COLLECTOR', desk: 'OPERATIONS', beat: 'Collection, the source gate and ground truth', runtime: 'Deterministic', quote: 'I had to be sure.' },
];

const SYDNEY = new Intl.DateTimeFormat('en-AU', {
  timeZone: 'Australia/Sydney',
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZoneName: 'short',
});

// ---------------------------------------------------------------- helpers

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  append(el, children);
  return el;
}

function s(tag, props = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function clear(el) {
  el.replaceChildren();
  return el;
}

function pad(n) {
  return String(n).padStart(2, '0');
}

export function formatSydney(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return '—';
  return SYDNEY.format(d).replace(',', '');
}

export function formatUtc(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return '—';
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

// The card's timestamp: "2026-10-03 14:30 AEST". Numeric and fixed-width so a column of them
// reads in order, and still Sydney time with the zone written out, so AEST and AEDT are never
// confused.
const SYDNEY_PARTS = new Intl.DateTimeFormat('en-AU', {
  timeZone: 'Australia/Sydney',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
  timeZoneName: 'short',
});

export function formatSydneyStamp(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return '—';
  const p = Object.fromEntries(SYDNEY_PARTS.formatToParts(d).map((part) => [part.type, part.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute} ${p.timeZoneName}`;
}

function timeEl(iso) {
  return h('time', { datetime: iso || undefined, class: 'mono', text: formatSydney(iso) });
}

function safeUrl(raw) {
  try {
    const u = new URL(raw);
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : null;
  } catch {
    return null;
  }
}

function sevKey(event) {
  return SEVERITY[event.severity] ? event.severity : 'unknown';
}

function safeStorage(kind) {
  try {
    return window[kind];
  } catch {
    return null;
  }
}

function storageGet(store, key) {
  try {
    return store ? store.getItem(key) : null;
  } catch {
    return null;
  }
}

function storageSet(store, key, value) {
  try {
    if (store) store.setItem(key, value);
  } catch {
    /* private mode: the choice simply is not remembered */
  }
}

function prominence(event) {
  return event.risk?.prominence ?? 0;
}

function byProminence(a, b) {
  return (
    prominence(b) - prominence(a) ||
    String(b.last_material_update || b.first_seen).localeCompare(String(a.last_material_update || a.first_seen))
  );
}

// Counts in prose are written by the page, so a quiet day reads "1 event queued", not
// "1 events queued". Only the regular -s plural is handled; every caller passes a noun
// that takes one.
function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

// ------------------------------------------------------------------- data

async function getJson(url, fetchImpl) {
  try {
    const res = await fetchImpl(url, { cache: 'no-cache' });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

// Tries data/ (as published on Pages), then ../data/ (serving the repository root locally).
export async function loadData({ bases = DATA_BASES, fetchImpl = globalThis.fetch } = {}) {
  for (const base of bases) {
    const feed = await getJson(`${base}live.json`, fetchImpl);
    if (!feed || !Array.isArray(feed.events)) continue;
    const [status, health] = await Promise.all([
      getJson(`${base}system-status.json`, fetchImpl),
      getJson(`${base}source-health.json`, fetchImpl),
    ]);
    return { base, feed, events: feed.events, status, health };
  }
  throw new Error('data/live.json could not be read');
}

// History index (one entry per UTC day) and a single day's snapshot, both from the
// same two-base search as loadData.
export async function loadIndex({ bases = DATA_BASES, fetchImpl = globalThis.fetch } = {}) {
  for (const base of bases) {
    const index = await getJson(`${base}index.json`, fetchImpl);
    if (index && Array.isArray(index.days)) return { base, index };
  }
  return null;
}

export async function loadDay(base, day, { fetchImpl = globalThis.fetch } = {}) {
  if (!base || !day || !day.path) return null;
  return getJson(`${base}${day.path}`, fetchImpl);
}

// How many archived days to search when an event is not in the current snapshot.
const HISTORY_SCAN = 45;

// Find one event: the current snapshot first, then archived days newest first.
// Returns { event, events, feed } or null; `events` pools everything searched so
// related-event links can be resolved without another round trip. An event that was
// merged into another (index.json's `merged` map) finds the event it joined, and the
// result says so with `mergedFrom`, the id that was asked for.
export async function findEvent(eventId, { bases = DATA_BASES, fetchImpl = globalThis.fetch } = {}) {
  for (const base of bases) {
    const feed = await getJson(`${base}live.json`, fetchImpl);
    const feedEvents = feed && Array.isArray(feed.events) ? feed.events : [];
    const hit = feedEvents.find((e) => e.event_id === eventId);
    if (hit) return { event: hit, events: feedEvents, feed };
    const index = await getJson(`${base}index.json`, fetchImpl);
    const joined = index && index.merged && typeof index.merged[eventId] === 'string' ? index.merged[eventId] : null;
    const wanted = joined || eventId;
    const merged = joined ? { mergedFrom: eventId } : {};
    const joinedHit = joined ? feedEvents.find((e) => e.event_id === wanted) : null;
    if (joinedHit) return { event: joinedHit, events: feedEvents, feed, ...merged };
    if (index && Array.isArray(index.days)) {
      const days = [...index.days]
        .sort((a, b) => String(b.date).localeCompare(String(a.date)))
        .slice(0, HISTORY_SCAN);
      for (const meta of days) {
        const day = await getJson(`${base}${meta.path}`, fetchImpl);
        const dayEvents = day && Array.isArray(day.events) ? day.events : [];
        const dayHit = dayEvents.find((e) => e.event_id === wanted);
        if (dayHit) return { event: dayHit, events: [...feedEvents, ...dayEvents], feed, ...merged };
      }
    }
  }
  return null;
}

// ------------------------------------------------------ scope, beat and feeds

const has = (list, ...wanted) => (list || []).some((x) => wanted.includes(String(x).toLowerCase()));
const isAu = (e) => Boolean(e.au?.directly_reported_in_au) || (e.au?.relevance ?? 0) >= 0.5;
// Events under a new or rising topic in trends.json. renderTrends fills it and EMERGING
// THREATS reads it, so a story the counts say is taking off is listed there even before a
// model has tagged it.
const trending = new Set();

// The scope row in the header. AUSTRALIA is preselected because this is an Australia-first
// product. GLOBAL is everything isAu() does not claim, so AUSTRALIA and GLOBAL always add up
// to ALL and no event can fall between them.
export const SCOPES = { au: 'AUSTRALIA', global: 'GLOBAL', all: 'ALL' };

export function inScope(event, scope) {
  if (scope === 'au') return isAu(event);
  if (scope === 'global') return !isAu(event);
  return true;
}

// The beat: which desk a story belongs to. The worker publishes `beat` once the AI beat ships
// (worker/models.py beat_of: from `domains`, never stored). A snapshot from before then has no
// such field, so it is derived here the same way, from `domains`. An event with no domains at
// all predates domain tagging; a source with no beat seeds `cybersecurity`, so it reads as cyber.
export const BEATS = { cyber: 'CYBER', ai: 'AI' };
const BEAT_LABEL = { cyber: 'CYBER', ai: 'AI', both: 'CYBER + AI', other: 'OTHER' };
const AI_INDUSTRY = 'AI_INDUSTRY';

export function beatOf(event) {
  if (event && Object.hasOwn(BEAT_LABEL, event.beat)) return event.beat;
  const domains = (event?.domains || []).map((d) => String(d).toLowerCase());
  if (!domains.length) {
    if (!event?.ai_subdomain) return 'cyber';
    return String(event.ai_subdomain).toUpperCase() === AI_INDUSTRY ? 'ai' : 'both';
  }
  const cyber = domains.includes('cybersecurity');
  const ai = domains.includes('ai');
  if (cyber && ai) return 'both';
  if (ai) return 'ai';
  return cyber ? 'cyber' : 'other';
}

// A story on both desks answers to either beat; OTHER answers to neither.
function inBeat(event, beats) {
  if (!beats?.length) return true;
  const beat = beatOf(event);
  return beats.some((want) => beat === want || beat === 'both');
}

// The intelligence feeds. These were seven of the old dashboard sections; the three domain
// sections (AUSTRALIA NOW, GLOBAL CYBER, AI + CYBER) became the scope and the beat, so every
// feed reads through the same scope row as everything else.
export const SECTIONS = [
  {
    id: 'active-exploitation',
    match: (e) =>
      (e.cves || []).some((c) => c.kev?.listed) ||
      has(e.categories, 'active-exploitation', 'active_exploitation') ||
      (e.timeline || []).some((t) => t.type === 'EXPLOIT_CONFIRMED'),
  },
  { id: 'developing-events', match: (e) => has([e.status], 'developing', 'active', 'monitoring') },
  {
    id: 'emerging-threats',
    match: (e) =>
      has(e.categories, 'emerging-threat', 'emerging_threat', 'malware', 'ransomware', 'zero-day') ||
      (e.status === 'new' && (e.severity === 'critical' || e.severity === 'high')) ||
      trending.has(e.event_id),
  },
  { id: 'threat-actors', match: (e) => (e.entities?.actors || []).length > 0 },
  { id: 'vulnerabilities', match: (e) => (e.cves || []).length > 0 || has(e.categories, 'vulnerability') },
  // `research` is security research only since the AI desk got `ai-research`, and AI law and
  // policy is `ai-governance`. Each feed takes its AI counterpart too, so RESEARCH and
  // POLICY / REGULATION (and the old #sec-research and #sec-policy-regulation links) still show
  // what they always did: the old `research` slug covered security and AI research alike.
  { id: 'research', match: (e) => has(e.categories, 'research', 'ai-research') },
  {
    id: 'policy-regulation',
    match: (e) => has(e.categories, 'policy', 'regulation', 'policy-regulation', 'policy_regulation', 'ai-governance'),
  },
];

// What a category is called in the tag filters. Cards keep the raw `#slug`, which is what a
// reader types and what the URL carries; a slug missing here is shown as it is.
const CATEGORY_LABEL = {
  vulnerability: 'VULNERABILITY',
  'zero-day': 'ZERO-DAY',
  malware: 'MALWARE',
  ransomware: 'RANSOMWARE',
  'data-breach': 'DATA BREACH',
  phishing: 'PHISHING',
  'supply-chain': 'SUPPLY CHAIN',
  ddos: 'DDOS',
  espionage: 'ESPIONAGE',
  fraud: 'FRAUD',
  'emerging-threat': 'EMERGING THREAT',
  research: 'SECURITY RESEARCH',
  policy: 'POLICY',
  regulation: 'REGULATION',
  'ai-security': 'AI SECURITY',
  'ai-industry': 'AI INDUSTRY',
  'model-release': 'MODEL RELEASE',
  'ai-governance': 'AI GOVERNANCE',
  'ai-incident': 'AI INCIDENT',
  'ai-research': 'AI RESEARCH',
};

const FEED_LABEL = {
  'active-exploitation': 'ACTIVE EXPLOITATION',
  'developing-events': 'DEVELOPING EVENTS',
  'emerging-threats': 'EMERGING THREATS',
  'threat-actors': 'THREAT ACTORS',
  vulnerabilities: 'VULNERABILITIES',
  research: 'RESEARCH',
  'policy-regulation': 'POLICY / REGULATION',
};

function inFeed(event, feeds) {
  if (!feeds?.length) return true;
  return SECTIONS.some((section) => feeds.includes(section.id) && section.match(event));
}

// ------------------------------------------------------------ view state and URL

// Everything a reader can choose is in the URL, so a link or a bookmark reopens the same view:
// ?view=events&scope=global&beat=ai&feed=vulnerabilities&key=1&tag=severity:critical&event=<id>.
// `anchor` is the one place a fragment is still used: a block inside a view (#sec-trends) that
// a link can jump to. `key` is KEY ONLY: just the stories the importance score rates KEY.
export const VIEWS = ['dashboard', 'events', 'crew', 'system', 'sources'];
const VIEW_ANCHORS = { dashboard: ['sec-trends'], events: [], crew: [], system: [], sources: [] };
const EVENT_CARDS = 20;

export function freshState(over = {}) {
  return { view: 'dashboard', scope: 'au', beat: [], feed: [], key: false, tags: {}, event: null, anchor: null, ...over };
}

// Every #sec-* anchor the site has ever published, and what it opens now. The page used to be
// one long run of sections and then five tabs, and links to all of them are in bookmarks, in
// event.html and history.html, and in other people's pages. Each opens the view that now holds
// what it used to show, at the scope that reproduces it: the old sections other than
// AUSTRALIA NOW were not limited to Australia, so they open at ALL. SYSTEM and the source list
// were blocks of one crew view until October 2026 and are views of their own now.
export const LEGACY_ANCHORS = {
  'sec-australia-now': { view: 'events', scope: 'au' },
  'sec-global-cyber': { view: 'events', scope: 'global' },
  'sec-ai-cyber': { view: 'events', scope: 'all', beat: ['ai'] },
  'sec-overview': { view: 'dashboard', scope: 'all' },
  'sec-trends': { view: 'dashboard', scope: 'all', anchor: 'sec-trends' },
  'sec-active-exploitation': { view: 'events', scope: 'all', feed: ['active-exploitation'] },
  'sec-developing-events': { view: 'events', scope: 'all', feed: ['developing-events'] },
  'sec-emerging-threats': { view: 'events', scope: 'all', feed: ['emerging-threats'] },
  'sec-threat-actors': { view: 'events', scope: 'all', feed: ['threat-actors'] },
  'sec-vulnerabilities': { view: 'events', scope: 'all', feed: ['vulnerabilities'] },
  'sec-research': { view: 'events', scope: 'all', feed: ['research'] },
  'sec-policy-regulation': { view: 'events', scope: 'all', feed: ['policy-regulation'] },
  'sec-world-map': { view: 'dashboard', scope: 'all' },
  'sec-the-crew': { view: 'crew' },
  'sec-system': { view: 'system' },
  'sec-sources': { view: 'sources' },
  'sec-source-health': { view: 'sources' },
  'source-health': { view: 'sources' },
};

// Anchors that were blocks inside a view this version still has, and the view that holds them
// now: ?view=crew&scope=au#sec-system is what the crew link wrote before SYSTEM had its own
// view. The rest of that URL still says what the reader chose, so only the view moves.
const MOVED_ANCHORS = { 'sec-system': 'system', 'sec-sources': 'sources', 'sec-source-health': 'sources', 'source-health': 'sources' };

// An old anchor only counts when the query has no `view`: a URL this version wrote says
// exactly what it wants, and a fragment on it is an anchor inside that view.
export function parseLocation(search = '', hash = '') {
  const q = new URLSearchParams(search);
  const anchor = String(hash || '').replace(/^#/, '');
  if (!q.has('view') && Object.hasOwn(LEGACY_ANCHORS, anchor)) {
    return { ...freshState(LEGACY_ANCHORS[anchor]), legacy: true };
  }
  if (q.has('view') && Object.hasOwn(MOVED_ANCHORS, anchor)) {
    const moved = new URLSearchParams(q);
    moved.set('view', MOVED_ANCHORS[anchor]);
    return { ...parseLocation(moved.toString(), ''), legacy: true };
  }
  const list = (key, allowed) => [
    ...new Set(
      q
        .getAll(key)
        .flatMap((v) => v.split(','))
        .map((v) => v.trim().toLowerCase())
        .filter((v) => allowed.includes(v)),
    ),
  ];
  const view = VIEWS.includes(q.get('view')) ? q.get('view') : 'dashboard';
  const dims = new Set(DIMENSIONS.map(([dim]) => dim));
  const tags = {};
  for (const raw of q.getAll('tag')) {
    const i = raw.indexOf(':');
    const dim = raw.slice(0, i);
    const value = raw.slice(i + 1);
    if (i < 1 || !value || !dims.has(dim)) continue;
    tags[dim] ||= [];
    if (!tags[dim].includes(value)) tags[dim].push(value);
  }
  return freshState({
    view,
    scope: Object.hasOwn(SCOPES, q.get('scope') ?? '') ? q.get('scope') : 'au',
    beat: list('beat', Object.keys(BEATS)),
    feed: list('feed', SECTIONS.map((section) => section.id)),
    key: q.get('key') === '1',
    tags,
    event: q.get('event') || null,
    anchor: VIEW_ANCHORS[view].includes(anchor) ? anchor : null,
  });
}

// The inverse. `view` and `scope` are always written, so the URL is never ambiguous about
// either and an old fragment can never be mistaken for a choice.
export function toSearch(state) {
  const q = new URLSearchParams();
  q.set('view', state.view);
  q.set('scope', state.scope);
  if (state.beat?.length) q.set('beat', state.beat.join(','));
  if (state.feed?.length) q.set('feed', state.feed.join(','));
  if (state.key) q.set('key', '1');
  for (const [dim, values] of Object.entries(state.tags || {})) for (const v of values) q.append('tag', `${dim}:${v}`);
  if (state.event) q.set('event', state.event);
  const search = q.toString().replace(/%2C/g, ',').replace(/%3A/g, ':');
  return `?${search}${state.anchor ? `#${state.anchor}` : ''}`;
}

// What a view lists. `skip` names the parts of the state to ignore: the dashboard ignores the
// feed, and the map ignores the country it is itself choosing.
export function filterEvents(events, state, { skip = [] } = {}) {
  const tags = skip.includes('tags') ? {} : { ...(state.tags || {}) };
  if (skip.includes('country')) delete tags.country;
  const pool = (events || []).filter(
    (e) =>
      (skip.includes('scope') || inScope(e, state.scope)) &&
      (skip.includes('beat') || inBeat(e, state.beat)) &&
      (skip.includes('feed') || inFeed(e, state.feed)) &&
      (skip.includes('key') || !state.key || isKey(e)),
  );
  return applyFilters({ events: pool, selected: tags });
}

// The FEEDS presets. Each is a change to the state, not a place: a geographic preset sets the
// scope, and a feed keeps the scope the reader chose. Tags and KEY ONLY always carry over.
// AUSTRALIA NOW and GLOBAL CYBER are no longer in the menu (the SCOPE radios do the same), but
// stay here so their old links and anchors still name a preset and still count.
const feedPreset = (id, label, feed) => ({
  id,
  label,
  group: 'feeds',
  apply: (st) => ({ ...st, view: 'events', beat: [], feed, event: null, anchor: null }),
});

export const PRESETS = [
  { id: 'australia-now', label: 'AUSTRALIA NOW', group: 'scope', apply: (st) => ({ ...st, view: 'events', scope: 'au', beat: [], feed: [], event: null, anchor: null }) },
  { id: 'global-cyber', label: 'GLOBAL CYBER', group: 'scope', apply: (st) => ({ ...st, view: 'events', scope: 'global', beat: [], feed: [], event: null, anchor: null }) },
  { id: 'ai-cyber', label: 'AI + CYBER', group: 'feeds', apply: (st) => ({ ...st, view: 'events', beat: ['ai'], feed: [], event: null, anchor: null }) },
  feedPreset('active-exploitation', 'ACTIVE EXPLOITATION', ['active-exploitation']),
  feedPreset('vulnerabilities', 'VULNERABILITIES', ['vulnerabilities']),
  feedPreset('threat-actors', 'THREAT ACTORS', ['threat-actors']),
  feedPreset('policy-research', 'POLICY & RESEARCH', ['research', 'policy-regulation']),
  feedPreset('developing-events', 'DEVELOPING EVENTS', ['developing-events']),
  feedPreset('emerging-threats', 'EMERGING THREATS', ['emerging-threats']),
];

const sameSet = (a = [], b = []) => a.length === b.length && a.every((x) => b.includes(x));

// The preset the state is showing, if it is exactly one of them. No two presets can describe
// the same state, so there is no precedence to settle.
export function currentPreset(state) {
  if (state.view !== 'events') return null;
  const hit = PRESETS.find((p) => {
    const next = p.apply(state);
    return next.scope === state.scope && sameSet(next.beat, state.beat) && sameSet(next.feed, state.feed);
  });
  return hit ? hit.id : null;
}

// How many events each preset would list from here, and how many more it would list at ALL:
// the FEEDS menu's counts and its one-click widen.
export function presetCounts(events, state) {
  const out = {};
  for (const preset of PRESETS) {
    const next = preset.apply(state);
    const count = filterEvents(events, next).length;
    const all = next.scope === 'all' ? count : filterEvents(events, { ...next, scope: 'all' }).length;
    out[preset.id] = { count, hidden: all - count, state: next };
  }
  return out;
}

// The state that shows `event` in the Events view, widened only as far as it has to be. A
// headline, a related-event link or a bookmark can name a story the current scope, beat, feed,
// KEY ONLY or a tag hides; each of those is lifted only if it is the one in the way, so a
// reader who chose AUSTRALIA and clicks a global story lands on ALL with their tags intact.
export function sectionFor(event, state = freshState()) {
  if (!event) return null;
  const next = { ...state, tags: { ...(state.tags || {}) }, view: 'events', event: event.event_id, anchor: null };
  if (!inScope(event, next.scope)) next.scope = 'all';
  if (!inBeat(event, next.beat)) next.beat = [];
  if (!inFeed(event, next.feed)) next.feed = [];
  if (next.key && !isKey(event)) next.key = false;
  for (const dim of Object.keys(next.tags)) {
    if (!applyFilters({ events: [event], selected: { [dim]: next.tags[dim] } }).length) delete next.tags[dim];
  }
  return next;
}

const expanded = new Set();
let filtersActive = false;
// An event can be listed in more than one place, so cards carry data-event-id, never a DOM id.
// main() fills these in so revealEvent can widen the view when a card is capped or filtered out.
// `menus` is initDropdowns()'s, so a tag toggled in main() can close TAG FILTERS.
const view = { events: [], state: freshState(), navigate: () => {}, menus: { applied: () => {} } };

function findCard(eventId) {
  const host = document.querySelector('[data-section-body="events"]');
  return host ? [...host.querySelectorAll('[data-event-id]')].find((el) => el.dataset.eventId === eventId) || null : null;
}

// Scrolls to and focuses the card for the event in the Events view, first widening the scope,
// beat, feed or tags that hide it (sectionFor) and lifting the card cap. Returns the card, or
// null when the event is not in this snapshot.
export function revealEvent(eventId, { replace = false } = {}) {
  const event = view.events.find((e) => e.event_id === eventId);
  if (!event) return null;
  view.navigate(sectionFor(event, view.state), { replace });
  let card = findCard(eventId);
  if (!card) {
    expanded.add('events');
    view.navigate(view.state, { replace: true });
    card = findCard(eventId);
  }
  if (card) {
    // Focus first: it forces content-visibility to lay the card out, so the scroll lands on real sizes.
    card.focus({ preventScroll: true });
    card.scrollIntoView({ block: 'center' });
    requestAnimationFrame(() => card.scrollIntoView({ block: 'center' }));
  }
  return card;
}

// `unread` is the third state, and it is not the same as zero. An empty event list can mean the
// snapshot holds nothing for this view, or that no snapshot could be read at all — and the
// difference is the whole honest-data rule (PLAN.md §2). Told to render nothing, this used to put
// "0 SIGNALS" and "NO SIGNALS IN THIS SNAPSHOT." under every section of a page whose data had
// 404'd: confident claims about a snapshot nobody had. history.html and the map count already
// said AWAITING DATA for exactly this reason. The Events view is one list now, but the rule holds.
export function renderSections(data) {
  const unread = Boolean(data.unread);
  const matches = [...(data.events || [])].sort(byProminence);
  const body = document.querySelector('[data-section-body="events"]');
  if (body) renderSectionBody(body, 'events', matches, unread);
  const count = document.querySelector('[data-count-for="events"]');
  if (count) {
    count.textContent = unread ? 'AWAITING DATA' : `${matches.length} ${matches.length === 1 ? 'SIGNAL' : 'SIGNALS'}`;
    const led = count.parentElement.querySelector('.led');
    if (led) led.dataset.state = !unread && matches.length ? 'ok' : 'idle';
  }
  return { events: matches };
}

function renderSectionBody(body, id, matches, unread = false) {
  clear(body);
  if (!matches.length) {
    body.append(
      h('p', {
        class: 'empty',
        // Short on purpose: the full explanation is already in the notice at the top of the
        // page. It only has to not claim the view is empty, which "UNKNOWN" does in one word.
        // KEY ONLY on a snapshot with no importance scores is not "nothing is key": nothing was rated.
        text: unread
          ? 'UNKNOWN — NO SNAPSHOT WAS READ.'
          : view.state.key && !view.events.some((e) => importanceOf(e))
            ? 'NO STORY IN THIS SNAPSHOT HAS AN IMPORTANCE SCORE YET, SO KEY ONLY LISTS NOTHING.'
            : filtersActive
              ? 'NO SIGNALS MATCH THE CURRENT FILTERS.'
              : 'NO SIGNALS IN THIS SNAPSHOT.',
      }),
    );
    return;
  }
  const shown = expanded.has(id) ? matches : matches.slice(0, EVENT_CARDS);
  body.append(renderEventCards(shown));
  if (matches.length > EVENT_CARDS) {
    const btn = h('button', {
      type: 'button',
      class: 'btn btn--small show-more',
      'aria-expanded': expanded.has(id) ? 'true' : 'false',
      text: expanded.has(id) ? 'SHOW FEWER' : `SHOW ALL ${matches.length}`,
    });
    btn.addEventListener('click', () => {
      if (expanded.has(id)) expanded.delete(id);
      else expanded.add(id);
      renderSectionBody(body, id, matches);
      body.querySelector('.show-more')?.focus();
    });
    body.append(btn);
  }
}

// Severity is never colour alone: label + shape glyph (data-severity-shape) + bar count. The
// label carries the highest CVSS base score the event has, when it has one, because a reader
// triaging a list wants the number next to the word.
function severityBadge(key, cvss = null) {
  const sev = SEVERITY[key] || SEVERITY.unknown;
  return h(
    'span',
    { class: 'sev-badge', 'data-severity': key },
    h('span', { class: 'sev-shape', 'data-severity-shape': sev.shape, 'aria-hidden': 'true', text: sev.glyph }),
    h('span', { class: 'sev-badge__label', text: hasNum(cvss) ? `${sev.label} (CVSS: ${Number(cvss).toFixed(1)})` : sev.label }),
    severityBars(key, sev),
  );
}

function severityBars(key, sev = SEVERITY[key] || SEVERITY.unknown) {
  const bars = h('span', {
    class: 'sev-bars',
    role: 'img',
    'aria-label': `severity ${sev.bars} of 4`,
    'data-severity': key,
  });
  for (let i = 1; i <= 4; i += 1) bars.append(h('i', { class: i <= sev.bars ? 'on' : undefined }));
  return bars;
}

// The AI desk's own scale. An AI-only story has no cyber severity — it is "unknown" by
// construction, not by omission — so its card shows how much the story matters on the AI
// beat instead. Same three signals as severity: a word, a glyph and a bar count, and a
// different hue so the two scales are never read as one.
const SIGNIFICANCE = {
  major: { glyph: '◆', label: 'MAJOR', bars: 3 },
  notable: { glyph: '▲', label: 'NOTABLE', bars: 2 },
  minor: { glyph: '●', label: 'MINOR', bars: 1 },
  unrated: { glyph: '◇', label: 'UNRATED', bars: 0 },
};

function significanceKey(event) {
  return Object.hasOwn(SIGNIFICANCE, event.ai_significance ?? '') ? event.ai_significance : 'unrated';
}

function significanceBadge(key) {
  const sig = SIGNIFICANCE[key] || SIGNIFICANCE.unrated;
  const bars = h('span', { class: 'sev-bars sev-bars--three', role: 'img', 'aria-label': `AI significance ${sig.bars} of 3` });
  for (let i = 1; i <= 3; i += 1) bars.append(h('i', { class: i <= sig.bars ? 'on' : undefined }));
  return h(
    'span',
    { class: 'sev-badge sig-badge', 'data-significance': key },
    h('span', { class: 'sev-shape', 'aria-hidden': 'true', text: sig.glyph }),
    h('span', { class: 'sev-badge__label', text: `AI · ${sig.label}` }),
    bars,
  );
}

// The badge a row shows: AI significance for an AI-only story, severity for everything else.
function rankBadge(event) {
  if (beatOf(event) === 'ai') return significanceBadge(significanceKey(event));
  const scores = (event.cves || []).map((c) => c.cvss?.score).filter(hasNum);
  return severityBadge(sevKey(event), scores.length ? Math.max(...scores) : null);
}

// How much a story matters to a reader, as the worker scored it: a 0-100 score, a tier and the
// reasons behind it (importance v1). It is a separate question from severity — a medium bug in
// an Australian agency can matter more here than a critical one in a product nobody runs — so
// it has its own badge rather than bending the rank. KEY and NOTABLE are badged; ROUTINE is not,
// so a list is not a wall of labels. A snapshot from before the score has no `importance`, and
// such a story is unrated: never KEY, and never counted as ROUTINE either.
export const IMPORTANCE = {
  key: { label: 'KEY' },
  notable: { label: 'NOTABLE' },
  routine: { label: 'ROUTINE' },
};

export function importanceOf(event) {
  const imp = event?.importance;
  if (!imp || typeof imp !== 'object' || !Object.hasOwn(IMPORTANCE, imp.tier ?? '')) return null;
  const score = hasNum(imp.score) ? Number(imp.score) : NaN;
  return {
    tier: imp.tier,
    score: Number.isFinite(score) ? Math.round(Math.min(100, Math.max(0, score))) : null,
    reasons: (Array.isArray(imp.reasons) ? imp.reasons : [])
      .filter((r) => typeof r === 'string' && r.trim())
      .map((r) => r.trim().replace(/\.+$/, '')),
  };
}

export function isKey(event) {
  return importanceOf(event)?.tier === 'key';
}

// The words are the signal, not the fill: KEY is filled and NOTABLE outlined, and both say so.
function importanceBadge(event) {
  const imp = importanceOf(event);
  if (!imp || imp.tier === 'routine') return null;
  return h(
    'span',
    { class: 'imp-badge', 'data-tier': imp.tier },
    h('span', { class: 'visually-hidden', text: 'Importance ' }),
    IMPORTANCE[imp.tier].label,
    hasNum(imp.score) ? h('span', { class: 'imp-badge__score', text: ` ${imp.score}` }) : null,
  );
}

// "Australian government target; reported by ABC (established). Score 78 of 100." The reasons
// are the worker's, joined as written; a score with none says so rather than inventing one.
function importanceWhy(imp) {
  const why = imp.reasons.length ? `${imp.reasons.join('; ')}.` : 'no reasons were published with the score.';
  return `${why}${hasNum(imp.score) ? ` Score ${imp.score} of 100.` : ''}`;
}

// A sorted list of event cards. Used by the Events view and by the history page.
export function renderEventCards(events) {
  const list = [...(events || [])].sort(byProminence);
  if (!list.length) return h('p', { class: 'empty', text: 'NO EVENTS IN THIS SNAPSHOT.' });
  return h('ul', { class: 'event-list' }, list.map((e) => h('li', {}, eventCard(e))));
}

const CARD_CATEGORIES = 3;
const CARD_CVES = 3;

// header: when, and how bad (or how much it matters, on the AI beat). body: what happened.
// footer: the tags a reader filters by, in the order they are most often wanted.
function eventCard(event) {
  const key = sevKey(event);
  const beat = beatOf(event);
  const stamp = event.last_material_update || event.first_seen;
  const chips = [h('span', { class: 'chip chip--beat', 'data-beat': beat, text: BEAT_LABEL[beat] })];
  if (isAu(event)) chips.push(h('span', { class: 'chip chip--au', text: event.au?.directly_reported_in_au ? 'AU' : 'AU RELEVANT' }));
  const categories = event.categories || [];
  for (const c of categories.slice(0, CARD_CATEGORIES)) chips.push(h('span', { class: 'chip chip--cat', text: `#${c}` }));
  if (categories.length > CARD_CATEGORIES) {
    chips.push(h('span', { class: 'chip chip--more', text: `+${categories.length - CARD_CATEGORIES}` }));
  }
  const cves = event.cves || [];
  for (const cve of cves.slice(0, CARD_CVES)) {
    chips.push(h('span', { class: cve.kev?.listed ? 'chip chip--cve chip--kev' : 'chip chip--cve', text: cve.kev?.listed ? `${cve.id} KEV` : cve.id }));
  }
  if (cves.length > CARD_CVES) chips.push(h('span', { class: 'chip chip--more', text: `+${cves.length - CARD_CVES} CVE` }));
  if (event.severity_source === 'ai_estimate') chips.push(h('span', { class: 'chip chip--ai', text: 'AI-SUGGESTED' }));
  const firstSource = (event.sources || [])[0];
  if (firstSource) chips.push(h('span', { class: 'chip chip--src', text: `${firstSource.evidence_class} · ${firstSource.source_id}` }));
  const badges = [importanceBadge(event), rankBadge(event)];
  if (beat === 'both' && event.ai_significance) badges.push(significanceBadge(significanceKey(event)));
  return h(
    'article',
    { class: 'event-card', 'data-event-id': event.event_id, tabindex: '-1', 'data-severity': key, 'data-beat': beat },
    h(
      'header',
      { class: 'event-card__head' },
      h('time', { class: 'event-card__time', datetime: stamp || undefined, text: formatSydneyStamp(stamp) }),
      h('span', { class: 'event-card__badges' }, badges),
    ),
    h(
      'div',
      { class: 'event-card__body' },
      h(
        'h3',
        { class: 'event-card__title' },
        h('a', { class: 'event-card__link', href: `event.html?id=${encodeURIComponent(event.event_id)}`, text: event.title }),
      ),
      event.summary ? h('p', { class: 'event-card__summary', text: event.summary }) : null,
    ),
    h('footer', { class: 'event-card__tags' }, chips),
    eventDetail(event),
  );
}

function eventDetail(event) {
  const body = h('div', { class: 'event-more__body' }, h('p', { class: 'mono event-more__id', text: `EVENT ${event.event_id}` }));
  const imp = importanceOf(event);
  if (imp) body.append(h('h4', { text: 'IMPORTANCE' }), h('p', { text: `Why this rates ${IMPORTANCE[imp.tier].label}: ${importanceWhy(imp)}` }));
  if (event.why_it_matters) body.append(h('h4', { text: 'WHY IT MATTERS' }), h('p', { text: event.why_it_matters }));
  if (event.resolution) body.append(h('h4', { text: 'RESOLUTION' }), h('p', { text: event.resolution }));
  if (event.au?.reasons?.length) {
    body.append(h('h4', { text: 'AUSTRALIAN RELEVANCE' }), h('ul', {}, event.au.reasons.map((r) => h('li', { text: r }))));
  }
  if (event.cves?.length) {
    body.append(
      h('h4', { text: 'VULNERABILITIES' }),
      h(
        'ul',
        {},
        event.cves.map((c) => {
          const f = cveFacts(c);
          return h('li', { class: 'mono' }, [c.id, ` · CVSS ${f.cvssScore}`, ` · EPSS ${f.epssScore}`, ` · ${f.kev}`].join(''));
        }),
      ),
    );
  }
  if (event.sources?.length) {
    body.append(
      h('h4', { text: 'SOURCE REPORTS (EVIDENCE)' }),
      h(
        'ul',
        {},
        event.sources.map((src) => {
          const href = safeUrl(src.url);
          const label = `${src.source_id} · ${src.evidence_class}${src.independent ? '' : ' · not independent'}`;
          return h(
            'li',
            {},
            href ? h('a', { href, rel: 'noopener noreferrer nofollow', target: '_blank', text: label }) : label,
            src.published ? [' ', timeEl(src.published)] : null,
          );
        }),
      ),
    );
  }
  const ai = (event.mitre_techniques || []).filter((m) => m.confidence_type === 'ai_suggested');
  if (ai.length) {
    body.append(
      h('h4', { text: 'AI-SUGGESTED, NOT EVIDENCE' }),
      h(
        'ul',
        {},
        ai.map((m) =>
          h('li', {}, h('span', { class: 'chip chip--ai', text: 'AI-SUGGESTED' }), ` ${m.id} ${m.name} (${Math.round(m.confidence * 100)}%)`),
        ),
      ),
    );
  }
  if (event.timeline?.length) {
    body.append(
      h('h4', { text: 'TIMELINE' }),
      h('ul', {}, event.timeline.map((t) => h('li', {}, timeEl(t.timestamp), ` ${String(t.type).replaceAll('_', ' ')}: ${t.summary}`))),
    );
  }
  return h('details', { class: 'event-more' }, h('summary', { text: 'DETAIL' }), body);
}

// -------------------------------------------------------- event detail page

// Absent reference data renders as the word "unknown", never as a zero: a score we
// do not have and a score of 0 are different facts and must not look the same.
function hasNum(value) {
  return value !== null && value !== undefined;
}

function cveFacts(cve) {
  return {
    cvssScore: cve.cvss && hasNum(cve.cvss.score) ? String(cve.cvss.score) : 'unknown',
    cvssVector: cve.cvss && cve.cvss.vector ? String(cve.cvss.vector) : 'unknown',
    cvssSource: cve.cvss && cve.cvss.source ? String(cve.cvss.source) : 'unknown',
    epssScore: cve.epss && hasNum(cve.epss.score) ? String(cve.epss.score) : 'unknown',
    kev:
      cve.kev && cve.kev.listed
        ? `KEV LISTED${cve.kev.due_date ? `, DUE ${cve.kev.due_date}` : ''}`
        : 'NOT IN KEV',
  };
}

function utcStamp(iso) {
  return iso ? `${formatUtc(iso)} UTC` : 'unknown';
}

function unitRatio(value) {
  return hasNum(value) ? `${Math.round(value * 100)}%` : 'unknown';
}

function unitScore(value) {
  return hasNum(value) ? String(value) : 'unknown';
}

function kvList(pairs) {
  const dl = h('dl', { class: 'kv' });
  for (const [label, value] of pairs) {
    if (value === null || value === undefined || value === '') continue;
    dl.append(h('dt', { text: label }), h('dd', { class: 'mono', text: String(value) }));
  }
  return dl;
}

function listOf(label, items) {
  if (!items || !items.length) return null;
  return h(
    'div',
    { class: 'detail-list' },
    h('h3', { class: 'detail-label', text: label }),
    h('ul', {}, items.map((x) => h('li', { text: x }))),
  );
}

function detailBlock(title, ...children) {
  return h('section', { class: 'detail-block' }, h('h2', { class: 'subhead', text: title }), ...children.filter(Boolean));
}

function sourceReportList(sources) {
  if (!sources.length) return [h('p', { class: 'empty', text: 'NO SOURCE REPORTS.' })];
  return [
    h('h3', { class: 'detail-label', text: 'SOURCE REPORTS' }),
    h(
      'ul',
      { class: 'source-reports' },
      sources.map((src) => {
        const href = safeUrl(src.url);
        const label = `${src.source_id} · ${src.evidence_class}`;
        return h(
          'li',
          { class: 'source-report' },
          href
            ? h('a', { href, rel: 'noopener noreferrer nofollow', target: '_blank', text: label })
            : h('span', { text: label }),
          h('span', { class: 'chip', text: src.independent ? 'INDEPENDENT' : 'NOT INDEPENDENT' }),
          src.published ? timeEl(src.published) : null,
          // Its own lineage says nothing; another's says whose report this one repeats.
          src.lineage_id && src.lineage_id !== src.source_id
            ? h('span', { class: 'mono source-report__lineage', text: `LINEAGE ${src.lineage_id}` })
            : null,
        );
      }),
    ),
  ];
}

function claimList(claims) {
  if (!claims || !claims.length) return null;
  return h(
    'div',
    { class: 'detail-list' },
    h('h3', { class: 'detail-label', text: 'CLAIMS (WITH EVIDENCE REFERENCES)' }),
    h(
      'ul',
      { class: 'claims' },
      claims.map((c) =>
        h(
          'li',
          { class: 'claim' },
          h('span', { class: 'claim__text', text: c.text }),
          h('span', { class: 'chip', text: `CONFIDENCE ${unitRatio(c.confidence)}` }),
          h('span', { class: 'mono claim__ev', text: (c.evidence || []).length ? `EVIDENCE: ${c.evidence.join(', ')}` : 'EVIDENCE: unknown' }),
        ),
      ),
    ),
  );
}

function cveBlock(cves) {
  if (!cves || !cves.length) return null;
  return h(
    'div',
    { class: 'detail-list' },
    h('h3', { class: 'detail-label', text: 'VULNERABILITIES' }),
    h(
      'ul',
      { class: 'cve-list' },
      cves.map((cve) => {
        const f = cveFacts(cve);
        return h(
          'li',
          { class: 'cve-row' },
          h('span', { class: 'chip mono', text: cve.id }),
          h('span', { class: 'chip', text: `CVSS ${f.cvssScore}` }),
          h('span', { class: 'chip', text: `EPSS ${f.epssScore}` }),
          h('span', { class: cve.kev?.listed ? 'chip chip--kev' : 'chip', text: f.kev }),
          h('span', { class: 'mono cve-row__vector', text: `VECTOR ${f.cvssVector} · SOURCE ${f.cvssSource}` }),
          advisoryList(cve.advisories),
        );
      }),
    ),
  );
}

// OSV and GitHub advisories: which packages are affected and which versions fix them. GitHub's
// rating is its own, shown beside the CVSS chain rather than in it.
function advisoryList(advisories) {
  if (!advisories || !advisories.length) return null;
  return h(
    'ul',
    { class: 'advisory-list' },
    advisories.map((a) => {
      const href = safeUrl(a.url);
      const label = String(a.id);
      const rating = a.severity
        ? `${a.source === 'ghsa' ? 'GITHUB' : 'OSV'} ${String(a.severity).toUpperCase()}${a.reviewed ? '' : ' · UNREVIEWED'}`
        : null;
      return h(
        'li',
        { class: 'advisory-row' },
        href
          ? h('a', { class: 'mono', href, rel: 'noopener noreferrer nofollow', target: '_blank', text: label })
          : h('span', { class: 'mono', text: label }),
        rating ? h('span', { class: 'chip', text: rating }) : null,
        (a.packages || []).map((pkg) =>
          h('span', {
            class: 'mono advisory-row__pkg',
            text: `${pkg.ecosystem} ${pkg.name} · ${pkg.fixed && pkg.fixed.length ? `FIXED IN ${pkg.fixed.join(', ')}` : 'NO FIX YET'}`,
          }),
        ),
      );
    }),
  );
}

function mitreList(techniques) {
  if (!techniques.length) return null;
  const atlas = techniques.some((m) => String(m.id).startsWith('AML.'));
  const attack = techniques.some((m) => !String(m.id).startsWith('AML.'));
  const matrices = [attack ? 'ATT&CK' : null, atlas ? 'ATLAS' : null].filter(Boolean).join(' / ');
  return h(
    'div',
    { class: 'detail-list' },
    h('h3', { class: 'detail-label', text: `MITRE ${matrices} TECHNIQUES` }),
    h(
      'ul',
      { class: 'mitre-list' },
      techniques.map((m) =>
        h(
          'li',
          { class: 'mitre-row' },
          h('span', { class: 'mono mitre-row__id', text: m.id }),
          h('span', { text: m.name }),
          m.confidence_type === 'ai_suggested'
            ? h('span', { class: 'chip chip--ai', text: 'AI SUGGESTED' })
            : h('span', { class: 'chip', text: String(m.confidence_type || 'unknown').toUpperCase().replace(/_/g, ' ') }),
          h('span', { class: 'chip', text: `CONFIDENCE ${unitRatio(m.confidence)}` }),
          m.dataset_version ? h('span', { class: 'mono mitre-row__set', text: m.dataset_version }) : null,
        ),
      ),
    ),
  );
}

const SYDNEY_DAY = new Intl.DateTimeFormat('en-AU', {
  timeZone: 'Australia/Sydney',
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  year: 'numeric',
});

function sydneyDayLabel(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return 'unknown';
  return SYDNEY_DAY.format(d);
}

// Timeline: tick-marked axis, hex nodes, grouped by Sydney day. Every node takes
// focus, so the sequence can be walked with the keyboard as well as read.
export function renderTimeline(event) {
  const wrap = h('section', { class: 'timeline' }, h('h2', { class: 'subhead', text: 'TIMELINE' }));
  const entries = [...(event.timeline || [])].sort((a, b) =>
    String(a.timestamp || '').localeCompare(String(b.timestamp || '')),
  );
  if (!entries.length) {
    wrap.append(h('p', { class: 'empty', text: 'NO TIMELINE ENTRIES YET.' }));
    return wrap;
  }
  const days = new Map();
  for (const entry of entries) {
    const key = sydneyDayLabel(entry.timestamp);
    if (!days.has(key)) days.set(key, []);
    days.get(key).push(entry);
  }
  for (const [day, items] of days) {
    wrap.append(
      h(
        'div',
        { class: 'timeline__day' },
        h('h3', { class: 'timeline__dayhead', text: day }),
        h(
          'ol',
          { class: 'timeline__list' },
          items.map((t) =>
            h(
              'li',
              { class: 'timeline__node', tabindex: '0', 'data-type': t.type },
              h('span', { class: 'timeline__hex', 'aria-hidden': 'true' }),
              timeEl(t.timestamp),
              h('span', { class: 'chip', text: String(t.type).replace(/_/g, ' ') }),
              h('span', { class: 'timeline__summary', text: t.summary }),
              (t.sources || []).length
                ? h('span', { class: 'mono timeline__src', text: `SOURCES: ${t.sources.join(', ')}` })
                : null,
            ),
          ),
        ),
      ),
    );
  }
  return wrap;
}

// The full §5 field set for one event, with the evidence column physically separate
// from the AI inference column: two panels, different accents, different headings.
export function renderEventDetail(event, related = []) {
  const key = sevKey(event);
  const beat = beatOf(event);
  const au = event.au || {};
  const entities = event.entities || {};
  const severitySource = String(event.severity_source || 'unknown').toUpperCase().replace(/_/g, ' ');
  const imp = importanceOf(event);
  const root = h('div', { class: 'detail', 'data-severity': key });

  // append(), not root.append(): the optional rows below are null when the field is absent, and
  // the native append stringifies null into a literal "null" line under the summary.
  append(root, [
    h('h1', { class: 'detail__title', text: event.title }),
    h(
      'div',
      { class: 'detail__meta' },
      importanceBadge(event),
      rankBadge(event),
      beat === 'both' && event.ai_significance ? significanceBadge(significanceKey(event)) : null,
      // An AI-only story is ranked by significance and has no severity to source.
      beat === 'ai' ? null : h('span', { class: 'chip', text: `SEVERITY VIA ${severitySource}` }),
      h('span', { class: 'chip', text: String(event.status || 'unknown').toUpperCase() }),
      h('span', { class: 'chip chip--beat', 'data-beat': beat, text: BEAT_LABEL[beat] }),
      au.directly_reported_in_au ? h('span', { class: 'chip chip--au', text: 'REPORTED IN AU' }) : null,
      h('span', { class: 'mono detail__id', text: event.event_id }),
    ),
    event.summary ? h('p', { class: 'detail__summary', text: event.summary }) : null,
    imp ? h('p', { class: 'detail__why detail__imp' }, h('strong', { text: `WHY THIS RATES ${IMPORTANCE[imp.tier].label}: ` }), importanceWhy(imp)) : null,
    event.why_it_matters
      ? h('p', { class: 'detail__why' }, h('strong', { text: 'WHY IT MATTERS: ' }), event.why_it_matters)
      : null,
    event.resolution
      ? h('p', { class: 'detail__why' }, h('strong', { text: 'RESOLUTION: ' }), event.resolution)
      : null,
  ]);

  const entityLists = [
    listOf('ACTORS', entities.actors),
    listOf('ORGANISATIONS', entities.organisations),
    listOf('PRODUCTS', entities.products),
    listOf('COUNTRIES', entities.countries),
    listOf('INDUSTRIES', entities.industries),
  ].filter(Boolean);
  if (!entityLists.length) entityLists.push(h('p', { class: 'empty', text: 'NONE RECORDED.' }));

  root.append(
    h(
      'div',
      { class: 'detail-grid' },
      detailBlock(
        'KEY TIMES',
        kvList([
          ['FIRST SEEN (AEST)', formatSydney(event.first_seen)],
          ['FIRST SEEN (UTC)', utcStamp(event.first_seen)],
          ['LAST SEEN (AEST)', formatSydney(event.last_seen)],
          ['LAST SEEN (UTC)', utcStamp(event.last_seen)],
          ['LAST MATERIAL UPDATE (AEST)', formatSydney(event.last_material_update)],
          // CONFIRMATION is wider than the label column, so it has to break somewhere. The
          // soft hyphen (U+00AD) puts the break where a typesetter would and renders a real
          // hyphen; left to itself the browser drops a bare "N" on the next line, which
          // reads as a typo. A dictionary-based hyphens: auto would not work here, because
          // the hyphenation dictionary is not present in every browser build.
          ['INDEPENDENT CONFIRMA­TION (UTC)', utcStamp(event.last_independent_confirmation)],
        ]),
      ),
      detailBlock(
        'CLASSIFICATION',
        kvList([
          ['DOMAINS', (event.domains || []).join(', ')],
          ['CATEGORIES', (event.categories || []).join(', ')],
          ['AI SUBDOMAIN', event.ai_subdomain || 'none'],
          ['STATUS', String(event.status || 'unknown').toUpperCase()],
          [
            'VERSIONS',
            `schema ${event.schema_version} · pipeline ${event.pipeline_version} · scoring ${event.scoring_version} · enrichment ${event.enrichment_version}`,
          ],
        ]),
      ),
      detailBlock(
        'RISK (DETERMINISTIC SCORING)',
        kvList([
          ['URGENCY', unitRatio(event.risk?.urgency)],
          ['CONFIDENCE', unitRatio(event.risk?.confidence)],
          ['NOVELTY', unitRatio(event.risk?.novelty)],
          ['PROMINENCE', unitRatio(event.risk?.prominence)],
        ]),
        h('p', { class: 'hint', text: 'Computed by the scoring rules in config/scoring.yaml. Not a model judgement.' }),
      ),
      detailBlock(
        'AUSTRALIAN RELEVANCE',
        kvList([
          ['RELEVANCE', unitRatio(au.relevance)],
          ['DIRECTLY REPORTED IN AU', au.directly_reported_in_au ? 'yes' : 'no'],
        ]),
        listOf('REASONS', au.reasons),
        listOf('SECTORS', au.sectors),
        listOf('SOCI SECTORS', au.soci_asset_classes),
        h('p', { class: 'hint', text: 'Set from what the sources said by the AU rules in config/scoring.yaml. A model reading may raise it, never lower it.' }),
      ),
      detailBlock('ENTITIES', ...entityLists),
    ),
  );

  // Evidence and inference: separate panels, side by side, always both present.
  const sources = event.sources || [];
  const evidenceSources = sources.filter((x) => x.evidence_class !== 'AI_INFERENCE');
  const aiSources = sources.filter((x) => x.evidence_class === 'AI_INFERENCE');
  const mitre = event.mitre_techniques || [];
  const aiMitre = mitre.filter((m) => m.confidence_type === 'ai_suggested');
  const referenceMitre = mitre.filter((m) => m.confidence_type !== 'ai_suggested');

  const evidenceCol = h(
    'section',
    { class: 'detail-col detail-col--ev' },
    h('h2', { class: 'detail-col__head', text: 'EVIDENCE AND REFERENCE DATA' }),
    h('p', {
      class: 'hint',
      text: 'Source reports, claims and published vulnerability data behind this event. Everything here can be checked against the original.',
    }),
    ...sourceReportList(evidenceSources),
    claimList(event.claims),
    cveBlock(event.cves),
    mitreList(referenceMitre),
  );

  const aiKids = [];
  if (event.severity_source === 'ai_estimate') {
    aiKids.push(
      h(
        'p',
        { class: 'detail-note' },
        h('span', { class: 'chip chip--ai', text: 'AI SUGGESTED' }),
        ' SEVERITY IS AN AI ESTIMATE, NOT A VENDOR OR SCORER FIGURE.',
      ),
    );
  }
  aiKids.push(mitreList(aiMitre));
  if (aiSources.length) aiKids.push(...sourceReportList(aiSources));
  if (!aiKids.filter(Boolean).length) {
    aiKids.push(
      h('p', {
        class: 'empty',
        text: 'NO AI INFERENCE IS ATTACHED TO THIS EVENT. THIS COLUMN STAYS VISIBLE SO EVIDENCE AND INFERENCE ARE NEVER BLENDED TOGETHER.',
      }),
    );
  }

  const aiCol = h(
    'section',
    { class: 'detail-col detail-col--ai' },
    h(
      'h2',
      { class: 'detail-col__head' },
      h('span', { class: 'chip chip--ai', text: 'AI SUGGESTED' }),
      h('span', { text: 'AI INFERENCE — NOT EVIDENCE' }),
    ),
    h('p', {
      class: 'hint',
      text: 'Model-derived output, kept apart from the evidence so it can be ignored, questioned or corrected on its own.',
    }),
    ...aiKids.filter(Boolean),
  );

  root.append(h('div', { class: 'detail-cols' }, evidenceCol, aiCol));

  // Timeline
  root.append(renderTimeline(event));

  // Related events: declared relationships first, then events sharing a CVE.
  const relatedBy = new Map((related || []).map((e) => [e.event_id, e]));
  const relItems = [];
  for (const rel of event.relationships || []) {
    const target = relatedBy.get(rel.event_id);
    relItems.push(
      h(
        'li',
        {},
        h('a', { href: `event.html?id=${rel.event_id}`, text: `${String(rel.type).replace(/_/g, ' ')}: ${rel.event_id}` }),
        target
          ? h('span', { class: 'related-title', text: ` — ${target.title}` })
          : h('span', { class: 'hint', text: ' — not in a published snapshot' }),
      ),
    );
    relatedBy.delete(rel.event_id);
  }
  for (const e of relatedBy.values()) {
    relItems.push(
      h(
        'li',
        {},
        h('a', { href: `event.html?id=${e.event_id}`, text: e.title }),
        h('span', { class: 'mono', text: ` ${e.event_id}` }),
        ' ',
        severityBadge(sevKey(e)),
        h('span', { class: 'chip', text: 'SHARED CVE' }),
      ),
    );
  }
  root.append(
    h(
      'section',
      { class: 'detail-block' },
      h('h2', { class: 'subhead', text: 'RELATED EVENTS' }),
      relItems.length
        ? h('ul', { class: 'related-events' }, relItems)
        : h('p', { class: 'empty', text: 'NO RELATED EVENTS RECORDED.' }),
    ),
  );

  root.append(
    detailBlock(
      'TAGS',
      (event.tags || []).length
        ? h('div', { class: 'event-card__tags' }, event.tags.map((t) => h('span', { class: 'chip', text: t })))
        : h('p', { class: 'empty', text: 'NO TAGS.' }),
    ),
  );

  if (event.pending_enrichment) {
    root.append(
      h('p', {
        class: 'notice',
        text: 'ENRICHMENT IS PENDING FOR THIS EVENT: MORE REFERENCE DATA MAY ARRIVE IN A LATER SNAPSHOT.',
      }),
    );
  }
  return root;
}

// ---------------------------------------------------------------- filters

// The tag filters. The scope (AUSTRALIA / GLOBAL / ALL) and the beat (CYBER / AI) are not in
// here: they have their own rows in the header, because every view reads through them. The AI
// beat's own facets are AI SIGNIFICANCE and AI SUBDOMAIN, which only AI stories carry.
// IMPORTANCE leads because it is the question most readers start with; KEY ONLY in the header is
// the one-click form of its KEY chip. A story with no published score has no IMPORTANCE token.
const DIMENSIONS = [
  ['importance', 'IMPORTANCE'],
  ['severity', 'SEVERITY'],
  ['significance', 'AI SIGNIFICANCE'],
  ['au', 'AUSTRALIA'],
  ['category', 'CATEGORY'],
  ['aidomain', 'AI SUBDOMAIN'],
  ['source', 'SOURCE'],
  ['cve', 'CVE'],
  ['country', 'COUNTRY'],
  ['org', 'ORGANISATION'],
  ['product', 'PRODUCT'],
  ['sector', 'SECTOR'],
  ['actor', 'ACTOR'],
  ['mitre', 'MITRE'],
];

export function facetTokens(event) {
  const imp = importanceOf(event);
  const out = imp ? [['importance', imp.tier]] : [];
  out.push(['severity', sevKey(event)]);
  if (Object.hasOwn(SIGNIFICANCE, event.ai_significance ?? '')) out.push(['significance', event.ai_significance]);
  if (event.au?.directly_reported_in_au) out.push(['au', 'REPORTED IN AU']);
  if ((event.au?.relevance ?? 0) >= 0.5) out.push(['au', 'AU RELEVANT']);
  for (const c of event.categories || []) out.push(['category', c]);
  if (event.ai_subdomain) out.push(['aidomain', String(event.ai_subdomain).toUpperCase()]);
  for (const src of event.sources || []) out.push(['source', src.source_id]);
  for (const c of event.cves || []) out.push(['cve', c.id]);
  for (const c of event.entities?.countries || []) out.push(['country', c]);
  for (const o of event.entities?.organisations || []) out.push(['org', o]);
  for (const p of event.entities?.products || []) out.push(['product', p]);
  for (const x of [...(event.au?.sectors || []), ...(event.entities?.industries || [])]) out.push(['sector', x]);
  for (const a of event.entities?.actors || []) out.push(['actor', a]);
  for (const m of event.mitre_techniques || []) out.push(['mitre', m.id]);
  return out;
}

// What a chip says. The value in the URL stays the raw token, so a link keeps working when a
// label is reworded.
function facetLabel(dim, value) {
  if (dim === 'importance') return IMPORTANCE[value]?.label || String(value).toUpperCase();
  if (dim === 'aidomain') return String(value).replaceAll('_', ' ');
  if (dim === 'significance') return SIGNIFICANCE[value]?.label || String(value).toUpperCase();
  if (dim === 'category' && Object.hasOwn(CATEGORY_LABEL, value)) return CATEGORY_LABEL[value];
  return String(value);
}

export function buildFacets(events) {
  const facets = new Map(DIMENSIONS.map(([dim]) => [dim, new Map()]));
  for (const event of events) {
    const seen = new Set();
    for (const [dim, value] of facetTokens(event)) {
      const key = `${dim}\u0000${value}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const bucket = facets.get(dim);
      bucket.set(value, (bucket.get(value) || 0) + 1);
    }
  }
  return facets;
}

// state: { events, selected: { [dimension]: Set|Array of values } }
// Within a dimension any selected value matches (OR); dimensions combine with AND.
// There was a free-text box here as well. A snapshot of one day's events is small enough to
// read, the tag facets already cover every dimension a reader would type into it, and a
// substring match over titles is a worse instrument than a facet: it silently misses a
// synonym and gives no clue that it did.
export function applyFilters(state) {
  const active = Object.entries(state.selected || {})
    .map(([dim, values]) => [dim, new Set(values)])
    .filter(([, values]) => values.size > 0);
  return (state.events || []).filter((event) => {
    if (!active.length) return true;
    const tokens = facetTokens(event);
    return active.every(([dim, values]) => tokens.some(([d, v]) => d === dim && values.has(v)));
  });
}

const FACET_CHIPS = 40;
// Chips are listed by count, except where the values are a scale: KEY, NOTABLE, ROUTINE reads
// in that order whatever the counts are.
const FACET_ORDER = { importance: Object.keys(IMPORTANCE) };
// Which dimensions the reader has open. The chips are rebuilt on every change of scope, beat or
// tag (their counts follow the scope), and a rebuild must not fold away what the reader opened.
const openFacets = new Set(['importance', 'severity']);

// `tags` is the state's { dim: [values] }; onToggle(dim, value) changes it. The counts are for
// the events the current scope and beat leave, so a chip never promises more than it gives.
function renderFacetUi(facets, tags, onToggle) {
  const host = document.getElementById('filter-facets');
  if (!host) return;
  const focused = document.activeElement?.closest?.('#filter-facets button[data-dim]');
  const refocus = focused ? { dim: focused.dataset.dim, value: focused.dataset.value } : null;
  clear(host);
  for (const [dim, label] of DIMENSIONS) {
    const bucket = new Map(facets.get(dim) || []);
    const chosen = tags[dim] || [];
    // A chosen value stays listed when the scope leaves none of it, at its honest count of
    // zero; otherwise the only way to undo it would be CLEAR FILTERS.
    for (const value of chosen) if (!bucket.has(value)) bucket.set(value, 0);
    if (!bucket.size) continue;
    const order = FACET_ORDER[dim];
    const entries = [...bucket.entries()].sort(
      order
        ? (a, b) => order.indexOf(a[0]) - order.indexOf(b[0])
        : (a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])),
    );
    const shown = entries.slice(0, FACET_CHIPS);
    for (const entry of entries.slice(FACET_CHIPS)) if (chosen.includes(entry[0])) shown.push(entry);
    const chips = h('div', { class: 'facet__chips' });
    for (const [value, count] of shown) {
      const btn = h('button', {
        type: 'button',
        class: 'chip facet-chip',
        'data-dim': dim,
        'data-value': value,
        'aria-pressed': chosen.includes(value) ? 'true' : 'false',
        text: `${facetLabel(dim, value)} (${count})`,
      });
      btn.addEventListener('click', () => onToggle(dim, value));
      chips.append(btn);
    }
    const facet = h(
      'details',
      { class: 'facet', 'data-facet': dim, open: openFacets.has(dim) || chosen.length > 0 },
      h('summary', { text: `${label} · ${bucket.size}${chosen.length ? ` · ${chosen.length} ON` : ''}` }),
      chips,
    );
    facet.addEventListener('toggle', () => {
      if (facet.open) openFacets.add(dim);
      else openFacets.delete(dim);
    });
    host.append(facet);
  }
  if (refocus) {
    [...host.querySelectorAll('button[data-dim]')]
      .find((b) => b.dataset.dim === refocus.dim && b.dataset.value === refocus.value)
      ?.focus();
  }
}

// ------------------------------------------------------------------ index

export function computeIndex(events) {
  let x = 0;
  for (const e of events) {
    const w = SEVERITY_WEIGHT[sevKey(e)];
    x += w * (0.5 + (e.risk?.prominence ?? 0.3)) * (e.au?.directly_reported_in_au ? 1.5 : 1);
  }
  return Math.round((100 * x) / (x + 60));
}

const BANDS = [
  { max: 20, label: 'QUIET', sev: 'info' },
  { max: 40, label: 'STEADY', sev: 'low' },
  { max: 60, label: 'ACTIVE', sev: 'medium' },
  { max: 80, label: 'BUSY', sev: 'high' },
  { max: 101, label: 'SURGE', sev: 'critical' },
];

export function hourlyVolume(events, endIso, hours = 24) {
  const end = new Date(endIso || Date.now()).getTime();
  const buckets = new Array(hours).fill(0);
  for (const e of events) {
    const t = new Date(e.last_material_update || e.first_seen).getTime();
    const ago = Math.floor((end - t) / 3600000);
    if (ago >= 0 && ago < hours) buckets[hours - 1 - ago] += 1;
  }
  return buckets;
}

function ekgPath(buckets) {
  const peak = Math.max(1, ...buckets);
  let d = 'M0 40';
  buckets.forEach((count, i) => {
    const x = i * 10 + 5;
    if (!count) {
      d += ` L${x} 40`;
      return;
    }
    const up = 6 + (count / peak) * 28;
    d += ` L${x - 3} 40 L${x - 2} 44 L${x} ${(40 - up).toFixed(1)} L${x + 2} 46 L${x + 3} 40`;
  });
  return `${d} L240 40`;
}

export function renderIndex(data) {
  const host = document.getElementById('cp-index');
  if (!host) return;
  const events = data.events || [];
  const value = computeIndex(events);
  const band = BANDS.find((b) => value < b.max);
  const sev = SEVERITY[band.sev];
  const end = data.feed?.last_completed_collection || data.feed?.generated_at;
  const buckets = hourlyVolume(events, end);
  const total = buckets.reduce((a, b) => a + b, 0);
  const d = ekgPath(buckets);
  const svg = s(
    'svg',
    {
      class: 'ekg',
      viewBox: '0 0 240 56',
      preserveAspectRatio: 'none',
      role: 'img',
      'aria-label': `Event volume by hour for the 24 hours before the last completed collection: ${plural(total, 'event')}, busiest hour ${Math.max(0, ...buckets)}.`,
    },
    s('path', { class: 'ekg-base', d }),
    s('path', { class: 'ekg-pulse', d, pathLength: 100 }),
  );
  clear(host).append(
    h(
      'div',
      { class: 'cp-index', 'data-severity': band.sev },
      h('div', { class: 'cp-index__label', text: 'CYBERPULSE INDEX' }),
      h(
        'div',
        { class: 'cp-index__value' },
        h('span', { class: 'cp-index__num', text: String(value) }),
        h('span', { class: 'cp-index__band' }, h('span', { 'aria-hidden': 'true', text: `${sev.glyph} ` }), band.label),
      ),
      svg,
      h('div', { class: 'cp-index__note', text: 'OUR OWN COMPOSITE OF SEVERITY, PROMINENCE AND AUSTRALIAN RELEVANCE. NOT AN OFFICIAL SCALE. PULSE LINE: EVENTS PER HOUR, LAST 24 H.' }),
    ),
  );
}

// The CyberPulse Index gauge and its 24-hour EKG pulse line — the §8.3 component
// name for the same renderer, so pages can import either name.
export const renderPulseIndex = renderIndex;

// ----------------------------------------------------------- gauges, radar

const ARC = 'M21.72 78.28 A40 40 0 1 1 78.28 78.28';

export function renderGauges(data) {
  const host = document.getElementById('gauges');
  if (!host) return;
  clear(host);
  const events = data.events || [];
  const total = events.length;
  for (const key of ['critical', 'high', 'medium', 'low', 'unknown']) {
    const count = events.filter((e) => sevKey(e) === key).length;
    const pct = total ? Math.round((count / total) * 100) : 0;
    const sev = SEVERITY[key];
    host.append(
      h(
        'div',
        { class: 'gauge', 'data-severity': key },
        s(
          'svg',
          { viewBox: '0 0 100 100', role: 'img', 'aria-label': `${sev.label}: ${count} of ${total} events, ${pct} per cent` },
          s('path', { class: 'gauge-track', d: ARC, pathLength: 100 }),
          s('path', { class: 'gauge-arc', d: ARC, pathLength: 100, 'stroke-dasharray': `${pct} 100` }),
          s('text', { class: 'gauge-num', x: 50, y: 58 }, String(count)),
        ),
        h('div', { class: 'gauge__label' }, severityBadge(key)),
      ),
    );
  }
}

function hashUnit(text) {
  let x = 2166136261;
  for (let i = 0; i < text.length; i += 1) x = Math.imul(x ^ text.charCodeAt(i), 16777619);
  return ((x >>> 0) % 1000) / 1000;
}

const RING = { critical: 12, high: 24, medium: 35, low: 44, info: 44, unknown: 44 };

function blipShape(key, x, y, r) {
  const attrs = { class: 'radar-blip', 'data-severity': key, 'data-severity-shape': SEVERITY[key].shape };
  switch (key) {
    case 'critical':
      return s('polygon', { ...attrs, points: `${x},${y - r} ${x + r},${y} ${x},${y + r} ${x - r},${y}` });
    case 'high':
      return s('polygon', { ...attrs, points: `${x},${y - r} ${x + r},${y + r * 0.8} ${x - r},${y + r * 0.8}` });
    case 'low':
      return s('rect', { ...attrs, x: x - r * 0.8, y: y - r * 0.8, width: r * 1.6, height: r * 1.6 });
    default:
      return s('circle', { ...attrs, cx: x, cy: y, r: key === 'medium' ? r : r * 0.8 });
  }
}

export function renderRadar(data) {
  const host = document.getElementById('radar-slot');
  if (!host) return;
  const events = [...(data.events || [])].sort(byProminence).slice(0, 80);
  const categories = [...new Set(events.map((e) => e.categories?.[0] || 'uncategorised'))].sort();
  const svg = s('svg', { viewBox: '0 0 100 100', 'aria-hidden': 'true' });
  for (const r of [12, 24, 35, 44]) svg.append(s('circle', { class: 'radar-ring', cx: 50, cy: 50, r }));
  svg.append(s('line', { class: 'radar-axis', x1: 50, y1: 4, x2: 50, y2: 96 }), s('line', { class: 'radar-axis', x1: 4, y1: 50, x2: 96, y2: 50 }));
  const sector = 360 / Math.max(1, categories.length);
  for (const e of events) {
    const key = sevKey(e);
    const cat = categories.indexOf(e.categories?.[0] || 'uncategorised');
    const angle = ((cat + 0.5) * sector + (hashUnit(e.event_id) - 0.5) * sector * 0.7) * (Math.PI / 180);
    const radius = RING[key] + (hashUnit(`${e.event_id}r`) - 0.5) * 5;
    const x = +(50 + radius * Math.sin(angle)).toFixed(2);
    const y = +(50 - radius * Math.cos(angle)).toFixed(2);
    if (e.au?.directly_reported_in_au) svg.append(s('circle', { class: 'radar-au-ring', cx: x, cy: y, r: 3.6 }));
    svg.append(blipShape(key, x, y, 2));
  }
  const auCount = events.filter((e) => e.au?.directly_reported_in_au).length;
  const radar = h(
    'div',
    { class: 'radar', role: 'img', 'aria-label': `Threat radar: ${plural(events.length, 'event')} plotted by severity ring and category angle, ${auCount} reported in Australia.` },
    svg,
    h('div', { class: 'radar-sweep', 'aria-hidden': 'true' }),
  );
  clear(host).append(
    h('div', { class: 'cp-index__label', text: 'THREAT RADAR' }),
    radar,
    h('p', { class: 'radar-legend', text: 'RING = SEVERITY (CENTRE IS CRITICAL). ANGLE = CATEGORY. DOUBLE RING = REPORTED IN AUSTRALIA.' }),
  );
}

// ----------------------------------------------------------------- trends

// trends.json is counted from stored reports, never estimated (worker/pipeline/trends.py).
// A state is always a word and a glyph, so none of this rests on colour.
export const TREND_STATE = {
  new: { glyph: '✦', label: 'NEW' },
  rising: { glyph: '▲', label: 'RISING' },
  steady: { glyph: '●', label: 'STEADY' },
  falling: { glyph: '▼', label: 'FALLING' },
  warming_up: { glyph: '◌', label: 'WARMING UP' },
};
// `ai` is the AI beat's topic kind. A trends.json published before the AI beat simply has no
// topic of that kind, so nothing here depends on it being present.
const TOPIC_KIND = { vendor: 'VENDOR', actor: 'ACTOR', malware: 'MALWARE', threat: 'THREAT', ai: 'AI' };
const DAY_COVERAGE = { full: 'WHOLE DAY', partial: 'PART OF THE DAY', none: 'NOT COLLECTED' };
const TREND_ROWS = 10;
const expandedTrends = new Set();

function hours(n) {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function trendState(state) {
  const key = TREND_STATE[state] ? state : 'steady';
  const info = TREND_STATE[key];
  return h('span', { class: 'trend-state', 'data-trend': key }, h('span', { 'aria-hidden': 'true', text: info.glyph }), info.label);
}

function trendEvents(ids, titles) {
  if (!ids?.length) return '—';
  return h(
    'ul',
    { class: 'trend-events' },
    ids.map((id) => h('li', {}, h('a', { href: `event.html?id=${encodeURIComponent(id)}`, text: titles.get(id) || id }))),
  );
}

function trendTable(caption, cols, rows) {
  return h(
    'div',
    { class: 'trend-scroll' },
    h(
      'table',
      { class: 'trend-table' },
      h('caption', { class: 'trend-caption', text: caption }),
      h('thead', {}, h('tr', {}, cols.map((c) => h('th', { scope: 'col', text: c })))),
      h('tbody', {}, rows.map((cells) => h('tr', {}, cells))),
    ),
  );
}

// The first TREND_ROWS rows, and a button for the rest, the way a section caps its cards.
function cappedRows(key, items, render) {
  const host = h('div', { class: 'trend-block' });
  const draw = () => {
    const open = expandedTrends.has(key);
    clear(host).append(render(open ? items : items.slice(0, TREND_ROWS)));
    if (items.length <= TREND_ROWS) return;
    const btn = h('button', {
      type: 'button',
      class: 'btn btn--small show-more',
      'aria-expanded': open ? 'true' : 'false',
      text: open ? 'SHOW FEWER' : `SHOW ALL ${items.length}`,
    });
    btn.addEventListener('click', () => {
      if (open) expandedTrends.delete(key);
      else expandedTrends.add(key);
      draw();
      host.querySelector('.show-more')?.focus();
    });
    host.append(btn);
  };
  draw();
  return host;
}

function trendCoverage(c) {
  if (!c.collecting_since) return 'No collection has run yet, so there is nothing to count.';
  const began = formatSydney(c.collecting_since);
  if (c.warming_up) {
    return `WARMING UP. Collection began ${began}. A trend compares the last ${hours(c.recent_hours)} hours with the per-day rate before them, and needs ${hours(c.baseline_hours_needed)} hours of that before anything is called rising or falling; there are ${hours(c.baseline_hours)} so far. Until then topics are counted, not judged.`;
  }
  const short = c.baseline_hours < c.baseline_hours_wanted;
  return `The last ${hours(c.recent_hours)} hours against the per-day rate over the ${hours(c.baseline_hours)} hours before them${short ? `, which is as far back as collection goes (it began ${began}; the full window is ${hours(c.baseline_hours_wanted)} hours)` : ''}.`;
}

// One bar per UTC day. Coverage is shape as well as tone: a whole day collected is a filled
// bar, part of one an outlined bar, and a day before collection began a flat line with no
// count, because nothing was counted on it.
function activityChart(days) {
  const step = 22;
  const base = 62;
  const counted = days.filter((d) => d.coverage !== 'none');
  const most = Math.max(0, ...counted.map((d) => d.stories));
  const peak = Math.max(1, most);
  const total = counted.reduce((n, d) => n + d.stories, 0);
  const svg = s('svg', {
    class: 'trend-chart',
    viewBox: `0 0 ${days.length * step} 78`,
    role: 'img',
    'aria-label': counted.length
      ? `New stories per UTC day over the last ${days.length} days: ${total} new ${total === 1 ? 'story' : 'stories'} on the ${plural(counted.length, 'day')} collected, the most on one day ${most}. ${plural(days.length - counted.length, 'day')} before collection began ${days.length - counted.length === 1 ? 'is' : 'are'} not counted. The table below has every figure.`
      : `Nothing was collected in the last ${days.length} days, so no day is counted.`,
  });
  days.forEach((d, i) => {
    const x = i * step;
    const mid = x + step / 2;
    const g = s('g', { class: 'trend-day', 'data-coverage': d.coverage });
    if (d.coverage === 'none') {
      g.append(s('line', { class: 'trend-day__gap', x1: x + 5, x2: x + step - 5, y1: base, y2: base }));
    } else {
      const height = d.stories ? Math.max(2, (d.stories / peak) * 42) : 0;
      g.append(
        s('rect', { class: 'trend-day__bar', x: x + 4, y: +(base - height).toFixed(1), width: step - 8, height: +height.toFixed(1) }),
        s('text', { class: 'trend-day__num', x: mid, y: +(base - height - 4).toFixed(1) }, String(d.stories)),
      );
    }
    g.append(s('text', { class: 'trend-day__date', x: mid, y: base + 12 }, d.date.slice(8)));
    svg.append(g);
  });
  return svg;
}

// AI STORIES appears only when the snapshot counts them. A trends.json from before the AI beat
// has no `ai_stories`, and a column of dashes would claim days nobody counted; a day that has
// the field elsewhere but not on this row still shows — rather than an invented zero.
function activityTable(days) {
  const ai = days.some((d) => d.ai_stories !== undefined);
  return trendTable(
    'Day by day, newest first. A day before collection began shows — rather than a zero; KEV additions are CISA’s own dates, so they are complete on every day.',
    ['DAY (UTC)', 'COLLECTED', 'NEW STORIES', 'REPORTS', 'CRITICAL / HIGH', 'AUSTRALIAN', ai ? 'AI STORIES' : null, 'ADDED TO KEV'].filter(Boolean),
    [...days].reverse().map((d) => {
      const n = (v) => (d.coverage === 'none' ? '—' : String(v));
      return [
        h('th', { scope: 'row', class: 'mono', text: d.date }),
        h('td', { text: DAY_COVERAGE[d.coverage] || d.coverage }),
        h('td', { class: 'mono', text: n(d.stories) }),
        h('td', { class: 'mono', text: n(d.reports) }),
        h('td', { class: 'mono', text: n(d.critical_high) }),
        h('td', { class: 'mono', text: n(d.au_stories) }),
        ai ? h('td', { class: 'mono', text: d.ai_stories === undefined || d.ai_stories === null ? '—' : n(d.ai_stories) }) : null,
        h('td', { class: 'mono', text: String(d.kev_added) }),
      ];
    }),
  );
}

function topicTable(topics, titles) {
  return trendTable(
    'Topics named in headlines, most reported in the last 24 hours first.',
    ['TOPIC', 'LAST 24 H', 'PER DAY BEFORE', 'CHANGE', 'STATE', 'MOST PROMINENT STORIES'],
    topics.map((t) => [
      h('th', { scope: 'row' }, h('span', { class: 'trend-topic', text: t.label }), h('span', { class: 'trend-kind mono', text: TOPIC_KIND[t.kind] || t.kind })),
      h('td', { class: 'mono', text: String(t.recent) }),
      h('td', { class: 'mono', text: t.baseline_per_day.toFixed(1) }),
      h('td', { class: 'mono', text: `×${t.ratio.toFixed(1)}` }),
      h('td', {}, trendState(t.state)),
      h('td', {}, trendEvents(t.event_ids, titles)),
    ]),
  );
}

function cveTable(cves, titles) {
  return trendTable(
    'CVEs named by the stories reported on, most reports first.',
    ['CVE', 'LAST 24 H', 'IN THE WINDOW', 'STORIES', 'CISA KEV', 'STATE', 'MOST PROMINENT STORIES'],
    cves.map((c) => [
      h('th', { scope: 'row', class: 'mono', text: c.cve_id }),
      h('td', { class: 'mono', text: String(c.recent) }),
      h('td', { class: 'mono', text: String(c.week) }),
      h('td', { class: 'mono', text: String(c.stories) }),
      h('td', {}, c.kev ? h('span', { class: 'chip chip--kev' }, h('span', { 'aria-hidden': 'true', text: '◆ ' }), 'LISTED') : 'NOT LISTED'),
      h('td', {}, trendState(c.state)),
      h('td', {}, trendEvents(c.event_ids, titles)),
    ]),
  );
}

// `unread` is the same third state renderSections has: no snapshot was read, which is not the
// same claim as a snapshot that carries no trends.json (one published before trends existed).
export function renderTrends(trends, events = [], { unread = false } = {}) {
  trending.clear();
  const host = document.getElementById('trends');
  const count = document.getElementById('trends-count');
  const led = document.getElementById('trends-led');
  if (!host) return;
  clear(host);
  const readable = trends && trends.coverage && Array.isArray(trends.activity) && Array.isArray(trends.topics) && Array.isArray(trends.cves);
  if (!readable) {
    host.append(h('p', { class: 'empty', text: unread ? 'UNKNOWN — NO SNAPSHOT WAS READ.' : 'NO TRENDS WERE PUBLISHED WITH THIS SNAPSHOT.' }));
    if (count) count.textContent = 'AWAITING DATA';
    if (led) led.dataset.state = 'idle';
    return;
  }
  for (const t of trends.topics) {
    if (t.state === 'new' || t.state === 'rising') for (const id of t.event_ids || []) trending.add(id);
  }
  const titles = new Map(events.map((e) => [e.event_id, e.title]));
  const { topics, cves, activity } = trends;
  if (count) count.textContent = `${topics.length} ${topics.length === 1 ? 'TOPIC' : 'TOPICS'}`;
  if (led) led.dataset.state = topics.length ? 'ok' : 'idle';
  host.append(
    h('p', { class: 'hint', text: trendCoverage(trends.coverage) }),
    h('p', {
      class: 'hint',
      text: 'Every figure is a count of independent reports since collection began: copies of one wire story count once, and no figure is a model’s estimate. A report counts toward a topic when its own headline names it. CHANGE compares the last 24 hours with the rate before them, softened by one so that a single report on a quiet topic does not read as a surge.',
    }),
    h('h4', { class: 'subhead', text: `NEW STORIES PER DAY, LAST ${activity.length} DAYS (UTC)` }),
    activityChart(activity),
    h('p', { class: 'trend-legend', text: 'FILLED BAR: WHOLE DAY COLLECTED. OUTLINED BAR: PART OF THE DAY. FLAT LINE: BEFORE COLLECTION BEGAN, NOT COUNTED.' }),
    h('details', { class: 'trend-days' }, h('summary', { text: 'DAY BY DAY' }), activityTable(activity)),
    h('h4', { class: 'subhead', text: 'TOPICS' }),
    topics.length
      ? cappedRows('topics', topics, (rows) => topicTable(rows, titles))
      : h('p', { class: 'empty', text: 'NO HEADLINE IN THESE WINDOWS NAMED A TRACKED TOPIC.' }),
    h('h4', { class: 'subhead', text: 'MOST-REPORTED CVES' }),
    cves.length
      ? cappedRows('cves', cves, (rows) => cveTable(rows, titles))
      : h('p', { class: 'empty', text: 'NO STORY REPORTED ON IN THESE WINDOWS NAMES A CVE.' }),
  );
}

// -------------------------------------------------------------------- crew

const CREW_JOBS = {
  MORPHEUS: { owns: 'Editorial direction: what leads, what is held, and the daily brief', run: () => null },
  DECKARD: { owns: 'Research and follow-up: the context behind an event, and keeping a developing one open until it is patched or closed', run: () => null },
  VOIGHT: {
    owns: 'The publication veto and the publish: every critical and high claim is checked or held, then the snapshot this page reads goes out',
    run: (run, feed) => (feed?.counts?.events === undefined ? null : `${plural(feed.counts.events, 'event')} published in this snapshot`),
  },
  TACHIKOMA: { owns: 'Finding sources we do not have yet, and proposing them for review', run: () => null },
  WHEELJACK: { owns: 'Writing and repairing the collectors and parsers this pipeline runs on', run: () => null },
  TELETRAAN: {
    owns: 'Watching the estate, checking every code change, and opening an incident before a reader notices',
    run: (run) => (run ? (run.error_count ? `${plural(run.error_count, 'error')} recorded during the run` : 'No errors recorded during the run') : null),
  },
  RIPPERDOC: {
    owns: 'The US$20/month ceiling and the spend ledger, and assigning every agent its model: cheapest capable, free where possible, never above US$1/M output — re-checked daily, and swapped immediately if a model starts failing an agent',
    run: () => null,
  },
  SERAPH: {
    owns: 'Collection: the source gate, fetching every source, matching items to events, and the KEV, CVE, EPSS, OSV and ATT&CK ground truth, looked up and never generated',
    run: (run, feed) => {
      const pending = feed?.counts?.pending_enrichment;
      const queue = pending === undefined ? null : pending ? `${plural(pending, 'event')} queued for enrichment` : 'nothing queued for enrichment';
      if (!run) return queue ? `${queue[0].toUpperCase()}${queue.slice(1)}` : null;
      const bad = run.sources_failed + run.sources_stale;
      return [
        `${plural(run.sources_ok, 'source')} passed the check${bad ? `, ${bad} did not` : ''}`,
        run.items_fetched === undefined || run.items_fetched === null ? null : `${plural(run.items_fetched, 'item')} fetched`,
        `${run.new_events} new and ${plural(run.updated_events, 'updated event')}, ${run.duplicates} merged as ${run.duplicates === 1 ? 'a duplicate' : 'duplicates'}`,
        queue,
      ]
        .filter(Boolean)
        .join('; ');
    },
  },
};

// THE CREW IN THE LAST RUN. A table, not cards: the question this answers is "who did what",
// which is a comparison down a column, and eight cards is the wrong shape for that.
export function renderCrewRun(status, feed) {
  const host = document.getElementById('crew-run');
  if (!host) return;
  clear(host);
  const run = status?.last_run || null;
  const when = run?.finished_at ? `${formatSydney(run.finished_at)}` : null;
  host.append(
    h('p', {
      class: 'hint',
      text: run
        ? `Last completed run: ${run.lane.toUpperCase()} lane, finished ${when}. An agent appears with a figure only where the run published a counter it owns; the rest state that plainly.`
        : 'No completed run has been published, so there is nothing for any agent to report yet. Each agent\u2019s ownership below is what it will answer for once it is wired up.',
    }),
    h(
      'table',
      { class: 'run-table' },
      h('thead', {}, h('tr', {}, h('th', { scope: 'col', text: 'AGENT' }), h('th', { scope: 'col', text: 'WHAT IT OWNS' }), h('th', { scope: 'col', text: 'IN THE LAST RUN' }))),
      h(
        'tbody',
        {},
        CREW.map((agent) => {
          const job = CREW_JOBS[agent.callsign] || {};
          const did = job.run ? job.run(run, feed) : null;
          return h(
            'tr',
            { 'data-desk': agent.desk },
            h(
              'th',
              { scope: 'row' },
              h(
                'span',
                { class: 'run-table__who' },
                botFace(agent, 'bot bot--sm'),
                h(
                  'span',
                  { class: 'run-table__id' },
                  h('span', { class: 'run-table__callsign', text: agent.callsign }),
                  h('span', { class: 'run-table__desk mono', text: agent.title }),
                ),
              ),
            ),
            h('td', { text: job.owns || agent.beat }),
            h('td', { class: did ? 'run-table__did' : 'run-table__did run-table__did--none', text: did || 'Nothing published for this run.' }),
          );
        }),
      ),
    ),
  );
}

// One accessory per agent, keyed by CREW[].face. `back` is drawn behind the head, so
// antennae, crowns and dishes sit around the silhouette; `front` is drawn over it, for the
// accessories that belong on the face itself. Nothing here is decorative-only: the accessory
// is how you tell the eight portraits apart at 46px.
const BOT_PARTS = {
  // Director: a low coronet, lit at the centre and tipped at both ends.
  crown: () => ({
    back: [
      s('path', { class: 'bot-line', d: 'M14 12 Q24 4 34 12' }),
      s('path', { class: 'bot-dim', d: 'M17.2 12.4 Q24 7.2 30.8 12.4' }),
      s('circle', { class: 'bot-eye', cx: 24, cy: 5, r: 1.9 }),
      s('circle', { class: 'bot-eye', cx: 14.2, cy: 11.6, r: 1.1 }),
      s('circle', { class: 'bot-eye', cx: 33.8, cy: 11.6, r: 1.1 }),
    ],
  }),
  // Discovery: one antenna cocked to the side, already picking something up.
  tilt: () => ({
    back: [
      s('circle', { class: 'bot-fill', cx: 24, cy: 13.2, r: 1.6 }),
      s('path', { class: 'bot-line', d: 'M24 13 L29 5' }),
      s('circle', { class: 'bot-eye', cx: 29.5, cy: 4, r: 2.1 }),
      s('path', { class: 'bot-dim', d: 'M32.6 2.4 Q36 4.8 34.6 8.6' }),
      s('path', { class: 'bot-dim', d: 'M35.8 1.6 Q40.8 5 38.2 10.4' }),
    ],
  }),
  // Follow-up: a magnifier, because the case stays open.
  lens: () => ({
    back: [
      s('circle', { class: 'bot-line', cx: 39, cy: 11, r: 4.6 }),
      s('path', { class: 'bot-glint', d: 'M36.6 9 Q35.6 10.8 36.2 12.6' }),
      s('path', { class: 'bot-line', d: 'M42.4 14.4 L46 18' }),
      s('path', { class: 'bot-dim', d: 'M43.4 16.8 L45 15.2' }),
      s('path', { class: 'bot-dim', d: 'M44.4 17.8 L46 16.2' }),
    ],
  }),
  // Editorial QA: a viewfinder bracketed around one eye, mid-interrogation.
  scan: () => ({
    front: [
      s('circle', { class: 'bot-line', cx: 28.5, cy: 24.5, r: 5 }),
      s('path', { class: 'bot-line', d: 'M24.2 21.6 V20.2 H25.6' }),
      s('path', { class: 'bot-line', d: 'M31.4 20.2 H32.8 V21.6' }),
      s('path', { class: 'bot-line', d: 'M32.8 27.4 V28.8 H31.4' }),
      s('path', { class: 'bot-line', d: 'M25.6 28.8 H24.2 V27.4' }),
    ],
  }),
  // Engineer: a hard hat, ridges cut into it, lamp on the crown.
  helmet: () => ({
    front: [
      s('path', { class: 'bot-fill', d: 'M13 15 Q24 4 35 15 Z' }),
      s('path', { class: 'bot-etch', d: 'M24 6.6 V10.2' }),
      s('path', { class: 'bot-etch', d: 'M18.6 8.6 Q17.9 11.6 18.3 14.4' }),
      s('path', { class: 'bot-etch', d: 'M29.4 8.6 Q30.1 11.6 29.7 14.4' }),
      s('circle', { class: 'bot-eye', cx: 24, cy: 12.6, r: 1.6 }),
      s('path', { class: 'bot-line', d: 'M7 15.4 H41' }),
    ],
  }),
  // Watchdog: a dish on a mast with its feed horn, listening to the whole estate.
  dish: () => ({
    back: [
      s('path', { class: 'bot-dim', d: 'M21 12.6 H27' }),
      s('path', { class: 'bot-line', d: 'M24 12.6 V8' }),
      s('path', { class: 'bot-line', d: 'M17.5 8 Q24 1 30.5 8' }),
      s('path', { class: 'bot-dim', d: 'M19.9 7.4 Q24 3.6 28.1 7.4' }),
      s('path', { class: 'bot-line', d: 'M24 8 V5' }),
      s('circle', { class: 'bot-eye', cx: 24, cy: 4.2, r: 1.3 }),
    ],
  }),
  // Verification gate: a shield, chevroned.
  shield: () => ({
    back: [
      s('path', { class: 'bot-line', d: 'M37.5 6 h8.5 v5 q0 5.5 -4.25 7.5 q-4.25 -2 -4.25 -7.5 z' }),
      s('path', { class: 'bot-dim', d: 'M37.6 8.4 H45.9' }),
      s('path', { class: 'bot-dim', d: 'M39.4 10.6 l2.35 2.1 l2.35 -2.1' }),
      s('path', { class: 'bot-dim', d: 'M39.4 13.8 l2.35 2.1 l2.35 -2.1' }),
    ],
  }),
  // Model scout: a surgeon's head mirror on a band — worn, not held, which is what keeps it
  // distinct from DECKARD's magnifier at 30px.
  mirror: () => ({
    front: [
      s('path', { class: 'bot-line', d: 'M11 16.6 H37' }),
      s('path', { class: 'bot-dim', d: 'M11.6 14.6 H36.4' }),
      s('circle', { class: 'bot-line', cx: 24, cy: 11.4, r: 4.2 }),
      s('path', { class: 'bot-glint', d: 'M21.2 9 Q20.3 11 21 13' }),
      s('circle', { class: 'bot-eye', cx: 24, cy: 11.4, r: 1.5 }),
    ],
  }),
};


// The shared body. Inline SVG rather than an image file: the CSP serves no external images,
// and drawn eyes take --bot-accent from the card, so the roster follows the palette. The class
// is a parameter because the same drawing serves the 62px crew card and the 30px run-table row;
// scaling one vector beats maintaining a second, coarser set of paths for the small size.
function botFace(agent, klass = 'bot') {
  const parts = (BOT_PARTS[agent.face] || (() => ({})))();
  return s(
    'svg',
    { class: klass, viewBox: '0 0 48 48', 'aria-hidden': 'true', focusable: 'false' },
    parts.back || [],
    s('rect', { class: 'bot-fill', x: 5.5, y: 21, width: 3, height: 8, rx: 1.5 }),
    s('rect', { class: 'bot-fill', x: 39.5, y: 21, width: 3, height: 8, rx: 1.5 }),
    s('rect', { class: 'bot-fill', x: 18, y: 38, width: 12, height: 3, rx: 1.5 }),
    s('rect', { class: 'bot-shell', x: 9, y: 13, width: 30, height: 26, rx: 9 }),
    s('path', { class: 'bot-etch', d: 'M6.3 25 H7.7' }),
    s('path', { class: 'bot-etch', d: 'M40.3 25 H41.7' }),
    s('path', { class: 'bot-etch', d: 'M13.6 37 H34.4' }),
    s('rect', { class: 'bot-visor', x: 13, y: 18, width: 22, height: 13, rx: 6.5 }),
    s('path', { class: 'bot-glint', d: 'M15.6 23.2 Q17.4 20.4 20.6 19.9' }),
    s('circle', { class: 'bot-eye', cx: 19.5, cy: 24.5, r: 2.6 }),
    s('circle', { class: 'bot-eye', cx: 28.5, cy: 24.5, r: 2.6 }),
    s('path', { class: 'bot-mouth', d: 'M21 34.5 H27' }),
    parts.front || [],
  );
}

function crewCard(agent, stat) {
  const status = stat?.status || 'not_active';
  const led = { active: 'ok', idle: 'idle', degraded: 'warn', not_active: 'idle' }[status] || 'idle';
  const label = { active: 'ACTIVE', idle: 'IDLE', degraded: 'DEGRADED', not_active: 'RUNS IN PAPERCLIP' }[status] || status.toUpperCase();
  const fmtStat = (value, fmt = (v) => String(v)) => (value === null || value === undefined ? '—' : fmt(value));
  return h(
    'li',
    { class: 'crew-card', 'data-status': status, 'data-desk': agent.desk },
    h(
      'div',
      { class: 'crew-card__head' },
      botFace(agent),
      h(
        'div',
        { class: 'crew-card__id' },
        h('span', { class: 'crew-card__callsign', text: agent.callsign }),
        h('span', { class: 'crew-card__desk mono', text: agent.title }),
      ),
      // Status reads once, here, next to the face — it used to repeat as a stats row too.
      h('span', { class: 'crew-card__status' }, h('span', { class: 'led', 'data-state': led }), h('span', { text: label })),
    ),
    h('p', { class: 'crew-card__beat', text: agent.beat }),
    agent.quote ? h('p', { class: 'crew-card__quote', text: `"${agent.quote}"` }) : null,
    // An agent with nothing published says so in one line instead of printing four rows of
    // "—". Same claim, a quarter of the ink; the stats table appears once there are figures,
    // and a single missing field inside a reporting agent still falls back to "—".
    stat
      ? h(
        'dl',
        { class: 'kv crew-card__stats mono' },
        h('dt', { text: 'TASKS THIS MONTH' }), h('dd', { text: fmtStat(stat.tasks_completed) }),
        h('dt', { text: 'ITEMS THIS MONTH' }), h('dd', { text: fmtStat(stat.items_processed) }),
        h('dt', { text: 'COST THIS MONTH' }), h('dd', { text: fmtStat(stat.cost_usd, (v) => `US$${Number(v).toFixed(2)}`) }),
        h('dt', { text: 'LAST ACTIVE' }), h('dd', { text: fmtStat(stat.last_active_at, formatSydney) }),
      )
      : h('p', { class: 'crew-card__idle', text: 'Runs in Paperclip’s control panel; the worker does not track its workload here.' }),
    h('span', { class: 'chip crew-card__runtime', text: agent.runtime }),
  );
}

// Renders the full eight-agent roster every time: personas ship with the site regardless
// of data, and a card falls back to RUNS IN PAPERCLIP when data/crew.json has nothing for
// that callsign. SERAPH and RIPPERDOC are counted from the worker's own rows; the others
// run in Paperclip's control panel, which this file never reads, so nothing is inferred.
export function renderCrew(crew) {
  const host = document.getElementById('crew-list');
  if (!host) return;
  clear(host);
  // Only callsigns on the roster count. A feed published before the crew went to eight still
  // names retired agents, and counting them read "5 of 8 agents publish" above eight cards
  // that mostly said RUNS IN PAPERCLIP.
  const rostered = new Set(CREW.map((a) => a.callsign));
  const byCallsign = new Map(
    (crew?.agents || []).filter((a) => rostered.has(a.callsign)).map((a) => [a.callsign, a]),
  );
  const summary = document.getElementById('crew-summary');
  if (summary) {
    const reporting = byCallsign.size;
    const totalTasks = [...byCallsign.values()].reduce((sum, a) => sum + (a.tasks_completed || 0), 0);
    const ai = crew?.pipeline_ai;
    const spend = ai ? ` · the pipeline's own model calls this month: ${plural(ai.calls, 'call')}, US$${Number(ai.cost_usd).toFixed(2)}` : '';
    clear(summary).append(
      h('p', {
        class: 'hint',
        text: reporting
          ? `${reporting} of ${CREW.length} agents publish their workload · ${plural(totalTasks, 'task')} completed this month${spend}.`
          : `${CREW.length} agents. No workload has been published yet.`,
      }),
    );
  }
  host.append(...CREW.map((agent) => crewCard(agent, byCallsign.get(agent.callsign))));
  const count = document.getElementById('crew-count');
  if (count) count.textContent = `${CREW.length} AGENTS`;
}

// ------------------------------------------------------------- the company

// THE COMPANY and ROUTINES read assets/org.json, which ships with the site rather than with the
// data: the worker writes it from the same table that builds the Paperclip package, so it is the
// configuration — who reports to whom, on which model and budget, and which routine wakes whom —
// and not a record of what ran. Paperclip can pause any agent or routine, and this page cannot
// see that, so it never says one is running.

const DAYS = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays'];
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

// One cron field as the values it matches, with `any` for "*". Understands n, a-b, a-b/n, */n
// and lists of those; anything else is null, so the caller can give up honestly.
function cronField(field, lo, hi) {
  if (field === '*') return { any: true, values: [] };
  const values = new Set();
  for (const part of String(field).split(',')) {
    const m = /^(\*|(\d+)(?:-(\d+))?)(?:\/(\d+))?$/.exec(part);
    if (!m) return null;
    const step = m[4] ? Number(m[4]) : 1;
    const from = m[2] === undefined ? lo : Number(m[2]);
    // "5/15" runs from 5 to the top of the range, as cron reads it.
    const to = m[3] !== undefined ? Number(m[3]) : m[2] === undefined || m[4] ? hi : from;
    if (!step || from < lo || to > hi || from > to) return null;
    for (let v = from; v <= to; v += step) values.add(v);
  }
  return { any: false, values: [...values].sort((a, b) => a - b) };
}

function ordinal(n) {
  const tail = n % 100 >= 11 && n % 100 <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th');
  return `${n}${tail}`;
}

const listWords = (items) => (items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`);

// A five-field cron line in plain English: "0 6,14,22 * * *" in Australia/Sydney reads "Daily at
// 06:00, 14:00 and 22:00 Sydney time". The cron is always printed beside it, so this only has to
// be right for the shapes the crew uses; for anything else it returns null and the page shows
// the cron alone rather than a confident misreading.
export function describeCron(cron, timezone = null) {
  const fields = String(cron || '').trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const [minute, hour, dom, month, dow] = [
    cronField(fields[0], 0, 59), cronField(fields[1], 0, 23), cronField(fields[2], 1, 31),
    cronField(fields[3], 1, 12), cronField(fields[4], 0, 7),
  ];
  if (!minute || !hour || !dom || !month || !dow || minute.any) return null;
  // Cron ORs the two day fields when both are set, which no short sentence says correctly.
  if (!dom.any && !dow.any) return null;
  let when;
  if (hour.any) {
    if (minute.values.length !== 1) return null;
    when = minute.values[0] ? `every hour at ${pad(minute.values[0])} past` : 'every hour on the hour';
  } else {
    const times = hour.values.flatMap((hh) => minute.values.map((mm) => `${pad(hh)}:${pad(mm)}`));
    if (times.length > 8) return null;
    when = `at ${listWords(times)}`;
  }
  let days = 'Daily';
  if (!dow.any) {
    const set = [...new Set(dow.values.map((d) => d % 7))].sort((a, b) => a - b);
    days = set.join() === '1,2,3,4,5' ? 'Weekdays' : listWords(set.map((d) => DAYS[d]));
  } else if (!dom.any) {
    days = `Monthly on the ${listWords(dom.values.map(ordinal))}`;
  }
  if (!month.any) days += ` in ${listWords(month.values.map((m) => MONTHS[m - 1]))}`;
  const zone = !timezone ? '' : timezone === 'Australia/Sydney' ? ' Sydney time' : ` ${timezone}`;
  // "Every hour on the hour" needs no day and no zone; any other shape is pinned to both.
  if (hour.any && days === 'Daily') return `${when[0].toUpperCase()}${when.slice(1)}`;
  return `${days} ${when}${zone}`;
}

// org.json in the shape the page draws: the one agent who reports to nobody at the top, then
// every team in the order the file first names it, each member after the manager they report
// to. Personas join by callsign from CREW, which is what the rest of the page keys on too.
// Returns null when there is nothing to draw, so the caller has one honest empty state.
export function orgModel(org) {
  const agents = (Array.isArray(org?.agents) ? org.agents : []).filter((a) => a && typeof a.callsign === 'string' && a.callsign);
  if (!agents.length) return null;
  const known = new Set(agents.map((a) => a.callsign));
  const top = agents.find((a) => !a.reports_to || !known.has(a.reports_to)) || null;
  const byCallsign = new Map(agents.map((a) => [a.callsign, a]));
  const depth = (a) => {
    let n = 0;
    for (let at = a; at && at !== top && n < agents.length; at = byCallsign.get(at.reports_to)) n += 1;
    return n;
  };
  const routines = (Array.isArray(org?.routines) ? org.routines : [])
    .filter((r) => r && typeof r.name === 'string' && r.name)
    .map((r) => ({ ...r, schedule: describeCron(r.cron, r.timezone) }));
  const node = (a) => ({
    ...a,
    persona: CREW.find((c) => c.callsign === a.callsign) || null,
    manager: a === top ? null : a.reports_to,
    routines: routines.filter((r) => r.assignee === a.callsign).length,
  });
  const teams = new Map();
  for (const a of agents) {
    if (a === top) continue;
    const team = String(a.team || 'Unassigned');
    if (!teams.has(team)) teams.set(team, []);
    teams.get(team).push(a);
  }
  return {
    company: org?.company && typeof org.company === 'object' ? org.company : {},
    top: top ? node(top) : null,
    teams: [...teams].map(([name, members]) => ({
      name,
      members: [...members].sort((a, b) => depth(a) - depth(b)).map(node),
    })),
    agents: agents.map(node),
    routines,
    unlisted: CREW.map((c) => c.callsign).filter((c) => !known.has(c)),
  };
}

function usd(value) {
  return hasNum(value) && Number.isFinite(Number(value)) ? `US$${Number(value).toFixed(2)}` : null;
}

// What an agent runs on, in one line: the model (without the router's prefix) or plain code.
function orgRuntime(a) {
  if (a.adapter === 'http') return 'CODE OVER HTTP · NO MODEL';
  if (a.adapter === 'model') return a.model ? `MODEL ${String(a.model).replace(/^openrouter\//, '')}` : 'MODEL NOT SET';
  return a.adapter ? String(a.adapter).toUpperCase() : null;
}

function orgNode(a, topCallsign) {
  const limits = [
    usd(a.budget_usd) ? `${usd(a.budget_usd)} A MONTH` : null,
    hasNum(a.max_daily_runs) ? `UP TO ${plural(Number(a.max_daily_runs), 'run').toUpperCase()} A DAY` : null,
    a.routines ? plural(a.routines, 'routine').toUpperCase() : null,
  ].filter(Boolean);
  return h(
    'article',
    { class: 'org-node', 'data-desk': a.persona?.desk || undefined, 'data-callsign': a.callsign },
    h(
      'div',
      { class: 'org-node__head' },
      a.persona ? botFace(a.persona, 'bot bot--sm') : null,
      h(
        'span',
        { class: 'org-node__id' },
        h('span', { class: 'org-node__callsign', text: a.callsign }),
        h('span', { class: 'org-node__title mono', text: String(a.title || a.role || '').toUpperCase() }),
      ),
    ),
    a.summary ? h('p', { class: 'org-node__summary', text: a.summary }) : null,
    orgRuntime(a) ? h('p', { class: 'org-node__meta mono', text: orgRuntime(a) }) : null,
    limits.length ? h('p', { class: 'org-node__meta mono', text: limits.join(' · ') }) : null,
    a.manager && a.manager !== topCallsign ? h('p', { class: 'org-node__reports mono', text: `REPORTS TO ${a.manager}` }) : null,
  );
}

function routineRow(r) {
  const later = r.starts && String(r.starts).toLowerCase() !== 'now';
  return h(
    'tr',
    {},
    h('th', { scope: 'row' }, h('span', { class: 'routine__name', text: r.name }), later ? h('span', { class: 'chip routine__starts', text: `FROM ${String(r.starts).toUpperCase()}` }) : null),
    h('td', { class: 'mono routine__agent', text: r.assignee || '—' }),
    h(
      'td',
      {},
      h('span', { class: 'routine__when', text: r.schedule || 'Custom schedule' }),
      h('span', { class: 'routine__cron mono', text: `${r.cron || '—'}${r.timezone ? ` · ${r.timezone}` : ''}` }),
    ),
    h('td', { text: r.summary || '' }),
  );
}

// The company panel: the facts, then the chart (top, then a column per team); and the routines
// panel, one row each, grouped by the agent they wake in the order the chart lists the agents.
export function renderOrg(org) {
  const model = orgModel(org);
  const facts = document.getElementById('org-facts');
  const chart = document.getElementById('org-chart');
  const table = document.getElementById('org-routines');
  const count = document.getElementById('org-count');
  const led = document.getElementById('org-led');
  const routineCount = document.getElementById('routines-count');
  if (facts) clear(facts);
  if (!model) {
    if (chart) clear(chart).append(h('p', { class: 'empty', text: 'ORG CHART NOT PUBLISHED YET.' }));
    if (table) clear(table).append(h('p', { class: 'empty', text: 'NO ROUTINES PUBLISHED YET.' }));
    if (count) count.textContent = 'NOT PUBLISHED';
    if (routineCount) routineCount.textContent = 'NOT PUBLISHED';
    if (led) led.dataset.state = 'idle';
    return null;
  }
  const { company } = model;
  if (facts) {
    const rows = [
      ['COMPANY', company.name],
      ['MISSION', company.mission],
      ['MONTHLY BUDGET', usd(company.budget_usd)],
      ['AGENTS', String(model.agents.length)],
      ['ROUTINES', String(model.routines.length)],
    ];
    for (const [k, v] of rows) if (v) facts.append(h('dt', { text: k }), h('dd', { text: v }));
  }
  // Teams as published, the CEO's own included, though the chart draws that one as the top.
  const teamCount = new Set(model.agents.map((a) => String(a.team || 'Unassigned'))).size;
  if (count) count.textContent = `${plural(model.agents.length, 'agent').toUpperCase()} · ${plural(teamCount, 'team').toUpperCase()}`;
  if (led) led.dataset.state = 'ok';
  const topCallsign = model.top?.callsign || null;
  if (chart) {
    // The helper, not Element.append, which would write an absent part as the text "null".
    append(clear(chart), [
      model.top
        ? h(
          'div',
          { class: 'org-top' },
          model.top.team ? h('h4', { class: 'org-team__name', text: String(model.top.team).toUpperCase() }) : null,
          orgNode(model.top, null),
        )
        : null,
      h(
        'ul',
        { class: 'org-teams', 'aria-label': model.top ? `Teams reporting to ${model.top.callsign}` : 'Teams' },
        model.teams.map((team) =>
          h(
            'li',
            { class: 'org-team' },
            h('h4', { class: 'org-team__name', text: team.name.toUpperCase() }),
            h('ul', { class: 'org-team__members' }, team.members.map((a) => h('li', {}, orgNode(a, topCallsign)))),
          ),
        ),
      ),
      model.unlisted.length
        ? h('p', { class: 'hint', text: `Not in the published org chart: ${listWords(model.unlisted)}.` })
        : null,
    ]);
  }
  if (routineCount) routineCount.textContent = plural(model.routines.length, 'routine').toUpperCase();
  if (table) {
    clear(table);
    if (!model.routines.length) {
      table.append(h('p', { class: 'empty', text: 'NO ROUTINES PUBLISHED YET.' }));
    } else {
      const order = model.agents.map((a) => a.callsign);
      const rank = (r) => (order.includes(r.assignee) ? order.indexOf(r.assignee) : order.length);
      const rows = model.routines.map((r, i) => [r, i]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(([r]) => routineRow(r));
      table.append(
        h(
          'table',
          { class: 'run-table routine-table' },
          h(
            'thead',
            {},
            h('tr', {}, h('th', { scope: 'col', text: 'ROUTINE' }), h('th', { scope: 'col', text: 'AGENT' }), h('th', { scope: 'col', text: 'SCHEDULE' }), h('th', { scope: 'col', text: 'WHAT IT DOES' })),
          ),
          h('tbody', {}, rows),
        ),
      );
    }
  }
  return model;
}

// ------------------------------------------------------------- top signals

// A link that opens one event in the Events view. The href is the state that shows it
// (sectionFor), so a new tab, a copied link or a bookmark lands on the card; a plain click does
// the same in place and moves focus to the card.
function eventLink(event, className) {
  const link = h('a', { class: className, href: toSearch(sectionFor(event, view.state)), text: event.title });
  link.addEventListener('click', (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (revealEvent(event.event_id)) e.preventDefault();
  });
  return link;
}

// The strongest headlines in the scope, as a plain static list. Deliberately not a scrolling
// banner: perpetual motion at the top of the page competes with the content for attention and
// cannot be read at a glance. Capped at 8 — past that the list stops being a summary and
// becomes a second copy of the Events view.
export function renderHeadlines(events) {
  const host = document.getElementById('headline-list');
  if (!host) return;
  clear(host);
  const top = [...(events || [])].sort(byProminence).slice(0, 8);
  if (!top.length) {
    host.append(h('li', { class: 'empty', text: filtersActive ? 'NO HEADLINES MATCH THE CURRENT FILTERS.' : 'NO HEADLINES IN THIS SNAPSHOT.' }));
    return;
  }
  // data-severity on the row, not just the badge, so the row's left stripe picks up
  // --sev-color from the one shared severity mapping in the CSS. The ranking stays prominence;
  // the importance badge says which of these the score rates KEY, without reordering them.
  host.append(
    ...top.map((e) =>
      h(
        'li',
        { class: 'headline', 'data-severity': sevKey(e), 'data-beat': beatOf(e) },
        h('span', { class: 'headline__badges' }, importanceBadge(e), rankBadge(e)),
        eventLink(e, 'headline__link'),
      ),
    ),
  );
}

const WATCH_ROWS = 5;

// DEVELOPING EVENTS and EMERGING THREATS on the dashboard: the first few of each feed in the
// current scope, and a link to the whole feed in the Events view.
function renderWatch(id, events) {
  const host = document.getElementById(`dash-${id}`);
  if (!host) return;
  const section = SECTIONS.find((x) => x.id === id);
  const matches = (events || []).filter(section.match).sort(byProminence);
  clear(host);
  if (!matches.length) {
    host.append(h('p', { class: 'empty', text: filtersActive ? 'NONE MATCH THE CURRENT FILTERS.' : 'NONE IN THIS SNAPSHOT.' }));
    return;
  }
  host.append(
    h(
      'ul',
      { class: 'watch-list' },
      matches.slice(0, WATCH_ROWS).map((e) => h('li', { class: 'watch', 'data-severity': sevKey(e) }, rankBadge(e), eventLink(e, 'watch__link'))),
    ),
    h('a', {
      class: 'btn btn--small watch__all',
      href: toSearch(PRESETS.find((p) => p.id === id).apply(view.state)),
      text: `VIEW ALL ${matches.length}`,
    }),
  );
}

// ------------------------------------------------------------------ feeds

// Counts beside every preset in the FEEDS menu, from the events the preset would list from here.
// When the scope hides some of them a feed gets a second, one-click link to the same feed at
// ALL: the reader sees that there is more, and how much, without having to guess which control
// to move. The two geographic presets are the scope, so they have nothing to widen.
function renderFeeds(events, state) {
  const counts = presetCounts(events, state);
  for (const out of document.querySelectorAll('[data-preset-count]')) {
    const entry = counts[out.dataset.presetCount];
    out.textContent = entry ? String(entry.count) : '';
  }
  for (const link of document.querySelectorAll('[data-preset-widen]')) {
    const id = link.dataset.presetWiden;
    const entry = counts[id];
    const preset = PRESETS.find((p) => p.id === id);
    const show = Boolean(entry && preset?.group === 'feeds' && entry.hidden > 0 && entry.state.scope !== 'all');
    link.hidden = !show;
    if (!show) continue;
    link.href = toSearch({ ...entry.state, scope: 'all' });
    link.textContent = `+${entry.hidden} IN ALL`;
    link.setAttribute('aria-label', `${preset.label}: ${plural(entry.hidden, 'more event')} outside ${SCOPES[entry.state.scope]}. Show ALL.`);
  }
}

// The Events view says what it is listing, in words, so the state in the URL is never only in
// the URL.
function renderEventsHead(events, listed, state) {
  const preset = PRESETS.find((p) => p.id === currentPreset(state));
  const title = document.getElementById('h-events');
  if (title) title.textContent = preset ? preset.label : 'EVENTS';
  const tagCount = Object.values(state.tags || {}).reduce((n, values) => n + values.length, 0);
  const desc = document.getElementById('events-desc');
  if (desc) {
    desc.textContent = [
      `SCOPE ${SCOPES[state.scope]}`,
      `BEAT ${state.beat.length ? state.beat.map((b) => BEATS[b]).join(' + ') : 'ALL'}`,
      state.feed.length ? state.feed.map((f) => FEED_LABEL[f]).join(' + ') : 'EVERY FEED',
      state.key ? 'KEY ONLY' : null,
      tagCount ? `${tagCount} TAG ${tagCount === 1 ? 'FILTER' : 'FILTERS'}` : null,
    ]
      .filter(Boolean)
      .join(' · ');
  }
  const widen = document.getElementById('events-widen');
  if (!widen) return;
  const hidden = state.scope === 'all' ? 0 : filterEvents(events, { ...state, scope: 'all' }).length - listed.length;
  widen.hidden = hidden <= 0;
  const text = document.getElementById('events-widen-text');
  if (text && hidden > 0) {
    text.textContent = `${plural(hidden, 'more event')} ${hidden === 1 ? 'matches' : 'match'} outside ${SCOPES[state.scope]}.`;
  }
}

// -------------------------------------------------------------- dropdowns

// FEEDS and TAG FILTERS are disclosure menus (<details data-dropdown>) that close themselves:
// once a choice is applied, on a click or tap anywhere else, and on Escape. At most one is open,
// so the two never stack over the page. What happens is a pure function of which menu is open
// and what just happened, so node can test it without a browser; initDropdowns() carries it out.
//   { type: 'open', id }          menu `id` opened (its summary, or anything that set .open)
//   { type: 'close', id }         menu `id` closed by its own summary
//   { type: 'click', inside }     a click or tap; `inside` is the menu it landed in, or null
//   { type: 'key', key }          a key pressed anywhere on the page
//   { type: 'apply', id, moved }  a choice in `id` was applied; `moved` if it changed the view
// Returns { open, focus }: the menu that is open now (or null), and the menu whose summary takes
// focus (or null, to leave focus alone). A choice that moved the view leaves focus to the view.
export function dropdownAfter(open, event) {
  switch (event?.type) {
    case 'open':
      return { open: event.id, focus: null };
    case 'close':
      return { open: open === event.id ? null : open, focus: null };
    case 'click':
      return { open: open && event.inside === open ? open : null, focus: null };
    case 'key':
      return event.key === 'Escape' && open ? { open: null, focus: open } : { open, focus: null };
    case 'apply':
      return { open: null, focus: event.moved ? null : event.id };
    default:
      return { open, focus: null };
  }
}

function initDropdowns() {
  const menus = [...document.querySelectorAll('details[data-dropdown]')];
  let open = menus.find((m) => m.open)?.id || null;
  const settle = ({ open: next, focus }) => {
    open = next;
    for (const m of menus) if (m.open !== (m.id === next)) m.open = m.id === next;
    if (focus) document.getElementById(focus)?.querySelector('summary')?.focus();
  };
  for (const m of menus) {
    // `toggle` fires after the fact and also for our own writes to .open, which settle() has
    // already accounted for; only a change it did not make is news.
    m.addEventListener('toggle', () => {
      if (m.open && open !== m.id) settle(dropdownAfter(open, { type: 'open', id: m.id }));
      else if (!m.open && open === m.id) settle(dropdownAfter(open, { type: 'close', id: m.id }));
    });
  }
  document.addEventListener('keydown', (e) => {
    if (open) settle(dropdownAfter(open, { type: 'key', key: e.key }));
  });
  // The path, not contains(): a facet chip's own click rebuilds the chips, so by the time the
  // click reaches the document its target is detached and contains() would call it outside.
  document.addEventListener('click', (e) => {
    if (!open) return;
    const path = e.composedPath();
    settle(dropdownAfter(open, { type: 'click', inside: menus.find((m) => path.includes(m))?.id || null }));
  });
  return { applied: (id, moved = false) => settle(dropdownAfter(open, { type: 'apply', id, moved })) };
}

// ------------------------------------------------------------------- views

const isPlainClick = (e) => e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;

// The view a tab opens from `st`. Only Events has a feed, so the feed is dropped elsewhere; the
// scope, the beat, KEY ONLY and the tags are the reader's and carry over.
function viewState(st, name) {
  return { ...st, view: name, feed: name === 'events' ? st.feed : [], event: null, anchor: null };
}

// Five views — DASHBOARD, EVENTS, CREW, SYSTEM, SOURCES — one visible at a time, with the whole
// state (view, scope, beat, feed, KEY ONLY, tags, an event to open) kept in the query string so
// it survives a reload and is linkable. The header tabs and the FEEDS menu are real hrefs to
// those states; a plain click is handled in place and anything else (new tab, copy link) gets
// the browser's own behaviour. An old #sec-* fragment opens the view and scope that now hold what
// it pointed at (LEGACY_ANCHORS), and the address is rewritten to say so. Returns null on a page
// without views.
export function initViews({ onChange = () => {} } = {}) {
  const panels = VIEWS.map((name) => document.getElementById(`view-${name}`));
  if (panels.some((p) => !p)) return null;
  const main = document.getElementById('main');
  const menus = initDropdowns();
  view.menus = menus;

  // .site-header is sticky on a wide screen, so anything jumped to has to be pushed clear of it,
  // and its height is not a number the stylesheet can hold: the scope row and the status strip
  // wrap as the viewport narrows, and the timestamps arrive after the first paint. So measure it
  // and publish it for .subsection's scroll-margin-top to use. Observed rather than sampled: the
  // header changes height when renderStrip() fills in the collection time, on resize, and when
  // the font swaps in, and one observer covers all three. Under 720px it is not sticky, so there
  // is nothing to clear and the margin is zero.
  const header = document.querySelector('.site-header');
  const syncHeaderHeight = () => {
    if (!header) return;
    const sticky = getComputedStyle(header).position === 'sticky';
    document.documentElement.style.setProperty('--header-h', `${sticky ? Math.round(header.offsetHeight) : 0}px`);
  };
  if (header && window.ResizeObserver) new ResizeObserver(syncHeaderHeight).observe(header);

  // When the reader last did something that moves the page themselves. Used to stand down: both
  // the settling loop below and the deep-link scroll give up rather than fight them. A scroll
  // listener cannot tell us this — scroll anchoring and our own scrolling fire it too.
  let lastInputAt = 0;
  for (const type of ['wheel', 'touchmove', 'keydown']) {
    window.addEventListener(type, () => { lastInputAt = Date.now(); }, { passive: true });
  }

  // One scrollIntoView is not enough to land on a block inside a view, and the reason is worth
  // writing down. .event-list is content-visibility: auto with contain-intrinsic-size: auto 600px,
  // so every list the reader has not reached yet is a flat 600px guess. The first scroll is
  // computed through those guesses and lands correctly — and then, on the next frame, the lists
  // that the scroll brought near the viewport lay out at their real heights, the content above the
  // target shrinks, and the target slides up under the sticky header. Traced at 1320px on
  // 2026-10-01: scroll 1 put the heading at 120px (correct), one frame later the four lists above
  // it went 600 → 1106/176/362/1097 and the heading was at 14px, and it took five rounds of
  // correcting to settle back at 120. The guess cannot be pre-warmed either: flipping the lists to
  // content-visibility: visible and back makes every one report 600px again. Hence a loop,
  // re-scrolling for as long as the target is not where it belongs.
  //
  // Termination is on the target's own position, not on whether the scroll moved: scroll anchoring
  // also shifts window.scrollY between frames to absorb the same relayout, so "scrollY stopped
  // changing" is true while the heading is still in the wrong place. The reader, by contrast, is
  // detected from their input, which is why lastInputAt exists rather than a scroll comparison.
  const settleScroll = (el) => {
    const startedAt = Date.now();
    let ticks = 20;
    // Three consecutive good frames, not one: the drift arrives a frame after a correct scroll, so
    // stopping at the first frame that looks right stops just before the frame that spoils it.
    let good = 0;
    const step = () => {
      if (lastInputAt > startedAt || ticks-- <= 0) return;
      const want = parseFloat(getComputedStyle(el).scrollMarginTop) || 0;
      const off = el.getBoundingClientRect().top - want;
      const maxY = document.documentElement.scrollHeight - window.innerHeight;
      if (Math.abs(off) <= 2) good += 1;
      else {
        good = 0;
        // Skip the scroll when the page has run out of it — the last block cannot come up to the
        // header, and asking repeatedly will not change that. Keep watching rather than giving up,
        // though: the views are still rendering, and when they lengthen the page the target
        // becomes reachable after all.
        if (!(off > 0 && window.scrollY >= maxY - 1)) el.scrollIntoView({ block: 'start' });
      }
      if (good < 3) requestAnimationFrame(step);
    };
    step();
  };

  // Switching the view while scrolled down would swap content the reader cannot see, which reads
  // as a dead link. Scrolling is one-way — up to the top of the view, never downwards — so a
  // choice made at the top of the page leaves the page where it is. Instant, not smooth: this is
  // a view change, not an animation.
  const scrollToView = () => {
    const host = main || panels[0];
    const offset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--header-h')) || 0;
    const top = window.scrollY + host.getBoundingClientRect().top - offset - 8;
    if (window.scrollY > top) window.scrollTo(0, Math.max(0, top));
  };

  // Puts the state on the page: which view shows, which scope, beat and KEY ONLY are chosen,
  // where every tab and feed now goes and which one is current. Then hands it to main() to render.
  const apply = (st, { focus = false, scroll = false } = {}) => {
    VIEWS.forEach((name, i) => { panels[i].hidden = name !== st.view; });
    for (const radio of document.querySelectorAll('input[name="scope"]')) radio.checked = radio.value === st.scope;
    for (const btn of document.querySelectorAll('[data-beat-toggle]')) {
      btn.setAttribute('aria-pressed', st.beat.includes(btn.dataset.beatToggle) ? 'true' : 'false');
    }
    document.getElementById('key-toggle')?.setAttribute('aria-pressed', st.key ? 'true' : 'false');
    for (const link of document.querySelectorAll('[data-view-link]')) {
      const name = link.dataset.viewLink;
      link.setAttribute('href', toSearch(viewState(st, name)));
      if (name === st.view) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    const current = currentPreset(st);
    for (const link of document.querySelectorAll('[data-preset]')) {
      const preset = PRESETS.find((p) => p.id === link.dataset.preset);
      if (!preset) continue;
      link.setAttribute('href', toSearch(preset.apply(st)));
      if (preset.id === current) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    }
    // The menus close after a choice, so their summaries say what is chosen in them.
    const feed = PRESETS.find((p) => p.id === current && p.group === 'feeds');
    const feedNow = document.getElementById('feed-now');
    if (feedNow) feedNow.textContent = feed ? feed.label : st.feed.map((f) => FEED_LABEL[f]).join(' + ');
    const tagsOn = Object.values(st.tags).reduce((n, values) => n + values.length, 0);
    const tagsNow = document.getElementById('tags-now');
    if (tagsNow) tagsNow.textContent = tagsOn ? `${tagsOn} ON` : '';
    onChange(st);
    if (scroll) scrollToView();
    if (focus) panels[VIEWS.indexOf(st.view)].focus({ preventScroll: true });
    if (st.anchor) {
      const target = document.getElementById(st.anchor);
      if (target && !target.closest('[hidden]')) settleScroll(target);
    }
  };

  // The one way the state changes. A new view, scope, beat or feed is a step the Back button
  // should undo, so it is pushed; a tag, a country or a correction to the address replaces.
  const navigate = (next, { replace = false } = {}) => {
    const prev = view.state;
    const { legacy, ...rest } = { ...freshState(), ...next };
    view.state = rest;
    const url = toSearch(view.state);
    if (url !== `${window.location.search}${window.location.hash}`) {
      window.history[replace ? 'replaceState' : 'pushState'](null, '', url);
    }
    const moved = prev.view !== view.state.view;
    apply(view.state, { focus: moved, scroll: moved });
  };
  view.navigate = navigate;

  const activate = (name) => {
    if (!VIEWS.includes(name)) return false;
    navigate(viewState(view.state, name));
    return true;
  };

  // Every in-page link to a state (?view=...) is handled here, so the tabs, the feeds, the widen
  // links and VIEW ALL behave the same. A link with its own handler (a headline, which also
  // focuses the card) has already called preventDefault and is left alone. A choice made in a
  // menu closes it: the reader asked for a feed, not for the menu to stay over the list.
  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || !isPlainClick(e)) return;
    const link = e.target.closest?.('a[href^="?view="]');
    if (!link) return;
    e.preventDefault();
    const url = new URL(link.href);
    const before = view.state.view;
    navigate({ ...parseLocation(url.search, url.hash), event: null });
    const menu = link.closest('details[data-dropdown]');
    if (menu) menus.applied(menu.id, view.state.view !== before);
  });

  for (const radio of document.querySelectorAll('input[name="scope"]')) {
    radio.addEventListener('change', () => {
      if (radio.checked) navigate({ ...view.state, scope: radio.value, event: null, anchor: null });
    });
  }
  for (const btn of document.querySelectorAll('[data-beat-toggle]')) {
    btn.addEventListener('click', () => {
      const beat = btn.dataset.beatToggle;
      const on = view.state.beat.includes(beat);
      navigate({ ...view.state, beat: on ? view.state.beat.filter((b) => b !== beat) : [...view.state.beat, beat], event: null, anchor: null });
    });
  }
  // KEY ONLY is a filter like a beat: a step Back undoes, kept on every view.
  document.getElementById('key-toggle')?.addEventListener('click', () => {
    navigate({ ...view.state, key: !view.state.key, event: null, anchor: null });
  });
  document.getElementById('events-widen-btn')?.addEventListener('click', () => {
    navigate({ ...view.state, scope: 'all', event: null });
  });

  window.addEventListener('popstate', () => {
    const { legacy, ...st } = parseLocation(window.location.search, window.location.hash);
    view.state = st;
    apply(view.state, { scroll: true });
  });
  // An old #sec-* link followed inside the page (or typed into the address bar) is a request for
  // the view that now holds it. The reader's tags carry over; the rest is what the anchor means.
  window.addEventListener('hashchange', () => {
    const anchor = window.location.hash.slice(1);
    if (Object.hasOwn(LEGACY_ANCHORS, anchor) && !VIEW_ANCHORS[view.state.view].includes(anchor)) {
      navigate({ ...parseLocation('', window.location.hash), tags: view.state.tags }, { replace: true });
    } else if (VIEW_ANCHORS[view.state.view].includes(anchor)) {
      view.state = { ...view.state, anchor };
      const target = document.getElementById(anchor);
      if (target) settleScroll(target);
    }
  });

  // A deep link to a block inside a view cannot be honoured on first paint: the views are still
  // empty, so the target sits a few hundred pixels down a short page and that position stops
  // existing the moment the events render. Measured 2026-10-01 on a cold load: the scroll settled
  // at 960 against a final heading position of 3926. So main() calls this once the first render
  // is in. It declines if the reader has already started moving the page: landing somewhere you
  // did not ask for is bad, being yanked out of where you went instead is worse.
  const rescrollToHash = () => {
    const id = view.state.anchor;
    if (!id || lastInputAt) return false;
    const target = document.getElementById(id);
    if (!target || target.closest('[hidden]')) return false;
    settleScroll(target);
    return true;
  };

  // The observer above covers the usual case; this is for a browser without ResizeObserver, where
  // a stale --header-h is better than none, and for the 720px line, where the header stops being
  // sticky without changing size.
  syncHeaderHeight();
  window.addEventListener('resize', syncHeaderHeight);

  // First run: read the address, and if it was an old anchor, rewrite it to the state it means so
  // the address bar, a reload and a copied link all agree. It must not move a reader who
  // deep-linked; rescrollToHash() does that once there is something to land on.
  const { legacy, ...initial } = parseLocation(window.location.search, window.location.hash);
  view.state = initial;
  if (legacy) window.history.replaceState(null, '', toSearch(view.state));
  VIEWS.forEach((name, i) => {
    panels[i].hidden = name !== view.state.view;
    panels[i].tabIndex = -1;
  });
  apply({ ...view.state, anchor: null });
  return { activate, rescrollToHash };
}

// -------------------------------------------------------------------- fx

// The pieces every page shares: the FX OFF toggle and the canvas particle field. The views
// belong to the dashboard alone, so main() starts them itself.
function initCommon() {
  initFxToggle();
  initFxField();
}

export function initFxToggle({ button = document.getElementById('fx-toggle'), root = document.documentElement, storage = safeStorage('localStorage') } = {}) {
  // The label is fixed ("FX OFF"); aria-pressed=true means effects are off.
  const apply = (off) => {
    root.classList.toggle('fx-off', off);
    if (button) button.setAttribute('aria-pressed', off ? 'true' : 'false');
  };
  apply(storageGet(storage, FX_KEY) === 'off');
  if (button) {
    button.addEventListener('click', () => {
      const off = !root.classList.contains('fx-off');
      apply(off);
      storageSet(storage, FX_KEY, off ? 'off' : 'on');
    });
  }
}

// A slow particle field on a fixed canvas behind the page. The rules §8.4 asks
// for, all enforced in one loop: 30 fps, device pixel ratio capped at 2, stopped
// outright when the tab is hidden (visibilitychange) or the canvas is not being
// drawn (IntersectionObserver), and never started under FX OFF or reduced motion.
export function initFxField({ root = document.documentElement, host = document.body } = {}) {
  if (!host || typeof IntersectionObserver === 'undefined') return null;
  const canvas = h('canvas', { class: 'fx-field', 'aria-hidden': 'true' });
  host.prepend(canvas);
  const ctx = canvas.getContext && canvas.getContext('2d');
  if (!ctx) {
    canvas.remove();
    return null;
  }

  const reduce = window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : { matches: false };
  const particles = [];
  let width = 0;
  let height = 0;
  let raf = 0;
  let last = 0;
  let onScreen = true;

  const shouldRun = () =>
    onScreen && !document.hidden && !root.classList.contains('fx-off') && !reduce.matches;

  function resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function seed() {
    particles.length = 0;
    for (let i = 0; i < 46; i += 1) {
      particles.push({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.16,
        vy: (Math.random() - 0.5) * 0.16,
        r: 0.6 + Math.random() * 1.5,
        a: 0.1 + Math.random() * 0.22,
        c: i % 7 === 0 ? '#A78BFA' : '#06B6D4',
      });
    }
  }

  function frame(now) {
    raf = window.requestAnimationFrame(frame);
    if (now - last < 33) return; // 30 fps cap
    last = now;
    ctx.clearRect(0, 0, width, height);
    for (const p of particles) {
      p.x = (p.x + p.vx + width) % width;
      p.y = (p.y + p.vy + height) % height;
      ctx.globalAlpha = p.a;
      ctx.fillStyle = p.c;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  function stop() {
    if (raf) {
      window.cancelAnimationFrame(raf);
      raf = 0;
    }
  }

  function sync() {
    if (shouldRun()) {
      if (!raf) {
        resize();
        if (!particles.length) seed();
        raf = window.requestAnimationFrame(frame);
      }
    } else {
      stop();
    }
  }

  document.addEventListener('visibilitychange', sync);
  new IntersectionObserver((entries) => {
    onScreen = entries.some((entry) => entry.isIntersecting);
    sync();
  }, { threshold: 0 }).observe(canvas);
  if (typeof reduce.addEventListener === 'function') reduce.addEventListener('change', sync);
  else if (typeof reduce.addListener === 'function') reduce.addListener(sync);
  // FX OFF adds or removes .fx-off on the root element; restart or stop with it.
  new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ['class'] });
  window.addEventListener('resize', () => {
    if (raf) resize();
  });
  sync();
  return canvas;
}

// ------------------------------------------------------------------- main

function renderStrip(data) {
  const stamp = data.feed?.last_completed_collection;
  const value = document.getElementById('strip-collection');
  if (value) {
    clear(value).append(h('time', { datetime: stamp || undefined, text: stamp ? `${formatUtc(stamp)} UTC` : 'NO COMPLETED COLLECTION YET' }));
  }
  const local = document.getElementById('strip-local');
  if (local) local.textContent = stamp ? `SYDNEY ${formatSydney(stamp)}` : '';
}

function showError(message) {
  const box = document.getElementById('load-error');
  if (!box) return;
  box.hidden = false;
  box.textContent = message;
}

// The SYSTEM and SOURCES views. pages.js draws them; this gives each its host and its inputs and
// puts what it hands back — { label, led } — in the view head. Any input may be null (no snapshot
// read, or a file missing) and the page says so itself. A page that throws must not take the
// rest of the site down with it, so the failure is caught here and the view says it plainly.
const PAGE_LEDS = new Set(['ok', 'warn', 'fail', 'idle']);

function renderPage(name, draw, args) {
  const host = document.getElementById(`${name}-page`);
  if (!host) return;
  let out = null;
  try {
    out = draw(host, args);
  } catch {
    clear(host).append(h('p', { class: 'empty', text: `THE ${name.toUpperCase()} PAGE COULD NOT BE DRAWN.` }));
    out = { label: 'NOT DRAWN', led: 'fail' };
  }
  const count = document.getElementById(`${name}-count`);
  if (count) count.textContent = out?.label ? String(out.label) : 'AWAITING DATA';
  const led = document.getElementById(`${name}-led`);
  if (led) led.dataset.state = PAGE_LEDS.has(out?.led) ? out.led : 'idle';
}

export async function main() {
  initCommon();
  // The views come up before the data, so the address is read (and an old anchor rewritten)
  // straight away, and the right view is showing while the snapshot loads. `refresh` is filled
  // in once there is something to render.
  let refresh = () => {};
  const views = initViews({ onChange: () => refresh() });
  // The org chart ships with the site, so it is asked for at once rather than after the feed.
  const orgRead = getJson('assets/org.json', globalThis.fetch);
  let data = null;
  try {
    data = await loadData();
  } catch (err) {
    showError(`The published data could not be read (${err.message}). Try again after the next collection completes.`);
  }

  // Two kinds of content on this page, and only one of them depends on the feed.
  //
  // The crew roster, the org chart, the routines and the per-agent ownership table are
  // presentation: they describe what the system is, not what the last run found, and they ship
  // with the site. So they render whether or not data/ could be read. Gating the roster behind a
  // successful read was a defect, not a simplification — until the data branch existed, THE CREW
  // served an empty grid underneath a count that still read its full size. The roster's own
  // fallbacks already say RUNS IN PAPERCLIP and that its workload is not tracked here per card,
  // which is the honest statement; reaching them was the problem. SYSTEM and SOURCES take whatever was
  // read, nulls included, and say what is missing.
  //
  // Everything below the guard is a reading of the feed, and each one would have to invent a
  // figure to render on empty input: the index and the gauges both compute "0 events, LOW" from
  // no events, and the headline list would claim this snapshot has no headlines when the truth is
  // that no snapshot was read. A fabricated zero is worse than an absence, so those wait.
  renderStrip(data || {});
  const crew = data ? await getJson(`${data.base}crew.json`, globalThis.fetch) : null;
  renderCrew(crew);
  renderCrewRun(data?.status || null, data?.feed || null);
  renderOrg(await orgRead);
  renderPage('system', renderSystemPage, { status: data?.status || null, health: data?.health || null, crew, live: data?.feed || null });
  renderPage('sources', renderSourcesPage, { health: data?.health || null, live: data?.feed || null });
  if (!data) {
    renderSections({ events: [], unread: true });
    renderTrends(null, [], { unread: true });
    // A deep link is still a deep link on a page with no events. The views have just been given
    // their final (one-line) contents, so this is the same moment as the call at the end of the
    // readable path: the layout will not move again.
    views?.rescrollToHash();
    return;
  }

  // Before the first refresh: EMERGING THREATS reads which events the trends say are rising.
  // Trends count stored reports over days, so they are not narrowed by the scope or the tags.
  renderTrends(await getJson(`${data.base}trends.json`, globalThis.fetch), data.events);

  const total = data.events.length;
  const scored = data.events.some((e) => importanceOf(e));
  const count = document.getElementById('filter-count');
  const dashCount = document.getElementById('dash-count');

  const navigate = (next, opts) => view.navigate(next, opts);
  const toggleTag = (dim, value) => {
    const st = view.state;
    const tags = { ...st.tags };
    const values = (tags[dim] || []).includes(value) ? tags[dim].filter((v) => v !== value) : [...(tags[dim] || []), value];
    if (values.length) tags[dim] = values;
    else delete tags[dim];
    navigate({ ...st, tags, event: null }, { replace: true });
    // The choice is made, so the menu gets out of the way of the list it just changed.
    view.menus.applied('filter-more');
  };

  // The world map and the country dropdown filter in exactly the same way a country chip does,
  // through the state's country tag. Both hand over every raw token for the chosen ISO id, so
  // the filter matches the count the map and its aria-label promise. The map is redrawn only when
  // the events it is drawn from change — the scope, the beat or another tag — never for the
  // country it is itself choosing.
  const onCountry = (tokens) => {
    const tags = { ...view.state.tags };
    if (tokens && [...tokens].length) tags.country = [...tokens].map(String);
    else delete tags.country;
    navigate({ ...view.state, tags, event: null }, { replace: true });
  };
  let mapKey = null;
  let mapDraw = Promise.resolve();

  refresh = () => {
    const st = view.state;
    const scoped = filterEvents(data.events, st, { skip: ['feed'] });
    const listed = filterEvents(data.events, st);
    filtersActive = st.scope !== 'all' || st.beat.length > 0 || st.feed.length > 0 || st.key || Object.keys(st.tags).length > 0;

    renderSections({ events: listed });
    renderEventsHead(data.events, listed, st);
    renderIndex({ events: scoped, feed: data.feed });
    renderGauges({ events: scoped });
    renderRadar({ events: scoped });
    renderHeadlines(scoped);
    renderWatch('developing-events', scoped);
    renderWatch('emerging-threats', scoped);
    renderFeeds(data.events, st);
    const facetSkip = st.view === 'events' ? ['tags'] : ['tags', 'feed'];
    renderFacetUi(buildFacets(filterEvents(data.events, st, { skip: facetSkip })), st.tags, toggleTag);

    const shown = st.view === 'events' ? listed.length : scoped.length;
    if (count) count.textContent = `${shown} OF ${total} EVENTS`;
    // "n KEY" only once the snapshot carries the score: before that it would be a made-up zero.
    if (dashCount) {
      dashCount.textContent = [
        plural(scoped.length, 'signal').toUpperCase(),
        scored ? `${scoped.filter(isKey).length} KEY` : null,
        SCOPES[st.scope],
      ]
        .filter(Boolean)
        .join(' · ');
    }

    const pool = filterEvents(data.events, st, { skip: ['feed', 'country'] });
    const key = pool.map((e) => e.event_id).join('|');
    const selected = st.tags.country || null;
    if (key !== mapKey) {
      mapKey = key;
      mapDraw = mapDraw.then(() => renderMap(pool, onCountry, { selected }));
    } else {
      mapDraw = mapDraw.then(() => syncMapSelection(selected));
    }
  };

  // CLEAR FILTERS clears what narrows the list — the tags, the beat, the feed and KEY ONLY — and
  // keeps the scope, which is a choice of where to look rather than a filter on what is there.
  const clearFilters = () => {
    syncMapSelection('');
    navigate({ ...view.state, beat: [], feed: [], key: false, tags: {}, event: null });
  };
  const clearCountry = () => {
    const tags = { ...view.state.tags };
    delete tags.country;
    syncMapSelection('');
    navigate({ ...view.state, tags, event: null }, { replace: true });
  };
  document.getElementById('filter-clear')?.addEventListener('click', clearFilters);
  document.getElementById('map-clear')?.addEventListener('click', clearCountry);
  Object.assign(view, { events: data.events });
  refresh();
  await mapDraw;
  // The views now exist at their real size, so a deep link into one can finally be honoured, and
  // an ?event= link can open its card — widening the scope first if the scope hides it.
  views?.rescrollToHash();
  if (view.state.event) revealEvent(view.state.event, { replace: true });
}

// ---------------------------------------------------------------- event page

async function mainEvent() {
  initCommon();
  const host = document.getElementById('event-detail');
  if (!host) return;
  const id = new URLSearchParams(window.location.search).get('id');
  if (!id) {
    showError('No event was specified. Choose one from the dashboard or the history.');
    return;
  }
  let found = null;
  try {
    found = await findEvent(id);
  } catch {
    found = null;
  }
  if (!found) {
    showError(`Event ${id} is not in the published snapshots. It may belong to a day that has not been published.`);
    return;
  }
  const { event, events, feed, mergedFrom } = found;
  if (mergedFrom) {
    // The link named an event since merged into this one: show this one's address.
    const url = new URL(window.location.href);
    url.searchParams.set('id', event.event_id);
    window.history.replaceState(null, '', url);
  }
  renderStrip({ feed });
  document.title = `${event.title} | CyberPulse-AI`;
  const wanted = new Set((event.relationships || []).map((r) => r.event_id));
  const cveIds = new Set((event.cves || []).map((c) => c.id));
  const related = events.filter(
    (e) =>
      e.event_id !== event.event_id &&
      (wanted.has(e.event_id) || (e.cves || []).some((c) => cveIds.has(c.id))),
  );
  clear(host).append(renderEventDetail(event, related));
  document.getElementById('event-loading')?.remove();
}

// --------------------------------------------------------------- history page

async function mainHistory() {
  initCommon();
  const nav = document.getElementById('history-nav');
  const dayHost = document.getElementById('history-day');
  const dateInput = document.getElementById('history-date');
  if (!dayHost) return;
  let loaded = null;
  try {
    loaded = await loadIndex();
  } catch {
    loaded = null;
  }
  if (!loaded) {
    showError('The history index could not be read. It is published once days have events.');
    return;
  }
  const { base, index } = loaded;
  const days = [...(index.days || [])].sort((a, b) => String(b.date).localeCompare(String(a.date)));
  if (!days.length) {
    clear(dayHost).append(h('p', { class: 'empty', text: 'NO ARCHIVED DAYS YET.' }));
    return;
  }
  const known = days.map((d) => d.date);
  // Every other panel head states how much it is showing; this one read a bare "PUBLISHED DAYS"
  // with a permanently idle lamp. The figure is only written once the index is in hand, so an
  // unreadable index still says AWAITING DATA rather than claiming zero days.
  const countLabel = document.getElementById('history-count');
  const countLed = document.getElementById('history-led');
  if (countLabel) countLabel.textContent = `${known.length} PUBLISHED ${known.length === 1 ? 'DAY' : 'DAYS'}`;
  if (countLed) countLed.dataset.state = 'ok';
  const requested = new URLSearchParams(window.location.search).get('date');
  const date = known.includes(requested)
    ? requested
    : known.includes(index.latest)
      ? index.latest
      : known[0];
  const position = known.indexOf(date);
  const older = days[position + 1];
  const newer = days[position - 1];
  const meta = days[position];

  if (nav) {
    const dayLink = (label, d) => h('a', { class: 'btn', href: `history.html?date=${d}`, text: label });
    clear(nav).append(
      older
        ? dayLink(`◀ PREVIOUS DAY ${older.date}`, older.date)
        : h('span', { class: 'btn history-nav__end', text: '◀ NO EARLIER DAY' }),
      h('span', { class: 'history-nav__now mono', text: date }),
      newer
        ? dayLink(`NEXT DAY ${newer.date} ▶`, newer.date)
        : h('span', { class: 'btn history-nav__end', text: 'NO LATER DAY ▶' }),
    );
  }

  if (dateInput) {
    dateInput.min = known[known.length - 1];
    dateInput.max = known[0];
    dateInput.value = date;
    dateInput.addEventListener('change', () => {
      const value = dateInput.value;
      if (known.includes(value)) window.location.assign(`history.html?date=${value}`);
      else showError(`No snapshot for ${value}. Published days run from ${known[known.length - 1]} to ${known[0]}.`);
    });
  }

  let day = null;
  try {
    day = await loadDay(base, meta);
  } catch {
    day = null;
  }
  if (!day) {
    clear(dayHost).append(h('p', { class: 'notice', text: `THE SNAPSHOT FOR ${date} COULD NOT BE READ.` }));
    return;
  }
  const events = Array.isArray(day.events) ? day.events : [];
  clear(dayHost).append(
    h(
      'div',
      { class: 'history-day__head' },
      h('h2', { class: 'subhead', text: `SNAPSHOT FOR UTC DAY ${date}` }),
      h('p', {
        class: 'mono hint',
        text: `PUBLISHED ${day.generated_at ? `${formatUtc(day.generated_at)} UTC` : 'unknown'} · ${events.length} EVENTS`,
      }),
    ),
    renderEventCards(events),
  );
}

// Pages opt in through <body data-page>; anything else gets the dashboard.
function route() {
  const page = document.body?.dataset?.page || '';
  if (page === 'event') mainEvent();
  else if (page === 'history') mainHistory();
  else main();
}

if (typeof document !== 'undefined') route();
