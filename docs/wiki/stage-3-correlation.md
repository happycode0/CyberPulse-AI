# Stage 3 — Correlation depth + trends

[← Stage 2 — Ground truth](stage-2-ground-truth.md) · [Wiki home](README.md) ·
[Stage 4 — Paperclip →](stage-4-paperclip.md)

**Status: ⬜ not started.** Nothing in it is yours to do. Plan:
[PLAN.md §9, Stage 3](../../PLAN.md#9-stages)

---

## What this stage gives you

Sharper events. One story reported by twenty outlets counts as one event, and copies of the same
wire story stop looking like independent confirmation. Trends come from measured velocity, not
impressions.

## What gets built (🟢 all Claude)

1. **Material-change detection** (14 types), so an event only moves up again when something real
   changed.
2. **Source lineage and independent confirmation:** syndicated copies no longer inflate
   confidence.
3. **Prominence decay**, gated on material updates, then **archiving**.
4. **Trend engine** with real velocity metrics, and the trends and emerging-threats pages.
5. **Adapters for sources with no feed:** YouTube, `web_page`, `sitemap`.
6. **pgvector embeddings, only if measurement shows they are needed.** The database image already
   has pgvector; it stays unused unless deterministic matching measurably falls short.

## Done when

- [ ] The duplicate rate is measured and acceptable
- [ ] Syndication no longer inflates confidence
- [ ] Trends are computed from real data

---

[← Stage 2](stage-2-ground-truth.md) · [Wiki home](README.md) ·
**Next:** [Stage 4 — Paperclip + first agents →](stage-4-paperclip.md)
