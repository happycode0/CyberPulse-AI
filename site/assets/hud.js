// CyberPulse-AI HUD. Plain ES module, no framework, no build step.
// Every string from the data files is written with textContent or setAttribute,
// never parsed as markup.

import { renderMap, syncMapSelection } from './map.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const DATA_BASES = ['data/', '../data/'];
const FX_KEY = 'cp-fx';
const BOOT_KEY = 'cp-boot';
const INITIAL_CARDS = 6;
const HERO_CARDS = 8;

export const SEVERITY = {
  critical: { glyph: '◆', shape: 'diamond', label: 'CRITICAL', bars: 4 },
  high: { glyph: '▲', shape: 'triangle', label: 'HIGH', bars: 3 },
  medium: { glyph: '●', shape: 'circle', label: 'MEDIUM', bars: 2 },
  low: { glyph: '■', shape: 'square', label: 'LOW', bars: 1 },
  info: { glyph: '○', shape: 'ring', label: 'INFO', bars: 1 },
  unknown: { glyph: '◇', shape: 'open-diamond', label: 'UNRATED', bars: 0 },
};
const SEVERITY_WEIGHT = { critical: 10, high: 6, medium: 3, low: 1, info: 0.5, unknown: 1 };

const HEALTH = {
  healthy: { glyph: '●', label: 'HEALTHY' },
  degraded: { glyph: '▲', label: 'DEGRADED' },
  broken: { glyph: '■', label: 'BROKEN' },
  off: { glyph: '○', label: 'OFF' },
};

// The crew roster is presentation, not authority (PLAN.md): personas are stable and
// ship with the site. Workload per agent, when there is any, comes from data/crew.json
// (published from Stage 5) and is looked up by callsign at render time.
export const CREW = [
  { callsign: 'MORPHEUS', desk: 'INTELLIGENCE', beat: 'Intelligence Director / Chief Editor', runtime: 'LLM · strong · 1×/day', quote: 'I can only show you the door.' },
  { callsign: 'ZION', desk: 'INTELLIGENCE', beat: 'Australian desk', runtime: 'LLM · fast', quote: 'Home ground. Our watch.' },
  { callsign: 'BLASTER', desk: 'INTELLIGENCE', beat: 'Global cyber desk', runtime: 'LLM · fast', quote: "I'm picking up chatter on every band." },
  { callsign: 'WINTERMUTE', desk: 'INTELLIGENCE', beat: 'AI + cyber↔AI convergence', runtime: 'LLM · fast', quote: 'The model is the attack surface.' },
  { callsign: 'TACHIKOMA', desk: 'INTELLIGENCE', beat: 'Source discovery', runtime: 'LLM + Tavily', quote: "Ooh — what's this one?" },
  { callsign: 'DECKARD', desk: 'INTELLIGENCE', beat: 'Follow-up, developing events', runtime: 'LLM + Strands', quote: "The case stays open until it's patched." },
  { callsign: 'VOIGHT', desk: 'INTELLIGENCE', beat: 'Editorial QA, publication veto', runtime: 'LLM · strong · gated', quote: 'Says who?' },
  { callsign: 'WHEELJACK', desk: 'ENGINEERING', beat: 'Source & platform engineer', runtime: 'CODE agent · wakes on request', quote: '' },
  { callsign: 'TRON', desk: 'ENGINEERING', beat: 'Independent verification', runtime: 'AUDIT agent · different model family', quote: 'A security program that answers to the users, not to the system it audits.' },
  { callsign: 'TELETRAAN', desk: 'OPERATIONS', beat: 'Watchdog / SRE', runtime: 'Deterministic + LLM triage', quote: '' },
  { callsign: 'ROGUE', desk: 'OPERATIONS', beat: 'Cost / FinOps, degradation tiers', runtime: 'Deterministic', quote: 'Nothing in this city is free.' },
  { callsign: 'LINK', desk: 'OPERATIONS', beat: 'Publishing + notifications', runtime: 'Deterministic', quote: "Transmission clean. Here's the diff." },
  { callsign: 'SERAPH', desk: 'OPERATIONS', beat: 'Source verification gate', runtime: 'Deterministic', quote: 'I had to be sure.' },
  { callsign: 'LIBRARIAN', desk: 'OPERATIONS', beat: 'KEV / CVE / EPSS / OSV / ATT&CK', runtime: 'Deterministic', quote: "Cite it or it didn't happen." },
  { callsign: 'PROWL', desk: 'OPERATIONS', beat: 'Event correlation, material change', runtime: 'Deterministic + Strands', quote: 'One event. Many threads.' },
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
// related-event links can be resolved without another round trip.
export async function findEvent(eventId, { bases = DATA_BASES, fetchImpl = globalThis.fetch } = {}) {
  for (const base of bases) {
    const feed = await getJson(`${base}live.json`, fetchImpl);
    const feedEvents = feed && Array.isArray(feed.events) ? feed.events : [];
    const hit = feedEvents.find((e) => e.event_id === eventId);
    if (hit) return { event: hit, events: feedEvents, feed };
    const index = await getJson(`${base}index.json`, fetchImpl);
    if (index && Array.isArray(index.days)) {
      const days = [...index.days]
        .sort((a, b) => String(b.date).localeCompare(String(a.date)))
        .slice(0, HISTORY_SCAN);
      for (const meta of days) {
        const day = await getJson(`${base}${meta.path}`, fetchImpl);
        const dayEvents = day && Array.isArray(day.events) ? day.events : [];
        const dayHit = dayEvents.find((e) => e.event_id === eventId);
        if (dayHit) return { event: dayHit, events: [...feedEvents, ...dayEvents], feed };
      }
    }
  }
  return null;
}

// --------------------------------------------------------------- sections

const has = (list, ...wanted) => (list || []).some((x) => wanted.includes(String(x).toLowerCase()));
const isAu = (e) => Boolean(e.au?.directly_reported_in_au) || (e.au?.relevance ?? 0) >= 0.5;
const isAi = (e) => Boolean(e.ai_subdomain) || has(e.domains, 'ai') || has(e.categories, 'ai_security', 'ai-security', 'ai');

export const SECTIONS = [
  { id: 'australia-now', match: isAu },
  { id: 'global-cyber', match: (e) => !isAu(e) && (!e.domains?.length || has(e.domains, 'cybersecurity')) },
  { id: 'ai-cyber', match: isAi },
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
      (e.status === 'new' && (e.severity === 'critical' || e.severity === 'high')),
  },
  { id: 'threat-actors', match: (e) => (e.entities?.actors || []).length > 0 },
  { id: 'vulnerabilities', match: (e) => (e.cves || []).length > 0 || has(e.categories, 'vulnerability') },
  { id: 'research', match: (e) => has(e.categories, 'research') },
  { id: 'policy-regulation', match: (e) => has(e.categories, 'policy', 'regulation', 'policy-regulation', 'policy_regulation') },
];

