Yes. For the system you've described, I would **keep everything on one dedicated Proxmox LXC for v1**.

You do **not** need Kafka, ClickHouse, Redis, Kubernetes, or multiple VMs at this stage.

Paperclip itself is relatively lightweight at the control-plane level: the current stack is Node.js/Express + React with PostgreSQL, and its Docker setup can run as a single container with an embedded PostgreSQL database. ([GitHub][1])

## What I would actually deploy

```text
PROXMOX
└── LXC: cyber-intelligence
    │
    ├── Docker
    │
    ├── paperclip
    │    ├── UI
    │    ├── API
    │    ├── agent orchestration
    │    └── Codex/adapter processes
    │
    ├── postgres
    │    ├── paperclip DB
    │    └── cyber_intel DB
    │
    └── cyber-worker
         ├── Python collectors
         ├── Strands workflows
         ├── event processing
         ├── CVE/KEV/MITRE
         ├── Tavily
         ├── OpenRouter
         └── GitHub publisher
```

I would keep these as **separate Docker containers but inside one LXC**.

That gives you isolation without turning the Proxmox environment into a zoo of VMs/LXCs.

---

# My recommended LXC size

### Start with:

| Resource    |               Recommendation |
| ----------- | ---------------------------: |
| CPU         |                   **4 vCPU** |
| RAM         |                     **8 GB** |
| Disk        |               **100 GB SSD** |
| Network     | 1 Gb/s virtual NIC is plenty |
| Swap        |                   **2–4 GB** |
| OS          |    Debian 12 or Ubuntu 24.04 |
| Docker      |                          Yes |
| LXC nesting |                          Yes |

I'd call this the **sweet spot for v1**.

### If you want plenty of headroom:

| Resource | Comfortable |
| -------- | ----------: |
| CPU      |  **6 vCPU** |
| RAM      |   **12 GB** |
| Disk     |  **150 GB** |

I would personally choose **6 vCPU / 12 GB / 150 GB** if your Proxmox host has room.

---

# Why the RAM requirement isn't huge

The important point is:

**you aren't running the AI models locally.**

OpenRouter is doing the model inference.

So the LXC is mainly doing:

```text
HTTP/RSS/API requests
database operations
Python processing
agent orchestration
Git operations
Codex CLI
Strands workflows
JSON generation
GitHub publishing
```

That's substantially lighter than running LLMs locally.

The biggest temporary RAM consumer is likely to be **Codex + tests/build tools**, not the news collector itself.

Paperclip's Docker image can run Codex and Claude Code adapter processes, and its documentation also provides an isolated PR-review container for untrusted review work. ([GitHub][2])

---

# Rough RAM picture

These are my planning estimates, **not vendor minimums**:

```text
Debian / OS                    ~0.5 GB
Paperclip                      ~0.8–1.5 GB
PostgreSQL                     ~0.5–1.5 GB
Python worker                  ~0.5–1.5 GB
Strands/research workloads     ~0.3–1.0 GB
Codex active                   ~1–3+ GB
temporary processes/browser    ~0.5–2 GB
------------------------------------------------
Normal operation               ~3–5 GB
Busy operation                 ~5–7 GB
```

That's why:

**8 GB should work.**

But:

**12 GB gives you much nicer headroom.**

---

# Do we need a database?

### Yes.

I would absolutely use PostgreSQL.

And this is one area where I'd modify the original design slightly.

Paperclip already uses PostgreSQL and supports an embedded database mode, so technically you could run the whole thing with a single Paperclip container and no separate database container. ([GitHub][3])

But for **your final architecture**, I'd use:

```text
PostgreSQL container
       │
       ├── database: paperclip
       │
       └── database: cyber_intel
```

One PostgreSQL instance.

Two databases.

That is simple and clean.

---

# What goes into PostgreSQL?

The `cyber_intel` database would hold:

```text
events
event_sources
event_timeline
claims
evidence
cves
mitre_techniques
organisations
products
threat_actors
countries
sectors
source_registry
source_health
source_lineage
runs
trends
cost_metrics
followup_tasks
```

This is where your system's **long-term memory** lives.

For example:

```text
Event 123
   │
   ├── 7 source reports
   ├── 2 CVEs
   ├── 5 evidence records
   ├── 3 timeline updates
   ├── 1 threat actor
   ├── AU relevance = 0.92
   └── status = developing
```

That's exactly the sort of relational data PostgreSQL is good at.

---

# What should NOT go into PostgreSQL?

Don't shove everything into the DB.

I would use the filesystem for:

```text
raw feed cache
temporary HTML
transcripts
debug logs
generated JSON
MITRE STIX cache
large research responses
Codex workspaces
```

Conceptually:

