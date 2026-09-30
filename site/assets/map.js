// CyberPulse-AI world map. Real Natural Earth geometry (world-atlas countries-110m,
// public domain) drawn through the vendored d3-geo Equal Earth projection, rotated to
// 150°E so Australia sits at the centre. One <path data-n3="..."> per country: the
// ISO 3166-1 numeric id is what couples the picture to the country filter, so map
// clicks and the paired <select> drive exactly the same selection.
//
// Built with the same h()/s() helpers as hud.js: textContent and setAttribute only,
// never innerHTML, and no third-party origins — everything is self-hosted.

import { geoEqualEarth, geoPath } from './vendor/d3-geo.min.js';
import { feature } from './vendor/topojson-client.min.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const ATLAS_URL = 'assets/vendor/countries-110m.json';

// ISO 3166-1 alpha-2 and alpha-3 -> numeric, transcribed from the world-countries
// dataset (MIT). Reference data, not map data: it lets a country written as "AU",
// "AUS" or "Australia" in an event resolve to the same numeric id as the atlas.
const ALPHA2_N3 = 'AD:020,AE:784,AF:004,AG:028,AI:660,AL:008,AM:051,AO:024,AQ:010,AR:032,AS:016,AT:040,AU:036,AW:533,AX:248,AZ:031,BA:070,BB:052,BD:050,BE:056,BF:854,BG:100,BH:048,BI:108,BJ:204,BL:652,BM:060,BN:096,BO:068,BQ:535,BR:076,BS:044,BT:064,BV:074,BW:072,BY:112,BZ:084,CA:124,CC:166,CD:180,CF:140,CG:178,CH:756,CI:384,CK:184,CL:152,CM:120,CN:156,CO:170,CR:188,CU:192,CV:132,CW:531,CX:162,CY:196,CZ:203,DE:276,DJ:262,DK:208,DM:212,DO:214,DZ:012,EC:218,EE:233,EG:818,EH:732,ER:232,ES:724,ET:231,FI:246,FJ:242,FK:238,FM:583,FO:234,FR:250,GA:266,GB:826,GD:308,GE:268,GF:254,GG:831,GH:288,GI:292,GL:304,GM:270,GN:324,GP:312,GQ:226,GR:300,GS:239,GT:320,GU:316,GW:624,GY:328,HK:344,HM:334,HN:340,HR:191,HT:332,HU:348,ID:360,IE:372,IL:376,IM:833,IN:356,IO:086,IQ:368,IR:364,IS:352,IT:380,JE:832,JM:388,JO:400,JP:392,KE:404,KG:417,KH:116,KI:296,KM:174,KN:659,KP:408,KR:410,KW:414,KY:136,KZ:398,LA:418,LB:422,LC:662,LI:438,LK:144,LR:430,LS:426,LT:440,LU:442,LV:428,LY:434,MA:504,MC:492,MD:498,ME:499,MF:663,MG:450,MH:584,MK:807,ML:466,MM:104,MN:496,MO:446,MP:580,MQ:474,MR:478,MS:500,MT:470,MU:480,MV:462,MW:454,MX:484,MY:458,MZ:508,NA:516,NC:540,NE:562,NF:574,NG:566,NI:558,NL:528,NO:578,NP:524,NR:520,NU:570,NZ:554,OM:512,PA:591,PE:604,PF:258,PG:598,PH:608,PK:586,PL:616,PM:666,PN:612,PR:630,PS:275,PT:620,PW:585,PY:600,QA:634,RE:638,RO:642,RS:688,RU:643,RW:646,SA:682,SB:090,SC:690,SD:729,SE:752,SG:702,SH:654,SI:705,SJ:744,SK:703,SL:694,SM:674,SN:686,SO:706,SR:740,SS:728,ST:678,SV:222,SX:534,SY:760,SZ:748,TC:796,TD:148,TF:260,TG:768,TH:764,TJ:762,TK:772,TL:626,TM:795,TN:788,TO:776,TR:792,TT:780,TV:798,TW:158,TZ:834,UA:804,UG:800,UM:581,US:840,UY:858,UZ:860,VA:336,VC:670,VE:862,VG:092,VI:850,VN:704,VU:548,WF:876,WS:882,YE:887,YT:175,ZA:710,ZM:894,ZW:716';
const ALPHA3_N3 = 'AND:020,ARE:784,AFG:004,ATG:028,AIA:660,ALB:008,ARM:051,AGO:024,ATA:010,ARG:032,ASM:016,AUT:040,AUS:036,ABW:533,ALA:248,AZE:031,BIH:070,BRB:052,BGD:050,BEL:056,BFA:854,BGR:100,BHR:048,BDI:108,BEN:204,BLM:652,BMU:060,BRN:096,BOL:068,BES:535,BRA:076,BHS:044,BTN:064,BVT:074,BWA:072,BLR:112,BLZ:084,CAN:124,CCK:166,COD:180,CAF:140,COG:178,CHE:756,CIV:384,COK:184,CHL:152,CMR:120,CHN:156,COL:170,CRI:188,CUB:192,CPV:132,CUW:531,CXR:162,CYP:196,CZE:203,DEU:276,DJI:262,DNK:208,DMA:212,DOM:214,DZA:012,ECU:218,EST:233,EGY:818,ESH:732,ERI:232,ESP:724,ETH:231,FIN:246,FJI:242,FLK:238,FSM:583,FRO:234,FRA:250,GAB:266,GBR:826,GRD:308,GEO:268,GUF:254,GGY:831,GHA:288,GIB:292,GRL:304,GMB:270,GIN:324,GLP:312,GNQ:226,GRC:300,SGS:239,GTM:320,GUM:316,GNB:624,GUY:328,HKG:344,HMD:334,HND:340,HRV:191,HTI:332,HUN:348,IDN:360,IRL:372,ISR:376,IMN:833,IND:356,IOT:086,IRQ:368,IRN:364,ISL:352,ITA:380,JEY:832,JAM:388,JOR:400,JPN:392,KEN:404,KGZ:417,KHM:116,KIR:296,COM:174,KNA:659,PRK:408,KOR:410,KWT:414,CYM:136,KAZ:398,LAO:418,LBN:422,LCA:662,LIE:438,LKA:144,LBR:430,LSO:426,LTU:440,LUX:442,LVA:428,LBY:434,MAR:504,MCO:492,MDA:498,MNE:499,MAF:663,MDG:450,MHL:584,MKD:807,MLI:466,MMR:104,MNG:496,MAC:446,MNP:580,MTQ:474,MRT:478,MSR:500,MLT:470,MUS:480,MDV:462,MWI:454,MEX:484,MYS:458,MOZ:508,NAM:516,NCL:540,NER:562,NFK:574,NGA:566,NIC:558,NLD:528,NOR:578,NPL:524,NRU:520,NIU:570,NZL:554,OMN:512,PAN:591,PER:604,PYF:258,PNG:598,PHL:608,PAK:586,POL:616,SPM:666,PCN:612,PRI:630,PSE:275,PRT:620,PLW:585,PRY:600,QAT:634,REU:638,ROU:642,SRB:688,RUS:643,RWA:646,SAU:682,SLB:090,SYC:690,SDN:729,SWE:752,SGP:702,SHN:654,SVN:705,SJM:744,SVK:703,SLE:694,SMR:674,SEN:686,SOM:706,SUR:740,SSD:728,STP:678,SLV:222,SXM:534,SYR:760,SWZ:748,TCA:796,TCD:148,ATF:260,TGO:768,THA:764,TJK:762,TKL:772,TLS:626,TKM:795,TUN:788,TON:776,TUR:792,TTO:780,TUV:798,TWN:158,TZA:834,UKR:804,UGA:800,UMI:581,USA:840,URY:858,UZB:860,VAT:336,VCT:670,VEN:862,VGB:092,VIR:850,VNM:704,VUT:548,WLF:876,WSM:882,YEM:887,MYT:175,ZAF:710,ZMB:894,ZWE:716';

