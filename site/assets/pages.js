// CyberPulse SYSTEM and SOURCES pages.
//
// Two renderers the shell calls with the published JSON (system-status, source-health, crew
// and the event snapshot), and the pure helpers they are built from, which the unit tests
// import in node. Nothing is imported: hud.js imports this module, so importing it back would
// make a cycle. The DOM is built with createElement and textContent only. Meters and strips
// are SVG sized by attribute, because the CSP forbids inline styles. Every data argument may
// be null or an older shape; each one degrades to a plain sentence, never to a crash.

const SVG_NS = 'http://www.w3.org/2000/svg';
const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const SYDNEY = 'Australia/Sydney';

const hasDom = () => typeof document !== 'undefined';
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const str = (v) => (typeof v === 'string' && v.trim() ? v.trim() : null);
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const plural = (n, one, many = `${one}s`) => (n === 1 ? one : many);

// ---------------------------------------------------------------------------------------------
// DOM helpers (a copy of hud.js's, so this module stands alone)

function setProps(el, props) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.setAttribute('class', value);
    else if (key === 'text') el.textContent = String(value);
    else el.setAttribute(key, value === true ? '' : String(value));
  }
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === '') continue;
    el.append(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
}

// Clear `el` and refill it. replaceChildren would write a null child as the text "null".
function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  setProps(el, props);
  append(el, children);
  return el;
}

function s(tag, props = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setProps(el, props);
  append(el, children);
  return el;
}

function panel(id, title, ...body) {
  return h(
    'section',
    { class: 'panel pg-panel', 'aria-labelledby': id },
    h('header', { class: 'panel__head' }, h('h3', { class: 'panel__title', id, text: title })),
    ...body,
  );
}

// ---------------------------------------------------------------------------------------------
// Time and number formatting. Times are shown in Sydney, as everywhere on the site.

const formatters = new Map();
function sydneyParts(ms) {
  if (!formatters.has('parts')) {
    formatters.set(
      'parts',
      new Intl.DateTimeFormat('en-AU', {
        timeZone: SYDNEY, day: 'numeric', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      }),
    );
  }
  const out = {};
  for (const p of formatters.get('parts').formatToParts(new Date(ms))) out[p.type] = p.value;
  return out;
}

export function parseTime(value) {
  if (typeof value !== 'string' || !value) return null;
  const t = Date.parse(value);
  return Number.isFinite(t) ? t : null;
}

function nowMs(now) {
  if (now instanceof Date) return now.getTime();
  if (typeof now === 'number' && Number.isFinite(now)) return now;
  return parseTime(now) ?? Date.now();
}

// "21:00", or "3 OCT 21:00" when the day is not today's in Sydney.
export function sydneyClock(ms, now = Date.now()) {
  const p = sydneyParts(ms);
  const today = sydneyParts(nowMs(now));
  const clock = `${p.hour}:${p.minute}`;
  const sameDay = p.day === today.day && p.month === today.month && p.year === today.year;
  return sameDay ? clock : `${p.day} ${String(p.month).toUpperCase()} ${clock}`;
}

function sydneyStamp(ms) {
  const p = sydneyParts(ms);
  return `${p.day} ${p.month} ${p.hour}:${p.minute}`;
}

export function fmtAgo(ms) {
  if (!Number.isFinite(ms) || ms < 60 * 1000) return 'just now';
  if (ms < HOUR) return `${Math.round(ms / 60000)} min ago`;
  if (ms < 48 * HOUR) return `${Math.round(ms / HOUR)} h ago`;
  const d = Math.round(ms / DAY);
  return `${d} ${plural(d, 'day')} ago`;
}

export function fmtDays(days) {
  if (!Number.isFinite(days) || days < 0) return null;
  if (days < 1 / 24) return 'under an hour';
  if (days < 1) return `${Math.round(days * 24)} h`;
  const d = days < 10 ? Math.round(days * 10) / 10 : Math.round(days);
  return `${d} ${plural(d, 'day')}`;
}

function fmtDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60 * 1000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 60000)} min`;
}

function fmtInt(v) {
  const n = num(v);
  return n === null ? '—' : Math.round(n).toLocaleString('en-AU');
}

export function fmtUsd(v) {
  const n = num(v);
  if (n === null) return null;
  if (n > 0 && n < 0.01) return 'under US$0.01';
  return `US$${n.toFixed(2)}`;
}

function shortText(value, max = 120) {
  const line = str(String(value ?? '').split('\n')[0]);
  if (!line) return null;
  const flat = line.replace(/\s+/g, ' ');
  return flat.length > max ? `${flat.slice(0, max - 1).trimEnd()}…` : flat;
}

// Error text from a feed rarely ends a sentence; the cards read better when it does.
function sentence(text) {
  if (!text) return text;
  return /[.!?…]$/.test(text) ? text : `${text}.`;
}

// ---------------------------------------------------------------------------------------------
// Cron to plain English. Covers the shapes the scheduler uses; anything else is "custom".

const DAY_NAMES = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays'];
const DAY_ABBR = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };
const COUNT_WORDS = { 2: 'twice', 3: 'three times', 4: 'four times' };

export function tzLabel(tz) {
  const t = str(tz);
  if (!t || t === 'UTC' || t === 'Etc/UTC') return 'UTC';
  if (t === SYDNEY) return 'Sydney';
  return t.split('/').pop().replace(/_/g, ' ');
}

function andList(items) {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

const pad2 = (n) => String(n).padStart(2, '0');
const isInt = (f) => /^\d+$/.test(f);
const intList = (f) => (/^\d+(,\d+)+$/.test(f) ? f.split(',').map(Number) : null);
const inRange = (values, lo, hi) => values.every((v) => v >= lo && v <= hi);

function dayWords(field) {
  const f = field.toLowerCase();
  if (f === '*') return '';
  if (['1-5', 'mon-fri'].includes(f)) return 'weekdays';
  if (['0,6', '6,0', 'sat,sun', 'sun,sat', '6,7'].includes(f)) return 'weekends';
  const days = f.split(',').map((d) => (Object.hasOwn(DAY_ABBR, d) ? DAY_ABBR[d] : /^\d$/.test(d) ? Number(d) % 7 : NaN));
  if (days.some((d) => !Number.isInteger(d) || d < 0 || d > 6)) return null;
  return andList([...new Set(days)].sort((a, b) => a - b).map((d) => DAY_NAMES[d]));
}

function ordinal(n) {
  const v = n % 100;
  const suffix = v >= 11 && v <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th');
  return `${n}${suffix}`;
}

export function cronToText(cron, tz) {
  const custom = 'custom schedule';
  const raw = str(cron);
  if (!raw) return custom;
  const zone = tzLabel(tz);
  const macros = {
    '@hourly': '0 * * * *', '@daily': '0 0 * * *', '@midnight': '0 0 * * *',
    '@weekly': '0 0 * * 0', '@monthly': '0 0 1 * *',
  };
  const fields = (macros[raw.toLowerCase()] || raw).split(/\s+/);
  if (fields.length !== 5) return custom;
  const [min, hour, dom, mon, dow] = fields;
  if (mon !== '*') return custom;

  // Clock times, when the minute and hour are fixed values or lists.
  const minutes = isInt(min) ? [Number(min)] : intList(min);
  const hours = isInt(hour) ? [Number(hour)] : intList(hour);
  const fixedTimes = () => {
    if (!minutes || !hours || !inRange(minutes, 0, 59) || !inRange(hours, 0, 23)) return null;
    const times = hours.flatMap((hh) => minutes.map((mm) => `${pad2(hh)}:${pad2(mm)}`)).sort();
    return times.length <= 4 ? `${andList(times)} ${zone}` : null;
  };

  if (dom !== '*' || dow !== '*') {
    const at = fixedTimes();
    if (!at) return custom;
    if (dom !== '*' && dow === '*') {
      const days = isInt(dom) ? [Number(dom)] : intList(dom);
      if (!days || !inRange(days, 1, 31)) return custom;
      return `monthly on the ${andList(days.map(ordinal))} at ${at}`;
    }
    if (dom !== '*') return custom;
    const words = dayWords(dow);
    return words ? `${words} ${at}` : custom;
  }

  // Every hour (or every minute), at given minutes.
  if (hour === '*') {
    if (min === '*') return 'every minute';
    let m = /^\*\/(\d+)$/.exec(min);
    if (m && Number(m[1]) > 0) return Number(m[1]) === 1 ? 'every minute' : `every ${Number(m[1])} minutes`;
    m = /^(\d+)-(\d+)\/(\d+)$/.exec(min);
    if (m && Number(m[3]) > 0 && inRange([Number(m[1]), Number(m[2])], 0, 59)) {
      return `every ${Number(m[3])} minutes from :${pad2(m[1])}`;
    }
    if (!minutes || !inRange(minutes, 0, 59)) return custom;
    if (minutes.length === 1) return minutes[0] === 0 ? 'every hour on the hour' : `every hour at :${pad2(minutes[0])}`;
    const marks = andList([...minutes].sort((a, b) => a - b).map((v) => `:${pad2(v)}`));
    return COUNT_WORDS[minutes.length] ? `${COUNT_WORDS[minutes.length]} an hour at ${marks}` : `every hour at ${marks}`;
  }

  // Every n hours.
  const step = /^(?:\*|(\d+)-(\d+))\/(\d+)$/.exec(hour);
  if (step && minutes && minutes.length === 1 && inRange(minutes, 0, 59) && Number(step[3]) > 0) {
    const n = Number(step[3]);
    const every = n === 1 ? 'every hour' : `every ${n} hours`;
    if (step[1] !== undefined && Number(step[1]) !== 0) {
      return `${every} at :${pad2(minutes[0])}, from ${pad2(step[1])}:${pad2(minutes[0])} ${zone}`;
    }
    return minutes[0] === 0 ? every : `${every} at :${pad2(minutes[0])}`;
  }

  const at = fixedTimes();
  return at ? `daily ${at}` : custom;
}

// ---------------------------------------------------------------------------------------------
// What the SYSTEM page says. Kept here so the copy is in one place and testable.

export const STORY_STAGES = [
  { id: 'collect', name: 'COLLECT', kind: 'code', text: 'Reads each source feed, ten at a time, and tidies every item.' },
  { id: 'match', name: 'MATCH', kind: 'code', text: 'Each item becomes a new story, an update or a duplicate. A doubtful match stays separate.' },
  { id: 'merge', name: 'MERGE', kind: 'code', text: 'Joins reports of one story from the last 30 days and works out which outlets copied which.' },
  { id: 'ground', name: 'GROUND TRUTH', kind: 'code', text: 'Every 6 hours, checks the KEV, EPSS, CVSS, OSV and MITRE registers. Unknown stays unknown.' },
  { id: 'au', name: 'AUSTRALIA', kind: 'code', text: 'Facts set the Australian floor: an agency, organisation, place, .au address or outlet.' },
  { id: 'score', name: 'SCORE', kind: 'code', text: 'Urgency, confidence and freshness, plus importance and source reputation, all by formula.' },
  { id: 'status', name: 'STATUS', kind: 'code', text: 'Marks each story new, developing, contained, resolved and so on. Faded stories are archived.' },
  { id: 'publish', name: 'PUBLISH', kind: 'code', text: 'Checks the schema and scans for secrets, then writes the files this site reads. Any failure stops it.' },
  { id: 'enrich', name: 'AI ENRICHMENT', kind: 'ai', side: true, text: 'Twice an hour, models add summaries and topics, and a severity estimate where no official score exists. Checked, and marked AI.' },
  { id: 'crew', name: 'CREW', kind: 'ai', side: true, text: 'Agents edit, check quality, look for new sources and watch the cost. Nothing waits for them.' },
];

export const CODE_DOES = [
  'Collecting, matching and merging stories',
  'Ground truth from KEV, EPSS, CVSS, OSV and MITRE',
  'Australian relevance floors, from facts',
  'Severity from official scores',
  'Scores, importance and source reputation',
  'Status, archiving and publishing',
];

export const AI_DOES = [
  'Topics, entities and tags',
  'Summaries and why a story matters',
  'A first Australian reading, which can raise relevance but never lower it',
  'A severity estimate where no official score exists, replaced when one arrives',
  'MITRE technique suggestions, picked from the loaded catalogue',
  'The crew: editing, quality checks, source discovery and cost',
];

export const BUDGET_SENTENCE =
  'AI runs under a hard US$20 monthly cap: as it runs down, AI steps move to cheaper or free ' +
  'models and then stop, and collection and publishing never depend on them.';

export const FALLBACK_SCHEDULE = [
  { job: 'fast', label: 'FAST LANE', cron: '0 * * * *', timezone: 'UTC', kind: 'code', summary: 'Checks the quickest sources: advisories and busy news feeds.' },
  { job: 'normal', label: 'NORMAL LANE', cron: '0 */4 * * *', timezone: 'UTC', kind: 'code', summary: 'Checks the rest of the regular sources.' },
  { job: 'deep', label: 'DEEP LANE AND DISCOVERY', cron: '10 3 * * *', timezone: SYDNEY, kind: 'mixed', summary: 'Slow sources, and a search for new ones.' },
];

const KIND_LABEL = { code: 'CODE', ai: 'AI', mixed: 'CODE + AI' };
const KIND_GLYPH = { code: '■', ai: '◇', mixed: '◈' };

function kindMarker(kind) {
  if (!Object.hasOwn(KIND_LABEL, kind)) return null;
  return h(
    'span',
    { class: 'pg-kind', 'data-kind': kind },
    h('span', { class: 'pg-kind__glyph', 'aria-hidden': 'true', text: KIND_GLYPH[kind] }),
    KIND_LABEL[kind],
  );
}

// ---------------------------------------------------------------------------------------------
// Sources: attention, grouping, reputation, sorting and filtering.

const FAIL_STATUSES = new Set(['error', 'timeout', 'empty', 'failed']);
const WATCH_STATUSES = new Set(['stale', 'degraded']);
const COMING_LIFECYCLES = new Set(['discovered', 'candidate']);
const ATTENTION_KINDS = new Set(['fix', 'watch', 'coming']);

export const STANDING_POINTS = { authoritative: 90, established: 75, specialist: 60, community: 40 };
export const REPUTATION_WEIGHTS = [
  ['Standing', 60, 'what kind of voice the source is'],
  ['Uptime', 15, 'share of checks that work'],
  ['Corroboration', 25, 'how often other sources confirm its stories'],
];
// The most each part can add (worker/pipeline/importance.py; test_site_pages checks they agree).
export const IMPORTANCE_POINTS = [
  ['Harm: severity, exploitation, an incident', 25],
  ['Australia', 25],
  ['Source reputation, a quarter of it', 25],
  ['Public sector or critical infrastructure', 15],
  ['Corroboration', 12],
  ['Cyber and AI together', 5],
];
export const KEY_AT = 60;
export const NOTABLE_AT = 40;

const STATUS_LABEL = {
  ok: 'OK', stale: 'STALE', degraded: 'DEGRADED', error: 'ERROR', timeout: 'TIMEOUT',
  empty: 'EMPTY', failed: 'FAILED', disabled: 'DISABLED', none: 'NOT CHECKED',
};
const GROUP_GLYPH = { ok: '●', watch: '▲', fail: '■', off: '○' };
const LIFECYCLE_LABEL = {
  discovered: 'DISCOVERED', candidate: 'CANDIDATE', testing: 'TESTING', validated: 'VALIDATED',
  active: 'ACTIVE', degraded: 'DEGRADED', broken: 'BROKEN', retired: 'RETIRED',
};
const CATEGORY_LABEL = {
  news: 'News', advisory: 'Advisories', research: 'Research', ai_security: 'AI security writing',
  analysis: 'Analysis', vendor_advisory: 'Vendor advisories', social: 'Posts',
};
const REGION_NAME = { au: 'AU', global: 'GLOBAL' };

export function statusOf(check) {
  const st = isObj(check) ? str(check.status) : null;
  return st ? st.toLowerCase() : 'none';
}

export function statusGroup(status) {
  if (status === 'ok') return 'ok';
  if (WATCH_STATUSES.has(status)) return 'watch';
  if (FAIL_STATUSES.has(status)) return 'fail';
  return 'off';
}

function checksOf(source) {
  const list = Array.isArray(source?.history) ? source.history.filter(isObj) : [];
  return list
    .map((c, i) => ({ c, i, t: parseTime(c.checked_at) }))
    .sort((a, b) => (a.t ?? 0) - (b.t ?? 0) || a.i - b.i)
    .map((x) => x.c);
}

// The newest check that came back OK, from the history or the latest check.
export function lastGoodCheck(source) {
  const checks = checksOf(source);
  if (isObj(source?.latest)) checks.push(source.latest);
  let best = null;
  for (const c of checks) {
    const t = parseTime(c.checked_at);
    if (statusOf(c) === 'ok' && t !== null && (best === null || t > best)) best = t;
  }
  return best === null ? null : new Date(best).toISOString();
}

function comingReason(source) {
  const life = source.lifecycle_state;
  if (life === 'discovered') return 'Listed, waiting to be tested and switched on.';
  if (life === 'candidate') return 'Being tested before it is switched on.';
  return 'Not switched on yet.';
}

// What, if anything, a source needs: { kind: 'fix'|'watch'|'coming'|'off'|null, reason }.
// A published `attention` object wins. Without one (an older file, or null for "nothing to
// add"), it is worked out from the lifecycle and the latest check.
export function attentionOf(source) {
  if (!isObj(source)) return { kind: null, reason: '' };
  const life = str(source.lifecycle_state);
  const latest = isObj(source.latest) ? source.latest : null;
  const status = statusOf(latest);
  const given = isObj(source.attention) ? source.attention : null;
  if (given && ATTENTION_KINDS.has(given.kind)) {
    const fallback = given.kind === 'coming' ? comingReason(source) : `Last check: ${STATUS_LABEL[status] || 'unknown'}.`;
    return { kind: given.kind, reason: sentence(shortText(given.reason, 160)) || fallback };
  }
  if (life === 'retired') return { kind: 'off', reason: 'Retired.' };
  if (source.enabled === false || COMING_LIFECYCLES.has(life) || status === 'disabled') {
    return { kind: 'coming', reason: comingReason(source) };
  }
  if (life === 'broken' || FAIL_STATUSES.has(status)) {
    const err = sentence(shortText(latest?.error));
    if (!latest) return { kind: 'fix', reason: 'Marked broken after repeated failures.' };
    return { kind: 'fix', reason: err ? `Last check ${STATUS_LABEL[status] || 'failed'}: ${err}` : `Last check: ${STATUS_LABEL[status] || 'failed'}.` };
  }
  if (WATCH_STATUSES.has(status) || life === 'degraded') {
    const age = fmtDays(num(latest?.newest_item_age_days));
    if (status === 'stale' && age) return { kind: 'watch', reason: `No new item for ${age}.` };
    if (status === 'ok') return { kind: 'watch', reason: 'Recovering after recent failures.' };
    return { kind: 'watch', reason: sentence(shortText(latest?.error)) || 'Only partly working at the last check.' };
  }
  if (!latest) return { kind: 'watch', reason: 'No check recorded yet.' };
  return { kind: null, reason: '' };
}

export function regionOf(source) {
  return String(source?.region || '').toLowerCase() === 'au' ? 'au' : 'global';
}

export function nameOf(source) {
  return str(source?.name) || str(source?.source_id) || str(source?.host) || 'Unnamed source';
}

function pct(v) {
  const n = num(v);
  if (n === null) return null;
  return Math.round(clamp(n <= 1 ? n * 100 : n, 0, 100));
}

export function reputationOf(source) {
  const r = isObj(source?.reputation) ? source.reputation : null;
  const pick = (v) => (Object.hasOwn(STANDING_POINTS, v) ? v : null);
  const score = num(r?.score);
  const events = num(r?.events_90d);
  return {
    score: score === null ? null : Math.round(clamp(score, 0, 100)),
    standing: pick(r?.standing) || pick(source?.standing),
    uptime: pct(r?.uptime),
    corroboration: pct(r?.corroboration),
    events: events === null ? null : Math.max(0, Math.round(events)),
    basis: shortText(r?.basis, 160),
    version: str(r?.version),
  };
}

export function classifySources(health) {
  if (!isObj(health) || !Array.isArray(health.sources)) return null;
  const sources = health.sources.filter(isObj);
  const groups = { fix: [], watch: [], active: [], coming: [], off: [] };
  for (const src of sources) {
    const { kind } = attentionOf(src);
    if (kind === 'fix') groups.fix.push(src);
    else if (kind === 'watch') groups.watch.push(src);
    else if (kind === 'coming') groups.coming.push(src);
    else if (kind === 'off') groups.off.push(src);
    else groups.active.push(src);
  }
  groups.pipeline = Array.isArray(health.pipeline) ? health.pipeline.filter(isObj) : [];
  groups.counts = {
    total: sources.length,
    healthy: groups.active.length,
    fix: groups.fix.length,
    watch: groups.watch.length,
    coming: groups.coming.length + groups.pipeline.length,
    retired: groups.off.length,
  };
  return groups;
}

export function filterSources(list, region = 'all') {
  const items = Array.isArray(list) ? list.filter(isObj) : [];
  if (region !== 'au' && region !== 'global') return items;
  return items.filter((src) => regionOf(src) === region);
}

const ATTENTION_RANK = { fix: 0, watch: 1, coming: 3, off: 4 };
const GROUP_RANK = { fail: 0, watch: 1, off: 2, ok: 3 };

export function sortSources(list, by = 'reputation') {
  const items = Array.isArray(list) ? list.filter(isObj) : [];
  const byName = (a, b) => nameOf(a).localeCompare(nameOf(b), 'en', { sensitivity: 'base' });
  if (by === 'name') return items.sort(byName);
  if (by === 'status') {
    const rank = (src) => ATTENTION_RANK[attentionOf(src).kind] ?? 2;
    const group = (src) => GROUP_RANK[statusGroup(statusOf(src.latest))];
    return items.sort((a, b) => rank(a) - rank(b) || group(a) - group(b) || byName(a, b));
  }
  const score = (src) => reputationOf(src).score ?? -1;
  return items.sort((a, b) => score(b) - score(a) || byName(a, b));
}

// ---------------------------------------------------------------------------------------------
// Events: importance tiers and links into the EVENTS view.

export function tierOf(event) {
  const imp = isObj(event?.importance) ? event.importance : null;
  if (!imp) return null;
  if (['key', 'notable', 'routine'].includes(imp.tier)) return imp.tier;
  const score = num(imp.score);
  if (score === null) return null;
  return score >= KEY_AT ? 'key' : score >= NOTABLE_AT ? 'notable' : 'routine';
}

export function tierCounts(events) {
  const out = { key: 0, notable: 0, routine: 0, unrated: 0, total: 0 };
  for (const e of Array.isArray(events) ? events : []) {
    if (!isObj(e)) continue;
    out.total += 1;
    out[tierOf(e) || 'unrated'] += 1;
  }
  return out;
}

export function topKey(events, n = 3) {
  const list = (Array.isArray(events) ? events : []).filter((e) => isObj(e) && tierOf(e) === 'key' && str(e.event_id));
  const score = (e) => num(e.importance?.score) ?? -1;
  const prominence = (e) => num(e.risk?.prominence) ?? 0;
  return list.sort((a, b) => score(b) - score(a) || prominence(b) - prominence(a)).slice(0, n);
}

function isAu(e) {
  return Boolean(e?.au?.directly_reported_in_au) || (num(e?.au?.relevance) ?? 0) >= 0.5;
}

// The EVENTS view opened on one story. AU stories open under AUSTRALIA, the rest under ALL,
// which is where the shell's sectionFor would widen to anyway.
export function eventHref(event) {
  const id = str(event?.event_id);
  if (!id) return null;
  const q = new URLSearchParams();
  q.set('view', 'events');
  q.set('scope', isAu(event) ? 'au' : 'all');
  q.set('event', id);
  return `?${q.toString()}`;
}

function safeLink(url) {
  const raw = str(url);
  if (!raw) return null;
  try {
    const u = new URL(raw);
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
    return { href: u.href, host: u.hostname.replace(/^www\./, '') };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------------------------
// Summaries: the label and LED the shell puts in each view's header.

export function systemSummary({ status = null, live = null, now } = {}) {
  const run = isObj(status) && isObj(status.last_run) ? status.last_run : null;
  if (!run) return { label: isObj(status) || isObj(live) ? 'NO RUN YET' : 'AWAITING DATA', led: 'idle' };
  const t = parseTime(run.finished_at) ?? parseTime(status.last_completed_collection) ?? parseTime(run.started_at);
  const at = nowMs(now);
  const label = t === null ? 'LAST RUN TIME UNKNOWN' : `LAST RUN ${sydneyClock(t, at)} SYDNEY`;
  const failed = num(run.sources_failed) ?? 0;
  const ok = num(run.sources_ok) ?? 0;
  let led = 'ok';
  if (failed > 0 && ok === 0) led = 'fail';
  else if (failed > 0) led = 'warn';
  else if (t !== null && at - t > 3 * HOUR) led = 'warn';
  return { label, led };
}

export function sourcesSummary({ health = null } = {}) {
  const groups = classifySources(health);
  if (!groups) return { label: 'AWAITING DATA', led: 'idle' };
  const { total, fix, watch } = groups.counts;
  if (total === 0) return { label: 'NO SOURCES LISTED', led: 'idle' };
  const lead = `${total} ${plural(total, 'SOURCE', 'SOURCES')}`;
  if (fix) return { label: `${lead} · ${fix} ${fix === 1 ? 'NEEDS' : 'NEED'} FIXING`, led: 'fail' };
  if (watch) return { label: `${lead} · ${watch} TO WATCH`, led: 'warn' };
  return { label: `${lead} · ALL HEALTHY`, led: 'ok' };
}

// ---------------------------------------------------------------------------------------------
// SYSTEM page

function stageItem(stage, n) {
  return h(
    'li',
    { class: 'pg-stage', 'data-kind': stage.kind },
    h(
      'div',
      { class: 'pg-stage__top' },
      n ? h('span', { class: 'pg-stage__n mono', 'aria-hidden': 'true', text: pad2(n) }) : null,
      kindMarker(stage.kind),
    ),
    h('p', { class: 'pg-stage__name', text: stage.name }),
    h('p', { class: 'pg-stage__text', text: stage.text }),
  );
}

function flowPanel() {
  const main = STORY_STAGES.filter((st) => !st.side);
  const side = STORY_STAGES.filter((st) => st.side);
  return panel(
    'pg-sys-flow',
    'HOW A STORY GETS HERE',
    h('ol', { class: 'pg-flow', 'aria-label': 'The pipeline, in order' }, main.map((st, i) => stageItem(st, i + 1))),
    h('p', { class: 'subhead', text: 'BESIDE THE PIPELINE' }),
    h('ul', { class: 'pg-flow pg-flow--side', 'aria-label': 'Beside the pipeline' }, side.map((st) => stageItem(st))),
    h('p', { class: 'hint', text: 'Every numbered step is code and runs without AI. The AI steps beside it add to stories; nothing waits for them.' }),
  );
}

function splitPanel() {
  const column = (kind, head, items) =>
    h(
      'div',
      { class: 'pg-split__col', 'data-kind': kind },
      h('p', { class: 'pg-split__head' }, kindMarker(kind), h('span', { text: head })),
      h('ul', { class: 'pg-list' }, items.map((t) => h('li', { text: t }))),
    );
  return panel(
    'pg-sys-split',
    'WHAT IS CODE, WHAT IS AI',
    h('div', { class: 'pg-split' }, column('code', 'Rules and formulas', CODE_DOES), column('ai', 'Language models', AI_DOES)),
    h('p', { class: 'pg-budget', text: BUDGET_SENTENCE }),
  );
}

function scheduleRows(status) {
  const list = Array.isArray(status?.schedule) ? status.schedule.filter((r) => isObj(r) && str(r.cron)) : [];
  return list.length ? { rows: list, fallback: false } : { rows: FALLBACK_SCHEDULE, fallback: true };
}

function schedulePanel(status) {
  const { rows, fallback } = scheduleRows(status);
  const head = ['JOB', 'WHEN', 'CRON', 'KIND', 'WHAT IT DOES'];
  const table = h(
    'table',
    { class: 'pg-table pg-schedule' },
    h('caption', { class: 'pg-sr', text: 'The schedule: each job, when it runs and what it does' }),
    h('thead', {}, h('tr', {}, head.map((t) => h('th', { scope: 'col', text: t })))),
    h(
      'tbody',
      {},
      rows.map((r) =>
        h(
          'tr',
          {},
          h('th', { scope: 'row', class: 'pg-schedule__job', text: (str(r.label) || str(r.job) || 'JOB').toUpperCase() }),
          h('td', { 'data-label': 'WHEN', text: cronToText(r.cron, r.timezone) }),
          h('td', { 'data-label': 'CRON' }, h('code', { class: 'mono', text: str(r.cron) }), h('span', { class: 'pg-tz', text: ` ${tzLabel(r.timezone)}` })),
          h('td', { 'data-label': 'KIND' }, kindMarker(r.kind) || h('span', { class: 'hint', text: '—' })),
          h('td', { 'data-label': 'WHAT IT DOES', text: str(r.summary) || '—' }),
        ),
      ),
    ),
  );
  return panel(
    'pg-sys-schedule',
    'SCHEDULE',
    table,
    fallback
      ? h('p', { class: 'hint', text: 'The full schedule has not been published yet, so these are the standing defaults. Deep collection and discovery run at about 03:10 Sydney.' })
      : h('p', { class: 'hint', text: 'Times are UTC unless marked Sydney. The site is republished after every collection run.' }),
  );
}

function aiLine(crew) {
  const ai = isObj(crew?.pipeline_ai) ? crew.pipeline_ai : null;
  const calls = num(ai?.calls);
  const cost = fmtUsd(ai?.cost_usd);
  if (calls === null && !cost) return null;
  const parts = [calls === null ? null : `${fmtInt(calls)} model ${plural(calls, 'call')}`, cost].filter(Boolean);
  return h('p', { class: 'pg-run__ai' }, kindMarker('ai'), h('span', { text: ` Pipeline AI this month: ${parts.join(', ')}.` }));
}

function lastRunPanel(status, crew, live, now) {
  const run = isObj(status?.last_run) ? status.last_run : null;
  if (!run) {
    return panel('pg-sys-run', 'LAST RUN', h('p', { class: 'empty', text: 'No completed run has been published yet.' }), aiLine(crew));
  }
  const at = nowMs(now);
  const fin = parseTime(run.finished_at);
  const st = parseTime(run.started_at);
  const lane = str(run.lane);
  const head = [
    lane ? `${lane.toUpperCase()} LANE` : 'COLLECTION RUN',
    fin === null ? null : `finished ${sydneyStamp(fin)} Sydney, ${fmtAgo(at - fin)}`,
    fin !== null && st !== null ? `took ${fmtDuration(fin - st)}` : null,
  ].filter(Boolean);
  const ok = num(run.sources_ok);
  const failed = num(run.sources_failed);
  const stale = num(run.sources_stale);
  const checked = [ok, failed, stale].some((v) => v !== null) ? (ok ?? 0) + (failed ?? 0) + (stale ?? 0) : null;
  const errors = num(run.error_count);
  const counts = isObj(live?.counts) ? live.counts : {};
  const published = num(counts.events) ?? (Array.isArray(live?.events) ? live.events.length : null);
  const pending = num(counts.pending_enrichment);
  const steps = [
    { name: 'SOURCES CHECKED', value: checked, sub: `${fmtInt(ok)} ok · ${fmtInt(failed)} failed · ${fmtInt(stale)} stale`, state: failed ? 'warn' : 'ok' },
    { name: 'ITEMS FETCHED', value: run.items_fetched, sub: 'read from the feeds' },
    { name: 'NEW', value: run.new_events, sub: 'new stories' },
    { name: 'UPDATED', value: run.updated_events, sub: 'stories with new facts' },
    { name: 'DUPLICATES', value: run.duplicates, sub: 'already held' },
    { name: 'ARCHIVED', value: run.archived_events, sub: 'merged or faded' },
    { name: 'ERRORS', value: errors, sub: errors ? 'recorded for the crew' : 'none', state: errors ? 'warn' : 'ok' },
    { name: 'PUBLISHED', value: published, sub: pending ? `stories, ${fmtInt(pending)} awaiting AI` : 'stories in this snapshot' },
  ];
  return panel(
    'pg-sys-run',
    'LAST RUN',
    h('p', { class: 'pg-run__head mono', text: head.join(' · ') }),
    h('p', { class: 'subhead', text: 'COLLECTION REPLAY' }),
    h(
      'ol',
      { class: 'pg-replay', 'aria-label': 'Collection replay, stage by stage' },
      steps.map((step) =>
        h(
          'li',
          { class: 'pg-step', 'data-state': step.state || null },
          h('span', { class: 'pg-step__name', text: step.name }),
          h('span', { class: 'pg-step__value mono', text: fmtInt(step.value) }),
          h('span', { class: 'pg-step__sub', text: step.sub }),
        ),
      ),
    ),
    aiLine(crew),
    str(run.run_id) ? h('p', { class: 'hint mono pg-run__id', text: `RUN ${run.run_id}` }) : null,
  );
}

// The most common value of a version stamp across a list, and how many carry another.
function versionAcross(items, pick) {
  const tally = new Map();
  for (const item of items) {
    const v = str(pick(item));
    if (v) tally.set(v, (tally.get(v) || 0) + 1);
  }
  if (!tally.size) return null;
  const [top, n] = [...tally.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0];
  const rest = [...tally.values()].reduce((a, b) => a + b, 0) - n;
  return rest ? `${top} (${fmtInt(rest)} on older)` : top;
}

function versionsPanel(status, health, live, now) {
  const events = Array.isArray(live?.events) ? live.events.filter(isObj) : [];
  const sources = Array.isArray(health?.sources) ? health.sources.filter(isObj) : [];
  if (!isObj(status) && !events.length) {
    return panel('pg-sys-versions', 'VERSIONS', h('p', { class: 'empty', text: 'No version stamps have been published yet.' }));
  }
  const at = nowMs(now);
  const when = (v) => {
    const t = parseTime(v);
    return t === null ? null : `${sydneyStamp(t)} Sydney (${fmtAgo(at - t)})`;
  };
  const rows = [
    ['PIPELINE', str(status?.pipeline_version) || str(live?.pipeline_version) || '—'],
    ['SCHEMA', str(status?.schema_version) || '—'],
    ['SCORING', str(status?.scoring_version) || '—'],
    ['ENRICHMENT', versionAcross(events, (e) => e.enrichment_version)],
    ['IMPORTANCE', versionAcross(events, (e) => e.importance?.version)],
    ['REPUTATION', versionAcross(sources, (src) => src.reputation?.version)],
    ['SNAPSHOT BUILT', when(status?.generated_at) || when(live?.generated_at)],
    ['LAST COMPLETED COLLECTION', when(status?.last_completed_collection) || when(live?.last_completed_collection)],
  ].filter(([, v]) => v);
  return panel(
    'pg-sys-versions',
    'VERSIONS',
    h('dl', { class: 'kv pg-versions' }, rows.map(([k, v]) => [h('dt', { text: k }), h('dd', { class: 'mono', text: v })])),
  );
}

export function renderSystemPage(host, data = {}) {
  const { status = null, health = null, crew = null, live = null, now } = isObj(data) ? data : {};
  const summary = systemSummary({ status, live, now });
  if (!host || !hasDom()) return summary;
  fill(host,
    flowPanel(),
    splitPanel(),
    schedulePanel(status),
    lastRunPanel(status, crew, live, now),
    versionsPanel(status, health, live, now),
  );
  return summary;
}

// ---------------------------------------------------------------------------------------------
// SOURCES page

const pageState = new WeakMap();

function statusBadge(status) {
  const group = statusGroup(status);
  return h(
    'span',
    { class: 'pg-status', 'data-group': group },
    h('span', { 'aria-hidden': 'true', text: GROUP_GLYPH[group] }),
    STATUS_LABEL[status] || status.toUpperCase(),
  );
}

function historyStrip(source) {
  const checks = checksOf(source).slice(-20);
  // Never checked at all: the card's reason already says so.
  if (!checks.length) return isObj(source.latest) ? h('p', { class: 'pg-history pg-history--none', text: 'No check history yet.' }) : null;
  const tally = new Map();
  for (const c of checks) {
    const label = (STATUS_LABEL[statusOf(c)] || statusOf(c)).toLowerCase();
    tally.set(label, (tally.get(label) || 0) + 1);
  }
  const words = [...tally.entries()].map(([k, n]) => `${n} ${k}`).join(', ');
  const text = `Last ${checks.length} ${plural(checks.length, 'check')}, oldest first: ${words}`;
  const W = 7;
  const GAP = 2;
  const H = 16;
  const width = checks.length * (W + GAP) - GAP;
  const cells = checks.map((c, i) => {
    const st = statusOf(c);
    const group = statusGroup(st);
    const x = i * (W + GAP);
    const shape = {
      ok: { x, y: 4, width: W, height: H - 4 },
      watch: { x: x + 0.75, y: 4.75, width: W - 1.5, height: H - 5.5 },
      fail: { x, y: 0, width: W, height: H },
      off: { x, y: 10, width: W, height: H - 10 },
    }[group];
    const t = parseTime(c.checked_at);
    return s(
      'rect',
      { class: 'pg-cell', 'data-group': group, rx: 1.5, ...shape },
      s('title', { text: `${t === null ? 'Time unknown' : sydneyStamp(t)} · ${STATUS_LABEL[st] || st.toUpperCase()}` }),
    );
  });
  const ok = checks.filter((c) => statusOf(c) === 'ok').length;
  return h(
    'div',
    { class: 'pg-history' },
    s('svg', { class: 'pg-history__cells', viewBox: `0 0 ${width} ${H}`, width, height: H, role: 'img', 'aria-label': text, focusable: 'false' }, cells),
    h('span', { class: 'pg-history__text mono', 'aria-hidden': 'true', text: `${ok}/${checks.length} OK` }),
  );
}

function reputationBlock(source) {
  const rep = reputationOf(source);
  const standing = rep.standing ? rep.standing.toUpperCase() : null;
  if (rep.score === null) {
    return h(
      'div',
      { class: 'pg-rep pg-rep--none' },
      h('span', { class: 'pg-rep__label', text: 'REPUTATION' }),
      h('span', { class: 'pg-rep__standing', text: standing ? `${standing} · not scored yet` : 'standing unknown' }),
    );
  }
  const basis = [
    rep.uptime === null ? null : `uptime ${rep.uptime}%`,
    rep.corroboration === null ? null : `corroboration ${rep.corroboration}%`,
    rep.events === null ? null : `${fmtInt(rep.events)} ${plural(rep.events, 'event')} in 90 days`,
  ].filter(Boolean);
  return h(
    'div',
    { class: 'pg-rep' },
    h(
      'div',
      { class: 'pg-rep__row' },
      h('span', { class: 'pg-rep__label', text: 'REPUTATION' }),
      h('span', { class: 'pg-rep__score mono' }, String(rep.score), h('span', { class: 'pg-rep__of', text: '/100' })),
      h('span', { class: 'pg-rep__standing', text: standing || 'standing unknown' }),
    ),
    s(
      'svg',
      { class: 'pg-meter', viewBox: '0 0 100 6', preserveAspectRatio: 'none', role: 'img', 'aria-label': `Reputation ${rep.score} out of 100`, focusable: 'false' },
      s('rect', { class: 'pg-meter__track', x: 0, y: 0, width: 100, height: 6 }),
      rep.score > 0 ? s('rect', { class: 'pg-meter__fill', x: 0, y: 0, width: rep.score, height: 6 }) : null,
    ),
    basis.length || rep.basis ? h('p', { class: 'pg-rep__basis', text: basis.length ? basis.join(' · ') : rep.basis }) : null,
  );
}

function fallbackDescription(source) {
  const cat = CATEGORY_LABEL[source.category] || (str(source.category) ? `${source.category.replace(/_/g, ' ')} feed` : 'A feed');
  const region = String(source.region || '').toLowerCase();
  const where = region === 'au' ? 'Australia' : region === 'us' ? 'the US' : 'around the world';
  return `${cat} from ${where}.`;
}

function sourceCard(source, ctx) {
  const att = attentionOf(source);
  const latest = isObj(source.latest) ? source.latest : null;
  const status = statusOf(latest);
  const region = regionOf(source);
  const life = str(source.lifecycle_state);
  const beat = str(source.beat);
  const publisher = str(source.publisher);
  const chips = [
    h('span', { class: region === 'au' ? 'chip chip--au' : 'chip', text: REGION_NAME[region] }),
    str(source.lane) ? h('span', { class: 'chip', text: `${source.lane.toUpperCase()} LANE` }) : null,
    beat ? h('span', { class: 'chip chip--beat', 'data-beat': beat, text: beat.toUpperCase() }) : null,
    life && life !== 'active' ? h('span', { class: 'chip pg-chip--life', text: LIFECYCLE_LABEL[life] || life.toUpperCase() }) : null,
  ];
  let reason = null;
  if (att.kind === 'fix' || att.kind === 'watch') {
    const good = parseTime(lastGoodCheck(source));
    // A stale feed still answers, it is only quiet, so "last good check" would mislead there.
    const everChecked = Boolean(latest) || checksOf(source).length > 0;
    let note = null;
    if (status === 'stale') note = null;
    else if (good !== null) note = ` Last good check ${fmtAgo(ctx.now - good)}.`;
    else if (everChecked) note = ' No good check on record.';
    reason = h(
      'p',
      { class: 'pg-card__reason', 'data-kind': att.kind },
      att.reason,
      note ? h('span', { class: 'pg-card__good', text: note }) : null,
    );
  } else if (att.kind === 'coming' || att.kind === 'off') {
    reason = h('p', { class: 'pg-card__reason', 'data-kind': att.kind, text: att.reason });
  }
  const facts = [];
  if (latest) {
    const age = fmtDays(num(latest.newest_item_age_days));
    facts.push(age ? `newest item ${age} old` : 'no item dated');
    if (num(latest.items_fetched) !== null) facts.push(`${fmtInt(latest.items_fetched)} ${plural(latest.items_fetched, 'item')} fetched`);
    const t = parseTime(latest.checked_at);
    if (t !== null) facts.push(`checked ${fmtAgo(ctx.now - t)}`);
  }
  const link = safeLink(source.url);
  const checked = att.kind !== 'coming' && att.kind !== 'off';
  return h(
    'li',
    { class: 'pg-card', 'data-attention': att.kind || 'healthy', 'data-region': region },
    h('div', { class: 'pg-card__head' }, h('h4', { class: 'pg-card__name', text: nameOf(source) }), statusBadge(status)),
    publisher ? h('p', { class: 'pg-card__publisher', text: publisher }) : null,
    h('div', { class: 'pg-card__chips' }, chips),
    h('p', { class: 'pg-card__desc', text: str(source.description) || fallbackDescription(source) }),
    reason,
    checked ? historyStrip(source) : null,
    facts.length ? h('p', { class: 'pg-card__facts mono', text: facts.join(' · ') }) : null,
    checked ? reputationBlock(source) : null,
    link ? h('a', { class: 'pg-card__link mono', href: link.href, rel: 'noopener noreferrer', text: link.host }) : null,
  );
}

function candidateCard(item, ctx) {
  const found = parseTime(item.found_at);
  const host = str(item.host);
  const bits = [str(item.state) ? item.state.toUpperCase() : 'CANDIDATE', found === null ? null : `found ${fmtAgo(ctx.now - found)}`].filter(Boolean);
  return h(
    'li',
    { class: 'pg-card pg-card--candidate', 'data-attention': 'coming' },
    h('div', { class: 'pg-card__head' }, h('h4', { class: 'pg-card__name', text: str(item.name) || host || 'Unnamed candidate' })),
    host ? h('p', { class: 'pg-card__publisher mono', text: host }) : null,
    h('p', { class: 'pg-card__facts mono', text: bits.join(' · ') }),
    str(item.reason) ? h('p', { class: 'pg-card__desc', text: shortText(item.reason, 200) }) : null,
  );
}

function group(id, title, items, ctx, { empty = null, note = null, extra = [] } = {}) {
  const n = items.length + extra.length;
  return h(
    'section',
    { class: 'pg-group', 'aria-labelledby': id },
    h('h3', { class: 'pg-group__title', id }, title, h('span', { class: 'panel-count mono', text: ` ${n}` })),
    note ? h('p', { class: 'hint', text: note }) : null,
    n ? h('ul', { class: 'pg-cards' }, items.map((src) => sourceCard(src, ctx)), extra) : h('p', { class: 'empty', text: empty || 'None.' }),
  );
}

function stripPanel(groups) {
  const tiles = [
    ['HEALTHY', groups.counts.healthy, 'ok'],
    ['NEEDS FIXING', groups.counts.fix, 'fail'],
    ['WATCH', groups.counts.watch, 'watch'],
    ['COMING', groups.counts.coming, 'off'],
  ];
  return h(
    'ul',
    { class: 'pg-strip', 'aria-label': 'Source summary' },
    tiles.map(([label, n, g]) =>
      h(
        'li',
        { class: 'pg-tile', 'data-group': g, 'data-zero': n === 0 ? 'true' : null },
        h('span', { class: 'pg-tile__value mono' }, h('span', { class: 'pg-tile__glyph', 'aria-hidden': 'true', text: GROUP_GLYPH[g] }), fmtInt(n)),
        h('span', { class: 'pg-tile__label', text: label }),
      ),
    ),
  );
}

function controls(state, redraw) {
  const segment = (id, label, key, options) => {
    const buttons = options.map(([value, text]) =>
      h('button', { type: 'button', class: 'seg-btn', 'data-value': value, 'aria-pressed': String(state[key] === value), text }),
    );
    for (const b of buttons) {
      b.addEventListener('click', () => {
        state[key] = b.getAttribute('data-value');
        for (const other of buttons) other.setAttribute('aria-pressed', String(other === b));
        redraw();
      });
    }
    return h(
      'div',
      { class: 'pg-control' },
      h('span', { class: 'pg-control__label', id, text: label }),
      h('div', { class: 'pg-seg', role: 'group', 'aria-labelledby': id }, buttons),
    );
  };
  return h(
    'div',
    { class: 'pg-controls' },
    segment('pg-src-region', 'REGION', 'region', [['all', 'ALL'], ['au', 'AU'], ['global', 'GLOBAL']]),
    segment('pg-src-sort', 'SORT', 'sort', [['reputation', 'REPUTATION'], ['name', 'NAME'], ['status', 'STATUS']]),
  );
}

function drawLists(lists, showing, groups, ctx, state) {
  const pick = (list) => sortSources(filterSources(list, state.region), state.sort);
  const fix = pick(groups.fix);
  const watch = pick(groups.watch);
  const active = pick(groups.active);
  const coming = pick(groups.coming);
  const off = pick(groups.off);
  const all = state.region === 'all';
  const pipeline = all ? groups.pipeline : [];
  const shown = fix.length + watch.length + active.length + coming.length + off.length;
  showing.textContent = `Showing ${shown} of ${groups.counts.total} ${plural(groups.counts.total, 'source')}${all ? '' : `, ${REGION_NAME[state.region]} only`}.`;
  const pipelineNote = !all && groups.pipeline.length
    ? `${groups.pipeline.length} discovery ${plural(groups.pipeline.length, 'candidate has', 'candidates have')} no region yet; choose ALL to see them.`
    : null;
  fill(lists,
    fix.length ? group('pg-src-fix', 'NEEDS FIXING', fix, ctx) : null,
    watch.length ? group('pg-src-watch', 'WATCH', watch, ctx) : null,
    group('pg-src-active', 'ACTIVE SOURCES', active, ctx, { empty: 'No healthy sources in this view.' }),
    group('pg-src-coming', 'IN THE PIPELINE / COMING', coming, ctx, {
      empty: 'Nothing is waiting to be added.',
      note: pipelineNote,
      extra: pipeline.map((item) => candidateCard(item, ctx)),
    }),
    off.length ? group('pg-src-retired', 'RETIRED', off, ctx) : null,
  );
}

function weightsTable(caption, head, rows) {
  return h(
    'table',
    { class: 'pg-table pg-weights' },
    h('caption', { class: 'pg-sr', text: caption }),
    h('thead', {}, h('tr', {}, head.map((t) => h('th', { scope: 'col', text: t })))),
    h('tbody', {}, rows.map(([name, value]) => h('tr', {}, h('th', { scope: 'row', text: name }), h('td', { class: 'mono', text: value })))),
  );
}

function tierBlock(counts) {
  const rated = counts.key + counts.notable + counts.routine;
  if (!rated) {
    return h('p', { class: 'hint', text: counts.total ? 'Stories in this snapshot are not rated for importance yet.' : 'No stories in this snapshot.' });
  }
  const tiers = [['key', 'KEY'], ['notable', 'NOTABLE'], ['routine', 'ROUTINE']];
  const words = tiers.map(([t, l]) => `${l} ${counts[t]}`);
  if (counts.unrated) words.push(`${counts.unrated} not rated`);
  const GAP = 4;
  const present = tiers.filter(([t]) => counts[t] > 0);
  const span = 1000 - GAP * (present.length - 1);
  let x = 0;
  const bars = present.map(([t, l]) => {
    const w = Math.max(2, (counts[t] / rated) * span);
    const rect = s('rect', { class: 'pg-tierbar__seg', 'data-tier': t, x: x.toFixed(1), y: 0, width: w.toFixed(1), height: 10 }, s('title', { text: `${l} ${counts[t]}` }));
    x += w + GAP;
    return rect;
  });
  return h(
    'div',
    { class: 'pg-tiers' },
    h(
      'p',
      { class: 'pg-tiers__legend mono' },
      tiers.map(([t, l], i) => [
        i ? h('span', { class: 'pg-tiers__dot', 'aria-hidden': 'true', text: ' · ' }) : null,
        h('span', { class: 'pg-tier', 'data-tier': t }, h('span', { class: 'pg-swatch', 'aria-hidden': 'true' }), `${l} ${fmtInt(counts[t])}`),
      ]),
      counts.unrated ? h('span', { class: 'pg-tiers__unrated', text: ` · ${fmtInt(counts.unrated)} not rated` }) : null,
    ),
    s('svg', { class: 'pg-tierbar', viewBox: '0 0 1000 10', preserveAspectRatio: 'none', role: 'img', 'aria-label': `Importance of ${rated} rated stories: ${words.join(', ')}`, focusable: 'false' }, bars),
  );
}

function topKeyBlock(events) {
  const top = topKey(events, 3);
  if (!top.length) return h('p', { class: 'hint', text: 'No KEY stories in this snapshot.' });
  return h(
    'ol',
    { class: 'pg-top' },
    top.map((e) => {
      const reasons = (Array.isArray(e.importance?.reasons) ? e.importance.reasons : []).map((r) => str(r)).filter(Boolean).slice(0, 2);
      const score = num(e.importance?.score);
      return h(
        'li',
        { class: 'pg-top__item' },
        score === null ? null : h('span', { class: 'pg-top__score mono', 'aria-label': `Importance ${Math.round(score)}`, text: String(Math.round(score)) }),
        h(
          'span',
          { class: 'pg-top__body' },
          h('a', { class: 'pg-top__link', href: eventHref(e), text: str(e.title) || e.event_id }),
          reasons.length ? h('span', { class: 'pg-top__why', text: reasons.join(' · ') }) : null,
        ),
      );
    }),
  );
}

function ratePanel(live) {
  const events = Array.isArray(live?.events) ? live.events : [];
  const most = IMPORTANCE_POINTS.reduce((a, [, p]) => a + p, 0);
  const standing = Object.entries(STANDING_POINTS).map(([k, v]) => `${k} ${v}`).join(', ');
  return panel(
    'pg-src-rate',
    'HOW WE RATE',
    h(
      'div',
      { class: 'pg-rate' },
      h(
        'div',
        { class: 'pg-rate__col' },
        h('p', { class: 'subhead', text: 'SOURCE REPUTATION' }),
        h('p', { class: 'hint', text: 'A score out of 100, worked out by code from the record.' }),
        weightsTable('Source reputation weights', ['FACTOR', 'WEIGHT'], REPUTATION_WEIGHTS.map(([n, w, why]) => [`${n}: ${why}`, `${w}%`])),
        h('p', { class: 'pg-rate__note', text: `Standing: ${standing}.` }),
      ),
      h(
        'div',
        { class: 'pg-rate__col' },
        h('p', { class: 'subhead', text: 'STORY IMPORTANCE' }),
        h('p', { class: 'hint', text: `Points from the facts of a story, up to ${most}, held to a score of 0–100. Code, not AI.` }),
        weightsTable('Story importance points', ['FACT', 'POINTS'], IMPORTANCE_POINTS.map(([n, p]) => [n, String(p)])),
        h('p', { class: 'pg-rate__note', text: `KEY at ${KEY_AT} or more, NOTABLE at ${NOTABLE_AT} or more, otherwise ROUTINE. A story on neither the cyber nor the AI desk stays ROUTINE.` }),
      ),
    ),
    h('p', { class: 'subhead', text: 'THIS SNAPSHOT' }),
    tierBlock(tierCounts(events)),
    h('p', { class: 'subhead', text: 'TOP KEY STORIES' }),
    topKeyBlock(events),
  );
}

export function renderSourcesPage(host, data = {}) {
  const { health = null, live = null, now } = isObj(data) ? data : {};
  const summary = sourcesSummary({ health });
  if (!host || !hasDom()) return summary;
  const groups = classifySources(health);
  if (!groups) {
    fill(host,
      panel('pg-src-none', 'SOURCES', h('p', { class: 'empty', text: 'Source health has not been published yet.' })),
      ratePanel(live),
    );
    return summary;
  }
  const state = pageState.get(host) || { region: 'all', sort: 'reputation' };
  pageState.set(host, state);
  const ctx = { now: nowMs(now) };
  const lists = h('div', { class: 'pg-lists' });
  const showing = h('p', { class: 'hint pg-showing', role: 'status' });
  const redraw = () => drawLists(lists, showing, groups, ctx, state);
  fill(host, stripPanel(groups), h('div', { class: 'pg-toolbar' }, controls(state, redraw), showing), lists, ratePanel(live));
  redraw();
  return summary;
}
