# Stage 2 — Ground truth + enrichment

[← Stage 1 — Foundation](stage-1-foundation.md) · [Wiki home](README.md) ·
[Stage 3 — Correlation →](stage-3-correlation.md)

**Status: 🟡 most of it running.** Ground truth, the extra registers, the money controls, AI
enrichment and MITRE suggestions all run on the VM (since 2026-10-03). The AU relevance and
evidence engines (item 7) are left. Plan:
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
| OSV and GitHub advisories per CVE: affected packages and fixed versions | ✅ |
| MITRE ATT&CK and ATLAS catalogues, cached per release | ✅ |
| Event detail page (`site/event.html`) | ✅ |
| AU relevance from facts, with reasons (`config/scoring.yaml` → `au:`) | ✅ |
| Claims with evidence: KEV, CVSS, EPSS, fixes, corroboration | ✅ |

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

5. ✅ **More registers** (2026-10-03, migration 007; `worker/groundtruth/osv.py`,
   `worker/groundtruth/mitre.py`, `worker/ai/mitre.py`). Each ground-truth sync now also:
   - **Looks up to 200 CVEs up in OSV** (`api.osv.dev`), then each GitHub advisory (GHSA) that
     OSV's record names. That gives GitHub's reviewed advisories without a GitHub token. An
     advisory is kept only if it names the CVE back and hasn't been withdrawn. What is kept is
     what a reader can act on: the affected packages, the versions that fix them, and GitHub's
     own rating. That rating is shown beside the CVSS chain, never inside it. OSV's own record
     of a CVE is kept only when it names a package. A CVE is asked again after 3 days if found,
     2 days if OSV had nothing, and 6 hours after an error. Only a complete answer can remove an
     advisory recorded earlier. The site lists them under each CVE.
   - **Checks for a new MITRE release**: ATT&CK Enterprise from MITRE's STIX index, ATLAS from
     its manifest. A release is downloaded only when it is new (the ATT&CK file is 54 MB), and
     it is loaded whole or not at all. Revoked and deprecated techniques are left out. Older
     releases stay, because a published suggestion names the release it came from.

   Then, after each enrichment pass, **MITRE suggestions** (strong tier, as §7.1 asks) run for
   up to 5 enriched events about an attack, most prominent first. The model doesn't recall
   ATT&CK: it is shown at most 30 techniques from the loaded release and may only pick from
   those. The schema's list of allowed ids is that shortlist, so an id it invents fails the
   schema. The shortlist is each category's usual techniques (for example T1190 and T1203 for
   a vulnerability, T1486 and T1490 for ransomware), then techniques whose names share words
   with the story. ATLAS joins for stories about attacks on or with AI. The model keeps at most
   three, each at confidence 0.5 or more, with a one-sentence basis that must pass the same
   checks as a brief. "None" is a valid answer. They are published as `ai_suggested` with the
   confidence and release, and the site labels them AI SUGGESTED. The basis is kept as AI
   inference evidence, not published. When money is short the suggestions wait; they never
   drop to the free tier, and they never hold an event in `pending_enrichment`.
