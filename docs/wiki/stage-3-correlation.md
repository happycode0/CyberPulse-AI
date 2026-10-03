# Stage 3 — Correlation depth + trends

[← Stage 2 — Ground truth](stage-2-ground-truth.md) · [Wiki home](README.md) ·
[Stage 4 — Paperclip →](stage-4-paperclip.md)

**Status: ✅ done (2026-10-03).** Consolidation (one story = one event), source lineage, material
change, archiving, trends and readers for the sources with no feed all run on VM 200. Embeddings
were measured as not needed. Nothing in it is yours to do. Plan:
[PLAN.md §9, Stage 3](../../PLAN.md#9-stages)

---

## What this stage gives you

Sharper events. One story reported by twenty outlets counts as one event, and copies of the same
wire story stop looking like independent confirmation. Trends come from measured velocity, not
impressions.

## What is done

| Piece | State |
|---|---|
| Resolver rewrite: CVE sets, generic notices ("CISA Adds Two Known Exploited…"), register rules | ✅ |
| Headline repair: markup and HTML entities stripped from titles (migration 009 fixes stored ones) | ✅ |
| Consolidation pass every run: stored events that are one story are merged (`worker/pipeline/correlate.py`) | ✅ |
| Merged events are archived with `merged_into`, never deleted; old links redirect on the site | ✅ |
| `python -m worker --check-duplicates`: the duplicate rate now, and as it would be after merging | ✅ |
| Source lineage: one organisation's feeds, a relay of an agency and a word-for-word copy are one voice (`worker/pipeline/lineage.py`) | ✅ |
| `python -m worker --check-lineage`: confirmations counted by outlet and by lineage | ✅ |
| Material change: what a new report or a register adds to an event moves it up again (`worker/pipeline/material.py`) | ✅ |
| Archiving: a faded event leaves the live set, keeps its day page, and comes back on news (`worker/db/archive.py`) | ✅ |
| Trends: topic and CVE velocity counted from independent reports, published as `trends.json` and shown under TRENDS (`worker/pipeline/trends.py`) | ✅ |
| Sources with no feed: ASD, OAIC and Anthropic read from their listing pages (`worker/collectors/web_page.py`), Microsoft's security releases from its API | ✅ |
| Source health judged against each feed's measured rhythm: the four ACSC feeds no longer read STALE between alerts | ✅ |
| AI sources moved from DEEP, which nothing schedules, to NORMAL, as PLAN.md §5 says | ✅ |

### The duplicate rate (measured on VM 200, 2026-10-03)

| | Events on the site (last 30 days) | In a possible-duplicate pair |
|---|---|---|
| Before consolidation | 220 | 64 (29.1%) |
| After (dry run) | 192 | 26 (13.5%) |

The pass folds 54 events into 36 stories. The largest is the Citrix NetScaler zero-days: nine
events from seven outlets, including both KEV entries, become one.

A "possible-duplicate pair" is loose on purpose: two live events that share a CVE or whose
headlines overlap at 0.20 (weighted) within 72 h. Most of what is left is one of two kinds:

- **Old ingest mistakes that the pass cannot undo.** Before this stage, the generic notices
  `evt-2026-000068` and `evt-2026-000069` were merged with unrelated KEV entries. A merge is never
  split automatically, so they share a CVE with several live events. They will age out.
- **The same story in quite different words** (0.21–0.27 weighted), such as KillSec's takedown
  told three ways. The pass leaves these apart rather than guess.

### How the pass decides

Each pair of stored events is asked, in order: the same headline within 72 h, the same CVEs,
shared CVEs plus words, near-identical words within 72 h. Then one more question: do the
headlines' **rare** words overlap (0.30 or more, each word weighted by how few of the last 180
days' headlines use it, within 72 h)? "KillSec" and "Medibank" carry a story; "ransomware" and
"attack" do not.

What keeps it from merging things that only look alike:

- **Different CVEs are different stories.** Microsoft's "July 2026 Security Update Review" and
  Apple's never join, whatever the words.
- **One register's two advisories are two advisories** (CISA KEV, ACSC, a vendor's PSIRT feed).
  A story naming both can still gather them, as with the NetScaler pair.
- **One outlet's house style is not a story.** triskele's "Critical Fortinet FortiOS … Under
  Active Exploitation" and "Critical MikroTik RouterOS … under Active Exploitation" share only
  the template. Weighted words never join two events that one outlet reported both of.