```text
PostgreSQL
    = structured intelligence

Filesystem
    = temporary/raw/supporting data
```

That keeps the DB small and fast.

---

# Do we need ClickHouse?

### No — not initially.

For this project, I would **not use ClickHouse**.

Your likely data volume isn't remotely large enough to justify the additional complexity.

Even something like:

```text
100 sources
×
20 items/day
=
2,000 raw items/day
```

is trivial for PostgreSQL.

And after deduplication, your canonical event count should be much smaller.

Even:

```text
100,000
500,000
1,000,000 events
```

is still a very normal PostgreSQL workload for this application.

ClickHouse becomes interesting later if you decide you want massive analytical workloads across:

```text
billions of observations
long-term telemetry
source behaviour analytics
full event history
large-scale time series
```

That's not where I'd start.

---

# Do we need Redis?

### No.

Not for v1.

Paperclip handles its own orchestration/task concepts, and your Python service can work directly with PostgreSQL.

Don't add Redis just because "agent platforms usually have Redis."

You don't currently need it.

---

# Do we need Kafka?

### Absolutely not for v1.

Kafka would be massive overkill here.

Your architecture is more like:

```text
source
 ↓
collector
 ↓
Postgres
 ↓
agent task
 ↓
event processing
 ↓
publish
```

rather than:

```text
millions of events/sec
```

---

# Do we need a vector database?

### Also no.

I would **not install Qdrant, Milvus, Weaviate, Pinecone, etc.**

For event matching:

```text
exact hash
→ normalised title
→ entities
→ token similarity
→ optional embedding
```

You can initially use PostgreSQL plus a simple embedding mechanism if needed.

Postgres can also support vector search later without introducing an entirely separate infrastructure component.

I'd postpone that until you actually demonstrate that normal event resolution isn't good enough.

---

# What about raw article storage?

This is one thing I'd deliberately control.

Don't save entire articles forever.

Instead:

```text
fetch
 ↓
extract metadata
 ↓
extract relevant evidence
 ↓
AI summary
 ↓
store structured record
 ↓
discard/expire raw fetch
```

Keep raw source material temporarily for debugging/verification.

This means your disk requirement stays small.

---

# Disk sizing

I'd use:

### 100 GB minimum

Something like:

```text
OS + Docker                 10–15 GB
Paperclip                   5–10 GB
Postgres                    10–20 GB
application                 5–10 GB
cached/raw data             10–20 GB
Codex workspaces            10–20 GB
logs                        5–10 GB
headroom                    20–30 GB
```

That gives you plenty of room.

### 150 GB preferred

Especially because your system is supposed to **self-repair**, meaning I'd rather have generous workspace/log capacity than constantly clean it up.

If later you start storing lots of:

* YouTube transcripts
* PDF research
* raw web captures
* historical source payloads

then I'd move the raw archive onto larger storage rather than increasing the whole LXC dramatically.

---

# One LXC or multiple LXCs?

For v1:

## One LXC

I'd do:

```text
LXC 200
Cyber Intelligence
6 CPU
12 GB RAM
150 GB disk
```

Inside:

```text
Docker
│
├── paperclip
├── postgres
└── cyber-worker
```

That's my preferred architecture.

### Why?

Because the components are tightly related:

```text
Paperclip
   ↕
agents
   ↕
Python
   ↕
Postgres
   ↕
GitHub
```

Splitting these across three LXCs gives you more networking, firewalling, maintenance and backup complexity without a meaningful benefit at this scale.

---

# The one exception: Codex security

There is one place where isolation is worth considering.

Your Codex agent will eventually be doing:

```text
git clone
code execution
tests
package installation
building
possibly modifying source
```

Paperclip itself documents an isolated container workflow specifically for untrusted PR review. ([GitHub][4])

So I'd design the LXC like this:

```text
LXC
│
├── Paperclip container
│
├── PostgreSQL container
│
├── Intelligence worker
│
└── Codex sandbox container
```

Still **one LXC**.

But Codex doesn't necessarily get unrestricted access to everything else.

That is a very good security boundary.

---

# And Strands doesn't require another server

This is important.

You don't need:

```text
Strands LXC
Strands VM
Strands server
Strands database
```

Strands is just part of the Python worker.

For example:

```text
cyber-worker
│
├── collectors
├── normaliser
├── event resolver
├── crossref
├── scoring
├── Strands
│    ├── verification workflow
│    ├── research workflow
│    └── followup workflow
└── publisher
```

So Strands has essentially **zero additional infrastructure burden**.

---

# The architecture becomes very compact

I'd actually aim for this:

