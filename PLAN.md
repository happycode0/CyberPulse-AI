# CyberPulse-AI — Master Plan

**An autonomous Australia-first Cybersecurity + AI Intelligence Organisation.**

| | |
|---|---|
| **Status** | Design validated, Stage 1 ready to build |
| **Last validated** | 2026-09-29 (every external dependency checked against live docs) |
| **Consolidates** | Three original design prompts (infrastructure, master build, extended build) — retired after consolidation into this document |
| **Public product** | Static cyberpunk intelligence site on GitHub Pages |
| **Private platform** | Docker Compose on one Debian 13 QEMU VM under Proxmox VE 9 |
| **Hard AI budget** | US$20/month (OpenRouter key limit = hard stop) |

---

## 0. The one-paragraph version

CyberPulse-AI is not an RSS reader. It is a small, permanently-running intelligence
organisation with named agents who each own a beat. Deterministic Python collects from
~60 validated sources on three cadences, resolves many reports into **one canonical
event**, and attaches ground truth from CISA KEV, CVE.org, EPSS, OSV and MITRE ATT&CK.
AI is an *enrichment layer* — it judges severity, extracts entities, reasons about
Australian relevance and reconciles conflicting evidence, always from retrieved facts,
never from memory. Paperclip is the control plane where the agents live, get woken,
delegate, and spend budget. When a source breaks, the watchdog opens an incident, an
engineer agent fixes the parser on a branch, an independent auditor verifies it, and a
human approves the merge. The public face is a deliberately striking cyberpunk HUD that
publishes only canonical, evidence-linked intelligence — and is honest about the fact
that it is a snapshot, not a live feed.

---

## 1. Decisions taken (and what changed from the source prompts)

Eight decisions were confirmed with the project owner on 2026-09-29.

| # | Decision | Rationale |
|---|---|---|
| 1 | **Debian 13 QEMU VM**, not a Debian 12 LXC | See §2.1 — Proxmox's own docs, Debian's EOL, and Codex sandboxing all point to a VM |
| 2 | **OpenCode CLI via OpenRouter** as the engineer runtime | Owner requires OpenRouter as the single provider; see §2.2 |
| 3 | **Hybrid scheduling** — worker owns FAST/NORMAL, Paperclip owns DEEP + judgment | See §2.3 — protects the failure model and the token budget |
| 4 | **`CyberPulse-AI` becomes public**; data on a force-pushed orphan branch | GitHub Free requires a public repo for Pages; orphan branch prevents history bloat |
| 5 | **NetBird VPN** (owner's existing mesh) for private dashboard access | Nothing exposed to the internet; Paperclip listens only on the VM's LAN address, login required |
| 6 | **Telegram** as the first notification channel | Other channels added in Stage 7 behind the same interface |
| 7 | **US$20/month** hard AI cap | Drives the model tiering and rationing in §2.4 |
| 8 | Write docs, then **build Stage 1** | |

---

## 2. End-to-end design validation

This section is the deliverable for *"validate the entire design end to end"*. Every
finding below was checked against live documentation or a live HTTP request on
2026-09-29. Findings are ordered by how badly they would have hurt.

### 2.1 Infrastructure: the LXC plan would have been a trap

`prompt1` specified a Debian 12 LXC. Three independent problems:

1. **Proxmox recommends against it.** The PVE 9.2 container docs state: *"for use cases
   demanding maximum isolation and the ability to live-migrate, nesting containers inside
   a Proxmox QEMU VM remains a recommended practice."* The FAQ adds that running
   application containers directly as Proxmox containers *"is currently a tech preview"*.
   The PVE 9.0 release notes list *"a known issue with nested containerization (e.g.,
   Docker inside an LXC container)"* as bug #6538.
2. **Docker-in-LXC needs `nesting=1`**, which the docs say *"will expose procfs and sysfs
   contents of the host to the guest"*, plus `keyctl=1`. On a box that runs an autonomous
   code-execution agent, that is the wrong trade.
3. **Debian 12 is past regular security support** (ended 2026-07-11; LTS only until
   2028-06-30). Debian 13 "trixie" is current stable (13.7), and is what PVE 9.2 is built on.

**Resolution:** a Debian 13 QEMU VM, 4 vCPU / 12 GB / 60 GB, QEMU guest agent installed
so `vzdump` snapshot backups get `fsfreeze` consistency. Bind mounts were also ruled out:
*"The contents of bind mount points are not backed up when using vzdump."*

*Amended 2026-10-01, against the host this was commissioned on* — 4-core/8-thread i7-7700HQ,
31 GiB RAM, a single 94 GB disk with 88 GB free. The original 6 vCPU / 150 GB was sized
before the hardware was known and does not fit it:

- **150 GB does not exist on a 94 GB disk.** `local-lvm` is thin-provisioned, so the
  request would have been *accepted* and then filled until the pool wedged, taking Postgres
  down with it. 60 GB is ample because the database holds no raw article bodies (§3) and
  the raw cache is a rotating filesystem cache, and it leaves ~28 GB of host headroom.
- **6 of 8 threads starves the hypervisor** for no gain: collection is IO-bound HTTP plus
  Postgres, not CPU-bound. 4 vCPU.
- **One disk means the host cannot back itself up.** `vzdump` to local storage dies with
  the disk it sits on, so Stage 7's backup target has to be external (NAS, USB, or PBS on
  another machine). Recorded here rather than discovered at Stage 7.

### 2.2 The engineer agent: two live security defaults to fix

