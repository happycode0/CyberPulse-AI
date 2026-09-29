# MASTER BUILD PROMPT

## Autonomous Australia-First Cybersecurity + AI Intelligence Platform

## 0. MANDATORY: READ THE OFFICIAL DOCUMENTATION FIRST

**Do not start building code until you have checked the current official documentation below.**

The documentation is authoritative for current APIs, installation methods, configuration, adapter behaviour and deployment details.

### Paperclip — read first

Repository:

`github.com/paperclipai/paperclip`

Read at minimum:

```text
docs/start/architecture.md
docs/agents-runtime.md
docs/deploy/docker.md
docs/deploy/database.md
docs/deploy/secrets.md
docs/adapters/codex-local.md
doc/DOCKER.md
doc/UNTRUSTED-PR-REVIEW.md
```

Also inspect the current Paperclip documentation for:

```text
agent creation/configuration
agent budgets
heartbeats
task assignment
GitHub integration
Codex adapter
secrets
Docker deployment
PostgreSQL deployment
authentication
```

### Strands Agents — read current documentation

Official documentation:

`strandsagents.com`

Read at minimum:

```text
SDK / multi-agent patterns
Graph
Workflow
custom nodes
conditional routing
cyclic workflows
Python SDK
current installation requirements
```

Current relevant documentation:

```text
strandsagents.com/docs/user-guide/sdk/multi-agent/multi-agent-patterns/
strandsagents.com/docs/user-guide/sdk/multi-agent/graph/
strandsagents.com/docs/user-guide/sdk/multi-agent/workflow/
```

### OpenRouter

Official documentation:

`openrouter.ai/docs`

Verify current documentation for:

```text
API keys
management API
key limits / guardrails
usage
generation information
cost reporting
model routing
```

Do not assume older API paths from this prompt.

### Tavily

Official documentation:

`docs.tavily.com`

Verify current APIs for:

```text
search
extract
usage
rate limits
```

### OpenAI / Codex

Official documentation:

`developers.openai.com`

Read the current Codex documentation, especially:

```text
Codex CLI
Codex agent workflows
code generation
code review
CLI authentication
current recommended model/runtime usage
```

### Docker

Official documentation:

`docs.docker.com/engine/install/debian/`

Use the current Debian 12 / Bookworm instructions.

### Proxmox

Official documentation:

`pve.proxmox.com/pve-docs/`

Verify current LXC documentation, especially:

```text
unprivileged containers
nesting
keyctl
resource limits
bind mounts
backup behaviour
Docker inside LXC considerations
```

### PostgreSQL

Official documentation:

`postgresql.org/docs/`

Use a currently supported PostgreSQL release. Do not pin to an obsolete version merely because an older example uses it.

### GitHub Pages

Official documentation:

`docs.github.com/en/pages`

Verify:

```text
static hosting
publishing
custom workflows
limits
deployment behaviour
```

GitHub Pages is static and publicly accessible; do not publish secrets or private operational data.

---

## 1. IMPORTANT IMPLEMENTATION RULE

The documentation above takes precedence over this prompt whenever an API, CLI flag, configuration field, adapter name, or deployment method has changed.

Before implementing a component:

```text
READ CURRENT DOCS
→ VERIFY CURRENT API
→ DESIGN
→ IMPLEMENT
→ TEST
```

Do not blindly copy examples from this prompt, old blog posts, cached documentation, or model memory.

At the end of the initial setup, document:

```text
documentation versions/commit dates checked
important API decisions
assumptions
known limitations
```

---

# 2. OBJECTIVE

Build a self-hosted **Australia-first Cybersecurity + AI Intelligence Organisation**.

This is **not just an RSS news reader**.

The system continuously:

```text
DISCOVER
→ COLLECT
→ NORMALISE
→ CORRELATE
→ VERIFY
→ ENRICH
→ CROSS-REFERENCE
→ SCORE
→ PUBLISH
→ MONITOR
→ FOLLOW UP
→ SELF-DIAGNOSE
→ SELF-FIX
→ VERIFY FIX
→ CONTINUE
```

The public product is a lightweight cyberpunk-themed static website hosted on GitHub Pages.

The private operating platform runs on Proxmox.

---

# 3. PRIMARY ARCHITECTURE

