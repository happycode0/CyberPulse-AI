# Stage 2 — Ground truth + enrichment

[← Stage 1 — Foundation](stage-1-foundation.md) · [Wiki home](README.md) ·
[Stage 3 — Correlation →](stage-3-correlation.md)

**Status: 🟡 half.** The ground-truth half runs on VM 200; the AI half is not built. Plan:
[PLAN.md §9, Stage 2](../../PLAN.md#9-stages) and §2.5 (the severity chain) · §7 (models and money)

---

## What this stage gives you

Every vulnerability carries an **official** severity, KEV status and exploit probability, never
a guessed one. Then AI adds judgment on top (summary, AU relevance, a suggested MITRE technique),
labelled as AI assessment, within a hard budget.

## What is done

| Piece | State |
|---|---|
| Ground-truth sync, every 6 h at :25 UTC (`groundtruth-sync` in the worker log) | ✅ running |
| CISA KEV (Known Exploited Vulnerabilities) | ✅ |
| FIRST EPSS (exploit prediction score) | ✅ |
| CVSS chain: CNA record → CISA Vulnrichment ADP → NVD, highest CVSS version first | ✅ |
| Event detail page (`site/event.html`) | ✅ |

The chain starts at the CNA, not NVD, on purpose. In a sample of 300 recent CVEs, 223 were
"Deferred" at NVD with no score, but 297 had a score from the CNA or CISA's ADP container
(`worker/groundtruth/cvss.py`).

## What's left (🟢 all Claude)

**Money first, before any agent spends:**

1. **OpenRouter client** with strict output schemas.
2. **Cost ledger** from each response's billed `usage.cost`. This is ROGUE's source of truth.
3. **Price-ceiling guard:** the worker refuses to start if any configured model costs more than
   US$1 per million output tokens. `.env.example` describes this guard, but **it is not enforced
   in code yet**.
4. **ROGUE's degradation tiers:** cheaper models automatically when spend runs ahead of plan.

These four are built alongside [Stage 4](stage-4-paperclip.md)'s agents, because they are what
makes it safe to switch the AI agents on.

**Then the rest of the ground truth and the enrichment:**

5. More registers: OSV, GitHub Advisories, MITRE ATT&CK and ATLAS.
6. AI enrichment: classification, entities, summary, severity judgment, MITRE suggestion.
7. AU relevance engine with its reasons, and the evidence engine (claims linked to sources).

## How to check it

```bash
docker compose logs --since 7h worker | grep -i 'ground-truth'     # one sync every 6 h
```

## Done when

- [ ] Events carry real KEV / CVSS / EPSS **and** AI enrichment
- [ ] The ledger shows per-event cost under budget
- [ ] Degradation demonstrably works when the tier is forced
- [ ] The ceiling guard rejects an over-priced model in a test

---

[← Stage 1](stage-1-foundation.md) · [Wiki home](README.md) ·
**Next:** [Stage 3 — Correlation depth + trends →](stage-3-correlation.md)