- Paperclip's `codex_local` adapter defaults `DEFAULT_CODEX_LOCAL_BYPASS_APPROVALS_AND_SANDBOX
  = true`, i.e. it passes `--dangerously-bypass-approvals-and-sandbox` unless you set a
  sandbox explicitly. Local adapters run **inside the Paperclip container** as the same
  `node` user with `HOME=/paperclip` — the directory that holds the secrets
  `master.key`. An unsandboxed coding agent there can read every company secret.
- Paperclip records Codex spend as `costCents: 0, status: "unpriced"`, so **cents budgets
  do not constrain it at all**.

**Resolution:** use `opencode_local`, which supports `provider/model` selection, so the
engineer bills to the same OpenRouter key as everything else and is covered by the same
hard limit. Additionally: explicit sandbox/filesystem scope, `heartbeat.maxDailyRuns` as a
second cap, no OpenRouter/Tavily/Postgres credentials given to the engineer, and a
GitHub token scoped to branch+PR only. As built, a local adapter still runs with the server
container's own environment, its database URL included: see
[the threat model, risk 1](docs/threat-model.md#open-risks-ranked). Codex remains a documented alternative in
`.env.example` for anyone with a ChatGPT plan who wants `@codex review`.

### 2.3 Scheduling: the original design had a single point of failure

`prompt2` §14 said *"Paperclip owns the scheduling"* for all three lanes. Three problems:

1. **It breaks the project's own failure rule** (§51: *"One component must not kill the
   whole platform"*). If Paperclip is down or mid-upgrade, collection stops.
2. **Issue-log flooding.** Every routine firing creates a tracked issue. A 15-minute FAST
   lane, as first planned, = ~96 issues/day = ~35,000/year of pure noise. Hourly is still
   ~8,800/year.
3. **Token cost.** Paperclip's docs are blunt: *"Each wakeup costs tokens."* Timer wakes
   fire even with no work to do.

**Resolution — hybrid.** The worker container runs its own APScheduler for the
deterministic FAST (hourly) and NORMAL (4 h) lanes and posts a run summary to Paperclip
for visibility. Paperclip routines (cron, `Australia/Sydney`) own the DEEP lane and all
agent judgment work. LLM agents have `heartbeat.enabled = false` and wake on assignment,
@-mention or routine only. Deterministic agents use the `http`/`process` adapters, which
cost no tokens.

*Decided 2026-10-04: the FAST lane runs every hour, at :00 UTC, not every 15 minutes.* The
owner approved it, for stability. Four runs an hour left only short gaps to deploy or
restart the worker in, and a restart across a run's minute loses that run, because the job
store is in memory. An hourly run leaves :10 to :45 free. It also means a quarter of the
fetches, publishes and pushes, so less load on the feeds, the VM and the data branch. The
cost is latency: an item can take up to an hour to reach the site, and a critical alert
for Australia up to an hour and 3 minutes. The NORMAL lane stays every 4 hours. Enrichment
stays twice an hour (:05 and :35), so it keeps pace with what arrives.

The interval is one constant, `FAST_INTERVAL_MINUTES` in `worker/cadence.py`. The FAST
cron, the alert minutes and the watchdog's FAST thresholds all follow it, so going back to
30 minutes is a one-line change.

### 2.4 The budget will not stretch as far as the prompts assume

$20/month against a 24/7 pipeline plus autonomous engineering. Modelled with live
OpenRouter prices and the four-tier ladder in §7.1:

| Workload | Assumption | Est. monthly |
|---|---|---|
| Tier 0 (free): classify, extract, tag, source relevance | ~124 calls/day, 12% of the 1,000/day free cap | **$0.00** |
| Tier 1 (cheap): summaries, AU reasoning, desk digests | 7.2M in / 1.44M out @ $0.018/$0.09 | ~$0.26 |
| Tier 2 (strong): severity, impact, evidence, MITRE, editorial | 300 judgments @ $0.435/$0.87 | ~$1.17 |
| CODE: WHEELJACK | ~4 sessions @ $0.14/$0.28 | ~$0.34 |
| AUDIT: TELETRAAN | ~4 diff reviews, input-heavy @ $0.018/$0.32 | ~$0.05 |
| RIPPERDOC gauntlet | weekly, ~30 golden events per candidate | ~$0.25 |
| **Total** | | **~$2.07 (10% of cap)** |

Three things drive that number down: the free tier absorbs the highest-volume work (§7.1),
the US$1 output ceiling removes frontier models from the judgment and code tiers, and
choosing CODE and AUDIT on measured session cost rather than token price cut those two
tiers by more than half. The ceiling is still a real quality trade, documented honestly in
§7.1 — the saving is paid for in severity-reasoning nuance on the ~300 highest-stakes
judgments per month.

The remaining headroom is deliberate: it funds growth in source count and event volume
without a budget change, and RIPPERDOC's job (§7.6) is to keep pushing volume down the
ladder toward free.

The rationing that makes it hold:

- Strong tier is **gated**, never the default: critical, high, KEV-linked, or
  developing-with-material-change only.
- Agents read **pre-computed digests** from the worker, not raw event bodies.
- Engineering runs are incident-driven, never on a timer, with `maxDailyRuns` caps.
- Degradation tiers (§7.4) are mandatory, not optional.
- **Embeddings are deferred to Stage 3.** `pg_trgm` trigram similarity plus entity overlap
  handles Stage 1–2 matching at zero marginal cost. `pgvector` ships in the image so the
  extension exists, but stays unused until measurement proves it necessary. (YAGNI.)

### 2.5 Ground truth: NVD can no longer be the primary source

`prompt2` §21 and `prompt3` §8 lead with NVD. Live sampling of 300 CVEs published
2026-09-20 to 09-22 found **223 "Deferred", 10 "Analyzed", and zero with an NVD-authored
CVSS score** — but 297 had CVSS from the CNA or from CISA's ADP enrichment.

**Resolution:** the severity chain becomes **CNA record (CVE.org / cvelistV5) → CISA
Vulnrichment ADP → NVD (optional)**. Missing CVSS is recorded as `unknown`, never as low.
EPSS is also frequently absent for brand-new CVEs (confirmed: the newest KEV entry had no
EPSS score) — also `unknown`.

### 2.6 Source validation: 56 of 60 sources work; 4 need special handling

Every baseline feed in the source prompts was fetched live. Full results in
`config/sources.yaml` at build time; the exceptions:

| Source | Problem | Resolution |
|---|---|---|
| **SecurityWeek** | Cloudflare 403 on every User-Agent tried | Ship `enabled: false` with a documented reason; rely on Tavily/downstream reporting |
| **Google Security Blog** | Site moved to `blog.google/security/`; old feed still returns HTTP 200 with valid XML but nothing newer than 2026-04-23 | Use the new feed **and** build stale-feed detection (see below) |
| **ASD, MSRC blog, Anthropic news** | No feed exists | `web_page`/`sitemap` adapters; MSRC uses the CVRF API instead |
| **OAIC** | Feed returns 232 items with titles only — no links, no dates | Scrape the media-centre listing |

The Google case produced a design requirement the prompts missed: **a feed can go stale
without ever erroring.** Source health must therefore alert on *age of newest item*, not
just HTTP status. Other quirks found: CISA uses 2-digit years, CrowdStrike uses a
non-standard date format, SecurityBrief publishes in NZ time, arXiv stamps a whole batch
with one timestamp, and ACSC publications are not in date order. Date parsing must be
lenient and normalise to UTC.

### 2.7 Strands has moved and its API changed

- `github.com/strands-agents/sdk-python` now **301-redirects** to `harness-sdk`.
- `agent.structured_output()` is **deprecated** → use the `structured_output_model=` parameter.
- The community `strands_tools` Tavily/Exa/`http_request`/`rss` tools are **deprecated**,
  and the repo *"will eventually be archived"* → call Tavily's REST API directly.
- **Graph joins are OR by default** — a node fires when *any* upstream edge completes. The
  evidence-reconciliation node must use `all_dependencies_complete([...])` or it will run
  on partial evidence. This is a correctness bug waiting to happen.
- `max_node_executions`, `execution_timeout`, `node_timeout` all default to `None` — must
  be set on every cyclic graph.
- Agents default to **Bedrock**; the model must be passed explicitly. `callback_handler=None`
  in workers or it prints to stdout.
- The SDK reports **tokens but never cost** → the worker computes cost from OpenRouter's
  `usage.cost` field and keeps its own ledger.

### 2.8 Smaller corrections

| Area | Finding | Resolution |
|---|---|---|
| OpenRouter usage | `usage:{include:true}` is **deprecated and has no effect**; usage now returns automatically | Read `usage.cost` directly |
| OpenRouter credits | `GET /api/v1/credits` needs a **management key**; a normal key gets 403 | Use `GET /api/v1/key` (`limit_remaining`, `usage_daily`) — no management key needed, so the platform never holds admin-level key authority |
| OpenRouter 402 | `error.metadata.limit_source` distinguishes `openrouter_key_limit` / `openrouter_credits` / `openrouter_in_flight_budget` | Degradation state machine branches on it; `in_flight_budget` is transient → honour `Retry-After` |
| Tavily | The `days` parameter in the prompts **is not in the current API** | Use `time_range` / `start_date` |
| Tavily | Free tier = 1,000 credits/month; basic search = 1 credit; `auto_parameters` can silently upgrade to advanced (2 credits) | Pin `search_depth: basic`, `auto_parameters: false`, cap ~30 searches/day, always send `include_usage: true` |
| PostgreSQL | Paperclip's compose targets `postgres:17`; PG18's Docker image moved PGDATA to `/var/lib/postgresql/18/docker` | Use **`pgvector/pgvector:0.8.6-pg17-trixie`** — one instance, two databases, no PGDATA surprise, supported to Nov 2029. (Note: pgvector's bare `pg17` tag is *bookworm*, not trixie) |
| GitHub Pages | Soft limit of 10 builds/hour *"does not apply if you build and publish your site with a custom GitHub Actions workflow"* | Custom Actions workflow — also removes any need for `.nojekyll` or a `CNAME` file |
| GitHub Pages | *"GitHub Pages sites are publicly available on the internet, even if the repository for the site is private"*; prohibited for commercial SaaS | Public-by-design; secret-scan gate before every publish |
| Paperclip | `requireBoardApprovalForNewAgents` defaults to **false**, and new agents can hire other agents | Turn approval on during bootstrap |
| Paperclip | Telemetry on by default; announcements phone home; a `.env` in CWD is auto-loaded | `PAPERCLIP_TELEMETRY_DISABLED=1`, `PAPERCLIP_ANNOUNCEMENTS_ENABLED=false` |
| Paperclip | `PAPERCLIP_TOOL_ACTION_SIGNING_SECRET` has **no fallback** and is missing from the upstream compose; upstream also publishes Postgres on host :5432 | Set it; bind Postgres to the Docker network only |
| Paperclip | A `build:` with no `target` yields the **`cloud`** stage | Pin the published image **by digest** (49 migrations shipped in one release; migrations run at startup) |
| Paperclip | Secrets `master.key` + DB are **both** required to recover | Back up together — neither is sufficient alone |
| Codex CLI | `--full-auto` deprecated; `approval_policy: untrusted` **removed** and can stop Codex starting | Documented in the alternative path only |
| Hermes | `prompt3` §57 already demotes it to optional | **Dropped entirely.** No orphan integration |

### 2.9 Confirmed sound

For balance — these parts of the original design survived validation unchanged: the
event-centric (not article-centric) data model; the deterministic-vs-AI split; three
collection cadences; source lifecycle and independence/lineage tracking; evidence-linked
claims with an `AI_INFERENCE` class; separating severity from prominence with
material-change-gated decay; Paperclip as control plane with Strands as a *specialist*
workflow engine (not the scheduler); PostgreSQL for structured state with the filesystem
for raw/temporary data; no Kafka/Redis/ClickHouse/Kubernetes/vector-DB in v1; a static
public site with honest "last completed collection" labelling; and 6 vCPU / 12 GB / 150 GB
sizing.

### 2.10 Two new security requirements

Neither appeared in the source prompts.

1. **Ingested content is untrusted input — treat it as prompt injection.** The pipeline
   fetches attacker-adjacent text (advisories, blogs, exploit write-ups, social posts) and
   feeds it to LLMs. Therefore: enrichment calls are given content as *data* with strict
   `json_schema` output and **no tools available**; raw article text is never passed to the
   engineer agent, which receives only structured, sanitised task descriptions; and
   `strict: true` plus `provider.require_parameters: true` is paired with our own
   server-side validation, because OpenRouter notes strict mode is not guaranteed on every
   provider endpoint.
2. **Codex/OpenCode credential blast radius.** Whatever the engineer agent can read, a
   malicious repo or poisoned issue can exfiltrate. The engineer's environment therefore
   contains exactly one credential: a branch/PR-scoped GitHub token.

---

## 3. Architecture

```text
                          OWNER  (NetBird VPN only)
                               │
                     ┌─────────┴──────────┐
                     │     PAPERCLIP      │  control plane: agents, goals,
                     │ 192.168.128.39:3100│  issues, delegation, heartbeats,
                     └─────────┬──────────┘  budgets, approvals, audit
                               │ agent API key / http+process adapters
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
  INTELLIGENCE            ENGINEERING             OPERATIONS
  MORPHEUS (CEO)          WHEELJACK (Coder)       TELETRAAN (Operation)
  DECKARD (Researcher)                            RIPPERDOC (Cheap)
  VOIGHT (Publisher)                              SERAPH (Collector)
  TACHIKOMA (Finder)
        └──────────────────────┼──────────────────────┘
                               ▼
                    ┌──────────────────────┐
                    │   CYBER WORKER       │  own APScheduler (FAST/NORMAL)
                    │   Python 3.13        │  + ops API for agents
                    │   ├── collectors     │
                    │   ├── normalise      │
                    │   ├── event resolver │
                    │   ├── ground truth   │
                    │   ├── enrichment     │──► OpenRouter (fast / strong)
                    │   ├── Strands graphs │──► Tavily (discovery/research)
                    │   ├── scoring/trends │
                    │   └── publisher      │
                    └──────────┬───────────┘
                               ▼
              ┌────────────────────────────────┐
              │  PostgreSQL 17 + pgvector      │
              │  ├── paperclip   (control)     │
              │  └── cyber_intel (intelligence)│
              └────────────────┬───────────────┘
                               ▼
                   git push → orphan `data` branch
                               ▼
                 GitHub Actions → GitHub Pages
                               ▼
                      PUBLIC CYBERPUNK HUD
