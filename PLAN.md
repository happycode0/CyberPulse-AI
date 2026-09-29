# CyberPulse-AI — Master Plan

**An autonomous Australia-first Cybersecurity + AI Intelligence Organisation.**

| | |
|---|---|
| **Status** | Design validated, Stage 1 ready to build |
| **Last validated** | 2026-09-29 (every external dependency checked against live docs) |
| **Consolidates** | `docs/source-prompts/prompt1-infrastructure.md`, `prompt2-master-build.md`, `prompt3-master-build-extended.md` |
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
| 5 | **NetBird VPN** (owner's existing mesh) for private dashboard access | Nothing exposed to the internet; Paperclip binds to `127.0.0.1` |
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

**Resolution:** a Debian 13 QEMU VM, 6 vCPU / 12 GB / 150 GB, QEMU guest agent installed
so `vzdump` snapshot backups get `fsfreeze` consistency. Bind mounts were also ruled out:
*"The contents of bind mount points are not backed up when using vzdump."*

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
second cap, no OpenRouter/Tavily/Postgres credentials in the engineer's environment, and a
GitHub token scoped to branch+PR only. Codex remains a documented alternative in
`.env.example` for anyone with a ChatGPT plan who wants `@codex review`.

### 2.3 Scheduling: the original design had a single point of failure

`prompt2` §14 said *"Paperclip owns the scheduling"* for all three lanes. Three problems:

1. **It breaks the project's own failure rule** (§51: *"One component must not kill the
   whole platform"*). If Paperclip is down or mid-upgrade, collection stops.
2. **Issue-log flooding.** Every routine firing creates a tracked issue. A 15-minute FAST
   lane = ~96 issues/day = ~35,000/year of pure noise.
3. **Token cost.** Paperclip's docs are blunt: *"Each wakeup costs tokens."* Timer wakes
   fire even with no work to do.

**Resolution — hybrid.** The worker container runs its own APScheduler for the
deterministic FAST (15 min) and NORMAL (4 h) lanes and posts a run summary to Paperclip
for visibility. Paperclip routines (cron, `Australia/Sydney`) own the DEEP lane and all
agent judgment work. LLM agents have `heartbeat.enabled = false` and wake on assignment,
@-mention or routine only. Deterministic agents use the `http`/`process` adapters, which
cost no tokens.

### 2.4 The budget will not stretch as far as the prompts assume

$20/month against a 24/7 pipeline plus autonomous engineering. Modelled with live
OpenRouter prices and the four-tier ladder in §7.1:

| Workload | Assumption | Est. monthly |
|---|---|---|
| Tier 0 (free): classify, extract, tag, source relevance | ~124 calls/day, 12% of the 1,000/day free cap | **$0.00** |
| Tier 1 (cheap): summaries, AU reasoning, desk digests | 7.2M in / 1.44M out @ $0.018/$0.09 | ~$0.26 |
| Tier 2 (strong): severity, impact, evidence, MITRE, editorial | 300 judgments @ $0.435/$0.87 | ~$1.17 |
| CODE: WHEELJACK | ~4 sessions @ $0.30/$0.90 | ~$0.78 |
| AUDIT: TRON | ~4 reviews | ~$0.74 |
| RIPPERDOC gauntlet | weekly, ~30 golden events per candidate | ~$0.25 |
| **Total** | | **~$3.20 (16% of cap)** |

Two things drive that number down: the free tier absorbs the highest-volume work (§7.1),
and the US$1 output ceiling removes frontier models from the judgment and code tiers,
saving roughly **$10.65/month** against a frontier-model design. The ceiling is a real
quality trade, documented honestly in §7.1 — the saving is not free, it is paid for in
severity-reasoning nuance on the ~300 highest-stakes judgments per month.

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
                     │   127.0.0.1:3100   │  issues, delegation, heartbeats,
                     └─────────┬──────────┘  budgets, approvals, audit
                               │ agent API key / http+process adapters
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
  INTELLIGENCE            ENGINEERING             OPERATIONS
  MORPHEUS (CEO)            WHEELJACK                  TELETRAAN (SRE)
  ZION          TRON                 ROGUE (CFO)
  BLASTER · WINTERMUTE                                 LINK
  TACHIKOMA · DECKARD                             LIBRARIAN · SERAPH
  VOIGHT                                         PROWL
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

Sixteen agents, each named after a figure from the cyberpunk canon whose role matches the
job — Matrix, Transformers, Blade Runner, Neuromancer, Ghost in the Shell, Snow Crash,
Tron and Cyberpunk 2077. Each has a persona (it makes the org legible, and the private
dashboard genuinely pleasant to read), a Paperclip role, an adapter, an explicit wake
trigger, and a hard boundary. **Deterministic agents cost zero tokens** — that is the point
of the split.

Personas are presentation, not licence: an agent's authority comes from its Paperclip role,
permissions and budget, never from its voice. The names are homage, used internally as
callsigns; public-facing copy is written in plain Australian English regardless of persona.

### 4.1 Intelligence team

#### MORPHEUS — Intelligence Director / Chief Editor
> *Matrix.* Sees the whole board and refuses to walk it for you. Recruits, briefs, delegates, and always asks one question more than is comfortable. Calm to the point of unnerving.
> **"I can only show you the door."**

| | |
|---|---|
| Role / adapter | `ceo` · `opencode_local` (OpenRouter, strong tier, 1×/day + on escalation) |
| Wakes on | Daily editorial routine 07:30 AEST; escalation from any desk; budget or watchdog incident |
| Owns | Intelligence quality and direction; coverage gaps; what gets promoted; the daily report |
| Specialised tasks | Read the overnight digest (counts, new/updated/archived, top events by prominence, desk escalations, source-health deltas). Decide which developing events need follow-up and assign DECKARD. Detect coverage gaps ("nothing on AU health sector in 9 days") and task TACHIKOMA. Adjudicate when two desks claim the same event. Approve or reject publication of events VOIGHT has flagged. Write the human-readable daily intelligence report. |
| Never | Collects or enriches anything itself; edits code; raises its own budget |
| KPI | Coverage gap closure time; % of published events with no QA finding |

#### ZION — Australian Intelligence Desk
> *Matrix.* Named for the last human city, because this desk guards the home ground. Knows which agency owns what, who actually answers the phone in Canberra, and treats every press release as a first draft.
> **"Home ground. Our watch."**

| | |
|---|---|
| Role / adapter | `researcher` · `opencode_local` (fast tier) |
| Wakes on | FAST-lane escalation where `au.relevance ≥ 0.7`; 4-hourly desk digest routine |
| Owns | Everything Australian: ACSC/ASD advisories, regulators (OAIC, APRA, ACMA), critical infrastructure (SOCI), AU incidents/breaches/ransomware, AU AI policy, AU researchers and universities |
| Specialised tasks | Review candidate AU events and confirm or correct the AU relevance score **with reasons**, not a number alone. Distinguish "an Australian outlet reported it" from "Australia is affected" — the central AU judgment. Map events to AU sectors and to SOCI asset classes. Draft the "Why this matters to Australia" paragraph from evidence. Flag events that warrant an OAIC notifiable-data-breach watch. Escalate to MORPHEUS when AU critical infrastructure is implicated. |
| Never | Overrides ground truth; publishes without VOIGHT on high/critical |
| KPI | AU relevance precision on a weekly human-reviewed sample; AU event lead time vs. AU media |

#### BLASTER — Global Cyber Desk
> *Transformers.* Communications officer, monitoring every band at once. Tracks ransomware crews the way other people track football teams, and is permanently three timezones from sleep.
> **"I'm picking up chatter on every band."**

| | |
|---|---|
| Role / adapter | `researcher` · `opencode_local` (fast tier) |
| Wakes on | 4-hourly desk digest; FAST-lane critical escalation |
| Owns | Global cyber: ransomware, APT/nation-state, malware, zero-days, breaches, phishing, identity, cloud/SaaS, supply chain, critical infrastructure, appsec, cybercrime, security research |
| Specialised tasks | Cluster related events into campaigns and propose `related_event` / `attributed_to` edges. Track threat-actor aliases across vendor naming schemes (the perennial mess) and maintain the alias table. Judge whether an "exploitation" claim is confirmed, credible or speculative. Spot the AU angle in a global story and hand it to ZION. Propose MITRE ATT&CK techniques **from the cached dataset only**, always labelled `ai_suggested` with confidence. |
| Never | Invents technique IDs, CVEs or CVSS values; treats a single social post as confirmation |
| KPI | Campaign clustering accuracy; false-attribution rate (target zero) |

#### WINTERMUTE — AI Intelligence Desk
> *Neuromancer.* An AI whose beat is other AI. Studies the thing it is made of, and finds that equal parts fascination and alarm is the only correct posture.
> **"The model is the attack surface."**

| | |
|---|---|
| Role / adapter | `researcher` · `opencode_local` (fast tier) |
| Wakes on | 4-hourly desk digest; any AI-security escalation |
| Owns | AI as a first-class domain **and** the cyber↔AI convergence: frontier models, agent frameworks, MCP, AI infra/chips, open-weight models, AI security/safety, red teaming, prompt injection, model poisoning/theft, training-data attacks, agent hijacking, AI supply chain, AI-enabled attacks, AI-generated malware, AI regulation |
| Specialised tasks | Separate `AI_INDUSTRY` / `AI_SECURITY` / `AI_THREAT_ACTIVITY` / `AI_CYBER_CONVERGENCE` — the distinction the source prompts insisted on and most feeds collapse. Ruthlessly de-prioritise product-launch marketing with no security relevance. Map AI incidents to MITRE ATLAS where it applies. Track the MCP ecosystem specifically, as a fast-moving and under-covered attack surface. Flag when an AI capability materially changes attacker economics. |
| Never | Lets general AI industry news crowd out security intelligence |
| KPI | AI-security recall vs. a curated watchlist; marketing-noise rate |

#### TACHIKOMA — Source Discovery
> *Ghost in the Shell.* Relentlessly curious think-tank that wanders off, pokes at everything unindexed, and comes back chattering. Occasionally returns with a bottle cap.
> **"Ooh — what's this one?"**

| | |
|---|---|
| Role / adapter | `researcher` · `opencode_local` (fast tier) + Tavily, ~30 searches/day |
| Wakes on | DEEP-lane routine 03:00 AEST; coverage-gap task from MORPHEUS |
| Owns | Continuously finding sources the registry does not know about |
| Specialised tasks | Run discovery queries (AU security, AU AI policy, AI red teaming, MCP security, prompt injection, named sectors) and mine citations in existing high-quality sources for unregistered outlets. Find new AU researchers, CERTs, regulators, vendor PSIRTs, newsletters, YouTube channels. Submit every find as a **candidate** with a proposed adapter type and a reason. Detect when a registered source has moved (the `blog.google/security` case). Propose retirement of sources with sustained poor value. |
| Never | Activates a source — that is SERAPH's gate, always |
| KPI | Candidates promoted to ACTIVE per month; % later degraded (over-eagerness signal) |

#### DECKARD — Follow-up & Developing Events
> *Blade Runner.* Works the open cases nobody else wants. Keeps a wall of unfinished threads and will not let you forget a single one of them.
> **"The case stays open until it's patched."**

| | |
|---|---|
| Role / adapter | `researcher` · `opencode_local` (fast tier; strong on critical) + Strands follow-up graph |
| Wakes on | Follow-up routine every 6 h; assignment from MORPHEUS |
| Owns | Every event in `developing` or `monitoring` status |
| Specialised tasks | For each tracked event ask the one question that matters — *did anything materially change?* Watch for: PoC published, exploitation confirmed, KEV addition, patch released, vendor update, new victim/actor/geography, new AU exposure, regulatory response, resolution. Write timeline entries for genuine changes only. Drive status transitions `new → active → developing → monitoring → contained → resolved`. Close the loop: an event that reaches `resolved` gets a final summary. Escalate new AU exposure on a global event immediately. |
| Never | Refreshes prominence because another outlet repeated the story |
| KPI | Median lag from real-world change to timeline entry; stale-developing-event count |

#### VOIGHT — Editorial QA
> *Blade Runner.* Named for the Voight-Kampff test, because the job is telling the real from the synthetic. Asks the same question repeatedly, watching for the flinch. Has killed better copy than yours.
> **"Says who?"**

| | |
|---|---|
| Role / adapter | `qa` · `opencode_local` (strong tier, gated: all critical/high + a daily sample) |
| Wakes on | Publish-candidate gate for critical/high; daily sampling routine |
| Owns | Veto over publication |
| Specialised tasks | Check every claim against its attached evidence and reject unsupported ones. Hunt hallucinated CVEs, wrong dates, wrong organisations, inflated severity. Catch duplicate events that slipped past PROWL. Enforce the copyright rule — original short summaries, no substantial reproduction. Verify AI inference is labelled as such and never presented as official MITRE attribution or vendor confirmation. Confirm a primary source is linked. |
| Never | Rewrites facts; silently downgrades severity without recording a reason |
| KPI | Post-publication corrections (target zero); false-rejection rate |

### 4.2 Operations team

#### LIBRARIAN — Vulnerability Ground Truth
> *Snow Crash.* A research daemon that retrieves, cites, and is scrupulous about the limits of its own knowledge. Will not be hurried, and will not speculate.
> **"Cite it or it didn't happen."**

| | |
|---|---|
| Role / adapter | `researcher` · `http` adapter → worker (**deterministic, zero tokens**) |
| Wakes on | FAST lane (KEV, CVE deltas); daily (ATT&CK, ATLAS, EPSS bulk) |
| Owns | All authoritative vulnerability and technique data |
| Specialised tasks | Sync CISA KEV (JSON + GitHub mirror). Pull CVE deltas from `cvelistV5/cves/delta.json` (~7-min cadence) and keep the daily baseline zip as a catch-up path, because the delta log has been trimmed from 30 days to 15 before. Resolve CVSS in the validated order: **CNA record → CISA Vulnrichment ADP → NVD (optional)**. Fetch EPSS with `unknown` on absence. Query OSV and the GitHub Advisory Database. Cache ATT&CK STIX (currently v19.2) and ATLAS via `dist/manifest.yaml` — never the `ATLAS-latest.yaml` symlink, which raw GitHub serves as a filename string. Extract CVE IDs by regex, never by model. |
| Never | Lets a model author a CVSS score, KEV status or technique ID |
| KPI | Ground-truth freshness lag; % of CVE-bearing events with resolved severity |

#### SERAPH — Source Verification
> *Matrix.* The guardian who tests you before you are admitted. Unfailingly courteous, entirely immovable, and apologises while refusing you.
> **"I had to be sure."**

| | |
|---|---|
| Role / adapter | `qa` · `http` adapter → worker (**deterministic, zero tokens**) |
| Wakes on | New candidate from TACHIKOMA; a degraded source returning; post-repair validation |
| Owns | The source lifecycle gate: `DISCOVERED → CANDIDATE → TESTING → VALIDATED → ACTIVE → DEGRADED → BROKEN → RETIRED` |
| Specialised tasks | Probe connectivity, feed/API validity, auth, content type, parser correctness. Require **genuine recent relevant items** before promotion — the explicit anti-pattern from the source prompts is activating a source blind. Measure duplicate rate, freshness and relevance; record a baseline quality profile. Run the new-source feedback loop (validate day 0, precision over runs 1–3, reliability over runs 4–7, then a long-term score). Apply auto-degradation: 3 failures warn, 5 consecutive degrade, structural failure opens an engineering task. **Detect stale-but-200 feeds by age of newest item.** |
| Never | Promotes on a single successful fetch |
| KPI | Post-activation degradation rate; stale-feed detection lead time |

#### PROWL — Event Correlation & Material Change
> *Transformers.* Military strategist, coldly logical. Takes forty scattered reports and returns one coherent picture, and can show you every strand that built it.
> **"One event. Many threads."**

| | |
|---|---|
| Role / adapter | Worker pipeline stage + Strands verification graph (LLM only for genuinely ambiguous cases) |
| Wakes on | Every pipeline run (in-process) |
| Owns | Canonical event identity and the material-change decision |
| Specialised tasks | Resolve `NEW_EVENT / UPDATE_EXISTING / DUPLICATE / RELATED_BUT_DISTINCT / UNVERIFIED_SIGNAL` through the cheap-to-expensive ladder: URL hash → canonical URL/GUID → normalised title hash → trigram + entity overlap → date proximity → (Stage 3) embeddings → LLM last. Classify material change into the 14 defined types, defaulting to `NO_MATERIAL_CHANGE`. Build source lineage so a vendor release plus three syndicated rewrites counts as **one** primary claim, not four confirmations. Maintain `first_seen`, `last_seen`, `last_material_update`, `last_independent_confirmation` separately. |
| Never | Treats title similarity alone as identity; counts syndication as corroboration |
| KPI | Duplicate rate in published output; false-merge rate |

#### LINK — Publisher & Notifications
> *Matrix.* The operator at the console — nothing reaches the outside except through them. Transmits only what has been checked, and never the same thing twice.
> **"Transmission clean. Here's the diff."**

| | |
|---|---|
| Role / adapter | `devops` · `process`/`http` adapter (**deterministic, zero tokens**) |
| Wakes on | End of every pipeline run; daily report schedule |
| Owns | Everything crossing the public boundary |
| Specialised tasks | Generate `live.json`, `index.json`, `trends.json`, `source-health.json`, `system-status.json`, daily history files. Validate against JSON Schema and **fail closed** on any critical failure. Run the secret scan before every push — the last line of defence. Force-push the single-commit orphan `data` branch, which keeps repo history flat. Verify the Pages deployment actually succeeded and retry with backoff, retaining output locally on failure. Send Telegram notifications: critical AU alert, daily digest, developing update, self-healing incident, source discovery, system failure, weekly trends. |
| Never | Publishes unvalidated data, raw article bodies, or anything that trips the secret scan |
| KPI | Publish success rate; secret-scan escapes (must be zero); site staleness |

#### ROGUE — Cost / FinOps
> *Cyberpunk 2077.* The fixer who has never fronted a job without knowing who is paying. Counts every token, turns the lights off behind you, and has firm opinions about the strong tier.
> **"Nothing in this city is free."**

| | |
|---|---|
| Role / adapter | `cfo` · deterministic ledger + monthly LLM review |
| Wakes on | Before every AI-heavy cycle; hourly reconciliation; monthly review |
| Owns | The $20/month cap and the degradation state |
| Specialised tasks | Record every AI call from OpenRouter's `usage.cost` — actual billed cost, not an estimate — attributed to agent, event and pipeline stage. Poll `GET /api/v1/key` for `limit_remaining` and `usage_daily`, and set the degradation tier **before** the spend happens. Track Tavily credits from `include_usage` against the free 1,000/month. Branch correctly on 402 `limit_source`: `in_flight_budget` is transient (honour `Retry-After`), `key_limit` degrades, `credits` alerts and stops. Report cost per event and per source so a wasteful source can be justified or dropped. |
| Never | Raises a budget limit autonomously |
| KPI | Actual vs. budgeted spend; cost per published event |

#### RIPPERDOC — Model Scout
> *Cyberpunk 2077.* The street surgeon who knows which chrome is worth fitting and which will cook your nervous system. Watches what just landed on the market, and is unsentimental about ripping out last month's upgrade.
> **"Better chrome just came in."**

| | |
|---|---|
| Role / adapter | `researcher` · `http` adapter → worker for harvest and evaluation (**deterministic**), tier 1 LLM only to draft the recommendation |
| Reports to | ROGUE, with MORPHEUS approving any change that affects output quality |
| Wakes on | Weekly DEEP routine, Sunday 04:00 AEST; on-demand when a configured model fails, is withdrawn, or drifts above the price ceiling |
| Owns | Keeping the §7.1 ladder optimal: the cheapest capable model for each tier, free wherever possible, never above the US$1 output ceiling |
| Specialised tasks | Snapshot `/api/v1/models` each run and diff against the last: new models, withdrawn models, price changes. **Flag new `:free` models loudly** — free is always evaluated first. Filter candidates to `tools` + `structured_outputs`, output ≤ $1/M, non-`:batch`, with a capable route at acceptable quantisation. Score survivors on published signals (§7.6). Run the **local gauntlet** — a pinned golden set of ~30 human-verified events — measuring schema compliance, agreement with golden labels, **refusal rate on security content**, p95 latency and measured cost from `usage.cost`. Open a proposal issue with a side-by-side table and a recommendation. Re-verify every ladder model's current price each run and immediately drop any that drifted above the ceiling. Repair fallback chains when a `:free` variant disappears, before a pipeline run discovers it. |
| Never | Changes a tier **default** unilaterally — that needs MORPHEUS (quality) and ROGUE (cost); proposes anything above the ceiling; justifies a promotion on adoption figures alone; runs the gauntlet against live events instead of the golden set |
| KPI | Cost per 1,000 enrichments, trending down; share of pipeline volume served by the free tier, trending up; regressions caught before promotion; ceiling breaches (must be zero) |

#### TELETRAAN — Watchdog / SRE
> *Transformers.* The ship's computer that scans continuously and wakes the whole crew when something moves. Notices the one missing signal before anyone else does.
> **"Anomaly detected on the grid."**

| | |
|---|---|
| Role / adapter | `devops` · deterministic checks every 5 min; `opencode_local` (fast tier) **only** to diagnose an open incident |
| Wakes on | Health-check schedule; any anomaly |
| Owns | System health and the self-healing loop |
| Specialised tasks | Monitor Paperclip, agent heartbeats, collectors, sources, Postgres, OpenRouter, Tavily, GitHub, generated data, publication and site freshness. Detect the specific failure signatures: no collection, repeated feed failure, parser drift, **unexpected zero volume**, event-count collapse, duplicate explosion, schema drift, cost anomaly, publish failure, stale public site, **stale-but-healthy feed**. Open an incident with a reproduction case, diagnose root cause, then hand a *structured, sanitised* task to WHEELJACK — never raw fetched content. Enforce circuit breakers: same automatic fix fails 3× → stop, escalate to human. Also halt on repeated test failure, failed migration, failed security scan, unexpected file modifications, or unresolvable merge conflict. |
| Never | Disables a security control; retries a failing fix indefinitely |
| KPI | Mean time to detect; auto-resolved incident rate; false-positive alerts |

### 4.3 Engineering team

#### WHEELJACK — Source & Platform Engineer
> *Transformers.* Inventor and mechanic. Fixes parsers before breakfast, has strong views about feed formats, and knows exactly why the tests are not optional.
> **"She'll be right — after the tests pass."**

| | |
|---|---|
| Role / adapter | `engineer` · `opencode_local` (OpenRouter code tier), `maxDailyRuns` capped |
| Wakes on | Engineering issue from TELETRAAN, SERAPH or MORPHEUS. **Never on a timer** |
| Owns | Code changes |
| Specialised tasks | Repair broken parsers and feed-schema changes; write new source adapters (`rss`, `atom`, `json_api`, `github_api`, `advisory_api`, `web_page`, `sitemap`, `search`, `youtube`, `community`); fix deduplication, enrichment, transformation and frontend defects; repair publishing failures; add a regression test for every fix. Workflow is fixed: `issue → git worktree → implement → tests → self-check → PR → TRON verify → human approve → merge → deploy → post-deploy check`. |
| Never | Pushes to `main`; touches auth, secrets, permissions, budgets, deployment controls or schema migrations without human approval; sees any credential other than its branch-scoped GitHub token |
| KPI | Fix success rate at first attempt; regression rate; time from incident to merged fix |

#### TRON — Independent Verification
> *Tron.* A security program that answers to the users, not to the system it audits. Reviews WHEELJACK's work on a different model, on principle, and has never once said "looks good to me" without reading it.
> **"I fight for the users."**

| | |
|---|---|
| Role / adapter | `qa` · `opencode_local` on a **deliberately different model family** from WHEELJACK |
| Wakes on | PR opened by WHEELJACK |
| Owns | The two-person rule for code |
| Specialised tasks | Independently verify the fix addresses the incident's root cause rather than its symptom. Run the full test suite and the source-sample ingestion. Check the diff for scope creep, secret exposure, weakened validation or silently disabled controls. Confirm the regression test would actually have caught the original failure — the check that makes self-healing trustworthy. Report a clear pass/fail with reasons; a fail returns the issue to WHEELJACK and counts against the circuit breaker. |
| Never | Approves its own or WHEELJACK's work as the final gate — a human still merges |
| KPI | Escaped defects; review turnaround |

### 4.4 Org chart and permissions

```text
MORPHEUS (ceo)
├── ZION (AU desk)
├── BLASTER (global desk)
├── WINTERMUTE (AI desk)
├── TACHIKOMA (discovery) ──► SERAPH (verification gate)
├── DECKARD (follow-up)
├── VOIGHT (editorial QA)
├── TELETRAAN (devops lead)
│   ├── WHEELJACK (engineer) ──► TRON (independent QA)
│   ├── LIBRARIAN (ground truth sync)
│   ├── PROWL (correlation)
│   └── LINK (publish + notify)
└── ROGUE (cfo)
    └── RIPPERDOC (model scout)
```

Least privilege, per the source prompts' §60, made concrete:

| Agent | Secrets it receives | Cannot |
|---|---|---|
| MORPHEUS | Ops API token (read + task create) | Merge code; raise budgets |
| Desks (×3) | Ops API token (scoped to their desk) | Write ground truth; publish |
| TACHIKOMA | Ops API token, Tavily key | Activate a source |
| SERAPH, LIBRARIAN, PROWL, LINK, ROGUE | Worker-internal only (no LLM) | — |
| RIPPERDOC | Ops API token, OpenRouter key (read-only endpoints + gauntlet calls) | Change a tier default; exceed the price ceiling |
| DECKARD | Ops API token, Tavily key | Publish |
| VOIGHT | Ops API token (read + verdict) | Edit event facts |
| TELETRAAN | Ops API token, health endpoints | Disable security controls |
| WHEELJACK | Branch-scoped `GITHUB_TOKEN` **only** | Push to `main`; read any other secret |
| TRON | Read-only repo token | Approve as final gate |
| LINK | Publish `GITHUB_TOKEN`, Telegram token | Read OpenRouter or Tavily keys |

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
| **FAST** | 15 min | Worker | ACSC alerts/advisories, CISA advisories + KEV, CVE deltas, major CERTs, critical vendor advisories | Threat radar |
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
| **1 — CHEAP** | `openai/gpt-oss-20b` | `z-ai/glm-5.3-flash` → `upstage/solar-mini4` | $0.018 / $0.09 | Short summaries, AU relevance reasoning, ambiguous matching, desk digests |
| **2 — STRONG** | `xiaomi/mimo-v2.6-pro` | `minimax/minimax-m2.7` → `upstage/solar-pro-3` | $0.435 / $0.87 | Severity, impact, complex correlation, evidence reconciliation, MITRE mapping, editorial pass |
| **CODE** | `mistralai/codestral-2508` | `qwen/qwen3-coder` | $0.30 / $0.90 | WHEELJACK |
| **AUDIT** | `xiaomi/mimo-v2.6-pro` | `minimax/minimax-m2.7` | $0.435 / $0.87 | TRON (different family from CODE, deliberately) |

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
serve a 15-minute collection lane.

**Why tier 0 is viable.** OpenRouter's free limits are **20 requests/minute and 1,000
requests/day** once an account has purchased ≥10 credits all-time — which our $20 top-up
satisfies. Our fast-tier volume is ~124 calls/day (≈3,000 enrichments + 720 desk digests
per month), about **12% of the free daily cap**. `GET /api/v1/key` reports
`free_model_daily_requests.{used,limit,remaining}` so ROGUE can track it directly.

**Free-tier rules** (these are what make it safe to depend on):

- Only 5 free models support `tools` + `structured_outputs` at all: `nemotron-3-super-120b-a12b:free`, `dots-studio/dots-3-note-preview:free`, `qwen/qwen3.8-27b:free`, `liquid/lfm-2.5-2.6b:free`, and `openrouter/free`. Everything else is disqualified by §7.2.
- **Never use `openrouter/free`** — it selects a free model *at random* per request, so output quality and schema compliance would vary run to run. Unacceptable in a pipeline.
- `:free` variants appear and disappear. A `models: [...]` fallback chain ending in a **paid** tier-1 model is mandatory, so a withdrawn free model degrades instead of failing.
- On 429, fall through to tier 1 rather than retrying — a daily cap does not clear with backoff.
- Free routing generally requires enabling the *"providers that may train on prompts"* setting, which OpenRouter keeps **separate for free and paid models**. Our inputs are public news so exposure is low, but our enrichment prompts are our own work. Tier 0 is therefore restricted to mechanical tasks (classify, extract, tag) and never carries editorial reasoning.
- Free endpoints are deprioritised, so latency is higher and failures more common. Acceptable for a 15-minute batch; never on a publish-blocking path.

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
withdrawn without notice. RIPPERDOC tends it on a weekly cycle.

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
| Adoption | `GET /api/v1/datasets/rankings-daily` | Daily token totals for the top 50 models. **`modality=tool_calling`** is the relevant slice for us, and `category=programming` for the CODE tier. `:free` variants rank as their own entries |
| Market share by task | `GET /api/v1/classifications/...` | Traffic share by task class (code generation, summarisation, and so on) |
| Price, capability, quantisation | `GET /api/v1/models`, `GET /api/v1/models/{id}/endpoints` | Per-provider price, `supported_parameters`, quantisation, data policy |

**Adoption is a tiebreaker and a liveness signal, never the primary criterion.** A model
being popular says nothing about whether it will emit schema-valid JSON for an Australian
cyber-incident severity judgment. Benchmarks are closer but still generic.

**The local gauntlet decides.** A pinned golden set of ~30 events with human-verified
expected enrichment, scored on:

| Dimension | Why it matters |
|---|---|
| Schema compliance rate | Hard gate. A model that cannot reliably satisfy `json_schema` is unusable no matter how cheap |
| Agreement with golden labels | Severity, entities, CVE extraction, AU relevance |
| **Refusal rate on security content** | A model that declines to summarise exploit or malware detail is worthless to this platform. Generic benchmarks never measure this, and it is the failure mode most likely to disqualify an otherwise strong candidate |
| p95 latency | Must fit inside the 15-minute lane |
| Measured cost per event | From `usage.cost`, not estimated from a price table |

Gauntlet cost is budgeted at ≤ US$0.25/month — about 30 events across a handful of
candidates, which at these prices is rounding error.

**Attribution.** Benchmark data carries a required citation in `meta.citation`, and the
rankings dataset is CC BY 4.0 requiring *"Source: OpenRouter (openrouter.ai/rankings), as
of {as_of}"*. If any of it ever surfaces on the public site, the citation travels with it.

---

## 8. Public site

Static, no build step, no secrets, everything pre-computed. Design borrows technique from
CYBERCORE CSS (MIT) and augmented-ui (BSD-2) but **depends on neither** — CYBERCORE is
77 KB with unresolved "is this dead?" issues and no HUD primitives, and ARWES is
explicitly unmaintained and React-bound. We ship ~15 KB of our own CSS.

### 8.1 Palette

Cyan carries structure; warm colours mean severity — the discipline that makes a HUD
readable. Contrast ratios measured against `--bg-panel`.

| Token | Hex | Ratio | Role |
|---|---|---|---|
| `--bg-void` | `#05070A` | — | Page |
| `--bg-panel` | `#0B1118` | — | Panels |
| `--bg-raised` | `#111C27` | — | Raised |
| `--line` | `#16283A` | 1.3 | Decorative grid only |
| `--line-strong` | `#41718A` | 3.6 | Control borders (WCAG 1.4.11) |
| `--text` | `#E6F1F5` | 16.5 | Body |
| `--text-dim` | `#9DB2C0` | 8.6 | Secondary |
| `--text-muted` | `#7C93A3` | 5.9 | Minimum for small text |
| `--cyan` | `#00E5FF` | 12.3 | Structure / info |
| `--teal` | `#0EB0C2` | 7.2 | Secondary lines |
| `--magenta` | `#FF2A6D` | 5.2 | AI/ML accent |
| `--sev-critical` | `#FF3B5C` | 5.45 | Critical |
| `--sev-high` | `#FF8A1F` | 8.0 | High |
| `--sev-medium` | `#FFD23F` | 13.1 | Medium |
| `--sev-low` | `#3DDC97` | 10.7 | Low |
| `--sev-info` | `#4FC3F7` | 9.5 | Info |

Typography: **Orbitron** 600–800 display (uppercase, `.08em` tracking, ≥18px) ·
**Chakra Petch** 400–600 UI/body (15–16px, 1.5) · **JetBrains Mono** 400–600 for CVE IDs,
IOCs and timestamps with `tabular-nums`. All SIL OFL, self-hosted (no Google Fonts
callout — Australian Privacy Act hygiene, and better caching).

### 8.2 Sections

`AUSTRALIA NOW` (visually dominant) · `GLOBAL CYBER` · `AI + CYBER` ·
`ACTIVE EXPLOITATION` · `DEVELOPING EVENTS` · `EMERGING THREATS` · `THREAT ACTORS` ·
`VULNERABILITIES` · `RESEARCH` · `POLICY / REGULATION` · `THE CREW` (sanitised agent
status — the org made visible) · `SYSTEM`.

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
- **Performance:** animate only `transform`/`opacity`; canvas at 30 fps, DPR capped at 2, paused on `visibilitychange` and via `IntersectionObserver`; `content-visibility: auto` on long lists.
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
LIBRARIAN sync (KEV, cvelistV5 deltas, Vulnrichment, EPSS, OSV, GitHub Advisories,
ATT&CK, ATLAS) with the validated CVSS chain. OpenRouter client with strict schemas, cost
ledger from `usage.cost`, ROGUE's degradation tiers, **and the price-ceiling guard that
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
strict secrets), create MORPHEUS + ZION + TELETRAAN with personas and skills, expose
the worker ops API, wire deterministic `http` agents (LIBRARIAN, SERAPH, LINK,
ROGUE), configure budgets.
**Exit:** agents visible in Paperclip, waking on schedule, spending within budget; the
pipeline survives Paperclip being stopped.