const expanded = new Set();
let filtersActive = false;
// An event can sit in several sections, so cards carry data-event-id, never a DOM id.
// main() fills these in so revealEvent can widen the view when a card is capped or filtered out.
const view = { events: [], refresh: () => {}, clearFilters: () => {} };
// Set by initTabs() when the nav works as tabs, so revealEvent can switch tabs before
// searching for a card that lives in a currently-hidden panel.
let activateTab = null;

export function sectionFor(event) {
  return SECTIONS.find((section) => section.match(event)) || null;
}

// Scrolls to and focuses a rendered card for the event, first showing it if a section
// cap, an active filter, or an inactive tab is hiding it. Returns the card, or null if
// it cannot be shown.
export function revealEvent(eventId) {
  const find = () => [...document.querySelectorAll('[data-event-id]')].find((el) => el.dataset.eventId === eventId) || null;
  let card = find();
  const event = view.events.find((e) => e.event_id === eventId);
  const section = event ? sectionFor(event) : null;
  if (section) activateTab?.(`sec-${section.id}`);
  if (!card && section) {
    expanded.add(section.id);
    view.refresh();
    card = find();
  }
  if (!card && section) {
    view.clearFilters();
    card = find();
  }
  if (card) {
    // Focus first: it forces content-visibility to lay the card out, so the scroll lands on real sizes.
    card.focus({ preventScroll: true });
    card.scrollIntoView({ block: 'center' });
    requestAnimationFrame(() => card.scrollIntoView({ block: 'center' }));
  }
  return card;
}

export function renderSections(data) {
  const events = [...(data.events || [])].sort(byProminence);
  const result = {};
  for (const section of SECTIONS) {
    const matches = events.filter(section.match);
    result[section.id] = matches;
    const body = document.querySelector(`[data-section-body="${section.id}"]`);
    if (body) renderSectionBody(body, section.id, matches);
    const count = document.querySelector(`[data-count-for="${section.id}"]`);
    if (count) {
      count.textContent = `${matches.length} ${matches.length === 1 ? 'SIGNAL' : 'SIGNALS'}`;
      const led = count.parentElement.querySelector('.led');
      if (led) led.dataset.state = matches.length ? 'ok' : 'idle';
    }
  }
  return result;
}