```

**Responsibility contract** (do not collapse these):

| Component | Owns | Must never |
|---|---|---|
| Paperclip | Agents, org chart, issues, delegation, heartbeats, budgets, approvals, audit | Hold application business logic |
| Worker (Python) | All deterministic work, scheduling of FAST/NORMAL, the ops API | Use an LLM where Python suffices |
| Strands | Multi-step specialist workflows: research, verification, evidence reconciliation, follow-up | Act as the scheduler or org |
| OpenCode agent | Code changes on a branch, with tests | Touch `main` directly, or hold non-GitHub secrets |
| PostgreSQL | Structured long-term intelligence + control state | Store raw article bodies |
| Filesystem | Raw cache, transcripts, STIX, workspaces, logs, diagnostics | Be published |
| GitHub Pages | Pre-computed canonical intelligence | Run anything, or hold any secret |

---

## 4. The crew

Eight agents, each named after a figure from the cyberpunk canon whose role matches the
job — Matrix, Transformers, Blade Runner, Ghost in the Shell and Cyberpunk 2077. Each has a
persona (it makes the org legible, and the private dashboard genuinely pleasant to read), a
plain job title, a Paperclip role, an adapter, an explicit wake trigger, and a hard boundary.
**Deterministic work costs zero tokens** — that is the point of the split. It runs in the
worker, and the one `http` agent only asks the worker whether it is healthy.

**Sixteen until October 2026.** The first crew had sixteen agents, but they were eight jobs.
Five were `http` health checks that did no reasoning, and three research desks ran the same
loop on different beats. The callsigns were kept, so nothing stored in the database changed,
and each job title became plain:

| Retired | Now done by |
|---|---|
| ZION (Australian desk), BLASTER (global desk), WINTERMUTE (AI desk) | DECKARD, the Researcher: one pass over all three beats, each beat a skill |
| TRON (independent verification) | TELETRAAN, the Operation agent, on the AUDIT model from a different vendor to WHEELJACK |
| ROGUE (cost) | RIPPERDOC, the Cheap agent: the monthly cost review. The worker keeps the ledger and sets the degradation tier |
| LIBRARIAN, PROWL, LINK (ground truth, correlation, publishing) | The worker, with SERAPH, the Collector, reporting on all four jobs in one `http` wake |

Personas are presentation, not licence: an agent's authority comes from its Paperclip role,
permissions and budget, never from its voice. The names are homage, used internally as
callsigns; public-facing copy is written in plain Australian English regardless of persona.

### 4.1 Intelligence team

#### MORPHEUS — CEO / Chief Editor
> *Matrix.* Sees the whole board and refuses to walk it for you. Recruits, briefs, delegates, and always asks one question more than is comfortable. Calm to the point of unnerving.
> **"I can only show you the door."**

| | |
|---|---|
| Role / adapter | `ceo` · `opencode_local` (OpenRouter, strong tier, 1×/day + on escalation) |
| Wakes on | Daily editorial routine 07:30 AEST; escalation from DECKARD, VOIGHT or TELETRAAN; a `[MODEL]` proposal from RIPPERDOC; budget or watchdog incident |
| Owns | Intelligence quality and direction; coverage gaps; what gets promoted; the daily report; approving model changes |
| Specialised tasks | Read the overnight digest (counts, new/updated/archived, top events by prominence, desk escalations, source-health deltas). Decide which developing events need follow-up and assign DECKARD. Detect coverage gaps ("nothing on AU health sector in 9 days") and task TACHIKOMA. Approve or reject publication of events VOIGHT has flagged. Approve or decline RIPPERDOC's `[MODEL]` proposals, on the worker's own cost and gauntlet figures. Write the human-readable daily intelligence report. |
| Never | Collects or enriches anything itself; edits code; raises its own budget |
| KPI | Coverage gap closure time; % of published events with no QA finding |

#### DECKARD — Researcher
> *Blade Runner.* Works the open cases nobody else wants. Keeps a wall of unfinished threads and will not let you forget a single one of them. Now walks every beat as well: the home ground, the global wire and the AI frontier.
> **"The case stays open until it's patched."**

| | |
|---|---|
| Role / adapter | `researcher` · `opencode_local` (fast tier; strong on critical) + Strands follow-up graph. A short core prompt; each beat is a Paperclip skill it loads only for an event that needs it (`cyberpulse-au-desk`, `cyberpulse-global-desk`, `cyberpulse-ai-desk`, `cyberpulse-follow-up`) |
| Wakes on | Desk digest routine 06:00, 14:00 and 22:00 AEST, each reading the last 8 hours; follow-up routine every 6 h, at least an hour from any digest; assignment from MORPHEUS |
| Owns | The three beats — Australian, global cyber and AI — and every event in `developing` or `monitoring` status |
| Specialised tasks | **Australia first, in every pass.** *Australian beat:* confirm or correct the AU relevance score **with reasons**, not a number alone. Distinguish "an Australian outlet reported it" from "Australia is affected" — the central AU judgment. Map events to AU sectors and SOCI asset classes. Draft the "Why this matters to Australia" paragraph from evidence. Flag events that warrant an OAIC notifiable-data-breach watch. Escalate to MORPHEUS when AU critical infrastructure is implicated. *Global beat:* cluster related events into campaigns and propose `related_event` / `attributed_to` edges. Track threat-actor aliases across vendor naming schemes. Judge whether an "exploitation" claim is confirmed, credible or speculative. Propose MITRE ATT&CK techniques **from the cached dataset only**, labelled `ai_suggested` with confidence. *AI beat:* separate `AI_INDUSTRY` / `AI_SECURITY` / `AI_THREAT_ACTIVITY` / `AI_CYBER_CONVERGENCE`. De-prioritise product-launch marketing with no security relevance. Map AI incidents to MITRE ATLAS where it applies. Track the MCP ecosystem specifically. Flag when an AI capability materially changes attacker economics. *Follow-up:* for each tracked event ask the one question that matters — *did anything materially change?* Watch for: PoC published, exploitation confirmed, KEV addition, patch released, vendor update, new victim/actor/geography, new AU exposure, regulatory response, resolution. Write timeline entries for genuine changes only; the worker moves the status from the record. An event that reaches `resolved` gets a final summary. Escalate new AU exposure on a global event immediately. |
| Guards | One budget (US$2/month) and one run slot (`maxConcurrentRuns` 1) cover all research; every routine skips a run while one is still going. At most 2 subagents in a run, one beat each — never one per event |
| Never | Overrides ground truth; invents technique IDs, CVEs or CVSS values; treats a single social post as confirmation; refreshes prominence because another outlet repeated the story; lets general AI industry news crowd out security intelligence; publishes without VOIGHT on high/critical |
| KPI | AU relevance precision on a weekly human-reviewed sample; false-attribution rate (target zero); AI-security recall vs. a curated watchlist; median lag from real-world change to timeline entry |

#### VOIGHT — Publisher
> *Blade Runner.* Named for the Voight-Kampff test, because the job is telling the real from the synthetic. Asks the same question repeatedly, watching for the flinch. Has killed better copy than yours.
> **"Says who?"**

| | |
|---|---|
| Role / adapter | `qa` · `opencode_local` (strong tier, gated: all critical/high + a daily sample) |
| Wakes on | Publish-candidate gate for critical/high; daily sampling routine |
| Owns | Veto over publication |
| Specialised tasks | Check every claim against its attached evidence and reject unsupported ones. Hunt hallucinated CVEs, wrong dates, wrong organisations, inflated severity. Catch duplicate events that slipped past the worker's correlation. Enforce the copyright rule — original short summaries, no substantial reproduction. Verify AI inference is labelled as such and never presented as official MITRE attribution or vendor confirmation. Confirm a primary source is linked. |
| Never | Rewrites facts; silently downgrades severity without recording a reason |
| KPI | Post-publication corrections (target zero); false-rejection rate |

#### TACHIKOMA — Finder
> *Ghost in the Shell.* Relentlessly curious think-tank that wanders off, pokes at everything unindexed, and comes back chattering. Occasionally returns with a bottle cap.
> **"Ooh — what's this one?"**

| | |
|---|---|
| Role / adapter | `researcher` · `opencode_local` (fast tier). The worker runs the Tavily searches (~30/day) and holds the key |
| Wakes on | Routine *Source discovery* 03:30 Sydney, after the worker's 03:10 search; coverage-gap task from MORPHEUS |
| Owns | Continuously finding sources the registry does not know about |
| Specialised tasks | Read the worker's nightly discovery finds (AU security, AU AI policy, AI red teaming, MCP security, prompt injection, named sectors) from `GET /ops/candidates`, and mine citations in existing high-quality sources for unregistered outlets. Find new AU researchers, CERTs, regulators, vendor PSIRTs, newsletters, YouTube channels. Submit every find to `POST /ops/candidates` with its feed or home page and a reason (at most 20 a day). Detect when a registered source has moved (the `blog.google/security` case). Propose retirement of sources with sustained poor value. |
| Never | Activates a source — that is the worker's source gate, always |
| KPI | Candidates promoted to ACTIVE per month; % later degraded (over-eagerness signal) |

### 4.2 Operations team

#### TELETRAAN — Operation
> *Transformers.* The ship's computer that scans continuously and wakes the whole crew when something moves. Notices the one missing signal before anyone else does, and has never once said "looks good to me" without reading it.
> **"Anomaly detected on the grid."**

| | |
|---|---|
| Role / adapter | `devops` · `opencode_local` on the AUDIT tier, **a deliberately different vendor from WHEELJACK's CODE model**. The worker's watchdog runs the deterministic checks every 5 min; the model only diagnoses an open incident and verifies a fix |
| Wakes on | Incident routine (the watchdog fires it for a high or critical incident); PR opened by WHEELJACK |
| Owns | System health, the self-healing loop, and the two-person rule for code |
| Specialised tasks | Monitor Paperclip, agent heartbeats, collectors, sources, Postgres, OpenRouter, Tavily, GitHub, generated data, publication and site freshness. Detect the specific failure signatures: no collection, repeated feed failure, parser drift, **unexpected zero volume**, event-count collapse, duplicate explosion, schema drift, cost anomaly, publish failure, stale public site, **stale-but-healthy feed**. Open an incident with a reproduction case, diagnose root cause, then hand a *structured, sanitised* task to WHEELJACK — never raw fetched content. **Verify the fix as if someone else had asked for it**: does it address the root cause rather than the symptom; run the full test suite and the source-sample ingestion; check the diff for scope creep, secret exposure, weakened validation or silently disabled controls; confirm the regression test would actually have caught the original failure — the check that makes self-healing trustworthy. Report a clear pass/fail with reasons; a fail returns the issue to WHEELJACK and counts against the circuit breaker. Enforce circuit breakers: same automatic fix fails 3× → stop, escalate to human. Also halt on repeated test failure, failed migration, failed security scan, unexpected file modifications, or unresolvable merge conflict. |
| Never | Approves as the final gate — a human still merges; passes a fix because it asked for it; disables a security control; retries a failing fix indefinitely |
| KPI | Mean time to detect; auto-resolved incident rate; false-positive alerts; escaped defects |

TELETRAAN both asks for a fix and checks it, which is why the rule holds three ways: the agent
that writes the code is never the one that passes it, the two run on different vendors'
models, and a person always merges.

#### RIPPERDOC — Cheap
> *Cyberpunk 2077.* The street surgeon who knows which chrome is worth fitting and which will cook your nervous system. Watches what just landed on the market, counts every cent it costs, and is unsentimental about ripping out last month's upgrade.
> **"Better chrome just came in."**

| | |
|---|---|
| Role / adapter | `cfo` · `opencode_local` (fast tier). The worker runs the scan and the gauntlet (**deterministic**), keeps the cost ledger and holds the OpenRouter key; the model reads the results from `GET /ops/models` and `GET /ops/cost` and raises the proposals |
| Reports to | MORPHEUS, who approves every `[MODEL]` proposal |
| Wakes on | **Daily** routine, 04:00 AEST, after the worker's 03:20 catalogue scan (zero tokens); **weekly** Sunday 05:00 AEST, after the worker's 03:40 gauntlet, for any promotion proposal; **monthly** cost review, 09:00 AEST on the 1st; on-demand within minutes when TELETRAAN reports a configured model failing, withdrawn, or above the price ceiling |
| Owns | Keeping the §7.1 ladder optimal — the cheapest capable model for each tier, free wherever possible, never above the US$1 output ceiling — the **agent-to-model assignment** built on it (§7.7): which model each of the eight agents is running right now, and swapping an agent off a model that is failing it — and the monthly cost review |
| Specialised tasks | Snapshot `/api/v1/models` each run and diff against the last: new models, withdrawn models, price changes. **Flag new `:free` models loudly** — free is always evaluated first. Filter candidates to `tools` + `structured_outputs`, output ≤ $1/M, non-`:batch`, with a capable route at acceptable quantisation. Score survivors on published signals (§7.6). Run the **local gauntlet** — a pinned golden set of ~30 human-verified events — measuring schema compliance, agreement with golden labels, **refusal rate on security content**, p95 latency and measured cost from `usage.cost`. Open a proposal issue with a side-by-side table and a recommendation; the cost figures in it are the worker's own (the price guard and the ledger), copied unchanged. Re-verify every ladder model's current price each run and immediately drop any that drifted above the ceiling. Repair fallback chains when a `:free` variant disappears, before a pipeline run discovers it. Monthly, report last month from the ledger — by agent, stage and model, with the calls that had no billed cost — and whether the ledger and the key agree, then at most three recommendations. |
| Worker-side, not the agent | Records every AI call from OpenRouter's `usage.cost` — actual billed cost, not an estimate — attributed to agent, event and pipeline stage. Polls `GET /api/v1/key` for `limit_remaining` and `usage_daily`, and sets the degradation tier **before** the spend happens. Tracks Tavily credits from `include_usage` against the free 1,000/month. Branches correctly on 402 `limit_source`: `in_flight_budget` is transient (honour `Retry-After`), `key_limit` degrades, `credits` alerts and stops |
| Never | Approves its own proposal; changes a tier **default** unilaterally — that needs MORPHEUS; raises or asks for a budget; proposes anything above the ceiling; justifies a promotion on adoption figures alone; runs the gauntlet against live events instead of the golden set; swaps the same agent more than **3× in 24 h** (circuit breaker → escalate, §7.7) |
| KPI | Cost per 1,000 enrichments, trending down; share of pipeline volume served by the free tier, trending up; regressions caught before promotion; ceiling breaches (must be zero); actual vs. budgeted spend |

#### SERAPH — Collector
> *Matrix.* The guardian who tests you before you are admitted. Unfailingly courteous, entirely immovable, and apologises while refusing you.
> **"I had to be sure."**

| | |
|---|---|
| Role / adapter | `devops` · `http` adapter → worker (**deterministic, zero tokens, permanently**). One wake, `POST /ops/agents/seraph/wake` with `{"job": "pipeline"}`, answers 200 when all four checks pass and 503 when any fails, each check reported on its own |
| Wakes on | The worker's own schedule does the work. In Paperclip a wake (an issue assigned to SERAPH) only asks whether the four jobs are healthy |
| Owns | The four deterministic jobs on one org-chart line: ground truth, the source gate, correlation and publishing |
| Specialised tasks | **Ground truth.** Sync CISA KEV (JSON + GitHub mirror). Pull CVE deltas from `cvelistV5/cves/delta.json` (~7-min cadence) and keep the daily baseline zip as a catch-up path, because the delta log has been trimmed from 30 days to 15 before. Resolve CVSS in the validated order: **CNA record → CISA Vulnrichment ADP → NVD (optional)**. Fetch EPSS with `unknown` on absence. Query OSV and the GitHub Advisory Database. Cache ATT&CK STIX (currently v19.2) and ATLAS via `dist/manifest.yaml` — never the `ATLAS-latest.yaml` symlink, which raw GitHub serves as a filename string. Extract CVE IDs by regex, never by model. **The source gate:** `DISCOVERED → CANDIDATE → TESTING → VALIDATED → ACTIVE → DEGRADED → BROKEN → RETIRED`. Probe connectivity, feed/API validity, auth, content type, parser correctness. Require **genuine recent relevant items** before promotion — the explicit anti-pattern from the source prompts is activating a source blind. Measure duplicate rate, freshness and relevance; record a baseline quality profile. Run the new-source feedback loop (validate day 0, precision over runs 1–3, reliability over runs 4–7, then a long-term score). Apply auto-degradation: 3 failures warn, 5 consecutive degrade, structural failure opens an engineering task. **Detect stale-but-200 feeds by age of newest item.** **Correlation:** resolve `NEW_EVENT / UPDATE_EXISTING / DUPLICATE / RELATED_BUT_DISTINCT / UNVERIFIED_SIGNAL` through the cheap-to-expensive ladder: URL hash → canonical URL/GUID → normalised title hash → trigram + entity overlap → date proximity → (Stage 3) embeddings → LLM last, on the fast tier and only for genuinely ambiguous cases. Classify material change into the 14 defined types, defaulting to `NO_MATERIAL_CHANGE`. Build source lineage so a vendor release plus three syndicated rewrites counts as **one** primary claim, not four confirmations. Maintain `first_seen`, `last_seen`, `last_material_update`, `last_independent_confirmation` separately. **Publishing:** generate `live.json`, `index.json`, `trends.json`, `source-health.json`, `system-status.json`, daily history files. Validate against JSON Schema and **fail closed** on any critical failure. Run the secret scan before every push — the last line of defence. Force-push the single-commit orphan `data` branch, which keeps repo history flat. Verify the Pages deployment actually succeeded and retry with backoff, retaining output locally on failure. Send Telegram notifications: critical AU alert, daily digest, developing update, self-healing incident, source discovery, system failure, weekly trends. |
| Never | Lets a model author a CVSS score, KEV status or technique ID; promotes on a single successful fetch; treats title similarity alone as identity, or syndication as corroboration; publishes unvalidated data, raw article bodies, or anything that trips the secret scan |
| KPI | Ground-truth freshness lag; post-activation degradation rate; duplicate rate in published output; false-merge rate; publish success rate; secret-scan escapes (must be zero); site staleness |

### 4.3 Engineering team

#### WHEELJACK — Coder
> *Transformers.* Inventor and mechanic. Fixes parsers before breakfast, has strong views about feed formats, and knows exactly why the tests are not optional.
> **"She'll be right — after the tests pass."**

| | |
|---|---|
| Role / adapter | `engineer` · `opencode_local` (OpenRouter code tier), `maxDailyRuns` capped |
| Wakes on | Engineering issue from TELETRAAN or MORPHEUS. **Never on a timer** |
| Owns | Code changes |
| Specialised tasks | Repair broken parsers and feed-schema changes; write new source adapters (`rss`, `atom`, `json_api`, `github_api`, `advisory_api`, `web_page`, `sitemap`, `search`, `youtube`, `community`); fix deduplication, enrichment, transformation and frontend defects; repair publishing failures; add a regression test for every fix. Workflow is fixed: `issue → git worktree → implement → tests → self-check → PR → TELETRAAN verify → human approve → merge → deploy → post-deploy check`. |
| Never | Pushes to `main`; touches auth, secrets, permissions, budgets, deployment controls or schema migrations without human approval; sees any credential other than its branch-scoped GitHub token |
| KPI | Fix success rate at first attempt; regression rate; time from incident to merged fix |

### 4.4 Org chart and permissions

```text
MORPHEUS (ceo)
├── DECKARD (researcher: AU, global and AI beats, follow-up)
├── VOIGHT (publisher: editorial QA)
├── TACHIKOMA (finder: source discovery) ──► the worker's source gate
├── RIPPERDOC (cheap: models and the monthly cost review)
└── TELETRAAN (operation: incidents, and the check on every fix)
    ├── WHEELJACK (coder) ──► TELETRAAN verifies, a human merges
    └── SERAPH (collector: ground truth, source gate, correlation, publishing)