Use:

```text
Paperclip = autonomous organisation / control plane
Strands   = specialist workflow engine
Codex     = software engineering agent
Python    = deterministic application logic
PostgreSQL = operational intelligence database
GitHub    = code + generated public data
GitHub Pages = public website
```

Do **not** make Strands the overall scheduler/company-management layer.

Do **not** make Paperclip responsible for application-specific business logic that belongs in Python.

---

# 4. PROXMOX DEPLOYMENT

## One LXC for v1

Run the whole private platform in **one dedicated Debian 12 LXC**.

Recommended starting resources:

```text
CPU:    6 vCPU
RAM:    12 GB
Disk:   150 GB SSD
Swap:   2–4 GB
OS:     Debian 12 Bookworm
```

8 GB RAM may work, but 12 GB is the preferred starting point.

Do not split this into multiple Proxmox VMs/LXCs unless measured workload justifies it.

---

# 5. CONTAINERS INSIDE THE LXC

Run Docker inside the Debian 12 LXC.

Preferred layout:

```text
Cyber-Intelligence LXC
│
├── Paperclip
├── PostgreSQL
├── Cyber Intelligence Worker
└── Optional isolated Codex/review container
```

Use separate Docker containers for logical isolation while keeping the Proxmox footprint simple.

Verify the current Proxmox requirements for Docker inside an LXC before configuring nesting/keyctl.

Prefer an **unprivileged LXC** unless a documented requirement makes that impractical.

---

# 6. RESOURCE PHILOSOPHY

This system does **not** run LLM inference locally.

AI inference is primarily through OpenRouter.

Therefore CPU/RAM are mainly used for:

```text
HTTP/RSS/API collection
Python processing
database operations
Paperclip
Strands workflows
Codex execution
Git operations
tests
temporary research
```

Do not over-provision infrastructure before measuring real usage.

No Kafka, Redis, ClickHouse, Kubernetes, vector DB, or similar infrastructure in v1 unless a measured workload proves it necessary.

---

# 7. DATABASE

Use **PostgreSQL**.

Prefer one PostgreSQL instance with separate logical databases:

```text
paperclip
cyber_intel
```

Use PostgreSQL for structured long-term application state.

Store:

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

Do not couple application schema directly to Paperclip internals.

---

# 8. FILESYSTEM STORAGE

Use filesystem storage for:

```text
raw feed cache
temporary HTML
transcripts
large research responses
MITRE STIX cache
temporary source payloads
logs
Codex workspaces
generated artifacts
diagnostic files
```

Do not retain raw article bodies indefinitely unless explicitly required.

Do not publish raw source content to GitHub Pages.

---

# 9. SECRETS AND ENVIRONMENT VARIABLES

**All passwords, tokens, API keys and secrets are configuration, never source code.**

Use a local `.env` file for development/initial deployment:

```text
.env
```

The `.env` file must:

```text
never be committed
be listed in .gitignore
never be copied into public GitHub data
never appear in logs
never appear in prompts
never appear in generated JSON
never be included in screenshots or diagnostics
```

Provide:

```text
.env.example
```

containing variable names only and safe placeholder values.

Example:

```text
OPENROUTER_API_KEY=
OPENROUTER_FAST_MODEL=
OPENROUTER_STRONG_MODEL=

TAVILY_API_KEY=

GITHUB_TOKEN=
GITHUB_REPOSITORY=

DATABASE_URL=

PAPERCLIP_PUBLIC_URL=
PAPERCLIP_AUTH_SECRET=
```

Use the exact variables required by the current Paperclip/Codex/OpenRouter/Tavily implementation after checking their current docs.

Each agent should receive only the environment variables it actually needs.

Never place secrets inside:

```text
Python files
YAML
JSON
source code
Git commits
Dockerfiles
frontend JS
public JSON
README examples
```

---

# 10. PAPERCLIP ROLE

Paperclip is the operating system for the agent organisation.

Use Paperclip for:

```text
agent definitions
roles
goals
tasks
delegation
heartbeats
scheduling
agent state
budgets
audit
agent runs
operational visibility
```

Paperclip agents are heartbeat-based, not permanently resident processes.

Use current Paperclip adapters and runtime behaviour.

---