- **Weights need a corpus.** Under 100 headlines every word looks rare, so the weighted question
  waits (a fresh install merges nothing on words alone).

The winner is the enriched event (its summary was paid for), then the most prominent, then the
oldest. It keeps its id. If its title is a generic notice, it takes the group's first real
headline. The losers' sources, CVEs, timeline, claims, MITRE techniques and links move to it.
Another outlet's report does not count as a material update, so it does not refresh the
winner's prominence. A CVE it did not have does.

### Independent confirmation (measured on VM 200, 2026-10-03)

A confirmation used to be any other outlet. Now it is any other **lineage**, and each report on
an event belongs to one, the first of these that applies:

1. **A relay.** The headline names an agency that also reported the event ("CISA Says Attackers
   Are Exploiting…", "ACSC warns of…"). Only agencies are matched, by the `names` under
   `publishers` in `config/sources.yaml`: "Microsoft" in a headline is as often the product.
2. **A copy.** The headline is word for word another publisher's earlier one (five words or
   more; "CVE-2026-1234" is a headline two registers arrive at on their own).
3. **The publisher's own.** ACSC's alert and its news item are one voice (`publisher:` on each
   of ASD's seven feeds, CISA's four, Google's, Microsoft's and My Security Media's).

One report per lineage is independent: the originator's own, else the earliest. Ingest still
guesses one outlet at a time; the run's correlation pass settles it for every event seen in the
last 30 days, in the same transaction as the merges, before anything is scored.

| | Last 30 days |
|---|---|
| Events | 2,216 |
| Confirmations counted by outlet | 2,265 |
| Counted by lineage | 2,255 |
| Events that had been counting one voice twice or more | 9 |

The NetScaler zero-days go from seven voices to five (CISA's KEV entries, its advisory and THN's
"CISA Says…" are CISA's; ACSC's alert and My Security Media's "ACSC warns…" are ASD's). Of the
other eight, six are one agency's two feeds on one item, and two are an outlet relaying an agency
(My Security Media on ACSC's AI-misalignment alert, THN on CISA's WSO2 KEV entry): one voice
each now. Few events change because most stories here have one outlet; the rule matters most on
the big ones.

### Material change (measured on VM 200, 2026-10-03)

An event's prominence decays from its last **material** update, so only news brings it back up,
and a new event that gains some becomes a developing one. Another outlet agreeing is evidence,
not news. Found without a model:

| Change | When |
|---|---|
| `NEW_CVE` | A report names a CVE the event did not have |
| `EXPLOIT_CONFIRMED`, `NEW_EXPLOIT`, `NEW_PATCH`, `NEW_MITIGATION`, `CORRECTION` | A report's headline says so ("…exploited in attacks", "PoC exploit released…", "…patches…") and nothing the event had already did. "No patch yet…" and "no evidence of exploitation" do not count |
| `NEW_AU_EXPOSURE` | The first Australian source on a story first reported elsewhere |
| `EXPLOIT_CONFIRMED` | CISA adds one of the event's CVEs to KEV after the event began |
| `NEW_PATCH` | An advisory on one of its CVEs that named no fixed version names one |

Each kind is written once per event: CISA's "Adds … to Catalog" notice and the listing it
announces are one confirmation. Headlines only, never feed summaries, whose boilerplate ("apply
the latest updates") would read as news on every story. New actors, targets, geography and impact
need the entities a model reads, so they wait for the AI layer.

Replayed over the last 30 days (118 events with more than one report), the headline rules find
4 confirmed exploitations, 1 new exploit and 12 first Australian reports: few, and each one real,
such as "Cisco warns of new SD-WAN zero-day exploited in attacks" joining a Cisco advisory.

### Archiving (measured on VM 200, 2026-10-03)

Every run, after scoring, an event is archived when its prominence has decayed below 0.05 or it
has had no material update for 30 days (`archive:` in `config/scoring.yaml`). Nothing is deleted.
An archived event:

- stays on its day page in the site's history, and its link still works;
- leaves `live.json`, and is no longer rescored, enriched, given MITRE techniques or merged;
- comes back as a **developing** event on a material change: a report that adds something, a
  KEV listing, or a published fix. Another outlet repeating the story does not bring it back.

| | Events |
|---|---|
| Standing before the first pass | 2,216 |
| Archived by the first pass | 2,026 |
| … faded below 0.05 in the last 30 days | 313 |
| … faded and idle for more than 30 days | 1,713 |
| Still live | 190 |

No event idle for 30 days still scored 0.05 or more (the highest was 0.021): decay already made
them invisible. Archiving turns that number into a status that the site, enrichment and the
merge pass all read the same way. The oldest dates from November 2021: some feeds list years of
items.

### Trends (measured on VM 200, 2026-10-03)

Every publish counts what the database already holds into `trends.json`
(`worker/pipeline/trends.py`); nothing is stored and no model is asked. The site shows it as
TRENDS, the last panel on the Dashboard view (`#sec-trends` still lands on it).

- **What is counted.** Independent reports (one per lineage, as above), each on the day it was
  published. A report counts toward a topic when its own headline names it: a vendor, actor,
  malware family or kind of threat from the curated list in `config/trends.yaml` (126 topics,
  whole words only). A CVE counts every report on an event that names it.
- **Only since collection began.** Feeds list years of items, so a report or story dated before
  the first run is not counted anywhere. CISA's KEV dates are the exception: they cover its whole
  catalogue, so KEV additions count on every day.
- **Velocity.** The last 24 hours against the per-day rate over up to six days before them,
  softened by one: (last day + 1) / (per day before + 1). **New** is three or more reports where
  there were none; **rising** is three or more at twice the rate or more; **falling** is a rate of
  one a day or more that has halved. Anything else is **steady**.
- **Warming up.** With under 48 hours before the last day, nothing is called rising or falling.
  The site says so, and says how many hours there are.
- **Emerging threats.** An event named by a new or rising topic is listed under EMERGING THREATS.

| | First production payload |
|---|---|
| Collecting since | 2026-10-01 11:33 UTC (21:33 AEST) |
| Hours before the last day | 18 of the 48 needed: warming up until about 2026-10-04 11:33 UTC |
| New stories, 1 Oct (part of the day) / 2 Oct (whole day) | 41 / 64 |
| Topics named in the last week / CVEs | 25 / 15 |
| Busiest topics, last 24 h | Microsoft 4, ransomware 3 |
| Size | 9.7 KB |

Days before collection began show as a flat line and "—", never as zero. A missing or unreadable
`trends.json` says so in the panel and the rest of the page still renders.

### Sources with no feed (measured on VM 200, 2026-10-03)

Four registered sources publish no feed. Each is now read another way, with the worker's own
fetcher and no browser:

| Source | Read from | Items | Newest |
|---|---|---|---|
| ASD (`asd`) | its news page, `asd_news` parser | 7 | 22 Jun 2026 |
| OAIC (`oaic`) | its media centre, `oaic_media` parser | 10 | 30 Sep 2026 |
| Anthropic (`anthropic_news`) | its newsroom, `anthropic_news` parser | 15 | 2 Oct 2026 |
| Microsoft security releases (`msrc_cvrf`, new) | `api.msrc.microsoft.com/cvrf/v3.0/updates`, newest 12 releases | 12 | 2 Oct 2026 |

- **One parser per site** (`worker/collectors/web_page.py`), named in `config/sources.yaml` as
  JSON APIs are. A page is parsed with the standard library, and its scripts never run. A site
  that changes its layout yields no items, so the source reads EMPTY in source health instead of
  publishing guesses.
- **A printed day** ("30 September 2026") is the start of that day where the publisher is:
  Sydney for ASD and OAIC, San Francisco for Anthropic. A later guess could date a page read the
  same morning in the future.
- **Links are https or nothing.** OAIC's cards go through a click-tracking redirect, and only a
  page on OAIC's own site is taken from it.
- **The MSRC blog stays off.** It is rendered by script, so the page holds no posts, and its
  feeds return 403. The security releases it announces now come from the API, and a release that
  is revised, or loses "Early" from its title, stays one event.

Not built, with the reason:

- **YouTube.** A channel's feed (`youtube.com/feeds/videos.xml?channel_id=…`) is plain Atom, so
  it needs a registry entry with `type: atom`, not an adapter. It returned 404 for every security
  channel tried (Black Hat, DEF CON, CyberCX, AusCERT), from both WSL and VM 200. The Data API
  needs a Google key, so no channel is registered.
- **`sitemap`.** A sitemap has no headlines, and its dates (`lastmod`) record when a page last
  changed, not when it was published. Every source without a feed has a listing page that gives
  both, so nothing needs a sitemap.
- **SecurityWeek** (PLAN.md open item 3) stays off. Its feed, and the FeedBurner address that
  redirects to it, answer with a Cloudflare challenge. Getting past one is not a lawful route, so
  the gap is accepted.

The four ACSC feeds had raised four STALE errors on every run: they were held to daily and
hourly rhythms they do not keep. Their thresholds now follow what was measured over the last
year (alerts: median 8 days apart, longest 41, now `monthly`; news: median 5, longest 21, now
`weekly`). ASD's own news page posts months apart (231 days in 2025), so it has a new
`quarterly` threshold of 270 days.

The new thresholds did not take at first. A feed that answers 304 Not Modified has no items to
judge, so its last verdict was carried forward, and a STALE verdict was carried as it was, error
text and all. The feeds kept reading STALE "within 2 days" under a 90-day threshold. A 304 now
judges the newest item's age, still counting, against the threshold the source has today.

### The AI sources were never read (found 2026-10-03)

The runs table had fast and normal runs and no deep run, ever. DEEP is a Paperclip routine with
no schedule in the worker, and the routines are paused, so the eight AI sources registered in
DEEP had never been collected. PLAN.md §5 puts AI sources in NORMAL, so they are there now:
Simon Willison, Embrace The Red, OpenAI, DeepMind, Microsoft AI security, OWASP GenAI, Hugging
Face and Anthropic. Each one's threshold follows its posting gaps over the last year, measured
on VM 200:

| Source | Median / longest gap | Threshold |
|---|---|---|
| Simon Willison | 0.2 / 1.3 days | `daily` |
| OpenAI | 0.3 / 11 days | `weekly` |
| Hugging Face | 0.8 / 14 days | `weekly` |
| Microsoft AI security | 1.0 / 7 days | `weekly` |
| DeepMind | 1.7 / 21 days | `weekly` |
| Anthropic | 2.0 / 9 days | `weekly` |
| Embrace The Red | 17 / 52 days | `monthly` |
| OWASP GenAI | 28 / 111 days | `quarterly` |

arXiv cs.CR stays in DEEP. It is long-form research, at 50 or more papers each weekday, and
PLAN.md §5 gives long-form research to DEEP. It is read once the DEEP routine is unpaused. A test now fails if an AI source sits in a lane the
worker does not schedule. The first normal run after the move takes in each feed's back catalogue
once. Old items land on their own day pages and are archived in the same run, as for every
feed added before.

### Embeddings: not needed (measured on VM 200, 2026-10-03)

PLAN.md open item 2 asked whether pgvector embeddings are needed at all. They are not, for now:

| | Events on the site (last 30 days) | In a possible-duplicate pair |
|---|---|---|
| Today, with consolidation in every run | 190 | 26 (13.7%) |

The 19 pairs the pass leaves apart are of two kinds:
- **11 are the two old generic CISA notices** (`evt-2026-000068`, `-000069`) merged with
  unrelated KEV entries before this stage. They age out.
- **8 are the same story in different words**, such as KillSec's takedown told three ways, or
  GitLab's AI Gateway flaw as "warns of" and "patches". That is 4% of the site.

Embeddings would add a model call to every event in a pipeline that uses none (collection and
matching are deterministic, and the site says so), and spend from a budget that is already in
conserve mode. That is not worth it to fold eight pairs. pgvector stays installed and unused.
Run `python -m worker --check-duplicates` again if the different-words pairs grow well past
this.

## Done when

- [x] The duplicate rate is measured and acceptable (29.1% → 13.5%; the rest is explained above)
- [x] Syndication no longer inflates confidence (2,265 → 2,255 confirmations; NetScaler 7 → 5)
- [x] Trends are computed from real data (independent reports since 2026-10-01; states once 48 h
  of baseline exist)
- [x] Every registered source with no feed is read, or off with a measured reason

---

[← Stage 2](stage-2-ground-truth.md) · [Wiki home](README.md) ·
**Next:** [Stage 4 — Paperclip + first agents →](stage-4-paperclip.md)