function renderSectionBody(body, id, matches) {
  clear(body);
  if (!matches.length) {
    body.append(
      h('p', {
        class: 'empty',
        text: filtersActive ? 'NO SIGNALS MATCH THE CURRENT FILTERS.' : 'NO SIGNALS IN THIS SNAPSHOT.',
      }),
    );
    return;
  }
  const cap = id === 'australia-now' ? HERO_CARDS : INITIAL_CARDS;
  const shown = expanded.has(id) ? matches : matches.slice(0, cap);
  body.append(renderEventCards(shown));
  if (matches.length > cap) {
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

// Severity is never colour alone: label + shape glyph (data-severity-shape) + bar count.
function severityBadge(key) {
  const sev = SEVERITY[key] || SEVERITY.unknown;
  return h(
    'span',
    { class: 'sev-badge', 'data-severity': key },
    h('span', { class: 'sev-shape', 'data-severity-shape': sev.shape, 'aria-hidden': 'true', text: sev.glyph }),
    h('span', { text: sev.label }),
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

function regionBadge(event) {
  const code = event.au?.directly_reported_in_au ? 'AU' : (event.entities?.countries?.[0] || 'GL').slice(0, 2).toUpperCase();
  return h('span', { class: 'hex-badge', title: `Region ${code}` }, h('span', { text: code }));
}

// A sorted list of event cards. Used by the dashboard sections and by the history page.
export function renderEventCards(events) {
  const list = [...(events || [])].sort(byProminence);
  if (!list.length) return h('p', { class: 'empty', text: 'NO EVENTS IN THIS SNAPSHOT.' });
  return h('ul', { class: 'event-list' }, list.map((e) => h('li', {}, eventCard(e))));
}

function eventCard(event) {
  const key = sevKey(event);
  const chips = [];
  if (event.au?.directly_reported_in_au) chips.push(h('span', { class: 'chip chip--au', text: 'AU' }));
  if (event.severity_source === 'ai_estimate') chips.push(h('span', { class: 'chip chip--ai', text: 'AI-SUGGESTED' }));
  for (const cve of (event.cves || []).slice(0, 4)) {
    chips.push(h('span', { class: cve.kev?.listed ? 'chip chip--kev' : 'chip', text: cve.kev?.listed ? `${cve.id} KEV` : cve.id }));
  }
  const firstSource = (event.sources || [])[0];
  return h(
    'article',
    { class: 'event-card', 'data-event-id': event.event_id, tabindex: '-1', 'data-severity': key },
    h(
      'div',
      { class: 'event-card__meta' },
      severityBadge(key),
      regionBadge(event),
      timeEl(event.last_material_update || event.first_seen),
      h('span', { class: 'mono', text: event.event_id }),
      firstSource ? h('span', { class: 'chip', text: `${firstSource.evidence_class} · ${firstSource.source_id}` }) : null,
    ),
    h(
      'h3',
      { class: 'event-card__title' },
      h('a', { class: 'event-card__link', href: `event.html?id=${event.event_id}`, text: event.title }),
    ),
    event.summary ? h('p', { class: 'event-card__summary', text: event.summary }) : null,
    chips.length ? h('div', { class: 'event-card__tags' }, chips) : null,
    eventDetail(event),
  );
}

function eventDetail(event) {
  const body = h('div', { class: 'event-more__body' });
  if (event.why_it_matters) body.append(h('h4', { text: 'WHY IT MATTERS' }), h('p', { text: event.why_it_matters }));
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
  if (!body.childNodes.length) return null;
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
          src.lineage_id ? h('span', { class: 'mono source-report__lineage', text: `LINEAGE ${src.lineage_id}` }) : null,
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
        );
      }),
    ),
  );
}