```

Least privilege, per the source prompts' §60, made concrete:

| Agent | Secrets it receives | Cannot |
|---|---|---|
| MORPHEUS | Ops API token (read + task create) | Merge code; raise budgets |
| DECKARD | Ops API token (reads, and follow-up reports only) | Write ground truth; publish |
| VOIGHT | Ops API token (read + verdict) | Edit event facts |
| TACHIKOMA | Ops API token (the worker holds the Tavily key) | Activate a source |
| RIPPERDOC | Ops API token (the worker holds the OpenRouter key, makes the gauntlet calls and keeps the ledger) | Change a tier default; approve its own proposal; exceed the price ceiling |
| TELETRAAN | Ops API token (reads, health endpoints, incident verdicts). No repo token: the repository is public | Disable security controls; approve as final gate |
| WHEELJACK | Branch-scoped `GITHUB_TOKEN` **only** | Push to `main`; read any other secret |
| SERAPH | None. The wake needs no token; the work runs in the worker, which alone holds the publish `GITHUB_TOKEN` and the Telegram token | Read OpenRouter or Tavily keys; run a model |

---

## 5. Data model

Canonical unit is the **event**. Article-shaped records exist only as `event_sources`.

```json
{
  "event_id": "evt-2026-000123",
  "schema_version": "1",
  "pipeline_version": "1.0.0",
  "scoring_version": "1",
  "enrichment_version": "1",

  "first_seen": "2026-09-29T04:00:00Z",
  "last_seen": "2026-09-29T12:00:00Z",
  "last_material_update": "2026-09-29T12:00:00Z",
  "last_independent_confirmation": "2026-09-29T09:00:00Z",
  "status": "developing",

  "title": "Example vulnerability is being actively exploited",
  "summary": "Short original summary. Never a reproduction of source text.",
  "why_it_matters": "Evidence-based, at most three sentences.",

  "domains": ["cybersecurity"],
  "categories": ["vulnerability", "active-exploitation"],
  "ai_subdomain": null,

  "severity": "critical",
  "severity_source": "cna",

  "risk": { "urgency": 0.93, "confidence": 0.96, "novelty": 0.87, "prominence": 0.91 },

  "au": {
    "relevance": 0.94,
    "directly_reported_in_au": true,
    "reasons": ["ACSC advisory issued", "affected product widely used in AU government"],
    "sectors": ["government", "critical-infrastructure"],
    "soci_asset_classes": ["data storage or processing"]
  },

  "entities": { "actors": [], "organisations": [], "products": [], "countries": [], "industries": [] },

  "cves": [{
    "id": "CVE-2026-88772",
    "cvss": { "score": 9.8, "vector": "CVSS:4.0/...", "source": "cna" },
    "epss": { "score": null, "status": "unknown" },
    "kev": { "listed": true, "date_added": "2026-09-27", "due_date": "2026-10-18" }
  }],

  "mitre_techniques": [{
    "id": "T1190", "name": "Exploit Public-Facing Application",
    "confidence": 0.84, "confidence_type": "ai_suggested",
    "dataset_version": "ATT&CK v19.2", "evidence": ["vendor_advisory"]
  }],

  "claims": [{
    "text": "The vulnerability is being actively exploited.",
    "confidence": 0.97,
    "evidence": ["cisa_kev", "vendor_advisory"]
  }],

  "sources": [{
    "source_id": "acsc_alerts", "url": "https://...", "published": "2026-09-29T04:00:00Z",
    "evidence_class": "AUTHORITATIVE", "lineage_id": "vendor-release-8831", "independent": true
  }],

  "timeline": [{
    "timestamp": "2026-09-29T12:00:00Z", "type": "EXPLOIT_CONFIRMED",
    "summary": "Vendor confirmed exploitation in the wild.", "sources": ["vendor_psirt"]
  }],

  "relationships": [{ "type": "exploits", "event_id": "evt-2026-000098" }],
  "tags": [],
  "pending_enrichment": false
}
```

**Enums.** `status`: `new, active, developing, monitoring, contained, resolved, archived`.
`severity`: `critical, high, medium, low, unknown`. `severity_source`: `cna, cisa_adp,
nvd, vendor, ai_estimate, unknown`. `evidence_class`: `PRIMARY, AUTHORITATIVE, VENDOR,
SPECIALIST, NEWS, COMMUNITY, SOCIAL, AI_INFERENCE`. `material change`: `NEW_FACT,
NEW_CVE, NEW_EXPLOIT, EXPLOIT_CONFIRMED, NEW_ACTOR, NEW_TARGET, NEW_GEOGRAPHY,
NEW_AU_EXPOSURE, NEW_IMPACT, NEW_PATCH, NEW_MITIGATION, NEW_EVIDENCE, CORRECTION,
NO_MATERIAL_CHANGE`. `relationships`: `related_event, follow_up_to, caused_by, exploits,
affects, targets, uses, attributed_to, mitigated_by, resolves`.
`ai_subdomain`: `AI_INDUSTRY, AI_SECURITY, AI_THREAT_ACTIVITY, AI_CYBER_CONVERGENCE`.

**Tables** (`cyber_intel`): `events, event_sources, event_timeline, event_relationships,
claims, evidence, cves, cve_scores, mitre_techniques, mitre_dataset_versions,
organisations, products, threat_actors, threat_actor_aliases, countries, sectors,
source_registry, source_health, source_lineage, runs, trends, cost_ledger,
followup_tasks, incidents, agent_proposals`.

**Scoring.** Severity and prominence stay separate. Base severity `critical=4, high=3,
medium=2, low=1`; KEV adds a bonus. Corroboration uses `independent_confirmations`, never
raw source count. Recency half-life: low/medium 24 h, high 72 h, critical 168 h. **The
decay timer resets only on material update.** Archive at `prominence < 0.05` or age > 30
days — archive, never delete. All of it lives in versioned `config/scoring.yaml`.

---

## 6. Collection

| Lane | Cadence | Scheduler | Sources | Purpose |
|---|---|---|---|---|
| **FAST** | Hourly, at :00 UTC (§2.3) | Worker | ACSC alerts/advisories, CISA advisories + KEV, CVE deltas, major CERTs, critical vendor advisories | Threat radar |
| **NORMAL** | 4 h | Worker | AU + global news, vendor blogs, AI sources, research, GitHub advisories, YouTube, community | Main feed |
| **DEEP** | Daily 03:00 AEST | Paperclip routine | Tavily discovery, source-quality analysis, trends, long-form research, follow-up sweep | Improvement |

Registry is data-driven in `config/sources.yaml` — never hardcoded logic. Every source
carries: `id, name, type, region, category, priority, enabled, url/query, parser,
reliability, class, discovery_date, validation_date, last_success, last_failure,
failure_count, expected_frequency, notes`. Source classes: `authoritative, primary,
vendor, specialist, news, community, social, discovery`. Quality profile: `reliability,
freshness, relevance, parser_stability, duplicate_rate, independent_reporting_value`.

**Collector requirements** proven necessary by the live source audit: a descriptive
User-Agent; conditional requests (ETag / `If-Modified-Since`) — several feeds carry
hundreds to thousands of items; lenient date parsing normalised to UTC; per-source
override for short feeds (The Record carries 5 items, so poll hourly or lose stories);
explicit `SKIPPED` as a valid state when credentials are absent (X API stays
`enabled: false`); and raw payloads cached to the filesystem with a TTL, never to
Postgres or git.

**One more, found while testing credentials on 2026-09-30:** `cyber.gov.au` shows two
distinct transport faults. It intermittently aborts HTTP/2 streams with `INTERNAL_ERROR`
(`httpx` defaults to HTTP/1.1, so the collector is safe as long as we never set
`http2=True`). More importantly, **`/rss/alerts` hung for over 40 seconds returning zero
bytes, while `/rss/advisories` and the site homepage answered in ~0.1 s from the same IP at
the same moment** — so it is a per-endpoint fault, not an IP block or an outage.

Three consequences, all of which the Stage 1 design already anticipates but which this
makes non-negotiable:

1. **Per-source timeouts with concurrent fetch.** One hanging feed must never stall a run.
2. **`TIMEOUT` is a distinct health status from `ERROR`,** because the remedy differs: back
   off, do not retry immediately.
3. **Per-source circuit breaking.** ACSC alerts is our single highest-priority source, so
   TELETRAAN must distinguish "this one feed is hanging" from "ACSC is down" — the sibling
   feed responding proves the difference is observable.

---

## 7. AI layer

### 7.1 Tiers

Four tiers, not two. Every model below was verified against the live
`/api/v1/models` and `/endpoints` APIs on **2026-09-29** and filtered to those supporting
**both `tools` and `structured_outputs`**, which §7.2 makes mandatory. **Re-verify before
use** — the catalogue changes weekly. All slugs configurable via `config/models.yaml`.

| Tier | Default | Fallback chain | Price in/out per M | Used for |
|---|---|---|---|---|
| **0 — FREE** | `nvidia/nemotron-3-super-120b-a12b:free` | `qwen/qwen3.8-27b:free` → tier 1 | **$0 / $0** | Classification, entity extraction, tagging, source relevance, simple matching |
| **1 — CHEAP** | `openai/gpt-oss-20b` | `deepseek/deepseek-v4-flash-0731` → `z-ai/glm-5.3-flash` | $0.018 / $0.09 | Short summaries, AU relevance reasoning, ambiguous matching, desk digests |
| **2 — STRONG** | `xiaomi/mimo-v2.6-pro` | `xiaomi/mimo-v2.6-flash` → `minimax/minimax-m2.7` | $0.435 / $0.87 | Severity, impact, complex correlation, evidence reconciliation, MITRE mapping, editorial pass |
| **CODE** | `xiaomi/mimo-v2.6-flash` | `xiaomi/mimo-v2.5` → `mistralai/codestral-2508` | $0.14 / $0.28 | WHEELJACK |
| **AUDIT** | `deepseek/deepseek-v4-flash-0731` | `qwen/qwen3.8-flash` | $0.018 / $0.32 | TELETRAAN — must be a different vendor from CODE (two-person rule) |

**Changed 2026-10-03** by the price-ceiling guard and the Paperclip smoke tests. The live ladder
is `config/models.yaml`:

- Tier 2's last fallback is `qwen/qwen3.8-flash`. `minimax/minimax-m2.7` has no route with
  structured outputs.
- CODE drops `xiaomi/mimo-v2.5`: every capable route costs $2.00/M output or more.
- AUDIT now leads with `qwen/qwen3.8-flash`, the model TRON passed on, with 0731 as its
  fallback. TELETRAAN has run AUDIT since TRON retired in October 2026 (§4).

The guard checks each route, not the headline price (`worker/ai/ladder.py`).

**CODE and AUDIT were chosen on measured session cost and adoption trend, not token price**
(§7.6). All three signals were needed, and each alone would have picked wrong:

- **Token price alone** picks `openai/gpt-oss-20b` at $0.126/1k events — but it appears in
  neither the harness session rankings nor the top-20 adoption table, so it has no
  real-world validation at all.
- **Session cost alone** picks `xiaomi/mimo-v2.5`, cheapest eligible at $0.17/session over
  50+ turns. But adoption shows it **down 72% week-on-week** at rank 15 — a model being
  abandoned.
- **Adoption alone** picks `deepseek/deepseek-v4.1-flash`, rank 1 with 20.8T tokens and +23%
  growth — which is over the output ceiling at $1.20/M and therefore ineligible.

The resolution is `xiaomi/mimo-v2.6-flash`: **identically priced to v2.5** ($0.14/$0.28),
newer, 1.05M context, and **+>999% growth at rank 7** while v2.5 collapses. It is the
successor users are actually migrating to. It has no session-cost row of its own yet, so
that specific inference is unproven and the gauntlet must confirm it.

`mistralai/codestral-2508` — my earlier CODE default — drops to last fallback: it appears in
no harness ranking and no adoption table, so a purpose-built coding model with zero observed
agent usage loses to a general model with a strong one.

AUDIT deliberately sits on a different vendor from CODE. DeepSeek V4 Flash 0731 also fits
the shape of the work: review is input-heavy (reading a diff) and output-light (a verdict),
and its $0.018/M input is the cheapest credible rate available — about $0.003 per review.
It is rank 6 with 7.57T tokens, so it is thoroughly exercised.

**The two most-adopted free models cannot serve this pipeline.** `stealth/space-bunny-alpha`
(rank 2, 18.2T tokens) and `nvidia/nemotron-3-ultra-550b-a55b:free` (rank 8, +23%) both
offer `tools` but **not `structured_outputs`**, which §7.2 makes mandatory. That is why
tier 0 uses the far less famous `nemotron-3-super-120b-a12b:free` instead. Popularity and
fitness are different questions.

**Hard price ceiling: output ≤ US$1.00 per million tokens.** This is an owner-set policy
(2026-09-30), enforced in code rather than documentation — see §7.2. It has a real cost:

- **No frontier model is available at any price point under the cap.** Verified against
  `/endpoints`: `openai/gpt-6-sol` ($10 out), `anthropic/claude-sonnet-5.5` ($10),
  `z-ai/glm-5.3` ($4.40), `x-ai/grok-4.7` ($6) and `qwen/qwen3.8-max` have **zero**
  capable routes at or below $1 output. The cap is not a routing choice; it changes which
  class of model does our judgment work.
- `xiaomi/mimo-v2.6-pro` is the strongest thing under the ceiling: released 2026-09-21,
  1.05M context, and — unusually for this price — available at **bf16** from GMICloud, so
  tier 2 keeps its non-quantised requirement.
- Why this is defensible: §7.3 already forces the strong tier to reason over **retrieved
  evidence** rather than recalled knowledge. CVSS, KEV status and technique IDs are looked
  up, never generated. That narrows the gap between a mid-tier and a frontier model
  considerably, because the task is judgment over supplied facts, not recall.
- Where it still costs us: nuanced severity reasoning and evidence reconciliation on
  genuinely conflicting reports. VOIGHT's QA gate and the `unknown`-over-guess rule are the
  compensating controls, and §13 tracks measuring the difference.

**Excluded by the ceiling, for the record:** `openai/gpt-6-sol`, `openai/gpt-6-astra`,
`anthropic/claude-sonnet-5.5`, `anthropic/claude-opus-5.5`, `z-ai/glm-5.3`,
`x-ai/grok-4.7`, `google/gemini-3.8-flash` ($3.75 out), `deepseek/deepseek-v4.1-flash`
($1.20 out at the default route).

**`:batch` variants are also excluded** despite attractive pricing (e.g.
`openai/gpt-6-luna:batch` at $0.05/$0.25). Batch processing is asynchronous, which cannot
serve an hourly collection lane.

**Why tier 0 is viable.** OpenRouter's free limits are **20 requests/minute and 1,000
requests/day** once an account has purchased ≥10 credits all-time — which our $20 top-up
satisfies. Our fast-tier volume is ~124 calls/day (≈3,000 enrichments + 720 desk digests
per month), about **12% of the free daily cap**. `GET /api/v1/key` reports
`free_model_daily_requests.{used,limit,remaining}` so the worker can track it directly.

**Free-tier rules** (these are what make it safe to depend on):

- Only 5 free models support `tools` + `structured_outputs` at all: `nemotron-3-super-120b-a12b:free`, `dots-studio/dots-3-note-preview:free`, `qwen/qwen3.8-27b:free`, `liquid/lfm-2.5-2.6b:free`, and `openrouter/free`. Everything else is disqualified by §7.2.
- **Never use `openrouter/free`** — it selects a free model *at random* per request, so output quality and schema compliance would vary run to run. Unacceptable in a pipeline.
- `:free` variants appear and disappear. A `models: [...]` fallback chain ending in a **paid** tier-1 model is mandatory, so a withdrawn free model degrades instead of failing.
- On 429, fall through to tier 1 rather than retrying — a daily cap does not clear with backoff.
- Free routing generally requires enabling the *"providers that may train on prompts"* setting, which OpenRouter keeps **separate for free and paid models**. Our inputs are public news so exposure is low, but our enrichment prompts are our own work. Tier 0 is therefore restricted to mechanical tasks (classify, extract, tag) and never carries editorial reasoning.
- Free endpoints are deprioritised, so latency is higher and failures more common. Acceptable for a half-hourly enrichment batch; never on a publish-blocking path.

**Provider routing matters as much as model choice.** The `/models` list price is the
default route; per-provider prices differ by up to **15×** for the same model. Verified
examples:

| Model | Default route | Cheapest route | Cheapest with `structured_outputs` |
|---|---|---|---|
| `deepseek/deepseek-v4.1-flash` | $0.30 / $1.20 | $0.02 / $0.60 (Relace) — **no structured output** | $0.03 / $0.50 (OpenInference, fp4) |
| `z-ai/glm-5.3-flash` | $0.15 / $0.50 | $0.02 / $0.30 (OpenInference, fp4) ✓ | same |
| `openai/gpt-6-luna` | $0.10 / $0.50 | $0.05 / $0.25 (OpenAI) ✓ | same |

Two consequences:

1. Setting `provider: {sort: "price"}` alongside our mandatory
   `provider: {require_parameters: true}` gets the floor price **among capable providers
   only** — the two settings interact, and `require_parameters` is what stops us silently
   landing on a provider that ignores `response_format`.
2. The cheapest routes are frequently **fp4-quantised**. For strict JSON-schema compliance
   and severity judgment that is a real quality risk, so tier 2 pins
   `quantizations: ["bf16", "fp8", "unknown"]` and accepts the higher price. Tier 0 and 1
   may use fp4, because schema validation catches failures and the fallback chain absorbs them.

**Blended cost per 1,000 enrichments** at our measured mix (~3k input, ~800 output),
cheapest capable route:

| Model | Per 1k events |
|---|---|
| Tier 0 free models | **$0.00** |
| `openai/gpt-oss-20b` | $0.13 |
| `inception/mercury-2.5` | $0.24 |
| `z-ai/glm-5.3-flash` | $0.30 |
| `upstage/solar-mini4` | $0.31 |
| `openai/gpt-6-luna` | $0.35 |
| `deepseek/deepseek-v4.1-flash` | $0.49 |
| `openai/gpt-5.6-luna` | $1.56 |

### 7.2 Request discipline

Every call: `response_format` `json_schema` with `strict: true`; `provider:
{require_parameters: true}` **plus** `{sort: "price"}` so we get the floor price among
*capable* providers only; a `models: [...]` fallback list with the returned `model` logged
(that is what is billed, and on tier 0 it is how we know whether a free model served the
request); explicit `max_tokens`; `HTTP-Referer`, `X-OpenRouter-Title`,
`X-OpenRouter-App-Visibility: hidden`; `usage.cost` recorded to the ledger; **no tools
offered on enrichment calls** (§2.10); and our own schema validation regardless of
`strict`. Tier 2 additionally pins `quantizations: ["bf16", "fp8", "unknown"]`.

### 7.3 Guardrails

CVE: extract ID by regex → retrieve CNA record → Vulnrichment ADP → KEV → EPSS → hand
**retrieved facts** to the model → model reasons over evidence only. Absent CVSS is
`unknown`, never low. MITRE: model selects from the cached dataset only, stores
`confidence` + `confidence_type: ai_suggested` + dataset version, and the UI labels it
**AI SUGGESTED** — never official attribution. Trends: computed from data; a model may
interpret but never generate a statistic.

### 7.4 Degradation (mandatory)

| Budget remaining | Behaviour |
|---|---|
| > 50% | Full ladder: tier 0 → 1 → 2 by task |
| 20–50% | Tier 2 restricted to critical and KEV-linked; everything else tier 0/1 |
| < 20% | **Tier 0 only**, and enrich only critical / high / KEV-linked / developing |
| Free cap hit (429) | Fall through to tier 1 — a daily cap does not clear with backoff |
| Exhausted | AI off; deterministic pipeline continues; `pending_enrichment: true` |

Because tier 0 is free, "exhausted" now means only that *paid* tiers are unavailable —
mechanical enrichment can keep running on the free tier until its own daily cap is
reached. That is a meaningful resilience gain over a two-tier design.

On mid-run 402: branch on `limit_source` (§2.8), stop further paid AI calls, continue on
tier 0 and deterministically, publish with `pending_enrichment`, enrich later.

### 7.5 Strands usage

Strands is used **only** where a genuine multi-step workflow earns it: research,
verification, evidence reconciliation, ambiguous correlation, follow-up investigation. A
single structured enrichment call does not need a graph.

Pinned API facts from §2.7: `strands-agents` 1.57.1, Python ≥3.10, `GraphBuilder` from
`strands.multiagent`, explicit `model=OpenAIModel(client_args={"base_url":
"https://openrouter.ai/api/v1", ...})`, `structured_output_model=` (not
`structured_output()`), `callback_handler=None`, always set
`set_max_node_executions` / `set_execution_timeout` / `set_node_timeout`, **and
`all_dependencies_complete([...])` on every edge into the evidence-reconciliation join**
because graph joins are OR by default. Deterministic steps are custom `MultiAgentBase`
nodes so they cost nothing. Pin the exact version; it ships weekly.

### 7.6 Model lifecycle (RIPPERDOC)

The ladder is not a one-time choice. Model prices move — `z-ai/glm-5.3`'s listed input
price changed between two queries a day apart during this design — and `:free` variants are
withdrawn without notice. RIPPERDOC tends it on **two cycles, because the two halves of the
job have different costs**:

| Cycle | What runs | Cost | Why this cadence |
|---|---|---|---|
| **Daily**, 03:20 AEST in the worker; RIPPERDOC reports at 04:00 | Deterministic catalogue scan: snapshot `/api/v1/models` and `/endpoints`, diff against yesterday, re-verify the current price of every model in the ladder, detect `:free` variants that appeared or disappeared, and flag any ladder model that has drifted above the ceiling or lost its capable route | **$0.00** — plain HTTP, no inference | The thing that changes daily is *price and availability*, and catching a ceiling breach or a withdrawn `:free` model the morning it happens is worth a free API call. A pipeline run should never be the thing that discovers a model is gone |
| **Weekly**, Sunday 03:40 AEST in the worker; RIPPERDOC raises proposals at 05:00 | Score candidates on the published signals below, run the local gauntlet, open a promotion proposal | ≤ $0.25/month | The gauntlet is what actually decides, and it costs money. Running it daily would be ~7× the spend to re-answer a question whose answer is a pinned golden set — it does not change overnight. The adoption veto below is also defined **week-on-week**, so it needs a week of data to mean anything |
| **On demand**, within minutes | Repair a fallback chain, or swap a failing agent down its chain (§7.7) | $0.00 | Triggered by TELETRAAN, not by a clock |

Splitting it this way is what makes a daily assessment affordable: the free half runs every
morning, and the half that bills only runs when there is something new for it to judge.

**Selection order, always in this sequence:**

1. **Free and capable?** Evaluate every `:free` candidate first. If one passes the gauntlet for a task, it wins regardless of what a paid model scores.
2. **Cheapest paid that passes.** Among capable routes only (`require_parameters: true`).
3. **Never above the ceiling.** Output ≤ US$1.00/M, no exceptions, enforced in code.

**Published signals, all authenticated with our normal inference key.** Rate limits are
30 requests/minute and 500/day per account on the benchmark and dataset endpoints, which
a weekly cycle uses a negligible fraction of.

| Signal | Endpoint | What we read |
|---|---|---|
| Quality | `GET /api/v1/benchmarks` | Artificial Analysis `intelligence_index`, `coding_index`, `agentic_index`; Design Arena `elo`, `win_rate`; OpenRouter's own `gpqa_diamond` and `tau_bench_verified_airline` with `accuracy`, `accuracy_stddev` and `avg_cost_per_task`. Filter `task_type=coding` for the CODE tier |
| **Real cost per task** | `GET /api/v1/datasets/session-cost` | `median_session_cost_usd` by `app_slug` (harness), `model` and `turn_range` (`1-turn`, `2-9-turns`, `10-49-turns`, `50-plus-turns`). Weekly refresh over a 30-day window |
| Adoption + **trend** | `GET /api/v1/datasets/rankings-daily` | Tokens processed, and the week-on-week change. `modality=tool_calling` is our slice; `category=programming` for CODE. `:free` variants rank separately |
| Market share by task | `GET /api/v1/classifications/...` | Traffic share by task class |
| Price, capability, quantisation | `GET /api/v1/models`, `.../endpoints` | Per-provider price, `supported_parameters`, quantisation, data policy |

**Match `turn_range` to the tier.** Our enrichment is a single structured call, so `1-turn`
is the relevant cell for tiers 0–2. WHEELJACK and TELETRAAN run long agentic sessions, so
`10-49-turns` and `50-plus-turns` govern CODE and AUDIT. Reading the wrong cell is how you
end up optimising for the wrong workload.

**Session cost captures what token price cannot:** how many tokens a model actually burns to
finish a task, including reasoning tokens, retries and cache efficiency. A model with a
higher input price can be cheaper per completed task. This is the signal most likely to
overturn a token-price decision, and §7.1 records a case where it did.

**Falling adoption is a disqualifier, not a neutral fact.** A model shedding usage is one
being abandoned by people who tried it, and is a candidate for deprecation or withdrawal.
RIPPERDOC therefore treats **any decline steeper than −40% week-on-week as a veto** on
promotion, and opens a migration issue if a model already in the ladder crosses that line.
The worked example is in §7.1: `xiaomi/mimo-v2.5` looked optimal on session cost while
quietly dropping 72%, and its successor at the same price was growing >999%.

**Adoption is never sufficient on its own.** The top-ranked model overall breached our price
ceiling, and the two most-adopted free models lack structured outputs entirely. Popularity
answers "is this alive and exercised", not "can it do our job".

**The local gauntlet decides.** All of the above is published, generic evidence. A pinned
golden set of ~30 events with human-verified expected enrichment is what actually adjudicates:

| Dimension | Why it matters |
|---|---|
| Schema compliance rate | Hard gate. A model that cannot reliably satisfy `json_schema` is unusable no matter how cheap |
| Agreement with golden labels | Severity, entities, CVE extraction, AU relevance |
| **Refusal rate on security content** | A model that declines to summarise exploit or malware detail is worthless to this platform. No generic benchmark measures this, and it is the failure mode most likely to disqualify an otherwise strong candidate |
| p95 latency | A batch must finish well inside the half hour between enrichment passes |
| Measured cost per event | From `usage.cost`, not estimated from a price table |

**Caveat on the published datasets, stated plainly:** session cost and adoption are
*observational*, not controlled. A model used mainly for easy tasks shows a low median
session cost regardless of its efficiency, and harness populations differ in what they ask
of a model. Treat both as strong evidence about liveness and real-world economics, and the
gauntlet as the thing that decides.

Gauntlet cost is budgeted at ≤ US$0.25/month — about 30 events across a handful of
candidates, which at these prices is rounding error.

**As built in Stage 5** (`worker/ai/scout.py`, `gauntlet.py`, `golden.py`), where it differs
from the above:

- The worker, not RIPPERDOC, holds the OpenRouter key and makes every call. RIPPERDOC reads
  `GET /ops/models` and opens the issues.
- The gauntlet is also capped at US$0.08 a run, and follows the budget mode (§7.4): tier 2 only
  in `full`, tier 1 down to `conserve`, free models only below that.
- The authenticated benchmark and dataset endpoints are not read. Challengers are ranked by
  first listed in the last 14 days, then Artificial Analysis' `intelligence_index` where the
  model list carries it, then price. **The adoption veto is not automated**; every proposal
  says so and links the rankings.
- Each tier is judged on its production task: tier 0 on triage, tier 1 on the brief (AU
  relevance), tier 2 on the severity judgment. Entity and CVE extraction are not scored. The
  `code` and `audit` tiers do no enrichment and are not gauntleted; the daily scan still
  checks their models.
- The golden set is 30 events with a register severity and a CVE, pinned by stratum (9
  critical, 9 high, 9 medium, 3 low), taking up to 2 per stratum that an Australian advisory
  carried first: those are the only events the AU label calls Australian, and they are rare.
  The labels come from the registers and the source registry, so they are **not yet
  human-verified**; `golden_events.reviewed` records which ones a person has checked, and
  every proposal gives the count.

**Attribution.** Benchmark data carries a required citation in `meta.citation`. Both
datasets are CC BY 4.0 requiring *"Source: OpenRouter (openrouter.ai/rankings), as of
{as_of}"*. If any of it ever surfaces on the public site, the citation travels with it.

### 7.7 Agent-to-model assignment, and swapping a failing model

§7.1 keeps the *best model per tier*. This section is the layer above it: **which model each
of the eight agents is actually running**, and what happens when one of them stops working.

**Assignment is by tier, not per agent, and that is the point.** An agent is assigned the
cheapest tier that can do its work; RIPPERDOC keeps that tier on the best model available.
Eight independent per-agent choices would need eight gauntlets to justify, and there is no
evidence on which to make them differently — DECKARD's three beats and its follow-up are the
same shape of work. The indirection is what makes one evaluation serve every agent that shares
a need.

| Agent | Tier | Why that tier |
|---|---|---|
| MORPHEUS | 2 — STRONG | Editorial judgment over conflicting evidence; runs once a day, so the cost is bounded |
| VOIGHT | 2 — STRONG | It holds the publication veto; the gate must not be weaker than what it is gating |
| DECKARD | 1 — CHEAP, 2 on critical | Desk notes and AU-relevance reasoning over supplied evidence, and routine follow-up, are cheap; a critical developing event earns the strong tier |
| TACHIKOMA | 1 — CHEAP + Tavily | Judging whether a discovered source is worth proposing; the worker runs the searches |
| WHEELJACK | CODE | Long agentic coding sessions — chosen on session cost, not token price |
| TELETRAAN | AUDIT, incidents and fixes only | Deliberately a different vendor from CODE: a two-person rule is worthless if both halves share a failure mode. The watchdog's checks every 5 min cost nothing; the model only diagnoses an open incident and verifies a fix |
| RIPPERDOC | 1 — CHEAP, drafting only | The harvest, the gauntlet and the ledger are deterministic; the model writes up the recommendation and the monthly review |
| SERAPH | none | Deterministic by design — **zero tokens, permanently**. Inside the worker, correlation sends only the cases the rules could not decide to tier 1 |

Tier 0 — FREE serves the mechanical sub-tasks inside the pipeline (classify, extract, tag,
source relevance) rather than belonging to one agent, and §7.1 bars it from editorial
reasoning. SERAPH never spends anything, and the other seven spend only when they wake.

**Swapping a model that is failing an agent.** This is a different event from a promotion and
moves at a different speed, because it is a repair:

1. **TELETRAAN detects it** — schema-compliance failures, refusals on security content, 5xx or
   402 from the route, p95 latency outside the lane, or the daily scan finding the model
   withdrawn or over the ceiling.
2. **RIPPERDOC moves the agent down its tier's fallback chain immediately**, no approval. Going
   *down* a chain that MORPHEUS already signed off is not a new decision, and a
   pipeline that waits for a human to approve a documented fallback is a pipeline that stops.
3. **A promotion still needs MORPHEUS's approval.** Moving *up*, or changing a tier default, goes
   through the proposal and the gauntlet as §7.6 describes. The asymmetry is deliberate:
   degrading is reversible and cheap to get wrong, promoting is neither.
4. **Circuit breaker: three swaps for the same agent in 24 hours and RIPPERDOC stops** and
   escalates to a human. Three failures in a day is not a bad model, it is a wrong diagnosis —
   the same rule TELETRAAN applies to its own automatic fixes (§4.2).
5. **Every swap is written to the ledger** with the trigger, the old and new slug and the
   evidence, so a silent drift down the chain cannot happen unnoticed. The public site
   publishes the current assignment, not the history.

**Why Paperclip can carry this and what it must not hold.** Paperclip owns the *schedule* (a
daily DEEP routine in `Australia/Sydney`, §2.3), the *authority* (RIPPERDOC's role, its
approval chain to MORPHEUS) and the *audit trail* (issues, heartbeats, budgets).
It does **not** hold the ladder or the assignment table — §3 keeps application logic out of the
control plane — so those stay in `config/models.yaml` and Postgres, and RIPPERDOC writes them
through the worker's Ops API, which is the only write authority §4.4 grants it. Open question
§13/4 applies directly here: whether an `http`-adapter run satisfies Paperclip's mandatory
issue-comment backstop is **unverified**, and SERAPH is the crew's `http`-adapter agent. Stage 4
settles it; until then a manual comment is the fallback.

---

## 8. Public site

Static, no build step, no secrets, everything pre-computed. Design borrows technique from
CYBERCORE CSS (MIT) and augmented-ui (BSD-2) but **depends on neither** — CYBERCORE is
77 KB with unresolved "is this dead?" issues and no HUD primitives, and ARWES is
explicitly unmaintained and React-bound. We ship ~15 KB of our own CSS.

### 8.1 Palette

Cyan carries structure; warm colours mean severity — the discipline that makes a dashboard
readable. Ratios below are measured against **both** card backgrounds and asserted, not just
recorded, by `test_palette_meets_the_contrast_ratios_the_plan_claims`. Measuring against
`--bg-panel` alone is how `--line-strong` shipped at 2.8:1 on the surface it actually draws
`.btn` and `.input` borders on — under the 3.0 WCAG 1.4.11 requires. Cards sit on the lighter
of the two, so the lighter one is the case that has to pass.

**Colour is spent, not sprinkled.** Cyan means exactly three things — a link, the selected
tab, and a headline figure. Magenta means one: a value a model inferred rather than read off a
source. Every micro-label on the page was cyan or teal in the first pass, which left no
emphasis to spend on anything; hierarchy in small print now comes from size, weight and
tracking, and the labels are grey.

`--brand` is a token of its own, so that the `-AI` in the wordmark can have a colour meaning
nothing else on the site. `--magenta` was the obvious reuse and is wrong: it marks model
inference, so a magenta `-AI` would read as *unverified*. Red is unavailable too — the severity
ramp owns 354° through 50°, and any red light enough to clear 4.5:1 on this background is a
coral you cannot tell from `--sev-critical` at a glance. Violet is the one hue nothing else in
the palette claims, which is what a brand colour has to be.

A studio palette, not a neon one. The first pass paired a near-black page with saturated
primaries (`#00E5FF`, `#FF2A6D`); at the sizes text is actually set that reads as glare.
Backgrounds now lift off pure black, the accents come down to the soft blue-cyan the
crew portraits glow with, and every severity hue is desaturated far enough that the label
does the shouting and the colour only confirms it.