// Short or colloquial country names that do not appear verbatim in Natural Earth's
// name field. Keys are matched case-insensitively; values are ISO numeric.
const NAME_ALIASES = {
  'united states': '840',
  'united states of america': '840',
  'uk': '826',
  'great britain': '826',
  'britain': '826',
  'south korea': '410',
  'north korea': '408',
  'republic of korea': '410',
  'russian federation': '643',
  'czech republic': '203',
  'ivory coast': '384',
  "cote d'ivoire": '384',
  'united arab emirates': '784',
  'uae': '784',
  'drc': '180',
  'dr congo': '180',
  'democratic republic of the congo': '180',
  'republic of the congo': '178',
  'burma': '104',
  'viet nam': '704',
  'east timor': '626',
  'swaziland': '748',
  'macedonia': '807',
  'cape verde': '132',
  'palestinian territories': '275',
  'taiwan, province of china': '158',
  'republic of china': '158',
  'laos': '418',
  'bolivia': '068',
  'venezuela': '862',
  'iran': '364',
  'syria': '760',
  'tanzania': '834',
  'moldova': '498',
  'brunei': '096',
};

const BUCKETS = [
  { min: 7, load: 4, label: '7+' },
  { min: 4, load: 3, label: '4–6' },
  { min: 2, load: 2, label: '2–3' },
  { min: 1, load: 1, label: '1' },
];

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