function mitreList(techniques) {
  if (!techniques.length) return null;
  return h(
    'div',
    { class: 'detail-list' },
    h('h3', { class: 'detail-label', text: 'MITRE ATT&CK TECHNIQUES' }),
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
  const au = event.au || {};
  const entities = event.entities || {};
  const severitySource = String(event.severity_source || 'unknown').toUpperCase().replace(/_/g, ' ');
  const root = h('div', { class: 'detail', 'data-severity': key });

  root.append(
    h('h1', { class: 'detail__title', text: event.title }),
    h(
      'div',
      { class: 'detail__meta' },
      severityBadge(key),
      h('span', { class: 'chip', text: `SEVERITY VIA ${severitySource}` }),
      h('span', { class: 'chip', text: String(event.status || 'unknown').toUpperCase() }),
      regionBadge(event),
      au.directly_reported_in_au ? h('span', { class: 'chip chip--au', text: 'REPORTED IN AU' }) : null,
      h('span', { class: 'mono detail__id', text: event.event_id }),
    ),
    event.summary ? h('p', { class: 'detail__summary', text: event.summary }) : null,
    event.why_it_matters
      ? h('p', { class: 'detail__why' }, h('strong', { text: 'WHY IT MATTERS: ' }), event.why_it_matters)
      : null,
  );

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
          ['INDEPENDENT CONFIRMATION (UTC)', utcStamp(event.last_independent_confirmation)],
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
        listOf('SOCI ASSET CLASSES', au.soci_asset_classes),
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

const DIMENSIONS = [
  ['severity', 'SEVERITY'],
  ['au', 'AUSTRALIA'],
  ['ai', 'AI'],
  ['category', 'CATEGORY'],
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
  const out = [['severity', sevKey(event)]];
  if (event.au?.directly_reported_in_au) out.push(['au', 'REPORTED IN AU']);
  if ((event.au?.relevance ?? 0) >= 0.5) out.push(['au', 'AU RELEVANT']);
  if (isAi(event)) out.push(['ai', 'AI']);
  for (const c of event.categories || []) out.push(['category', c]);
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

// state: { events, query: string, selected: { [dimension]: Set|Array of values } }
// Within a dimension any selected value matches (OR); dimensions combine with AND.
export function applyFilters(state) {
  const query = (state.query || '').trim().toLowerCase();
  const active = Object.entries(state.selected || {})
    .map(([dim, values]) => [dim, new Set(values)])
    .filter(([, values]) => values.size > 0);
  return (state.events || []).filter((event) => {
    if (query) {
      const haystack = [event.title, event.summary, event.event_id, ...(event.tags || []), ...(event.cves || []).map((c) => c.id)]
        .join(' ')
        .toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    if (!active.length) return true;
    const tokens = facetTokens(event);
    return active.every(([dim, values]) => tokens.some(([d, v]) => d === dim && values.has(v)));
  });
}

function renderFacetUi(facets, state, onChange) {
  const host = clear(document.getElementById('filter-facets'));
  for (const [dim, label] of DIMENSIONS) {
    const bucket = facets.get(dim);
    if (!bucket || !bucket.size) continue;
    const entries = [...bucket.entries()].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0]))).slice(0, 40);
    const chips = h('div', { class: 'facet__chips' });
    for (const [value, count] of entries) {
      const btn = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false', text: `${value} (${count})` });
      btn.addEventListener('click', () => {
        const set = (state.selected[dim] ||= new Set());
        if (set.has(value)) set.delete(value);
        else set.add(value);
        btn.setAttribute('aria-pressed', set.has(value) ? 'true' : 'false');
        onChange();
      });
      chips.append(btn);
    }
    host.append(h('details', { class: 'facet', open: dim === 'severity' }, h('summary', { text: `${label} · ${bucket.size}` }), chips));
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
      'aria-label': `Event volume by hour for the 24 hours before the last completed collection: ${total} events, busiest hour ${Math.max(0, ...buckets)}.`,
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
    { class: 'radar', role: 'img', 'aria-label': `Threat radar: ${events.length} events plotted by severity ring and category angle, ${auCount} reported in Australia.` },
    svg,
    h('div', { class: 'radar-sweep', 'aria-hidden': 'true' }),
  );
  clear(host).append(
    h('div', { class: 'cp-index__label', text: 'THREAT RADAR' }),
    radar,
    h('p', { class: 'radar-legend', text: 'RING = SEVERITY (CENTRE IS CRITICAL). ANGLE = CATEGORY. DOUBLE RING = REPORTED IN AUSTRALIA.' }),
  );
}

// --------------------------------------------------------------- pipeline

const STAGES = ['SOURCES', 'COLLECT', 'MATCH', 'VERIFY', 'ENRICH', 'CROSS-REF', 'SCORE', 'PUBLISH'];