# 11. AGENT ORGANISATION

Create these logical agents.

## Intelligence Director

Owns:

```text
overall intelligence quality
coverage
delegation
priority
editorial decisions
major developing events
system-level intelligence gaps
```

It coordinates other agents rather than doing all work itself.

---

## AU Intelligence Agent

Focus:

```text
ACSC
ASD
Australian government
Australian regulators
Australian cyber agencies
Australian critical infrastructure
Australian security companies
Australian researchers
Australian universities
Australian incidents
Australian ransomware
Australian data breaches
Australian vulnerabilities
Australian AI policy
Australian AI industry
Australian AI security
```

---

## Global Cyber Intelligence Agent

Focus:

```text
ransomware
APT
malware
zero-days
vulnerabilities
data breaches
phishing
identity
cloud
SaaS
supply chain
critical infrastructure
application security
endpoint security
network security
DevSecOps
cybercrime
security research
```

---

## AI Intelligence Agent

Treat AI as a first-class domain.

Monitor:

```text
frontier models
model releases
AI agents
agent frameworks
MCP
AI infrastructure
AI cloud
AI chips
open-source models
AI security
AI safety
AI red teaming
AI abuse
AI-enabled attacks
AI-generated malware
AI phishing
prompt injection
model poisoning
model theft
AI supply chain
AI vulnerabilities
AI regulation
AI governance
```

---

## Source Discovery Agent

Continuously identify missing sources.

Search for:

```text
new AU security sources
new researchers
new government feeds
new CERTs
new vendor advisories
new security blogs
new newsletters
new AI security researchers
new AI sources
new YouTube channels
new GitHub advisory sources
```

Use search/discovery tools such as Tavily.

Source lifecycle:

```text
DISCOVERED
→ CANDIDATE
→ TESTING
→ VALIDATED
→ ACTIVE
→ DEGRADED
→ BROKEN
→ RETIRED
```

Never activate a newly discovered source blindly.

---

## Source Verification Agent

Verify:

```text
connectivity
feed/API validity
authentication
recent timestamps
article extraction
relevance
freshness
duplicate rate
parser correctness
volume
```

A source must demonstrate genuine useful recent content before becoming active.

---

## Event Verification Agent

Determine:

```text
NEW_EVENT
UPDATE_EXISTING_EVENT
DUPLICATE
RELATED_BUT_DISTINCT
UNVERIFIED_SIGNAL
```

Use:

```text
URL/GUID
normalised title
entity overlap
date proximity
semantic similarity
embeddings where necessary
AI reasoning only for ambiguous cases
```

---

## Follow-up Agent

Continuously monitor important events.

Look for:

```text
new exploit
PoC
KEV addition
patch
vendor update
new victim
new actor
new geography
new AU exposure
new evidence
regulatory response
resolution
```

---

## Cost / FinOps Agent

Monitor:

```text
OpenRouter cost
OpenRouter requests
tokens
model usage
Tavily usage
cost per agent
cost per event
daily cost
monthly cost
budget state
```

---

## Watchdog / SRE Agent

Monitor:

```text
Paperclip
agent heartbeats
collectors
sources
PostgreSQL
OpenRouter
Tavily
GitHub
generated data
publication
GitHub Pages
```

Detect:

```text
stale collectors
source failures
parser drift
unexpected zero-volume
duplicate explosions
schema failures
cost anomalies
publishing failures
agent failures
```

---

## Codex Source Engineer

Use the current Paperclip Codex adapter and Codex documentation.

Responsibilities:

```text
add source adapters
repair parsers
repair integrations
fix bugs
write tests
improve source handling
improve frontend
repair publication
implement approved platform changes
```

Preferred lifecycle:

```text
TASK
→ BRANCH / WORKTREE
→ IMPLEMENT
→ TEST
→ SELF-CHECK
→ PR
→ INDEPENDENT VERIFICATION
→ MERGE
→ DEPLOY
→ POST-DEPLOY CHECK
```

Do not routinely give Codex direct unrestricted writes to production `main`.

---

# 12. STRANDS ROLE

Use Strands inside specialist agents when a complex multi-step reasoning workflow is beneficial.

Good uses:

```text
research
verification
evidence reconciliation
complex event correlation
follow-up investigation
threat analysis
```

