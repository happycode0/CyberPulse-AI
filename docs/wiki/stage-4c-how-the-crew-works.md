# 4c — How the crew works together

[← 4b — The crew](stage-4b-the-crew.md) · [Wiki home](README.md) ·
[Stage 5 — Full crew →](stage-5-full-crew.md)

The [crew page](stage-4b-the-crew.md) says what each agent is. This page shows how the work moves
between them — who starts it, who hands it to whom, and where it stops.

---

## The one rule that shapes everything

**Python does the work that never needs judgment. Agents only do the judgment.**

The worker container collects, de-duplicates, scores, fetches ground truth and publishes **on
its own schedule, with no AI and no Paperclip**. That is already running on the VM today. If
Paperclip is stopped, upgraded or broken, the site keeps updating every hour (PLAN.md
§2.3). Agents add judgment on top: Australian relevance, campaign links, follow-ups, quality
checks.

```text
               ┌──────────────── PAPERCLIP (judgment) ────────────────┐
               │  issues · assignments · routines · budgets · approvals │
               └───────▲───────────────────────────────────┬──────────┘
         run summaries │ digests, incidents        verdicts │ comments, tasks
                       │                                   ▼
┌──────────────────────┴──────── WORKER (no AI) ───────────────────────────┐
│ FAST every hour · NORMAL every 4 h · ground truth every 6 h · publish    │
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
| `[DIGEST]` | a routine | DECKARD |
| `[EDITORIAL]` | a routine | MORPHEUS |
| `[FOLLOW-UP]` | MORPHEUS | DECKARD |
| `[GAP]` | MORPHEUS | TACHIKOMA |
| `[QA]` | DECKARD, or the daily sample | VOIGHT |
| `[INCIDENT]` | worker health checks | TELETRAAN |
| `[ENGINEERING]` | TELETRAAN · MORPHEUS | WHEELJACK |
| `[MODEL]` | RIPPERDOC | MORPHEUS |

---

## The six loops

### 1. Collection loop — every hour, no AI *(running today)*

```text
worker FAST lane ──► correlate: one event or a new one? ──► ground truth: CVSS / KEV / EPSS
                                                                     │
                                       validate, secret-scan, publish ◄┘
```

Nobody in Paperclip has to wake for this. The worker posts a run summary so you can see it.
SERAPH is the one face of it all in Paperclip. Its wake answers 200 when the sources,
correlation, ground truth and publishing all check out, and 503 naming any that do not. It runs
nothing itself.

### 2. Desk loop — 06:00, 14:00 and 22:00 Sydney

```text
routine "[DIGEST] Desk" ──► DECKARD reads the last 8 hours: escalations first, then the rest
                             ├─ loads the event's beat skill: AU desk · global desk · AI desk
                             ├─ AU relevance + reasons, "Why this matters to Australia"
                             ├─ high / critical?            ──► VOIGHT ──► PASS ──► the worker publishes
                             │                                         └─ FIX / REJECT ──► back to DECKARD
                             └─ AU critical infrastructure?  ──► MORPHEUS (immediately)