function stageNodes(health, status, feed) {
  const run = status?.last_run;
  const num = (v) => (v === undefined || v === null ? '—' : String(v));
  const later = { count: '—', state: 'NOT IN STAGE 1', led: 'idle' };
  if (!run && !feed) {
    return STAGES.map((name) => ({ name, ...(name === 'VERIFY' || name === 'CROSS-REF' ? later : { count: '—', state: 'NO DATA', led: 'idle' }) }));
  }
  const attention = run ? run.sources_failed + run.sources_stale : 0;
  const counted = run ? run.sources_ok + attention : (health?.sources || []).length;
  const pending = feed?.counts?.pending_enrichment;
  return [
    { name: 'SOURCES', count: num(counted), state: attention ? `${attention} NEED ATTENTION` : 'ALL CHECKED OK', led: attention ? 'warn' : 'ok' },
    { name: 'COLLECT', count: num(run?.items_fetched), state: 'ITEMS FETCHED', led: run ? 'ok' : 'idle' },
    { name: 'MATCH', count: run ? String(run.new_events + run.updated_events) : '—', state: run ? `${run.duplicates} MERGED` : 'NO DATA', led: run ? 'ok' : 'idle' },
    { name: 'VERIFY', ...later },
    { name: 'ENRICH', count: num(pending), state: pending ? 'QUEUED' : 'NONE QUEUED', led: 'idle' },
    { name: 'CROSS-REF', ...later },
    { name: 'SCORE', count: num(feed?.counts?.events), state: 'EVENTS SCORED', led: feed ? 'ok' : 'idle' },
    { name: 'PUBLISH', count: num(feed?.counts?.events), state: 'IN SNAPSHOT', led: feed ? 'ok' : 'idle' },
  ];
}

export function healthOf(source) {
  const st = source.latest?.status;
  if (!source.enabled || source.lifecycle_state === 'retired' || st === 'disabled' || !source.latest) return 'off';
  if (source.lifecycle_state === 'broken' || st === 'error' || st === 'timeout') return 'broken';
  if (st === 'ok') return 'healthy';
  return 'degraded';
}

export function renderPipeline(health, extras = {}) {
  const { status = null, feed = null } = extras;
  const host = document.getElementById('pipeline');
  if (host) {
    const nodes = stageNodes(health, status, feed);
    const svg = s('svg', { class: 'pipe-svg', viewBox: '0 0 800 40', preserveAspectRatio: 'none', 'aria-hidden': 'true' });
    let d = '';
    for (let i = 0; i < nodes.length - 1; i += 1) {
      const x1 = i * 100 + 50;
      const x2 = x1 + 100;
      d += `M${x1} 30 H${x1 + 22} V12 H${x2 - 22} V30 H${x2} `;
    }
    svg.append(s('path', { class: 'pipe-trace', d }), s('path', { class: 'pipe-pulse', d, pathLength: 100 }));
    const list = h(
      'ol',
      { class: 'pipe-nodes', 'aria-label': 'Collection replay stages' },
      nodes.map((n) =>
        h(
          'li',
          { class: 'pipe-node' },
          h('span', { class: 'led', 'data-state': n.led }),
          h('span', { class: 'pipe-node__name', text: n.name }),
          h('span', { class: 'pipe-node__count mono', text: n.count }),
          h('span', { class: 'pipe-node__state', text: n.state }),
        ),
      ),
    );
    clear(host).append(svg, list);
  }
  renderSources(health);
  renderSystem(status, feed, health);
}

function renderSources(health) {
  const host = document.getElementById('source-health');
  if (!host) return;
  clear(host);
  const sources = health?.sources || [];
  if (!sources.length) {
    host.append(h('p', { class: 'empty', text: 'NO SOURCE HEALTH PUBLISHED YET.' }));
    return;
  }
  host.append(
    h(
      'ul',
      { class: 'source-grid' },
      sources.map((src) => {
        const key = healthOf(src);
        const info = HEALTH[key];
        return h(
          'li',
          { class: 'source-node', 'data-health': key },
          h('span', { class: 'sev-shape', 'aria-hidden': 'true', text: info.glyph }),
          h('span', { class: 'source-node__name', text: src.name }),
          h('span', { class: 'source-node__state', text: info.label }),
        );
      }),
    ),
  );
}

