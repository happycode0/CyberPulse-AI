// CyberPulse-AI HUD. Plain ES module, no framework, no build step.
// Every string from the data files is written with textContent or setAttribute,
// never parsed as markup.

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
  body.append(h('ul', { class: 'event-list' }, shown.map((e) => h('li', {}, eventCard(e)))));
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
    { class: 'event-card', id: event.event_id, 'data-severity': key },
    h(
      'div',
      { class: 'event-card__meta' },
      severityBadge(key),
      regionBadge(event),
      timeEl(event.last_material_update || event.first_seen),
      h('span', { class: 'mono', text: event.event_id }),
      firstSource ? h('span', { class: 'chip', text: `${firstSource.evidence_class} · ${firstSource.source_id}` }) : null,
    ),
    h('h3', { class: 'event-card__title', text: event.title }),
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
        event.cves.map((c) =>
          h(
            'li',
            { class: 'mono' },
            [
              c.id,
              c.cvss ? ` · CVSS ${c.cvss.score}` : '',
              c.epss?.score !== null && c.epss?.score !== undefined ? ` · EPSS ${c.epss.score}` : '',
              c.kev?.listed ? ` · KEV due ${c.kev.due_date || 'n/a'}` : '',
            ].join(''),
          ),
        ),
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

// ----------------------------------------------------------------- ticker

export function renderTicker(events) {
  const lane = document.getElementById('ticker-lane');
  if (!lane) return;
  clear(lane);
  const top = [...events].sort(byProminence).slice(0, 12);
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
          duplicate ? h('span', { text: e.title }) : h('a', { href: `#${e.event_id}`, text: e.title }),
        ),
      ),
    );
  lane.append(track(false), track(true));
}

// ------------------------------------------------------------ fx and boot

export function initFxToggle({ button = document.getElementById('fx-toggle'), root = document.documentElement, storage = safeStorage('localStorage') } = {}) {
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
  initFxToggle();
  initTickerToggle();
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

  const count = document.getElementById('filter-count');
  const refresh = () => {
    const filtered = applyFilters(state);
    filtersActive = Boolean(state.query.trim()) || Object.values(state.selected).some((v) => v.size);
    renderSections({ events: filtered });
    if (count) count.textContent = `${filtered.length} OF ${data.events.length} EVENTS`;
  };
  renderFacetUi(buildFacets(data.events), state, refresh);
  document.getElementById('filter-query')?.addEventListener('input', (e) => {
    state.query = e.target.value;
    refresh();
  });
  document.getElementById('filter-clear')?.addEventListener('click', () => {
    state.query = '';
    state.selected = {};
    const q = document.getElementById('filter-query');
    if (q) q.value = '';
    document.querySelectorAll('#filter-facets button[aria-pressed="true"]').forEach((b) => b.setAttribute('aria-pressed', 'false'));
    refresh();
  });
  refresh();
  runBoot(data);
}

if (typeof document !== 'undefined') main();