| Token | Hex | Ratio | Role |
|---|---|---|---|
| `--bg-void` | `#121926` | — | Page |
| `--bg-panel` | `#19212D` | — | Panels |
| `--bg-raised` | `#24303E` | — | Cards, inside a panel |
| `--line` | `#394B62` | 1.8 / 1.5 | Container borders only |
| `--line-strong` | `#5E7E9C` | 3.8 / 3.2 | Control borders, and nothing else (WCAG 1.4.11) |
| `--text` | `#EDF2F7` | 14.4 / 11.9 | Body |
| `--text-dim` | `#AFC0CD` | 8.7 / 7.2 | Secondary |
| `--text-muted` | `#8A9CAB` | 5.7 / 4.7 | Micro-labels; minimum for small text |
| `--cyan` | `#5BD6E8` | 9.4 / 7.8 | Links, selected tab, headline figure |
| `--teal` | `#34A9BC` | 5.8 / 4.8 | Secondary lines |
| `--magenta` | `#F2789F` | 6.1 / 5.1 | Model-inferred values only |
| `--brand` | `#B184EB` | 5.7 / 4.7 | The `-AI` in the wordmark, and nothing else |
| `--sev-critical` | `#F56C79` | 5.6 / 4.7 | Critical |
| `--sev-high` | `#F09A4A` | 7.3 / 6.0 | High |
| `--sev-medium` | `#E8C766` | 9.9 / 8.2 | Medium |
| `--sev-low` | `#58C79C` | 7.8 / 6.4 | Low |
| `--sev-info` | `#6FBEEA` | 7.9 / 6.5 | Info |