```text
                    PROXMOX
                       │
                       ▼
              ┌─────────────────┐
              │      LXC        │
              │ 6 CPU / 12 GB   │
              │ 150 GB SSD      │
              └────────┬────────┘
                       │
              ┌────────┴────────┐
              │                 │
              ▼                 ▼
       ┌─────────────┐   ┌─────────────┐
       │  PAPERCLIP  │   │ POSTGRESQL  │
       │             │   │             │
       │ Control     │   │ Paperclip   │
       │ Plane       │   │ Cyber Intel │
       └──────┬──────┘   └──────┬──────┘
              │                 │
              └────────┬────────┘
                       ▼
                ┌─────────────┐
                │ CYBER WORKER│
                │             │
                │ Python      │
                │ Strands     │
                │ Tavily      │
                │ OpenRouter  │
                │ GitHub      │
                └──────┬──────┘
                       │
                       ▼
                   GitHub
                       │
                       ▼
                GitHub Pages
```

---

# What happens when all agents are busy?

This is where I'd size for the **peak**, rather than the average.

Normal:

```text
AU agent        running
Global agent    running
AI agent        running
Watchdog        running
```

Most of these are waiting on network/API responses.

Then suddenly:

```text
Source discovery
      ↓
Codex engineering
      ↓
tests
      ↓
verification
```

CPU/RAM spikes.

That's why I'd choose:

> **6 vCPU + 12 GB RAM**

rather than trying to squeeze everything into 2 CPU / 4 GB.

---

# I would NOT run the LXC at 16+ GB initially

You could, but I'd rather start at:

```text
6 CPU
12 GB RAM
150 GB
```

and monitor actual usage for a few weeks.

If:

```text
RAM > 80%
CPU frequently saturated
Codex queues building up
```

then increase it.

Proxmox makes that easy.

---

# Database backup

This is important because the database becomes the memory of the whole organisation.

I'd back up:

```text
PostgreSQL
Paperclip persistent data
application configuration
source registry
```

daily.

Keep:

```text
7 daily
4 weekly
3 monthly
```

or similar retention.

The GitHub repository already gives you version history for code/public data, but **the operational DB still needs its own backup strategy**.

---

# What I would put in your master prompt

I'd add this architecture section:

```text
## Proxmox Resource Plan

The entire private platform should run initially inside one dedicated Proxmox LXC.

Recommended v1 sizing:

- 6 vCPU
- 12 GB RAM
- 150 GB SSD
- 2–4 GB swap

Run Docker inside the LXC.

Containers:

1. Paperclip
   - control plane
   - UI
   - API
   - agent orchestration
   - Codex/agent adapters

2. PostgreSQL
   - one PostgreSQL instance
   - separate database for Paperclip
   - separate database for Cyber Intelligence

3. Cyber Intelligence Worker
   - Python
   - collectors
   - event processing
   - Strands workflows
   - CVE/KEV/MITRE
   - Tavily
   - OpenRouter
   - GitHub publishing

4. Optional isolated Codex execution/review container
   - used for untrusted PR review or higher-risk code execution

Do not introduce Kafka, Redis, ClickHouse, Kubernetes,
a vector database, or other infrastructure in v1 unless
a measured workload demonstrates a real requirement.

Use PostgreSQL as the system's structured intelligence store.

Use filesystem storage for temporary/raw fetched content,
transcripts, STIX caches, workspaces and diagnostics.

The architecture must allow each component to be moved
to its own VM/container later without redesigning the
application contracts.
```

That is the setup I would build around.

**Bottom line: one LXC is absolutely enough.** I'd choose **6 vCPU / 12 GB RAM / 150 GB SSD**, run **Paperclip + PostgreSQL + one Python/Strands worker inside Docker**, and keep the architecture modular enough that you can split components later. Paperclip's current deployment model already supports both an embedded PostgreSQL setup and a separate PostgreSQL service, so this fits naturally with how the project is currently structured. ([GitHub][3])

[1]: https://github.com/paperclipai/paperclip/blob/master/docs/start/architecture.md?utm_source=chatgpt.com "paperclip/docs/start/architecture.md at master · paperclipai/paperclip · GitHub"
[2]: https://github.com/paperclipai/docs/blob/main/deploy/docker.md?utm_source=chatgpt.com "docs/deploy/docker.md at main · paperclipai/docs · GitHub"
[3]: https://github.com/paperclipai/paperclip/blob/master/docs/deploy/database.md?utm_source=chatgpt.com "paperclip/docs/deploy/database.md at master · paperclipai/paperclip · GitHub"
[4]: https://github.com/paperclipai/paperclip/blob/master/doc/DOCKER.md?utm_source=chatgpt.com "paperclip/doc/DOCKER.md at master · paperclipai/paperclip · GitHub"
