# Stage 2 — Ground truth + enrichment

[← Stage 1 — Foundation](stage-1-foundation.md) · [Wiki home](README.md) ·
[Stage 3 — Correlation →](stage-3-correlation.md)

**Status: 🟡 half.** The ground-truth half runs on VM 200. The AI half is built (money controls
and enrichment) but not deployed yet: it goes live on VM 200 with the next push. Plan:
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

1. ✅ **OpenRouter client** (2026-10-03, `worker/ai/client.py`). Every call asks for strict
   JSON output and checks the answer itself. It goes only to providers that support that, at
   US$1.00/M output or less, cheapest first, with the tier's fallback models behind it. Out of
   money (402), rate limited (429) and a billed but unusable answer each raise their own error,
   so the budget logic can tell them apart. Enrichment (item 6) is its caller.
2. ✅ **Cost ledger** (2026-10-03, migration 005). One row per billed call: the agent, the
   pipeline stage and the event, the model asked for and the model that answered (they differ
   when a fallback served), and the cost OpenRouter billed. An unusable answer still gets a row,
   because it was still paid for. A cost OpenRouter did not report is stored as unknown, never
   as $0. This is ROGUE's source of truth.
3. ✅ **Price-ceiling guard** (2026-10-03). The ladder is now in `config/models.yaml`, not
   `.env`. When the worker starts, it checks every model there against OpenRouter's live list of
   routes (the providers serving it). A model passes only if at least one route offers tools and
   structured outputs at US$1.00 or less per million output tokens. If any model fails, the AI
   layer stays off and collection and publishing carry on. Check by hand with
   `python -m worker --check-models`.
   Its first run caught two models the plan had picked. `minimax/minimax-m2.7` has no route with
   structured outputs. `xiaomi/mimo-v2.5` is listed at $0.28, but every route that can do the
   job costs $2.00 or more. Both are out of the ladder.
4. ✅ **ROGUE's degradation tiers** (2026-10-03, `worker/ai/budget.py`). Before spending, the
   worker asks OpenRouter how much the key has spent this month and picks a mode. With over half
   the budget (`AI_MONTHLY_BUDGET_USD`) left, every task gets its own tier. With 20–50% left,
   only critical and KEV-linked events get the strong tier. Under 20%, only critical, high,
   KEV-linked and developing events get the free tier. At zero, free models only. A key limit
   lower than the budget counts, so it degrades early. If no reading has come in for 30 minutes,
   it uses free models only rather than spending blind. Out-of-money errors change the mode at
   once: a busy-budget 402 pauses paid calls briefly, a key-limit 402 stops them until a reading
   shows money, and an out-of-credits 402 stops all AI until a person tops up and restarts. It
   never raises a limit. Check by hand with `python -m worker --check-budget` (it spends
   nothing).

These four are built alongside [Stage 4](stage-4-paperclip.md)'s agents, because they are what
makes it safe to switch the AI agents on.

**Then the rest of the ground truth and the enrichment:**

5. More registers: OSV, GitHub Advisories, MITRE ATT&CK and ATLAS.
6. ✅ **AI enrichment** (2026-10-03, built, not yet deployed; `worker/ai/tasks.py`,
   `worker/ai/enrich.py`, migration 006). Each pending event gets up to three separate calls:
   - **Triage** (free tier): categories, entities, tags.
   - **Brief** (cheap tier): an original summary, why it matters, and AU relevance with reasons.
   - **Severity judgment** (strong tier): only where no official score exists. It is published
     as `ai_estimate`, and only at confidence 0.6 or more.

   The model sees the feed text as data, never as instructions, and has no tools. Every answer
   must match a strict schema and then pass our own checks, or it is retried once and then
   recorded as failed. The checks reject:
   - a URL;
   - a CVE the record doesn't name;
   - anything shaped like a secret;
   - 12 or more words copied in a row from the source.

   An entity name that doesn't appear in the record is dropped. Only an Australian source makes
   an event "reported by an Australian source"; a model can't claim it. A failed task backs off
   1 h, 6 h, then 24 h, then gives up. A later official score always beats the AI estimate.
   Only events the site would publish are enriched, most prominent first, in batches of 10 at
   :05 and :35 past each hour, inside the budget mode above. Free models never write editorial
   text: when only the free tier is open, briefs and judgments wait. Every call is a ledger row
   against its event. The MITRE suggestion waits for ATT&CK and ATLAS (item 5).
7. AU relevance engine with its reasons, and the evidence engine (claims linked to sources).

## How to check it

```bash
docker compose logs --since 7h worker | grep -i 'ground-truth'     # one sync every 6 h
docker compose exec worker python -m worker --check-budget          # the mode the spend allows
docker compose exec worker python -m worker --enrich                # one enrichment pass now
docker compose logs --since 1h worker | grep 'enrichment:'           # counts per pass
```

To stop all AI spending at once, set `AI_MONTHLY_BUDGET_USD=0` in `.env`, then run
`docker compose up -d worker`. A plain `restart` does not re-read `.env`. The mode goes to `off`,
and collection and publishing carry on.

## Done when

- [ ] Events carry real KEV / CVSS / EPSS **and** AI enrichment
- [ ] The ledger shows per-event cost under budget
- [x] Degradation demonstrably works when the tier is forced (`tests/unit/test_budget.py`)
- [x] The ceiling guard rejects an over-priced model in a test (`tests/unit/test_ladder.py`)

---

[← Stage 1](stage-1-foundation.md) · [Wiki home](README.md) ·
**Next:** [Stage 3 — Correlation depth + trends →](stage-3-correlation.md)
