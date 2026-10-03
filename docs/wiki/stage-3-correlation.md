# Stage 3 — Correlation depth + trends

[← Stage 2 — Ground truth](stage-2-ground-truth.md) · [Wiki home](README.md) ·
[Stage 4 — Paperclip →](stage-4-paperclip.md)

**Status: ▶ in progress.** Consolidation (one story = one event) and source lineage are built.
Archiving, material changes, trends and the new adapters come next. Nothing in it is yours to do.
Plan:
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

### The duplicate rate (measured on the VM, 2026-10-03)

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

### Independent confirmation (measured on the VM, 2026-10-03)

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

## Still to build (🟢 all Claude)

1. **Material-change detection** (14 types), so an event only moves up again when something real
   changed.
2. **Prominence decay**, gated on material updates, then **archiving**.
3. **Trend engine** with real velocity metrics, and the trends and emerging-threats pages.
4. **Adapters for sources with no feed:** YouTube, `web_page`, `sitemap`.
5. **pgvector embeddings, only if measurement shows they are needed.** The database image already
   has pgvector; it stays unused unless deterministic matching measurably falls short.

## Done when

- [x] The duplicate rate is measured and acceptable (29.1% → 13.5%; the rest is explained above)
- [x] Syndication no longer inflates confidence (2,265 → 2,255 confirmations; NetScaler 7 → 5)
- [ ] Trends are computed from real data

---

[← Stage 2](stage-2-ground-truth.md) · [Wiki home](README.md) ·
**Next:** [Stage 4 — Paperclip + first agents →](stage-4-paperclip.md)