function renderSystem(status, feed, health) {
  const led = document.getElementById('system-led');
  const label = document.getElementById('system-state');
  const sources = health?.sources || [];
  const broken = sources.filter((x) => healthOf(x) === 'broken').length;
  const degraded = sources.filter((x) => healthOf(x) === 'degraded').length;
  if (led && label) {
    if (!status && !feed) {
      led.dataset.state = 'idle';
      label.textContent = 'AWAITING DATA';
    } else if (broken) {
      led.dataset.state = 'bad';
      label.textContent = `${broken} SOURCES BROKEN`;
    } else if (degraded) {
      led.dataset.state = 'warn';
      label.textContent = `${degraded} SOURCES DEGRADED`;
    } else {
      led.dataset.state = 'ok';
      label.textContent = 'SOURCES HEALTHY';
    }
  }
  const dl = document.getElementById('system-versions');
  if (!dl) return;
  clear(dl);
  const rows = [
    ['PIPELINE', status?.pipeline_version || feed?.pipeline_version],
    ['SCHEMA', status?.schema_version],
    ['SCORING', status?.scoring_version],
    ['SNAPSHOT BUILT', feed?.generated_at ? `${formatUtc(feed.generated_at)} UTC` : null],
    ['LAST RUN LANE', status?.last_run?.lane?.toUpperCase()],
  ];
  for (const [k, v] of rows) if (v) dl.append(h('dt', { text: k }), h('dd', { text: v }));
}

// -------------------------------------------------------------------- crew

function crewCard(agent, stat) {
  const status = stat?.status || 'not_active';
  const led = { active: 'ok', idle: 'idle', degraded: 'warn', not_active: 'idle' }[status] || 'idle';
  const label = { active: 'ACTIVE', idle: 'IDLE', degraded: 'DEGRADED', not_active: 'NOT YET ACTIVE' }[status] || status.toUpperCase();
  const fmtStat = (value, fmt = (v) => String(v)) => (value === null || value === undefined ? '—' : fmt(value));
  return h(
    'li',
    { class: 'crew-card', 'data-status': status },
    h(
      'div',
      { class: 'crew-card__head' },
      h('span', { class: 'led', 'data-state': led }),
      h('span', { class: 'crew-card__callsign', text: agent.callsign }),
      h('span', { class: 'crew-card__desk mono', text: agent.desk }),
    ),
    h('p', { class: 'crew-card__beat', text: agent.beat }),
    agent.quote ? h('p', { class: 'crew-card__quote', text: `"${agent.quote}"` }) : null,
    h(
      'dl',
      { class: 'kv crew-card__stats mono' },
      h('dt', { text: 'STATUS' }), h('dd', { text: label }),
      h('dt', { text: 'TASKS COMPLETED' }), h('dd', { text: fmtStat(stat?.tasks_completed) }),
      h('dt', { text: 'ITEMS PROCESSED' }), h('dd', { text: fmtStat(stat?.items_processed) }),
      h('dt', { text: 'EST. COST (MTD)' }), h('dd', { text: fmtStat(stat?.cost_usd, (v) => `US$${Number(v).toFixed(2)}`) }),
      h('dt', { text: 'LAST ACTIVE' }), h('dd', { text: fmtStat(stat?.last_active_at, formatSydney) }),
    ),
    h('span', { class: 'chip crew-card__runtime', text: agent.runtime }),
  );
}

// Renders the full 15-agent roster every time: personas ship with the site regardless
// of data, and each card's workload falls back to "—"/NOT YET ACTIVE when data/crew.json
// has nothing for that callsign, which is the honest state for as long as Stage 1 runs
// with no agents wired up yet.
export function renderCrew(crew) {
  const host = document.getElementById('crew-list');
  if (!host) return;
  clear(host);
  const byCallsign = new Map((crew?.agents || []).map((a) => [a.callsign, a]));
  const summary = document.getElementById('crew-summary');
  if (summary) {
    const reporting = byCallsign.size;
    const totalTasks = [...byCallsign.values()].reduce((sum, a) => sum + (a.tasks_completed || 0), 0);
    const totalCost = [...byCallsign.values()].reduce((sum, a) => sum + (a.cost_usd || 0), 0);
    clear(summary).append(
      h('p', {
        class: 'hint',
        text: reporting
          ? `${reporting} of ${CREW.length} agents reporting · ${totalTasks} tasks completed · US$${totalCost.toFixed(2)} spent this month.`
          : `${CREW.length} agents are planned for this project; none are wired up yet. Workload figures publish from Stage 5 onward.`,
      }),
    );
  }
  host.append(...CREW.map((agent) => crewCard(agent, byCallsign.get(agent.callsign))));
  const count = document.getElementById('crew-count');
  if (count) count.textContent = `${CREW.length} AGENTS`;
}