6. ✅ **AI enrichment** (2026-10-03, running on the VM; `worker/ai/tasks.py`,
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

   Models often write CVE ids with non-breaking hyphens. Those are published as plain hyphens,
   and the CVE check catches an id written with any dash.

   An entity name that doesn't appear in the record is dropped. Only an Australian source makes
   an event "reported by an Australian source"; a model can't claim it. A failed task backs off
   1 h, 6 h, then 24 h, then gives up. A later official score always beats the AI estimate.
   Only events the site would publish are enriched, most prominent first, in batches of 10 at
   :05 and :35 past each hour, inside the budget mode above. Free models never write editorial
   text: when only the free tier is open, briefs and judgments wait. Every call is a ledger row
   against its event. MITRE suggestions are a separate pass (item 5).

   The first live pass (2026-10-03 00:35 UTC, conserve mode) took 10 events: 9 finished and 1
   brief failed the copy check. It made 21 billed calls for US$0.00068 in all, about US$0.00007
   an event. Triage ran on a free model and briefs on `openai/gpt-oss-20b`.
7. ✅ **AU relevance and evidence engines** (2026-10-03, migration 008;
   `worker/pipeline/au.py`, `worker/publish/claims.py`).
   - **AU relevance** is now set from the record's facts at every rescore, by the rules under
     `au:` in `config/scoring.yaml`. Each fact sets a floor and gives its reason in fixed words:
     - an Australian government authority published it (ACSC, ASD, OAIC, DTA): 0.70;
     - it names an Australian organisation (Optus, Medibank, APRA and 58 more), which also adds
       that organisation's sectors: 0.75;
     - it names Australia, an Australian place or a .au web address: 0.55;
     - an Australian outlet reported it: 0.30.

     Matching reads what the sources said (the title, the feed's text, every source's
     headline), never the model's summary, so no model can make an event Australian by writing
     the word. It matches whole words, case-sensitive, so "NAB" doesn't match "unable". Names
     that often mean something else (Victoria, Darwin, Medicare, ASIC, ABC, ATO) are left out.
     Feed boilerplate and the outlet's own name don't count.

     The brief's AU reading is now kept apart, in migration 008's `au_model_*` columns, which
     are not published. It may raise the number but never lower it, and its reasons come after
     the facts'. SOCI sectors come from the sectors through a fixed map, and only at 0.5 or more.
     Because this runs at every rescore, an Australian report merged in later now marks the
     event "reported in AU". Before, only the first source counted.
   - **Claims with evidence.** Each published event gets claims a reader can check against the
     register they cite:
     - the CISA KEV listing and its dates;
     - the highest CVSS score and who set it;
     - the highest EPSS score;
     - the fixed versions of each package, from the best advisory, or that no fix is published;
     - how many independent sources report it.

     A claim's confidence is its register's evidence-class weight from `config/scoring.yaml`.
     Claims are worked out at publish time from what the event already carries, so they can't
     disagree with its CVE data, and nothing new is stored.

   Scoring version 2 makes the worker rescore every event once, which sets AU relevance on all
   of them.

## How to check it

```bash
docker compose logs --since 7h worker | grep -i 'ground-truth'     # one sync every 6 h
docker compose exec worker python -m worker --check-budget          # the mode the spend allows
docker compose exec worker python -m worker --enrich                # enrichment + MITRE pass now
docker compose logs --since 1h worker | grep 'enrichment:'           # counts per pass
docker compose logs --since 1h worker | grep 'MITRE suggestions'     # counts per MITRE pass
```

To stop all AI spending at once, set `AI_MONTHLY_BUDGET_USD=0` in `.env`, then run
`docker compose up -d worker`. A plain `restart` does not re-read `.env`. The mode goes to `off`,
and collection and publishing carry on.

## Done when

- [x] Events carry real KEV / CVSS / EPSS **and** AI enrichment (live on the site 2026-10-03)
- [x] The ledger shows per-event cost under budget (`cost_ledger.event_id`, about US$0.00007 an
  event)
- [x] Degradation demonstrably works when the tier is forced (`tests/unit/test_budget.py`)
- [x] The ceiling guard rejects an over-priced model in a test (`tests/unit/test_ladder.py`)
- [x] CVEs carry OSV / GitHub advisories, and technique suggestions come only from the cached
  ATT&CK / ATLAS release (`tests/unit/test_ai_mitre.py`)
- [x] AU relevance comes from the record's facts with reasons, and the model can only raise it
  (`tests/unit/test_au.py`, `tests/integration/test_au_db.py`)
- [x] Published claims cite the register they come from (`tests/unit/test_claims.py`)

---

[← Stage 1](stage-1-foundation.md) · [Wiki home](README.md) ·
**Next:** [Stage 3 — Correlation depth + trends →](stage-3-correlation.md)