```

One agent covers all three beats in one pass: Australia first, then global, then AI. Its own
prompt is short. Each beat's steps are a skill it loads only for an event on that beat, so a run
never carries all three. It may hand at most two events to subagents in a run, one beat each.

### 3. Editorial loop — every morning, 07:30 Sydney

```text
07:00  VOIGHT samples yesterday's published events          ──► verdicts
07:30  MORPHEUS reads the overnight digest + VOIGHT's verdicts
         ├─ developing story needs watching  ──► [FOLLOW-UP] ──► DECKARD (beside the worker's own queue)
         ├─ topic has gone quiet             ──► [GAP]       ──► TACHIKOMA
         └─ writes the daily intelligence report
```

### 4. Source loop — nightly, from 03:10 Sydney

```text
03:10 worker searches (Tavily) ──┐
03:30 TACHIKOMA proposes ────────┴─► candidate ──► SERAPH's gate in the worker, every 4 h (deterministic)
                                               ├─ 6 healthy probes in a row ──► ACTIVE (community evidence)
                                               └─ 3 failures in a row       ──► rejected, with the reason
```

The agent that *finds* a source can never *activate* it. That split is the point.

### 5. Money loop — always on

```text
every AI call ──► the worker's cost ledger (the real billed cost, from OpenRouter)
                    ├─ spend running ahead of plan ──► cheaper degradation tier, BEFORE spending more
                    └─ RIPPERDOC's monthly review, 09:00 on the 1st ──► MORPHEUS

RIPPERDOC daily scan + weekly test ──► [MODEL] proposal ──► MORPHEUS approves or declines
model failing an agent             ──► RIPPERDOC drops that agent down its fallback chain at once
```

A `[MODEL]` proposal carries the worker's own cost figures: every call went through the price
guard, and the cost per event is what OpenRouter billed. RIPPERDOC passes them on unchanged and
never approves its own proposal.

Three stops are stacked on top of each other, so a runaway agent cannot run up a bill:

1. The agent's own monthly budget. Paperclip pauses it at 100%.
2. The company budget, US$12.
3. The OpenRouter key's hard limit, US$20. Nothing gets past this.

### 6. Repair loop — Stage 6

```text
watchdog (every 5 min) sees a signature ──► incident INC-<n> ──► Telegram (high and critical)
   └─► Incident routine ──► [INCIDENT] ──► TELETRAAN reads GET /ops/incidents, finds the cause
          └─► [ENGINEERING] INC-<n> (structured summary only, no raw content)
                 └─► WHEELJACK: fix/inc-<n>-… ► fix ► regression test ► PR
                        └─► TELETRAAN (AUDIT model, not WHEELJACK's vendor): POST /ops/incidents/<n>/verdict, PASS / FAIL
                               └─► YOU approve and merge ──► the fault clears ──► resolved after 15 min
3 FAIL verdicts ──► circuit breaker: needs_human, verdicts refused (409), everything waits for you
```

The watchdog opens and resolves incidents itself; no agent can close one.
[Stage 6](stage-6-self-healing.md) has the signatures and the setup.

---

## Who may do what

The prompt asks nicely. These are the controls that actually enforce it:

| Control | Where it is set | What it stops |
|---|---|---|
| Board approval for new hires | Company settings | An agent hiring more agents |
| Monthly budget per agent | Each agent | One agent spending everyone's money |
| Wake on demand only, no heartbeat | Each agent | Agents waking (and paying) with nothing to do |
| Max daily runs | Each agent | A loop of agents waking each other |
| Ops API token scope | Worker (Stage 4) | An agent writing ground truth, or publishing, through the ops API. Not through the server's own database URL, which `opencode_local` agents inherit: [threat model, risk 1](../threat-model.md#open-risks-ranked) |
| WHEELJACK is given only a branch token | Its environment | Pushing to `main`. Not reading other secrets: it runs with the server's environment ([risk 1](../threat-model.md#open-risks-ranked)) |
| A human merges | GitHub branch protection | Any code reaching production unreviewed |
| The publisher fails closed | Worker | A secret, or unvalidated data, reaching the public site |

## What you (the board) do

- **Approvals inbox** — new hires, budget changes, and `[MODEL]` proposals MORPHEUS has approved.
  Approving one changes nothing by itself: the change is a reviewed edit to `config/models.yaml`.
- **The daily report** — MORPHEUS's 07:30 comment is the one thing to read each day.
- **`needs human`** — the circuit breaker or VOIGHT is asking you something.
- **Pause** — any agent, any time, from its page. Collection and publishing carry on without it.

---

[← 4b — The crew](stage-4b-the-crew.md) · [Wiki home](README.md) ·
**Next:** [Stage 5 — Full crew + notifications →](stage-5-full-crew.md)
