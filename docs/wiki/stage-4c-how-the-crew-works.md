# 4c — How the crew works together

[← 4b — The crew](stage-4b-the-crew.md) · [Wiki home](README.md) ·
[Stage 5 — Full crew →](stage-5-full-crew.md)

The [crew page](stage-4b-the-crew.md) says what each agent is. This page shows how the work moves
between them — who starts it, who hands it to whom, and where it stops.

---

## The one rule that shapes everything

**Python does the work that never needs judgment. Agents only do the judgment.**

The worker container collects, de-duplicates, scores, fetches ground truth and publishes **on
its own schedule, with no AI and no Paperclip**. That is already running on VM 200 today. If
Paperclip is stopped, upgraded or broken, the site keeps updating every 15 minutes (PLAN.md
§2.3). Agents add judgment on top: Australian relevance, campaign links, follow-ups, quality
checks.

```text
               ┌──────────────── PAPERCLIP (judgment) ────────────────┐
               │  issues · assignments · routines · budgets · approvals │
               └───────▲───────────────────────────────────┬──────────┘
         run summaries │ digests, incidents        verdicts │ comments, tasks
                       │                                   ▼
┌──────────────────────┴──────── WORKER (no AI) ───────────────────────────┐
│ FAST every 15 min · NORMAL every 4 h · ground truth every 6 h · publish  │
└──────────────────────┬───────────────────────────────────────────────────┘
                       ▼
          Postgres ──► data/*.json ──► GitHub Pages (the public site)
```

## How agents talk to each other

There is no chat between agents. Everything is a **Paperclip issue**:

- **A routine** (a scheduled job) creates an issue and assigns it — that is what wakes an agent.
- **Handing off** means @-mentioning the next agent in a comment, or creating a new issue
  assigned to them.
- **Every run ends with a comment** saying what was done and who owns the next step (house rule 9).
- **The data** comes from the worker's ops API, never from the database directly.

So the whole history of every decision is in the issue log. You read it the same way the agents do.

**Issue title prefixes**, so you can filter the board:

| Prefix | Created by | Assigned to |
|---|---|---|
| `[DIGEST]` | a routine | ZION · BLASTER · WINTERMUTE |
| `[EDITORIAL]` | a routine | MORPHEUS |
| `[FOLLOW-UP]` | MORPHEUS | DECKARD |
| `[GAP]` | MORPHEUS | TACHIKOMA |
| `[QA]` | a desk, or the daily sample | VOIGHT |
| `[INCIDENT]` | worker health checks | TELETRAAN |
| `[ENGINEERING]` | TELETRAAN · SERAPH · MORPHEUS | WHEELJACK |
| `[MODEL]` | RIPPERDOC | ROGUE + MORPHEUS |

---

## The six loops

### 1. Collection loop — every 15 minutes, no AI *(running today)*

```text
worker FAST lane ──► PROWL: one event or a new one? ──► LIBRARIAN: CVSS / KEV / EPSS
                                                               │
                                       LINK: validate, secret-scan, publish ◄┘
```

Nobody in Paperclip has to wake for this. The worker posts a run summary so you can see it.

### 2. Desk loop — every 4 hours

```text
routine "[DIGEST] AU desk" ──► ZION
                                ├─ AU relevance + reasons, "Why this matters to Australia"
                                ├─ global story, no AU angle?  ──► BLASTER
                                ├─ high / critical?            ──► VOIGHT ──► PASS ──► LINK publishes
                                │                                         └─ FIX / REJECT ──► back to ZION
                                └─ AU critical infrastructure?  ──► MORPHEUS (immediately)
```

BLASTER (global) and WINTERMUTE (AI) run the same loop for their own beat, 10 and 20 minutes
after ZION, so three desks never wake on the same minute.

### 3. Editorial loop — every morning, 07:30 Sydney

```text
07:00  VOIGHT samples yesterday's published events          ──► verdicts
07:30  MORPHEUS reads the overnight digest + VOIGHT's verdicts
         ├─ developing story needs watching  ──► [FOLLOW-UP] ──► DECKARD (beside the worker's own queue)
         ├─ topic has gone quiet             ──► [GAP]       ──► TACHIKOMA
         ├─ two desks claim one story        ──► decides the owner
         └─ writes the daily intelligence report
```

### 4. Source loop — nightly, 03:00 Sydney

```text
worker searches (Tavily) ──┐
TACHIKOMA proposes ────────┴─► candidate ──► SERAPH's gate in the worker, every 4 h (deterministic)
                                               ├─ 6 healthy probes in a row ──► ACTIVE (community evidence)
                                               └─ 3 failures in a row       ──► rejected, with the reason
```

The agent that *finds* a source can never *activate* it. That split is the point.

### 5. Money loop — always on

```text
every AI call ──► ROGUE ledger (the real billed cost, from OpenRouter)
                    ├─ spend running ahead of plan ──► cheaper degradation tier, BEFORE spending more
                    └─ monthly review ──► MORPHEUS

RIPPERDOC daily scan + weekly test ──► [MODEL] proposal ──► ROGUE (cost) + MORPHEUS (quality)
model failing an agent             ──► RIPPERDOC drops that agent down its fallback chain at once
```

Three stops are stacked on top of each other, so a runaway agent cannot run up a bill:

1. The agent's own monthly budget. Paperclip pauses it at 100%.
2. The company budget, US$12.
3. The OpenRouter key's hard limit, US$20. Nothing gets past this.

### 6. Repair loop — Stage 6

```text
worker health check fails ──► [INCIDENT] ──► TELETRAAN diagnoses the root cause
                                                └─► [ENGINEERING] (structured summary only, no raw content)
                                                       └─► WHEELJACK: branch ► fix ► regression test ► PR
                                                              └─► TRON (different model vendor): PASS / FAIL
                                                                     └─► YOU approve and merge
same fix fails 3 times ──► circuit breaker: everything stops and waits for you
```

---

## Who may do what

The prompt asks nicely. These are the controls that actually enforce it:

| Control | Where it is set | What it stops |
|---|---|---|
| Board approval for new hires | Company settings | An agent hiring more agents |
| Monthly budget per agent | Each agent | One agent spending everyone's money |
| Wake on demand only, no heartbeat | Each agent | Agents waking (and paying) with nothing to do |
| Max daily runs | Each agent | A loop of agents waking each other |
| Ops API token scope | Worker (Stage 4) | A desk writing ground truth, or publishing |
| WHEELJACK has only a branch token | Its environment | Pushing to `main`, or reading any other secret |
| A human merges | GitHub branch protection | Any code reaching production unreviewed |
| LINK fails closed | Worker | A secret, or unvalidated data, reaching the public site |

## What you (the board) do

- **Approvals inbox** — new hires, budget changes, and `[MODEL]` proposals that change quality.
- **The daily report** — MORPHEUS's 07:30 comment is the one thing to read each day.
- **`needs human`** — the circuit breaker or VOIGHT is asking you something.
- **Pause** — any agent, any time, from its page. Collection and publishing carry on without it.

---

[← 4b — The crew](stage-4b-the-crew.md) · [Wiki home](README.md) ·
**Next:** [Stage 5 — Full crew + notifications →](stage-5-full-crew.md)