Prefer current **Graph** or **Workflow** patterns according to the task.

Example:

```text
Event
 ├── CVE lookup
 ├── KEV lookup
 ├── vendor research
 └── Tavily research
         ↓
 evidence reconciliation
         ↓
 conflict detection
         ↓
 confidence
         ↓
 event update
```

Use typed/validated schemas between stages.

Do not use an LLM for deterministic work.

---

# 13. DETERMINISTIC VS AI WORK

Deterministic Python:

```text
fetching
parsing
retries
normalisation
hashing
basic deduplication
CVE regex extraction
database operations
source health
scoring
publishing
schema validation
```

AI:

```text
semantic classification
ambiguous event correlation
summary generation
entity extraction
severity judgment
impact reasoning
MITRE mapping
evidence reconciliation
trend interpretation
source discovery reasoning
```

---

# 14. SOURCE LANES

Use three collection frequencies.

## FAST

Approximately every 15–30 minutes:

```text
ACSC
CISA
KEV
major CERTs
major vendor advisories
critical vulnerability announcements
major AI security incidents
```

Purpose:

```text
THREAT RADAR
```

## NORMAL

Every 4 hours:

```text
AU news
global cyber news
vendor blogs
AI news
research
GitHub advisories
YouTube
community sources
```

Purpose:

```text
MAIN INTELLIGENCE FEED
```

## DEEP

Once daily:

```text
Tavily discovery
source discovery
emerging researchers
long-form research
trend analysis
event follow-up
source-quality analysis
```

Purpose:

```text
INTELLIGENCE IMPROVEMENT
```

Paperclip owns the scheduling.

---

# 15. SOURCE TYPES

Support:

```text
rss
atom
json_api
github_api
advisory_api
web_page
search
youtube
x_api
community
manual
```

---

# 16. SOURCE REGISTRY

Use data-driven configuration, not hardcoded source logic.

Example:

```yaml
sources:
  - id: acsc_alerts
    name: "ACSC Alerts"
    type: rss
    region: AU
    category: government
    priority: critical
    enabled: true

  - id: bleepingcomputer
    name: "BleepingComputer"
    type: rss
    region: Global
    category: news
    priority: high
    enabled: true

  - id: x_security
    name: "X Security Search"
    type: x_api
    region: Global
    category: social
    priority: low
    enabled: false
```

Maintain:

```text
id
name
type
region
category
priority
enabled
url/query
reliability
discovery_date
validation_date
last_success
last_failure
failure_count
expected_frequency
parser
```

---

# 17. SOURCE CLASSES

Classify sources:

```text
authoritative
primary
vendor
specialist
news
community
social
discovery
```

Maintain a reliability profile:

```text
reliability
freshness
relevance
parser_stability
duplicate_rate
independent_reporting_value
```

Do not let high-volume low-quality sources dominate the feed.

---

# 18. AUSTRALIAN BASELINE SOURCES

Start with and validate:

```text
ACSC Alerts
ACSC Publications
ACSC Advisories
ACSC News
ACSC Threats
ASD
DTA
relevant Australian government sources
relevant Australian regulators

SecurityBrief Australia
Cyber Daily
Australian Cyber Security Magazine
Australian Security Magazine
Triskele Labs
```

Continuously expand through Source Discovery.

---

# 19. GLOBAL BASELINE SOURCES

Start with:

```text
The Hacker News
BleepingComputer
Krebs on Security
Dark Reading
SecurityWeek
The Record
CyberWire
Schneier on Security
Graham Cluley
SANS Internet Storm Center
CISA
```

Also include major vendor security/threat intelligence sources such as:

```text
Microsoft Security / MSRC
CrowdStrike
Palo Alto Unit 42
Malwarebytes Labs
```

Validate feeds against current source documentation.

---

# 20. AI SOURCE BASELINE

Include:

```text
Simon Willison
Embrace The Red
major AI lab security/safety material
AI agent security research
MCP security research
AI red-team research
AI safety research
AI threat research
AI regulation/governance
```

Only include generic AI industry material when it has meaningful relevance to the platform.

---

# 21. GROUND-TRUTH DATA

Use:

