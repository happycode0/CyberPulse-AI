# Importance and reputation

[Wiki home](README.md) · [The AI news beat](ai-news-beat.md) ·
Code: `worker/pipeline/importance.py`, `worker/pipeline/reputation.py`, `worker/publish/build.py`

**Status: built.** Every published event has an `importance`, and every source a `reputation`.
Both are worked out in code at publish time, from facts already in the database and
`config/sources.yaml`. No model is called. Where the model has estimated a severity or an AI
significance, that estimate counts, and the reason says so. An event no model has read is
still rated on what is known.

---

## What it gives you

- **Every event says how much it matters**: a score from 0 to 100, a tier (`key`, `notable`
  or `routine`) and up to five short reasons. These are in `live.json` and every day page in
  `history/`.
- **Every source says how far it can be trusted**: a score from 0 to 100 and the basis it
  rests on. This is in `source-health.json`, with the source's description, its standing and
  what needs doing about it, if anything.

Importance is not prominence. Prominence (`worker/pipeline/score.py`, `SCORING_VERSION`)
decides which events are live and in what order. Importance is a label a reader can check,
and it changes neither.

## Source reputation

### Standing

Every source in `config/sources.yaml` has a `standing`. It says what kind of voice the source
is. It is set by hand, and it is the base of the source's reputation.

| `standing` | Base | Who | Examples (of 60 sources) |
|---|---|---|---|
| `authoritative` | 90 | Government agencies, CERTs, standards bodies, a vendor's own advisories about its own products | ACSC, ASD, CISA and the KEV list, the OAIC, the DTA, Microsoft's CVRF feed (16) |
| `established` | 75 | National public broadcasters, major newsrooms, long-running specialist outlets with editorial standards | ABC, iTnews, BleepingComputer, Krebs on Security, The Record, Dark Reading (13) |
| `specialist` | 60 | Trade press, vendor research and blogs, respected individual researchers, academic preprints | The Hacker News, Cyber Daily, SANS ISC, Unit 42, Simon Willison, OpenAI news (29) |
| `community` | 40 | Aggregators, social search, personal blogs without editorial review | CVE Details, X security search (2) |

A source the discovery gate added (it lives in the database, not the file) has no standing,
so it counts as `community`. Its description is null on the Sources page.

### The formula

```text
reputation = (0.60 × standing base + 0.15 × uptime + 0.25 × corroboration) / weights used
```

| Part | Weight | What it measures |
|---|---|---|
| Standing | 60% | The base in the table above |
| Uptime | 15% | The share of the source's last 20 health checks that came back `ok`, × 100. A check made while the source was disabled is left out |
| Corroboration | 25% | Of the source's events first seen in the last 90 days, the share another independent lineage also reported, × 100 |

**Independent lineage** is the same rule that counts confirmations
(`worker/pipeline/lineage.py`). The source's own second feed, a copy of its own headline and a
report marked not independent never corroborate it. An event merged into another is counted as
the one it joined.

**A part with too little behind it is left out**, and the weights of the rest are scaled up
to make 100%. A source with no health checks yet has no uptime. A source with fewer than 5
events in the 90 days has no corroboration. `basis` says what was used and what was left out:

```text
standing, uptime over the last 20 checks and corroboration of 7 events in 90 days
standing only; left out: no health checks yet; 0 events in 90 days, too few to judge corroboration (needs 5)
```

The score is rounded half up. Live figures on 2026-10-04:

| Source | Standing | Uptime | Corroboration | Score |
|---|---|---|---|---|
| ASD | authoritative | 1.0 (7 checks) | left out (0 events) | (54 + 15) / 0.75 = **92** |
| ACSC Alerts | authoritative | 1.0 | 0.429 (7 events) | 54 + 15 + 10.7 = **80** |
| CISA KEV | authoritative | 0.45 | 0.143 (98 events) | 54 + 6.8 + 3.6 = **64** |
| ABC cyber | established | 1.0 | 0.0 (19 events) | 45 + 15 + 0 = **60** |
| The Hacker News | specialist | 1.0 | 0.222 (63 events) | 36 + 15 + 5.6 = **57** |
| Fortinet blog | specialist | 0.105 | 0.0 (7 events) | 36 + 1.6 + 0 = **38** |

Corroboration is low for nearly everyone. Most stories are reported by one source in the
registry, so the share is a raw count and not adjusted for how often a beat is shared. The
standing therefore sets most of the order, and corroboration and uptime move a source up or
down within it. A source that has been timing out for days (Sophos, 48) falls below the other
specialists.

## Event importance

### The six parts

Each part is worked out from the event as it is published. They are added up and capped at 100.

| Part | Points | When |
|---|---|---|
| Standing | up to 25 | A quarter of the best reputation among the event's sources. A source no longer in the registry counts as community's 40, so 10 |
| Australia | 0, 8, 15 or 25 | Relevance over 0: 8 (some). 0.5 and over: 15 (relevant). 0.75 and over: 25 (strong). Reported in Australia lifts it one step, to at least 15 |
| Public sector | 15, or 8 | A government body, critical infrastructure or a critical sector is the target of a threat: 15. Named without a threat (a policy, a contract): 8 |
| Harm | up to 25 | The largest of: severity (critical 25, high 18, medium 8); actively exploited, at least 20; an incident with no register severity, 15; on the AI desk alone, AI significance (major 22, notable 12) |
| Convergence | 5 | The event is on both desks, cyber and AI |
| Corroboration | 6 or 12 | 2 independent sources: 6. 3 or more: 12 |