// ----------------------------------------------------------------- ticker

export function renderTicker(events) {
  const lane = document.getElementById('ticker-lane');
  if (!lane) return;
  clear(lane);
  // Only events that land in a section are listed, so every link has a card to reveal.
  // Capped at 8 (not the full snapshot): a scrolling line of near-identical advisory
  // titles reads as noise past that, however many events actually qualify.
  const top = [...events].filter(sectionFor).sort(byProminence).slice(0, 8);
  if (!top.length) {
    lane.append(h('ul', { class: 'ticker-track' }, h('li', { class: 'ticker-item', text: 'NO HEADLINES IN THIS SNAPSHOT' })));
    return;
  }
  // The second track is a decorative duplicate so the scroll loops seamlessly.
  const track = (duplicate) =>
    h(
      'ul',
      { class: 'ticker-track', 'aria-hidden': duplicate ? 'true' : undefined },
      top.map((e) =>
        h(
          'li',
          { class: 'ticker-item' },
          severityBadge(sevKey(e)),
          h('span', { class: 'ticker-sep', 'aria-hidden': 'true', text: '·' }),
          duplicate ? h('span', { text: e.title }) : tickerLink(e),
        ),
      ),
    );
  lane.append(track(false), track(true));
}

function tickerLink(event) {
  const link = h('a', { href: `#sec-${sectionFor(event).id}`, text: event.title });
  link.addEventListener('click', (e) => {
    if (revealEvent(event.event_id)) e.preventDefault();
  });
  return link;
}

// ------------------------------------------------------------------- tabs

// Turns the section nav into a WAI-ARIA tabs widget: one panel visible at a time,
// arrow/Home/End keyboard movement, and the active tab kept in the URL hash so it
// survives reload and stays linkable. No-ops (leaves the plain anchor links working)
// when the nav does not have same-page #sec-* links, e.g. event.html and history.html.
export function initTabs({ nav = document.querySelector('.site-nav') } = {}) {
  if (!nav) return null;
  const tabs = [...nav.querySelectorAll('a[href^="#sec-"]')];
  const panels = tabs.map((a) => document.getElementById(a.getAttribute('href').slice(1)));
  if (!tabs.length || panels.some((p) => !p)) return null;

  nav.setAttribute('role', 'tablist');
  tabs.forEach((a, i) => {
    const id = a.getAttribute('href').slice(1);
    a.id = `tab-${id}`;
    a.setAttribute('role', 'tab');
    a.setAttribute('aria-selected', 'false');
    a.tabIndex = -1;
    panels[i].setAttribute('role', 'tabpanel');
    panels[i].tabIndex = 0;
  });

  const activate = (targetId, { focusPanel = false } = {}) => {
    const index = tabs.findIndex((a) => a.getAttribute('href') === `#${targetId}`);
    if (index === -1) return false;
    tabs.forEach((a, i) => {
      const active = i === index;
      a.setAttribute('aria-selected', active ? 'true' : 'false');
      a.tabIndex = active ? 0 : -1;
      panels[i].hidden = !active;
    });
    if (focusPanel) panels[index].focus({ preventScroll: true });
    return true;
  };

  const activateFromHash = () => {
    const id = window.location.hash.slice(1);
    if (!id || !activate(id)) activate(tabs[0].getAttribute('href').slice(1));
  };

  nav.addEventListener('click', (e) => {
    const a = e.target.closest('a[role="tab"]');
    if (!a) return;
    e.preventDefault();
    const id = a.getAttribute('href').slice(1);
    activate(id, { focusPanel: true });
    history.replaceState(null, '', `#${id}`);
  });

  nav.addEventListener('keydown', (e) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    const current = tabs.findIndex((a) => a.getAttribute('aria-selected') === 'true');
    let next = current;
    if (e.key === 'ArrowRight') next = (current + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabs.length - 1;
    if (next === current) return;
    e.preventDefault();
    const id = tabs[next].getAttribute('href').slice(1);
    activate(id);
    tabs[next].focus();
    history.replaceState(null, '', `#${id}`);
  });

  window.addEventListener('hashchange', activateFromHash);
  activateFromHash();
  activateTab = activate;
  return { activate };
}

// ------------------------------------------------------------ fx and boot