function pad3(id) {
  const raw = String(id ?? '');
  return raw.length >= 3 ? raw : raw.padStart(3, '0');
}

function parseCodeTable(text) {
  const map = new Map();
  for (const pair of text.split(',')) {
    const i = pair.indexOf(':');
    map.set(pair.slice(0, i), pair.slice(i + 1));
  }
  return map;
}

const ALPHA2 = parseCodeTable(ALPHA2_N3);
const ALPHA3 = parseCodeTable(ALPHA3_N3);

function bucketFor(count) {
  return BUCKETS.find((b) => count >= b.min) || BUCKETS[BUCKETS.length - 1];
}

// ------------------------------------------------------------------- state

// The most recent render, so syncMapSelection() can keep the picture and the
// <select> in step when the tag filter clears the country selection.
let current = null;

// Resolve one country token ("AU", "AUS", "Australia", "036") to an ISO numeric id.
function makeResolver(names) {
  return (token) => {
    const raw = String(token || '').trim();
    if (!raw) return null;
    if (/^\d{1,3}$/.test(raw)) return pad3(raw);
    const upper = raw.toUpperCase();
    if (/^[A-Z]{2}$/.test(upper)) return ALPHA2.get(upper) || null;
    if (/^[A-Z]{3}$/.test(upper)) return ALPHA3.get(upper) || null;
    const lower = raw.toLowerCase();
    return names.get(lower) || NAME_ALIASES[lower] || null;
  };
}

// ------------------------------------------------------------------ render