```text
CISA KEV
NVD
CVE.org / CNA records
GitHub Advisory Database
OSV
EPSS
vendor security advisories
MITRE ATT&CK
MITRE ATLAS where relevant
```

Never invent CVE or MITRE details from model memory.

---

# 22. EVENT-CENTRIC DATA MODEL

The canonical unit is an **event**, not an article.

Example:

```json
{
  "event_id": "evt-2026-000123",
  "first_seen": "2026-09-29T04:00:00Z",
  "last_seen": "2026-09-29T12:00:00Z",
  "last_material_update": "2026-09-29T12:00:00Z",
  "status": "developing",

  "title": "Example vulnerability is being actively exploited",

  "summary": "Short original summary.",

  "domains": ["cybersecurity"],

  "categories": [
    "vulnerability",
    "active-exploitation"
  ],

  "severity": "critical",

  "risk": {
    "urgency": 0.93,
    "confidence": 0.96,
    "novelty": 0.87,
    "prominence": 0.91
  },

  "au": {
    "relevance": 0.94,
    "directly_reported_in_au": true,
    "reasons": [],
    "sectors": []
  },

  "entities": {
    "actors": [],
    "organisations": [],
    "products": [],
    "countries": [],
    "industries": []
  },

  "cves": [],
  "mitre_techniques": [],
  "sources": [],
  "evidence": [],
  "timeline": [],
  "tags": []
}
```

---

# 23. MATERIAL CHANGE DETECTION

When a new report matches an existing event, classify:

```text
NEW_FACT
NEW_CVE
NEW_EXPLOIT
EXPLOIT_CONFIRMED
NEW_ACTOR
NEW_TARGET
NEW_GEOGRAPHY
NEW_AU_EXPOSURE
NEW_IMPACT
NEW_PATCH
NEW_MITIGATION
NEW_EVIDENCE
CORRECTION
NO_MATERIAL_CHANGE
```

Do not refresh an event merely because another site repeated the same report.

Maintain:

```text
first_seen
last_seen
last_material_update
last_independent_confirmation
```

---

# 24. SOURCE INDEPENDENCE

Multiple sites copying the same source do not equal independent confirmation.

Track:

```text
distinct_sources
independent_confirmations
source_lineage
```

Example:

```text
Vendor release
 ├── Reuters
 ├── BleepingComputer
 └── The Hacker News
```

may represent one primary claim plus downstream reporting.

Corroboration calculations should use independent confirmation.

---

# 25. EVIDENCE

Important claims must reference supporting evidence.

Example:

```json
{
  "claims": [
    {
      "text": "The vulnerability is being actively exploited.",
      "confidence": 0.97,
      "evidence": [
        "cisa_kev",
        "vendor_advisory"
      ]
    }
  ]
}
```

Evidence classes:

```text
PRIMARY
AUTHORITATIVE
VENDOR
SPECIALIST
NEWS
COMMUNITY
SOCIAL
AI_INFERENCE
```

The UI must distinguish verified evidence from AI inference.

---

# 26. AU RELEVANCE ENGINE

Every event gets:

```text
au.relevance = 0.00–1.00
```

Reasons can include:

```text
Australian source
Australian organisation
Australian victim
Australian government
Australian critical infrastructure
Australian sector
Australian vendor
Australian researcher
Australian regulatory impact
clear practical relevance to Australian defenders
```

The frontend must prioritise AU intelligence independently of global severity.

---

# 27. AI ENRICHMENT

Use two model tiers:

```text
FAST / CHEAP
STRONG / JUDGMENT
```

Fast model:

```text
classification
entity extraction
simple summaries
source relevance
simple matching
```

Strong model:

```text
severity
impact
complex event correlation
evidence reconciliation
MITRE mapping
threat interpretation
```

Use configurable model IDs via environment/configuration.

---

# 28. CVE / MITRE GUARDRAILS

CVE workflow:

```text
extract CVE
→ lookup current data
→ lookup KEV
→ lookup CVSS where available
→ lookup CNA/vendor information where useful
→ give actual retrieved evidence to model
→ reason from evidence
```

If CVSS is absent:

```text
unknown
```

not automatically low severity.

MITRE workflow:

```text
load actual ATT&CK dataset
→ select from real technique IDs
→ store confidence
→ label as ai_suggested
```