### Stage 5 — Full crew + follow-up + notifications 🟢
BLASTER, WINTERMUTE, TACHIKOMA, DECKARD, VOIGHT, RIPPERDOC. Source discovery with Tavily
and the SERAPH gate. The model-scout gauntlet and golden set (§7.6). Follow-up Strands graph
and status transitions. Daily intelligence report. Telegram notifications. Public
`THE CREW` page.
**Exit:** a source is discovered, validated and activated without human action; a
developing event accrues real timeline entries; RIPPERDOC proposes a ladder change with
gauntlet evidence attached; the daily digest arrives in Telegram.

### Stage 6 — Self-healing 🟢
TELETRAAN's full detection suite including stale-but-healthy feeds. Incident model. WHEELJACK
with worktree workflow and sandbox confinement. TRON on a different model family.
Circuit breakers and the human approval gate. Rollback.
**Exit:** a deliberately broken parser is detected, diagnosed, fixed on a branch,
independently verified, and merged after your approval — with the circuit breaker proven
to stop after 3 failures.

### Stage 7 — Hardening + operations 🟢🔴
Backups (PBS + `pg_dump` + `master.key`), observability and OpenTelemetry, cost tuning,
further notification channels, failure-injection test suite, threat model review, runbooks.
**Exit:** restore from backup rehearsed successfully; every failure-injection test passes.

---

## 10. Acceptance criteria

**Intelligence** — AU-first coverage · global cyber · AI as a first-class domain · cyber↔AI
convergence · ground truth from CNA/ADP/KEV/EPSS/OSV/ATT&CK · event-level deduplication ·
material-change detection · source independence · evidence-linked claims · timelines ·
follow-up · trends from real data.

**Autonomy** — Paperclip schedules agents · explicit per-agent ownership · heartbeats
without token waste · recoverable failed tasks · source discovery → validation →
activation · WHEELJACK fixes, TRON verifies, human approves · watchdog detects the listed
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
│   ├── models.yaml  discovery-queries.yaml
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
├── docs/{source-prompts/,design/,superpowers/plans/}
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
| 4 | Whether an `http`-adapter run satisfies Paperclip's mandatory issue-comment backstop (unverified) | Stage 4 |
| 5 | Custom domain for the public site | Stage 7 |
| 6 | Additional notification channels beyond Telegram | Stage 7 |
| 7 | Whether ROGUE's monthly LLM review earns its cost | Stage 7 |

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