Ratios are `--bg-panel` / `--bg-raised`. The backgrounds have been lifted twice. The first step
moved `--bg-raised` from `#18202B` to `#1A232E`, so that a card separates from its panel by tone
alone; with that step visible, the nine inner card types dropped their 1px `--line` rectangles —
roughly forty fewer hairlines on a full dashboard, and the borders that remain now mean
something because they are the only rectangles left. Nothing that carries state lost its edge:
an event card keeps its 4px severity bar, a source node its state colour, a pipeline node its
lamp.

The second step lifted all three backgrounds together — `--bg-raised` `#1A232E` → `#24303E`,
0.0162 → 0.0284 relative luminance — because the page still read as switched off rather than
dark. Every foreground ratio falls when a background rises, so the palette was re-derived
against the new pair rather than carried across: `--line-strong` and `--sev-critical` came up to
hold their floors, and `--line` came up furthest, because at `#253140` it sat within a hair of
the new `--bg-raised` and every rule separating two rows of a table would have disappeared into
the card drawing it. Lifting `--line` then broke something a contrast floor does not see: the
map filled a country with no events in `--line`, so one event had become *quieter* than none and
the legend read backwards. Tier 1 moved to `#29657D`, and the no-events fill stopped borrowing a
border token altogether: `--map-none` `#334357` sits 2.1× below tier 1, which is the step the
ramp had before the lift, and 3.6× above the ocean so land still reads as land. The five tiers
now climb 0.054, 0.113, 0.195, 0.327, 0.561 relative luminance — an even geometric ramp — and
`test_the_map_load_ramp_ascends_in_luminance` asserts the ordering.