// Draw the world map for `events` and pair it with the #map-country select.
// onCountryClick(tokensOrNull) fires from both the map and the select; tokens is a
// Set of every raw country token that resolves to the chosen ISO id.
export async function renderMap(events, onCountryClick, opts = {}) {
  const fetchImpl = opts.fetchImpl || globalThis.fetch;
  const host = opts.host || document.getElementById('world-map');
  const select = opts.select || document.getElementById('map-country');
  const url = opts.url || ATLAS_URL;

  let topology = null;
  try {
    const res = await fetchImpl(url, { cache: 'force-cache' });
    if (res.ok) topology = await res.json();
  } catch {
    topology = null;
  }
  if (!topology || !topology.objects || !topology.objects.countries) {
    if (host) clear(host).append(h('p', { class: 'empty', text: 'THE BASEMAP COULD NOT BE READ, SO THE MAP IS NOT SHOWN. THE COUNTRY DROPDOWN BELOW STILL WORKS.' }));
    return null;
  }

  const countries = feature(topology, topology.objects.countries).features;
  const names = new Map();
  for (const f of countries) {
    const name = f.properties && f.properties.name ? String(f.properties.name).toLowerCase() : '';
    if (name) names.set(name, pad3(f.id));
  }
  const n3Of = makeResolver(names);

  // Events may name a country once each; count events, not raw mentions.
  const tokenCounts = new Map();
  for (const event of events || []) {
    const seen = new Set();
    for (const raw of (event.entities && event.entities.countries) || []) {
      const token = String(raw).trim();
      if (!token || seen.has(token.toLowerCase())) continue;
      seen.add(token.toLowerCase());
      tokenCounts.set(token, (tokenCounts.get(token) || 0) + 1);
    }
  }
  const n3Counts = new Map();
  const n3Tokens = new Map();
  const unplaced = [];
  for (const [token, count] of tokenCounts) {
    const n3 = n3Of(token);
    if (n3) {
      n3Counts.set(n3, (n3Counts.get(n3) || 0) + count);
      if (!n3Tokens.has(n3)) n3Tokens.set(n3, []);
      n3Tokens.get(n3).push(token);
    } else {
      unplaced.push(token);
    }
  }

  const projection = geoEqualEarth().rotate([-150, 0]).fitExtent(
    [[6, 6], [954, 494]],
    { type: 'Sphere' },
  );
  const path = geoPath(projection);

  const svg = s('svg', {
    class: 'map-svg',
    viewBox: '0 0 960 500',
    role: 'group',
    'aria-label':
      'World map of this snapshot, centred on Australia. Countries with events are shaded by event count and can be selected. The country dropdown is the keyboard and screen-reader equivalent.',
  });

  const paths = new Map();
  for (const feature of countries) {
    const n3 = pad3(feature.id);
    const d = path(feature);
    if (!d) continue;
    const count = n3Counts.get(n3) || 0;
    const attrs = { class: 'map-country', 'data-n3': n3, d };
    let el;
    if (count) {
      const name = (feature.properties && feature.properties.name) || n3;
      attrs['data-count'] = String(count);
      attrs['data-load'] = String(bucketFor(count).load);
      attrs.role = 'button';
      attrs.tabindex = '0';
      attrs['aria-label'] = `${name}: ${count} ${count === 1 ? 'event' : 'events'} in this snapshot. Select to filter.`;
      el = s('path', attrs);
      const choose = () => {
        // Every token that resolves to this id ("Australia" and "AU" both mean 036), so
        // the filtered count is the same number the aria-label promises.
        const tokens = n3Tokens.get(n3) || [];
        if (!tokens.length) return;
        const selection = new Set(tokens);
        syncMapSelection(selection);
        if (onCountryClick) onCountryClick(selection);
      };
      el.addEventListener('click', choose);
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          choose();
        }
      });
    } else {
      attrs['aria-hidden'] = 'true';
      el = s('path', attrs);
    }
    paths.set(n3, el);
    svg.append(el);
  }

  if (host) {
    clear(host).append(svg);
    if (unplaced.length) {
      host.append(
        h('p', {
          class: 'hint',
          text: `Country tags not on the map (no numeric ISO match): ${unplaced.join(', ')}.`,
        }),
      );
    } else if (!n3Counts.size) {
      host.append(h('p', { class: 'empty', text: 'NO COUNTRY-TAGGED EVENTS IN THIS SNAPSHOT.' }));
    }
  }

  // Paired <select>: one option per ISO id, labelled with every token that resolves to it
  // ("Australia · AU (13)"), so the option, the path's aria-label and the filtered count can
  // never drift apart. Tokens with no numeric ISO match stay selectable on their own, and the
  // ALL COUNTRIES option clears the filter. This is the keyboard/screen-reader equivalent of
  // clicking a path.
  if (select) {
    clear(select);
    select.append(h('option', { value: '', text: 'ALL COUNTRIES' }));
    const grouped = new Map();
    for (const [token, count] of tokenCounts) {
      const n3 = n3Of(token);
      if (!n3) continue;
      const entry = grouped.get(n3) || { tokens: [], count: 0 };
      entry.tokens.push(token);
      entry.count += count;
      grouped.set(n3, entry);
    }
    const options = [
      ...[...grouped.entries()].map(([n3, entry]) => ({
        value: n3,
        text: `${entry.tokens.join(' · ')} (${entry.count})`,
        count: entry.count,
      })),
      ...unplaced.map((token) => ({
        value: token,
        text: `${token} (${tokenCounts.get(token)})`,
        count: tokenCounts.get(token),
      })),
    ].sort((a, b) => b.count - a.count || a.text.localeCompare(b.text));
    for (const option of options) {
      select.append(h('option', { value: option.value, text: option.text }));
    }
    select.onchange = () => {
      const value = select.value;
      const selection = value
        ? (n3Tokens.has(value) ? new Set(n3Tokens.get(value)) : new Set([value]))
        : null;
      applySelection(selection);
      if (onCountryClick) onCountryClick(selection);
    };
  }

  current = { paths, n3Of, select };
  applySelection(null);

  const countOut = document.getElementById('map-count');
  if (countOut) {
    countOut.textContent = `${n3Counts.size} ${n3Counts.size === 1 ? 'COUNTRY' : 'COUNTRIES'} · ${[...n3Counts.values()].reduce((a, b) => a + b, 0)} SIGNALS`;
  }
  const led = document.getElementById('map-led');
  if (led) led.dataset.state = n3Counts.size ? 'ok' : 'idle';

  return svg;
}

// Highlight the selected country in the picture and mirror it in the <select>.
// `selection` is null/"" (nothing selected), one raw token, or the Set of every raw token
// that resolves to a single ISO id — which is what a path click hands over.
function applySelection(selection) {
  if (!current) return;
  const list = selection == null || selection === ''
    ? []
    : Array.isArray(selection) || selection instanceof Set
      ? [...selection]
      : [String(selection)];
  let n3 = null;
  for (const token of list) {
    const id = current.n3Of(token);
    if (id) { n3 = id; break; }
  }
  for (const [key, el] of current.paths) {
    el.classList.toggle('is-selected', Boolean(n3) && key === n3);
  }
  if (current.select) {
    // The <select> is keyed by ISO id when the token maps to one, else by the raw token.
    const value = n3 || (list.length ? list[0] : '');
    if (current.select.value !== value) current.select.value = value;
  }
}

// Keep map and select in step when a filter is cleared elsewhere (CLEAR FILTERS).
export function syncMapSelection(selection) {
  applySelection(selection || null);
}