Do not represent AI mappings as official MITRE attribution.

---

# 29. TREND ENGINE

Detect:

```text
topic_frequency
topic_velocity
severity_growth
source_growth
independent_confirmation_growth
geographic_spread
AU relevance growth
AI/cyber convergence
```

Generate:

```text
Emerging Topics
Emerging Vulnerabilities
Emerging Threat Actors
Emerging Malware
Emerging Techniques
Emerging AI Security Issues
```

Never invent statistics.

---

# 30. FOLLOW-UP

Important events remain under monitoring.

Example:

```text
Disclosure
→ Vendor confirmation
→ PoC
→ Exploitation
→ KEV
→ AU impact
→ Patch
→ Resolution
```

Follow-up updates become event timeline entries.

---

# 31. COST MANAGEMENT

Use a dedicated OpenRouter key for this project.

Track:

```text
request count
tokens in/out
model
per-call cost
daily cost
monthly cost
cost by agent
cost by event
```

Use OpenRouter's current management/budget mechanisms after verifying the current API.

---

# 32. COST DEGRADATION

Baseline:

```text
>50% budget remaining
    normal operation

20–50%
    cheap model only

<20%
    enrich only critical/high/KEV/developing events

exhausted
    no AI
    deterministic collection continues
```

If OpenRouter fails mid-run:

```text
stop additional AI calls
continue deterministic processing
mark:
pending_enrichment = true
```

---

# 33. WATCHDOG

Monitor:

```text
agent health
heartbeats
source health
database health
API availability
OpenRouter
Tavily
GitHub
generated data
publication
site freshness
```

Detect anomalies such as:

```text
no collection
source repeatedly failing
parser breakage
zero items unexpectedly
massive volume spikes
duplicate explosion
invalid schema
cost spike
failed publication
stale public site
```

---

# 34. SELF-HEALING

Preferred loop:

```text
DETECT
→ CREATE INCIDENT
→ DIAGNOSE
→ REPRODUCE
→ CREATE CODEX TASK
→ IMPLEMENT FIX
→ TEST
→ INDEPENDENT VERIFY
→ MERGE
→ DEPLOY
→ MONITOR
```

Example:

```text
ACSC parser fails
→ Watchdog detects
→ Source agent diagnoses
→ Codex fixes parser
→ tests
→ verification agent validates
→ PR merged
→ next collection succeeds
```

---

# 35. SELF-HEALING SAFETY

Agents can automatically repair:

```text
source adapters
parser changes
non-critical bugs
tests
frontend bugs
data transformation bugs
```

Require stronger review for:

```text
authentication
secrets
permissions
deployment controls
security controls
agent permissions
budget changes
production data deletion
major schema changes
network controls
```

Use circuit breakers:

```text
same automatic fix fails 3 times
→ stop retrying
→ create incident
→ notify owner
```

---

# 36. CODEX SECURITY

Codex must work through branches/worktrees where possible.

For untrusted or higher-risk PR review, use the current isolated review-container pattern documented by Paperclip where appropriate.

Codex must never:

```text
print secrets
commit .env
expose API keys
modify unrelated repositories
disable security controls silently
delete operational history
```

---

# 37. SOURCE HEALTH

Every fetch produces:

```json
{
  "source_id": "acsc_alerts",
  "status": "ok",
  "items_fetched": 11,
  "items_new": 3,
  "items_relevant": 3,
  "duration_ms": 812,
  "timestamp": "2026-09-29T08:00:00Z",
  "error": null
}
```

Statuses:

```text
ok
error
timeout
empty
degraded
disabled
```

Track history and detect degradation.

---

# 38. PUBLIC WEBSITE

Host on GitHub Pages.

Use:

```text
HTML
CSS
JavaScript
SVG/Canvas
```

No runtime secrets.

No server-side API calls requiring credentials.

All intelligence is precomputed into static JSON.

GitHub Pages remains a static presentation layer.

---

# 39. PUBLIC SITE STRUCTURE

Main sections:

```text
AUSTRALIA NOW
GLOBAL CYBER
AI + CYBER
ACTIVE EXPLOITATION
DEVELOPING EVENTS
EMERGING THREATS
THREAT ACTORS
VULNERABILITIES
RESEARCH
POLICY / REGULATION
```