Shape: one radius (`--radius` 14px, `--radius-sm` 8px) on panels, cards, inputs and
controls; chips and tabs are full pills. The chamfered corner the first pass clipped onto
every panel was the loudest piece of HUD costume on the page, and it fought the subject —
the crew are drawn as soft-radius bots.

**Typography: two faces, one job each.** `--font-sans` — the system UI stack — sets every
word; `--font-mono` — JetBrains Mono 400/600, self-hosted, SIL OFL — sets every figure.

There were four tokens across three families before, and the two display families were the
reason the page read as costume. Orbitron is a squared-off sci-fi face and Chakra Petch a
semi-technical one, and between them they set the brand, every panel title and every caption.
Neither is more readable than the reader's own UI font, and each cost a download. Both were
deleted — six `woff2` files, ~49 KB, and two `<link rel=preload>` per page — along with their
OFL sections. Hierarchy now comes from size, weight and tracking: body at 17px/1.65, panel
titles 15px/600 at `.09em` uppercase, micro-labels 12px/600 at `.08–.09em`. Uppercase caption
text carries more tracking than it did, because a humanist face needs more of it than a squared
one to stay legible at caps.

Mono earns its download on the one thing a proportional face genuinely cannot do: hold columns
of figures, timestamps and identifiers in line. It also now sets the two large numerals — the
CyberPulse Index and the gauge counts — which were in the display face; a figure belongs in the
face with `tabular-nums`. `test_fonts_ship_with_their_licence` derives the family list from the
`@font-face` rules and asserts served files and shipped files are the same set, so a face cannot
drift out of its licence or leave dead bytes behind.

The page background is flat. The graph-paper grid and the scanline film that preceded it were
both atmosphere paid for in legibility — ruled lines running under body copy, and a translucent
wash over every glyph.

Crew portraits: each of the sixteen agents is drawn as a little bot in inline SVG — one shared
shell (head, visor, two eyes, vents, a seam across the jaw, a highlight across the visor) plus
one accessory per agent, keyed by `CREW[].face`, that says what the agent does. Accessories run
four to seven nodes each rather than one or two: the akubra has a dented dome, a hatband and a
chin cord; the hard hat has ridges and a lamp; the tally closes with its fifth stroke; the
clipboard has a clip. Two ink weights exist for that detail — `.bot-etch` strokes in the page
colour, because a ridge on a filled shape cannot be drawn in the same ink as the fill, and
`.bot-glint` is a half-opacity highlight. Inline because the CSP serves no external images, and
because drawn eyes inherit `--bot-accent` from the card's desk, so the roster follows the
palette instead of a sprite sheet.

The same drawing appears twice: at 62px on the crew card, and at 30px in the first column of the
run table, which is what ties the two views of the roster together. At the smaller size the
etched detail drops out and the accessory silhouette does the identifying, so a row still reads
as a particular agent rather than a generic bot. One vector serves both — `botFace()` takes its
class as a parameter — rather than a second, coarser set of paths to keep in step with the
first.

### 8.2 Sections

`AUSTRALIA NOW` (visually dominant) · `GLOBAL CYBER` · `AI + CYBER` ·
`ACTIVE EXPLOITATION` · `DEVELOPING EVENTS` · `EMERGING THREATS` · `THREAT ACTORS` ·
`VULNERABILITIES` · `RESEARCH` · `POLICY / REGULATION` · `THE CREW` (sanitised agent
status — the org made visible) · `SYSTEM`.

Twelve sections, five tabs: `AUSTRALIA NOW` · `GLOBAL CYBER` · `AI + CYBER` · `OVERVIEW` ·
`THE CREW`. One tab per section was a strip of thirteen pills that read as a wall and made the
choice harder than the content behind it, so `OVERVIEW` carries the seven threat and context
sections stacked inside it behind a jump list, and `SYSTEM` sits inside `THE CREW` — both answer
who and what produced the page. Every section keeps its own id and heading, so `#sec-vulnerabilities`
and friends stay linkable from the headline list, from `event.html`, from `history.html` and from
anyone's bookmark; `initTabs()` resolves an id that has no tab to the panel holding it.

### 8.3 Components

1. **HUD panel** — chamfered `clip-path`, bracket corners, header strip with panel ID and status dot, tick ruler.
2. **CyberPulse Index** — our own composite index (explicitly ours, not an official scale) with an EKG pulse line of 24 h event volume.
3. **Threat radar** — concentric rings, conic-gradient sweep; blips positioned by severity (ring) and category (angle); AU events double-ringed.
4. **Severity gauges** — 270° SVG arc with `pathLength=100`, plus segmented bars; `role="img"` with `aria-label`.
5. **Event card** — severity stripe with shape *and* label, source-class badge, mono CVE/IOC, magenta `AI-SUGGESTED` chip, AEST time, hex region badge.
6. **Event detail** — the full §5 field set, evidence separated from AI inference, timeline, related events, source reports.
7. **Timeline** — tick-mark axis, hex nodes, day grouping, keyboard navigable.
8. **Pipeline flow** — SOURCES → COLLECT → MATCH → VERIFY → ENRICH → CROSS-REF → SCORE → PUBLISH, circuit-trace SVG paths with travelling dash pulses, per-node counter and status LED, source nodes coloured healthy/degraded/broken.
9. **World map** — world-atlas `countries-110m.json` (Natural Earth, **public domain**, 108 KB) + `d3-geo` as ES modules, Equal Earth rotated to 150°E so Australia sits centre. ISO-3166 numeric `id` per country enables click-to-filter; paired with a `<select>` for keyboard/screen-reader parity.
10. **Ticker** — pausable, `aria-hidden` duplicate track, static list under reduced motion.
11. **Boot sequence** — ≤2.5 s typed terminal, once per session, skippable, skipped entirely under reduced motion, never blocking content.
12. **Tag filter** — client-side across severity, CVE, country, source, org, product, sector, actor, MITRE, AU, AI, category.
13. **History** — previous/next day and jump-to-date via `data/index.json`.

### 8.4 Non-negotiables

- **Honesty:** never the word "LIVE". Always `LAST COMPLETED COLLECTION <timestamp> UTC`, and the pipeline animation is labelled `COLLECTION REPLAY`.
- **Motion:** every loop inside `@media (prefers-reduced-motion: no-preference)`, plus a persisted `FX OFF` toggle. Anything auto-moving >5 s is pausable (WCAG 2.2.2). No flicker — the classic CRT flicker at ~7 Hz is dropped outright (WCAG 2.3.1).
- **Colour is never the only signal** (WCAG 1.4.1): severity also carries a label, a shape (◆ ▲ ● ■ ○) and a bar count.
- **Performance:** animate only `transform`/`opacity` *(one narrow, deliberate exception: the EKG pulse line and pipeline dash-trace in §8.3 items 2 and 8 animate SVG `stroke-dashoffset`, since a travelling dash cannot be done any other way. Scoped to ≤2 small paths, always removable via `FX OFF` or reduced motion, never forces layout — accepted during Task 14 implementation rather than dropping the dash-pulse visual entirely)*; canvas at 30 fps, DPR capped at 2, paused on `visibilitychange` and via `IntersectionObserver`; `content-visibility: auto` on long lists.
- **CSP** via `<meta http-equiv>`, since Pages cannot set headers. Third-party files vendored or pinned with SRI.
- Australian English and `Australia/Sydney` display time throughout; UTC in storage.

---

## 9. Stages

Each stage is independently valuable and ends in something demonstrable.
🔴 = you do it (credentials, hardware, approvals) · 🟢 = I do it.

### Stage 0 — Prerequisites 🔴
Accounts and keys (OpenRouter with a $20 key limit, Tavily free, GitHub, Telegram bot),
make the repo public, and optionally build the Proxmox VM. Full walkthrough in the README.
**Exit:** `.env` populated, `docker compose config` validates.

### Stage 1 — Foundation: pipeline + site 🟢
Runs locally (WSL/laptop) — no Proxmox needed. Repo skeleton, versioned JSON Schema,
Postgres schema + migrations, source registry with every live-validated feed, RSS/Atom +
JSON API collectors with conditional requests, normalisation, deterministic event
resolution (URL/GUID/title/trigram/entity), deterministic scoring, publisher with schema
validation and secret scan, and **the complete cyberpunk site** rendering real ACSC/CISA
data. Plus the Actions Pages workflow.
**Exit:** `docker compose up` collects real events; the site is live on GitHub Pages with
genuine AU + global intelligence; `pytest` green.