**Government** is found from the sectors (`government`, `defence`), or from the headline or
the organisations naming one: "government", "ministry", "minister", "department of",
"parliament", "council", "federal agency", "public sector", "defence force", "Services
Australia", "Home Affairs", "National Parks" and a few more (`GOVERNMENT_WORDS`). "Governance"
does not count, and nor does "police" on its own. **Critical sectors** are critical infrastructure, energy, water,
health, telecommunications, transport, finance and education, and any SOCI asset class. A
**threat** is any incident, exploitation, malware, phishing, emerging-threat or vulnerability
category, or a KEV-listed CVE.

**Exploited** means a KEV-listed CVE, or the `active-exploitation` or `zero-day` category.
**Incidents** are a data breach, ransomware, denial of service, espionage, a supply-chain
attack, fraud or an AI incident. An incident counts 15 when its severity is unknown or only
the model's estimate: harm was done, whatever the guess. A severity from a register (CNA,
CISA-ADP, NVD or a vendor) is a fact and stands.

**Off both desks** (`beat: other`), an event is `routine` whatever it adds up to. Its score is
capped at 39 and its first reason is "neither cyber nor AI".

### Tiers and reasons

| Tier | Score |
|---|---|
| `key` | 60 and over |
| `notable` | 40 to 59 |
| `routine` | under 40 |

The reasons are the parts that scored, largest first, at most five. Each names its source of
truth: "critical severity (CNA score)", "high severity (AI estimate)", "actively exploited
(CISA KEV)", "reported by ABC (established)".

### A worked example

*Rogue OpenAI agent accessed second NSW government website* (ABC, 2026-10-04):

| Part | Points | Reason |
|---|---|---|
| Australia | 25 | relevance 0.6, reported in Australia: "relevant to Australia, reported there" |
| Harm | 15 | `ai-incident`, severity only the model's low estimate: "AI incident" |
| Public sector | 15 | "government website" in the headline, and a threat: "Australian government target" |
| Standing | 15 | ABC cyber's reputation 60, a quarter: "reported by ABC (established)" |
| Convergence | 5 | on both desks: "cyber and AI" |
| **Total** | **75** | **key** |

### Calibration

Run on the published data of 2026-10-04, the live page (194 events) and the last 90 days of
day pages (1,099 events):

| | key | notable | routine |
|---|---|---|---|
| Live page | 17 (9%) | 64 (33%) | 113 (58%) |
| 90 days of history | 24 (2%) | 174 (16%) | 901 (82%) |

The aim was 10 to 20% key and 25 to 35% notable on the live page. The live page holds the most
prominent events, so the history is mostly routine, as it should be. Corroboration was raised
from 5 and 10 to 6 and 12: at 5 and 10, a zero-day three outlets reported fell just short of
key.

## What the Sources page flags

Each source in `source-health.json` has `attention`: null, or a `kind` and a plain-English
`reason`. The first rule that matches wins.

| `kind` | When | Example reason |
|---|---|---|
| none | The source is retired | |
| `coming` | It is not enabled; the registry's note says why | "Not collected yet. Blocked: Cloudflare 403 error on feed endpoint." |
| `coming` | It is a discovered or candidate find | "A new find, on trial before it is collected." |
| `fix` | Its last check failed, timed out or came back empty | "The last check timed out: ReadTimeout after 30s" |
| `fix` | It is degraded or broken and still unhealthy | "Degraded: 11 unhealthy checks in a row, nothing new in 2.4 days (it usually publishes daily)." |
| `watch` | Its last check was stale | "Stale: nothing new in 8.2 days (it usually publishes daily)." |
| `watch` | It is degraded or broken but healthy again | "Recovering: healthy again after failed checks." |
| none | Anything else, including a source in testing whose checks are all `ok` | |

`pipeline` lists up to 25 finds still at the discovery gate, furthest through first: the name,
the domain, the state, when it was found and where it stands, in the worker's own words. It
never carries a feed URL, the search evidence or the finder's reason.

## How to change a standing

1. Edit the source's `standing:` in `config/sources.yaml`. The comment above `sources:` says
   which kind of voice each standing is for. Keep the `description:` to one plain sentence.
2. Run the unit tests. `tests/unit/test_registry.py` checks that every source has a valid
   standing and a description.
3. Merge it. The worker reads the file at each publish, so the source's reputation, and the
   importance of every event it reported, change at the next hourly publish. No migration,
   and nothing stored changes.

A new source needs both fields before it can be added. A source the discovery gate activates
is `community` until someone gives it a line in the file.

## Changing a weight

The weights and thresholds are constants at the top of `worker/pipeline/importance.py` and
`worker/pipeline/reputation.py`. A change re-rates every published event at the next publish,
because nothing is stored. Bump `IMPORTANCE_VERSION` or `REPUTATION_VERSION` with any change
that can move a score, update the tests and the tables on this page, and run the calibration
again on the live page. Both versions are `"1"` today.

---

[Wiki home](README.md)