// The pieces every page shares: the FX OFF toggle, the ticker pause (when the
// ticker exists) and the canvas particle field.
function initCommon() {
  initFxToggle();
  initTickerToggle();
  initFxField();
  initTabs();
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

function initTickerToggle() {
  const button = document.getElementById('ticker-toggle');
  const ticker = document.getElementById('ticker');
  if (!button || !ticker) return;
  button.addEventListener('click', () => {
    const paused = ticker.classList.toggle('is-paused');
    button.setAttribute('aria-pressed', paused ? 'true' : 'false');
  });
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
        c: i % 7 === 0 ? '#FF2A6D' : '#00E5FF',
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

// A small corner readout, at most 2.4 s, once per session, skippable, and skipped
// entirely under reduced motion or FX OFF. It never covers or delays the content.
export function runBoot(data, { storage = safeStorage('sessionStorage') } = {}) {
  const panel = document.getElementById('boot');
  const out = document.getElementById('boot-text');
  if (!panel || !out) return;
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (reduce || document.documentElement.classList.contains('fx-off') || storageGet(storage, BOOT_KEY) === '1') return;
  storageSet(storage, BOOT_KEY, '1');
  const events = data.events || [];
  const au = events.filter((e) => e.au?.directly_reported_in_au).length;
  const stamp = data.feed?.last_completed_collection ? `${formatUtc(data.feed.last_completed_collection)} UTC` : 'NONE YET';
  const script = [
    '> CYBERPULSE-AI // HUD INIT',
    '> READING PUBLISHED SNAPSHOT',
    `> ${events.length} EVENTS // ${au} REPORTED IN AU`,
    `> LAST COMPLETED COLLECTION ${stamp}`,
    '> READY',
  ].join('\n');
  panel.hidden = false;
  let i = 0;
  let closeTimer = 0;
  let typer = 0;
  const onKey = (e) => {
    if (e.key === 'Escape') finish();
  };
  function finish() {
    clearInterval(typer);
    clearTimeout(closeTimer);
    panel.hidden = true;
    document.removeEventListener('keydown', onKey);
  }
  typer = setInterval(() => {
    i += 2;
    out.textContent = script.slice(0, i);
    if (i >= script.length) {
      clearInterval(typer);
      closeTimer = setTimeout(finish, 400);
    }
  }, 30);
  document.addEventListener('keydown', onKey);
  panel.addEventListener('click', finish);
  document.getElementById('boot-skip')?.addEventListener('click', finish);
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

export async function main() {
  initCommon();
  let data;
  try {
    data = await loadData();
  } catch (err) {
    showError(`The published data could not be read (${err.message}). Try again after the next collection completes.`);
    renderPipeline(null);
    renderSections({ events: [] });
    return;
  }
  const state = { events: data.events, query: '', selected: {} };
  renderStrip(data);
  renderIndex(data);
  renderGauges(data);
  renderRadar(data);
  renderTicker(data.events);
  renderPipeline(data.health, { status: data.status, feed: data.feed });
  renderCrew(await getJson(`${data.base}crew.json`, globalThis.fetch));

  const count = document.getElementById('filter-count');
  const refresh = () => {
    const filtered = applyFilters(state);
    filtersActive = Boolean(state.query.trim()) || Object.values(state.selected).some((v) => v.size);
    renderSections({ events: filtered });
    if (count) count.textContent = `${filtered.length} OF ${data.events.length} EVENTS`;
  };
  renderFacetUi(buildFacets(data.events), state, refresh);

  // The world map and the country dropdown filter in exactly the same way a
  // country facet chip does, through state.selected.country.
  const onCountry = (token) => {
    if (token) state.selected.country = new Set([token]);
    else delete state.selected.country;
    refresh();
  };
  await renderMap(data.events, onCountry);

  document.getElementById('filter-query')?.addEventListener('input', (e) => {
    state.query = e.target.value;
    refresh();
  });
  const clearFilters = () => {
    state.query = '';
    state.selected = {};
    const q = document.getElementById('filter-query');
    if (q) q.value = '';
    document.querySelectorAll('#filter-facets button[aria-pressed="true"]').forEach((b) => b.setAttribute('aria-pressed', 'false'));
    syncMapSelection('');
    refresh();
  };
  document.getElementById('filter-clear')?.addEventListener('click', clearFilters);
  document.getElementById('map-clear')?.addEventListener('click', clearFilters);
  Object.assign(view, { events: data.events, refresh, clearFilters });
  refresh();
  runBoot(data);
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
  const { event, events, feed } = found;
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