Event detail should show:

```text
title
summary
why it matters
Australia relevance
severity
urgency
confidence
status
first seen
last material update
affected products
organisations
countries
actors
CVEs
KEV
CVSS
EPSS
MITRE
evidence
timeline
related events
source reports
```

---

# 40. FILTERING

Use tags as the filtering mechanism.

Filter by:

```text
severity
CVE
country
source
organisation
product
sector
threat actor
MITRE
AU
AI
category
```

Client-side filtering is sufficient for v1.

---

# 41. COLLECTION VISUALISATION

Use cyberpunk network visualisation:

```text
SOURCES
 ↓
COLLECT
 ↓
MATCH
 ↓
VERIFY
 ↓
ENRICH
 ↓
CROSS-REFERENCE
 ↓
SCORE
 ↓
PUBLISH
```

Show source health using:

```text
healthy
degraded
broken
```

Animate the latest completed collection run.

Do not claim the public animation is real-time.

Show:

```text
LAST COMPLETED COLLECTION
timestamp
```

---

# 42. WORLD MAP

Include a lightweight SVG world map.

Use event country information to display:

```text
incident density
severity
```

Clicking a country filters events.

Australia receives special prominence.

---

# 43. HISTORY

Maintain:

```text
previous day
next day
jump to date
```

Use an index such as:

```text
data/index.json
```

Archive events instead of deleting historical intelligence.

---

# 44. PRIVATE OPERATIONAL VIEW

Paperclip should expose the organisational state.

Track:

```text
agent
role
status
last heartbeat
current task
recent failures
cost
```

The private system should also track:

```text
source health
pipeline health
database health
publication health
self-healing incidents
```

---

# 45. REPOSITORY STRUCTURE

Recommended:

```text
repo/
├── index.html
├── assets/
│   ├── style.css
│   ├── app.js
│   └── map.svg
│
├── data/
│   ├── live.json
│   ├── index.json
│   ├── trends.json
│   ├── source-health.json
│   ├── system-status.json
│   └── history/
│
├── config/
│   ├── sources.yaml
│   ├── scoring.yaml
│   └── categories.yaml
│
├── scripts/
│   ├── collect.py
│   ├── normalise.py
│   ├── match.py
│   ├── event_resolver.py
│   ├── enrich.py
│   ├── crossref.py
│   ├── evidence.py
│   ├── score.py
│   ├── trends.py
│   ├── publish.py
│   ├── source_health.py
│   └── cost_guardian.py
│
├── agents/
├── workflows/
├── schemas/
├── tests/
│
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

---

# 46. PRIVATE VS PUBLIC DATA

Private:

```text
raw source payloads
provider responses
agent logs
operational database
cost details
credentials
internal prompts
diagnostics
Codex workspaces
```

Public:

```text
canonical event data
short original summaries
evidence references
source URLs
timestamps
structured metadata
sanitised operational status
```

Never publish private data.

---

# 47. NOTIFICATIONS

Create an abstract notification layer supporting:

```text
critical event
daily intelligence digest
developing event update
self-healing incident
source discovery
system failure
weekly trends
```

Example:

```text
CRITICAL AU EVENT

Event:
...

Why important:
...

Australian relevance:
...

Evidence:
...

Current status:
monitoring
```

---

# 48. DAILY INTELLIGENCE REPORT

Generate:

```text
AUSTRALIA
GLOBAL
AI
ACTIVE EXPLOITATION
DEVELOPING EVENTS
EMERGING TRENDS
SYSTEM HEALTH
COST
FOLLOW-UP
```

Do not merely dump article titles.

Produce event-oriented intelligence.

---

# 49. VALIDATION

Every pipeline output must pass:

```text
JSON schema
required fields
enum validation
timestamps
unique event IDs
valid CVE formats
valid URLs
secret scan
unexpected null detection
```

Do not publish if critical validation fails.

---

# 50. TESTING

Include:

```text
unit tests
integration tests
agent workflow tests
failure tests
source parser tests
event correlation tests
schema tests
publication tests
```

Simulate:

```text
feed outage
feed schema change
OpenRouter outage
Tavily outage
GitHub outage
database outage
bad model output
duplicate storm
invalid source data
failed publication
```

---

# 51. FAILURE MODEL

One component must not kill the whole platform.

Examples:

```text
Tavily fails
→ collection continues