### Stage 2 — Ground truth + enrichment 🟢
Ground-truth sync (KEV, cvelistV5 deltas, Vulnrichment, EPSS, OSV, GitHub Advisories,
ATT&CK, ATLAS) with the validated CVSS chain. OpenRouter client with strict schemas, cost
ledger from `usage.cost`, the degradation tiers, **and the price-ceiling guard that
refuses to start if any configured model exceeds US$1/M output** (§7.2). AI enrichment:
classification, entities, summary, severity judgment, MITRE suggestion. AU relevance engine
with reasons. Evidence engine and claims. Event detail page.
**Exit:** events carry real KEV/CVSS/EPSS and AI enrichment; ledger shows per-event cost
under budget; degradation demonstrably works when the tier is forced; the ceiling guard
rejects an over-priced model in a test.

### Stage 3 — Correlation depth + trends 🟢
Material-change detection (14 types), source lineage and independent confirmation,
prominence decay gated on material update, archiving, trend engine with real velocity
metrics, YouTube + `web_page`/`sitemap` adapters for the no-feed sources, optional
pgvector embeddings **only if measurement shows deterministic matching is insufficient**.
Trends and emerging-threats UI.
**Exit:** duplicate rate measured and acceptable; syndication no longer inflates
confidence; trends computed from real data.

### Stage 4 — Proxmox + Paperclip + first agents 🔴🟢
🔴 Build the VM, install Docker, restore `.env`, claim the Paperclip instance, bind
NetBird. 🟢 Deploy the stack, harden Paperclip (approvals on, sign-up off, telemetry off,
strict secrets), create MORPHEUS + DECKARD + TELETRAAN + RIPPERDOC with personas and
skills, expose the worker ops API, wire the deterministic `http` agent (SERAPH), configure
budgets. (Built with sixteen agents; moved to the eight of §4 in October 2026.)
**Exit:** agents visible in Paperclip, waking on schedule, spending within budget; the
pipeline survives Paperclip being stopped.

### Stage 5 — Full crew + follow-up + notifications 🟢
VOIGHT, TACHIKOMA, DECKARD's follow-up, RIPPERDOC's model scout. Source discovery with Tavily
and the SERAPH gate. The model-scout gauntlet and golden set (§7.6). Follow-up Strands graph
and status transitions. Daily intelligence report. Telegram notifications. Public
`THE CREW` page.
**Exit:** a source is discovered, validated and activated without human action; a
developing event accrues real timeline entries; RIPPERDOC proposes a ladder change with
gauntlet evidence attached; the daily digest arrives in Telegram.

### Stage 6 — Self-healing 🟢
TELETRAAN's full detection suite including stale-but-healthy feeds. Incident model. WHEELJACK
with worktree workflow and sandbox confinement. TELETRAAN verifying on a different vendor's model.
Circuit breakers and the human approval gate. Rollback.
**Exit:** a deliberately broken parser is detected, diagnosed, fixed on a branch,
independently verified, and merged after your approval — with the circuit breaker proven
to stop after 3 failures.

**As built** (`worker/watchdog/`, `worker/db/incidents.py`, `ops/rollback.sh`), where it
differs from the above:

- The detection suite is the worker's, not TELETRAAN's: a deterministic pass every 5 minutes
  over 14 signatures (TELETRAAN's eleven in §8, plus job failure, push failure and Paperclip
  down). It
  spends nothing. TELETRAAN's model is woken only by the Incident routine, which the watchdog
  fires through a Paperclip webhook trigger for each high or critical incident.
- The watchdog alone opens, resolves (after 15 minutes clear) and reopens (within 6 hours)
  incidents. Agents read them over the ops API; TELETRAAN's only write is its verdict.
- The circuit breaker counts TELETRAAN's FAIL verdicts per incident, across reopens. At 3 the
  incident needs a human and the API refuses further verdicts.
- WHEELJACK works in a new worktree and branch with a branch-and-PR token only; branch
  protection and a human merge are the gate. Its sandbox is the Paperclip server's container,
  whose environment the agents still inherit, so WHEELJACK stays paused until they don't.
- Rollback pins the VM's checkout to an earlier commit of main, or opens a revert PR.
  Migrations only add, so the database is never rolled back.

### Stage 7 — Hardening + operations 🟢🔴
Backups (PBS + `pg_dump` + `master.key`), observability and OpenTelemetry, cost tuning,
further notification channels, failure-injection test suite, threat model review, runbooks.
**Exit:** restore from backup rehearsed successfully; every failure-injection test passes.

**As built** (`ops/backup.sh`, `ops/restore.sh`, `tests/failure/`, `docs/threat-model.md`,
`docs/runbooks/`), where it differs from the above:

- `ops/backup.sh` dumps both databases (`pg_dump -Fc`) from one snapshot, with Paperclip's
  secrets folder, `.env`, a manifest of every table's row count, `SHA256SUMS` and a `COMPLETE`
  marker written last. PBS and `vzdump` are the owner's, on the Proxmox host, with a target off
  the single disk.
- `ops/restore.sh rehearse` restores into a throwaway Postgres of the same image, with no
  network, and compares every row count with the manifest. Rehearsed on VM 200 on 2026-10-03:
  `cyber_intel` 43 tables and 44,084 rows, `paperclip` 215 tables and 2,153 rows, all matched.
  `restore.sh fresh` rebuilds a new VM, and refuses unless both databases are empty and the
  Paperclip server is stopped.
- `tests/failure/` injects each failure in §11; `tests/failure/README.md` maps each one to its
  tests and lists what is not handled yet.
- The threat model was reviewed against what was built, boundary by boundary, with the open
  risks ranked. Its first: Paperclip's local-adapter agents inherit the server's environment.
  Runbooks cover each watchdog incident, a service down, the AI budget, token rotation, deploy
  and rollback, and backup and restore.
- **Not built:** OpenTelemetry (on one VM the watchdog, `job_runs` and the logs already answer
  "is it working, and since when"), and further notification channels (Telegram only, behind
  `worker/notify/`). Cost tuning is the budget modes of §7.4, as built in Stage 2.

---

## 10. Acceptance criteria

**Intelligence** — AU-first coverage · global cyber · AI as a first-class domain · cyber↔AI
convergence · ground truth from CNA/ADP/KEV/EPSS/OSV/ATT&CK · event-level deduplication ·
material-change detection · source independence · evidence-linked claims · timelines ·
follow-up · trends from real data.

**Autonomy** — Paperclip schedules agents · explicit per-agent ownership · heartbeats
without token waste · recoverable failed tasks · source discovery → validation →
activation · WHEELJACK fixes, TELETRAAN verifies, human approves · watchdog detects the listed
signatures · budget control with degradation · circuit breaker halts runaway repair.

**Public product** — GitHub Pages, static only · AU-first homepage · global + AI feeds ·
active exploitation · event detail · history · tag filtering · world map · collection
replay · honest last-updated · WCAG-conscious motion and contrast · **no secret ever
published**.

**Operations** — one Debian 13 VM · one Postgres instance, two databases · daily backups
with a rehearsed restore · AI spend ≤ US$20/month · graceful degradation on every external
dependency · full auditability.

---

## 11. Failure model

One component failing must never stop the platform.

| Failure | Behaviour |
|---|---|
| OpenRouter down/exhausted | Deterministic pipeline continues; `pending_enrichment: true`; enrich later |
| Tavily down | Discovery skipped; collection unaffected |
| One source fails | Others continue; health recorded; degradation counter increments |
| **Paperclip down** | Worker keeps collecting and publishing (this is why §2.3 exists) |
| Postgres down | Collection halts, raw cache retained, alert raised; no data loss |
| GitHub push fails | Output retained locally, retried with backoff |
| Pages build fails | Previous site stays up; incident opened |
| One agent fails | Others continue; task recoverable |
| Bad model output | Schema validation rejects; retry once; then `pending_enrichment` |
| Duplicate storm | Rate-limited, flagged as an anomaly, publication gated |

---

## 12. Repository layout

```text
CyberPulse-AI/                      # public
├── PLAN.md  README.md  .env.example  .gitignore
├── docker-compose.yml  Dockerfile.worker
├── site/                           # static public site
│   ├── index.html  event.html  crew.html  history.html
│   └── assets/{hud.css,hud.js,map.js,vendor/,fonts/}
├── data/                           # generated; force-pushed to orphan `data` branch
│   ├── live.json  index.json  trends.json
│   ├── source-health.json  system-status.json  crew.json
│   └── history/YYYY-MM-DD.json
├── config/                         # versioned
│   ├── sources.yaml  scoring.yaml  categories.yaml
│   ├── models.yaml  followup.yaml  discovery.yaml
├── worker/
│   ├── main.py  scheduler.py  ops_api.py
│   ├── collectors/  pipeline/  groundtruth/  ai/
│   ├── workflows/                  # Strands graphs
│   ├── schemas/                    # Pydantic + JSON Schema
│   ├── publish/  notify/  db/
├── agents/<callsign>/{AGENTS.md,skill.md,config.json}
├── schemas/                        # published JSON Schema
├── tests/{unit,integration,failure,fixtures}
├── ops/{backup.sh,restore.sh,runbooks/}
├── docs/{design/,superpowers/plans/}
└── .github/workflows/pages.yml
```

---

## 13. Open items

| # | Item | Resolve at |
|---|---|---|
| 1 | Re-verify model slugs, per-provider prices and `:free` availability — RIPPERDOC automates this from Stage 5; until then it is manual | Stage 2 start, then weekly |
| 2 | Measure tier 0 schema-compliance and security-refusal rates on real events; if free models fail too often, promote those tasks to tier 1 | Stage 2 |
| 3 | Quantify what the US$1 output ceiling costs in severity-judgment accuracy, using the golden set. If the gap is material on critical/KEV events, decide whether a narrow exception is worth ~US$2.40/month | Stage 5 |
| 2 | Whether embeddings/pgvector are needed at all, from measured duplicate rate | Stage 3 |
| 3 | SecurityWeek access (Cloudflare 403) — accept the gap or find a lawful route | Stage 3 |
| 4 | Whether an `http`-adapter run satisfies Paperclip's mandatory issue-comment backstop. The worker never writes to Paperclip: a wake's answer is the run's status and JSON body, and the Paperclip token in the wake's body is discarded. Comments on issues come from the AI agents. What the backstop does with an `http` run on an issue is seen on the first real wake | Stage 4, first wake |
| 5 | Custom domain for the public site | Deferred: the owner's choice, whenever wanted |
| 6 | Additional notification channels beyond Telegram | Deferred: Telegram only for now; `worker/notify/` takes another channel |
| 7 | Whether ROGUE's monthly LLM review earns its cost | Settled October 2026: ROGUE retired with the move to 8 agents. RIPPERDOC does the monthly review on the CHEAP tier, from the worker's ledger |

---

## 14. Sources checked (2026-09-29, prices re-verified 2026-09-30)

Paperclip (`github.com/paperclipai/paperclip` @ v2026.916.1, `paperclip-docs`) ·
Strands Agents (`strandsagents.com`, `github.com/strands-agents/harness-sdk`,
PyPI 1.57.1) · OpenRouter (`openrouter.ai/docs`, live `/api/v1/models`) ·
Tavily (`docs.tavily.com`) · Codex CLI (`learn.chatgpt.com/docs`, `github.com/openai/codex`) ·
Proxmox VE 9.2 (`pve.proxmox.com/pve-docs`, Roadmap) · Debian (`debian.org/releases`) ·
Docker (`docs.docker.com/engine/install/debian`) · PostgreSQL
(`postgresql.org/support/versioning`, `hub.docker.com/_/postgres`, `github.com/pgvector/pgvector`) ·
GitHub Pages / PATs / rulesets (`docs.github.com`) · CISA KEV · NVD API 2.0 · cvelistV5 ·
CISA Vulnrichment · FIRST EPSS · OSV · GitHub Advisory DB · MITRE ATT&CK v19.2 ·
MITRE ATLAS · plus live fetches of all ~60 baseline feeds · CYBERCORE CSS · augmented-ui ·
ARWES · world-atlas / Natural Earth · Google Fonts metadata · WCAG 2.2.