OpenRouter fails
→ deterministic pipeline continues

one source fails
→ other sources continue

YouTube fails
→ cyber collection continues

GitHub publish fails
→ generated output retained locally and retried

one agent fails
→ other agents continue
```

---

# 52. VERSIONING

Version:

```text
pipeline
schemas
source registry
scoring
enrichment prompts
AI model configuration
MITRE dataset
taxonomy
```

Every event should know which pipeline/enrichment/scoring version produced it.

---

# 53. BACKUPS

Back up:

```text
PostgreSQL
Paperclip persistent data
configuration
source registry
operational state
```

Use a sensible daily/weekly/monthly retention policy.

Remember that external bind mounts may have different Proxmox backup behaviour; verify against current Proxmox documentation before relying on them for backup coverage.

---

# 54. IMPLEMENTATION PHASES

## Phase 1 — Infrastructure

```text
Debian 12 LXC
Docker
Paperclip
PostgreSQL
.env
backup
basic monitoring
```

## Phase 2 — Core application

```text
source registry
collectors
event schema
database
basic frontend
GitHub publication
```

## Phase 3 — Intelligence

```text
deduplication
event resolution
CVE/KEV/MITRE
AI enrichment
evidence
AU relevance
```

## Phase 4 — Autonomous organisation

```text
AU agent
Global agent
AI agent
source discovery
verification
follow-up
watchdog
cost agent
Codex engineering
```

## Phase 5 — Self-improvement

```text
source repair
parser repair
quality evaluation
trend analysis
self-healing
rollback
```

---

# 55. ACCEPTANCE CRITERIA

The final platform must provide:

```text
✓ AU-first cyber intelligence
✓ Global cybersecurity intelligence
✓ AI intelligence
✓ AI/cyber convergence monitoring
✓ CVE/KEV/MITRE ground truth
✓ event-level deduplication
✓ material-change detection
✓ source independence
✓ evidence-linked claims
✓ event timelines
✓ follow-up monitoring
✓ trends
✓ source discovery
✓ autonomous source repair
✓ Codex engineering
✓ watchdog
✓ budget control
✓ Paperclip orchestration
✓ Strands specialist workflows
✓ PostgreSQL persistence
✓ GitHub Pages publication
✓ honest static status
✓ no secrets in repository
✓ single Debian 12 LXC deployment
```

---

# 56. FINAL ARCHITECTURAL PRINCIPLE

The finished system should behave like a continuously operating organisation:

```text
                    PAPERCLIP
                        │
             CYBER INTELLIGENCE CO
                        │
      ┌─────────────────┼──────────────────┐
      │                 │                  │
      ▼                 ▼                  ▼
   AU INTEL         GLOBAL INTEL        AI INTEL
      │                 │                  │
      └─────────────────┼──────────────────┘
                        ▼
                 SOURCE DISCOVERY
                        │
                        ▼
                     COLLECT
                        │
                        ▼
                   NORMALISE
                        │
                        ▼
                EVENT RESOLUTION
                        │
                        ▼
                 MATERIAL CHANGE
                        │
                        ▼
                   EVIDENCE
                        │
                        ▼
                  AI ENRICHMENT
                        │
                        ▼
             CVE / KEV / MITRE / OSV
                        │
                        ▼
                AU RELEVANCE
                        │
                        ▼
                RISK / PROMINENCE
                        │
                        ▼
                  TREND ENGINE
                        │
                        ▼
                   PUBLISHER
                        │
                        ▼
                  GitHub Pages
                        │
                        ▼
                 FOLLOW-UP AGENT
                        │
                        └───────────┐
                                    │
WATCHDOG → INCIDENT → CODEX → TEST │
                         ↓          │
                    VERIFY → MERGE ─┘
```

The goal is not merely to automate news collection.

The goal is to create a **self-operating, self-monitoring, self-improving Cybersecurity + AI Intelligence Organisation**, with Australia as the primary lens and global intelligence as the wider context.

Before writing implementation code, inspect the current official documentation listed at the top, verify all current APIs and deployment requirements, then produce the implementation plan and begin building from the validated architecture.
