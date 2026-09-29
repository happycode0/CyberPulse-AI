# MASTER BUILD PROMPT

# Autonomous Australia-First Cybersecurity + AI Intelligence Platform

## 0. ROLE AND OBJECTIVE

You are designing and eventually implementing a self-hosted, autonomous cybersecurity and AI intelligence platform.

This is **not just an RSS news reader**.

The goal is to create an **Australia-first Cyber + AI Intelligence Organisation** that continuously:

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
→ VERIFY THE FIX
→ CONTINUE
```

The platform should behave like a small autonomous cyber intelligence team operating continuously.

The public product is a lightweight cyberpunk-themed static website hosted on GitHub Pages.

The private operational system is self-hosted on Proxmox.

The control plane is Paperclip.

Strands is used where a specialist requires a deterministic multi-step research/agent workflow.

Codex is the software engineering agent responsible for maintaining and improving the codebase.

Python is the deterministic data-processing and integration layer.

---

# 1. CORE DESIGN PRINCIPLES

## 1.1 Build an intelligence system, not an article database

The primary object is a **canonical security/AI event**, not an article.

Multiple reports can belong to the same event.

Example:

```text
EVENT
CVE-2026-XXXX exploitation

├── ACSC advisory
├── CISA KEV
├── vendor advisory
├── BleepingComputer
├── The Record
├── SecurityWeek
├── researcher report
├── GitHub advisory
├── YouTube analysis
└── social signals
```

The UI should show one canonical event with multiple evidence sources rather than eight duplicate news cards.

---

## 1.2 Australia first

Australia is the primary geographic lens.

The system must determine Australian relevance independently of source location.

An event can be AU-relevant because:

```text
Australian source
OR
Australian organisation
OR
Australian person/researcher
OR
Australian government
OR
Australian critical infrastructure
OR
Australian sector
OR
Australian victim
OR
Australian regulatory impact
OR
Australian vendor/product
OR
significant direct relevance to Australian defenders
```

Every event should receive:

```json
"au": {
  "relevance": 0.00,
  "directly_reported_in_au": false,
  "reasons": [],
  "sectors": []
}
```

Do not reduce Australian intelligence to a simple `AU=true` tag.

---

## 1.3 Cybersecurity and AI are equal first-class domains

The platform must treat these as two major domains:

```text
CYBERSECURITY
AI
```

and explicitly monitor the convergence:

```text
CYBER ↔ AI
```

AI scope includes:

```text
- Frontier models
- Model releases
- AI agents
- Agent frameworks
- MCP
- AI infrastructure
- AI cloud
- AI chips
- Open-source models
- AI security
- AI safety
- AI incidents
- AI abuse
- AI-enabled attacks
- AI-generated malware
- AI phishing
- Prompt injection
- Model poisoning
- Model theft
- Training-data attacks
- Agent hijacking
- AI supply chain
- AI regulation
- AI governance
- AI vulnerabilities
- Autonomous vulnerability research
```

Do not restrict "AI news" to company announcements.

---

# 2. OPERATING ARCHITECTURE

Use the following logical architecture:

```text
                          USER / OWNER
                               │
                               ▼
                     ┌───────────────────┐
                     │     PAPERCLIP     │
                     │   CONTROL PLANE   │
                     └─────────┬─────────┘
                               │
        ┌──────────────────────┼────────────────────────┐
        │                      │                        │
        ▼                      ▼                        ▼
 INTELLIGENCE TEAM       ENGINEERING TEAM          OPERATIONS TEAM
        │                      │                        │
        │                      │                        │
        ▼                      ▼                        ▼
 AU / GLOBAL / AI       Codex Source Engineer     Cost / FinOps
 Discovery / Research   Codex Platform Engineer  Publisher
 Verification / QA      QA / Review               Watchdog / SRE
 Event Follow-up
        │                      │                        │
        └──────────────────────┼────────────────────────┘
                               ▼
                     ┌───────────────────┐
                     │ Python Services   │
                     │ Deterministic     │
                     └─────────┬─────────┘
                               │
                               ▼
                     ┌───────────────────┐
                     │ Strands Workflows │
                     │ Where Needed      │
                     └─────────┬─────────┘
                               │
                               ▼
                     Canonical Event Store
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
              Trend Engine          Evidence Engine
                    │                     │
                    └──────────┬──────────┘
                               ▼
                       Static JSON Export
                               │
                               ▼
                           GitHub
                               │
                               ▼
                       GitHub Pages
```

---

# 3. PAPERCLIP IS THE CONTROL PLANE

Paperclip is the organisation and operating control plane.

Do not make Hermes the primary scheduler or orchestrator.

Paperclip should own the concepts of:

```text
- Agents
- Responsibilities
- Goals
- Tasks
- Delegation
- Heartbeats
- Scheduling
- Agent status
- Budgets
- Operational visibility
- Auditability
- Human intervention
```

The system must not depend on one giant agent.

Use a team of narrowly scoped agents with explicit ownership.

---

# 4. AGENT ORGANISATION

Create the following logical agents.

## 4.1 Intelligence Director / Chief Editor

Mission:

Own the quality and direction of the intelligence product.

Responsibilities:

```text
- Review overall system state
- Decide what requires follow-up
- Delegate intelligence tasks
- Detect coverage gaps
- Detect important developing events
- Review major anomalies
- Coordinate AU / Global / AI intelligence
- Trigger re-research
- Coordinate editorial QA
- Decide whether an event is sufficiently verified for publication
- Produce executive/daily status
```

Do not make this agent perform all collection itself.

It is a coordinator.

---

# 5. AU INTELLIGENCE AGENT

Mission:

Continuously monitor Australian cyber and AI developments.

Focus:

```text
- ASD
- ACSC
- Australian government
- Australian regulators
- Australian cyber agencies
- Australian critical infrastructure
- Australian security companies
- Australian researchers
- Australian universities
- Australian cyber incidents
- Australian data breaches
- Australian ransomware
- Australian policy
- Australian AI regulation
- Australian AI industry
- Australian security research
```

Priority:

```text
AUTHORITATIVE AU SOURCES
>
PRIMARY AU SOURCES
>
SPECIALIST AU MEDIA
>
GENERAL MEDIA
>
DISCOVERY SIGNALS
```

Run on the fast and normal schedules according to source criticality.

---

# 6. GLOBAL CYBER INTELLIGENCE AGENT

Mission:

Monitor the global cybersecurity environment.

Categories:

```text
- Ransomware
- Data breaches
- Zero-days
- Vulnerabilities
- Malware
- APT / nation-state activity
- Phishing
- Identity attacks
- Cloud security
- SaaS security
- Supply-chain attacks
- Critical infrastructure
- Security research
- Authentication
- Network security
- Endpoint security
- Container/Kubernetes security
- Application security
- DevSecOps
- Cloud-native security
- Privacy/security incidents
- Cybercrime
- Security tooling
```

---

# 7. AI INTELLIGENCE AGENT

Mission:

Monitor the AI ecosystem and especially the intersection between AI and cybersecurity.

Separate:

```text
AI INDUSTRY
AI SECURITY
AI THREAT ACTIVITY
AI + CYBER CONVERGENCE
```

Monitor:

```text
- OpenAI
- Anthropic
- Google DeepMind
- Microsoft AI/security
- Meta AI
- major model vendors
- open-source model ecosystem
- agent frameworks
- MCP ecosystem
- AI security researchers
- AI safety researchers
- AI red teaming
- AI governance
- AI regulation
- AI attacks
- AI-enabled cybercrime
- AI vulnerabilities
```

AI lab announcements are allowed, but only when they have meaningful relevance to the platform's scope.

---

# 8. VULNERABILITY / GROUND-TRUTH AGENT

This is primarily deterministic, with optional AI assistance.

Ground-truth sources:

```text
- CISA KEV
- NVD
- CVE.org / CNA records
- GitHub Advisory Database
- OSV
- EPSS
- Vendor security advisories
- Vendor PSIRTs
- MITRE ATT&CK
- MITRE ATLAS where relevant
```

Never allow an LLM to invent:

```text
- CVSS values
- KEV status
- CVE descriptions
- MITRE technique IDs
```

The model must work from actual retrieved data.

---

# 9. SOURCE DISCOVERY AGENT

This is a permanent part of the organisation.

Mission:

Continuously find sources the current source registry does not know about.

Search for:

```text
- New Australian security sources
- New Australian researchers
- New CERTs
- New government feeds
- New vendor advisories
- New security researchers
- New blogs
- New newsletters
- New GitHub advisory sources
- New AI security researchers
- New AI blogs
- New YouTube channels
- Emerging specialist publications
```

Discovery sources can include:

```text
- Tavily
- search engines
- GitHub
- YouTube
- security communities
- researcher pages
- conference pages
- existing source references
```

The discovery agent must never blindly activate a discovered source.

Lifecycle:

```text
DISCOVERED
    ↓
CANDIDATE
    ↓
TESTING
    ↓
VALIDATED
    ↓
ACTIVE
    ↓
DEGRADED
    ↓
BROKEN
    ↓
RETIRED
```

---

# 10. CODEX SOURCE ENGINEER

Codex is the software engineer for the autonomous organisation.

Its responsibilities include:

```text
- Add new source adapters
- Fix broken parsers
- Fix feed schema changes
- Add source configuration
- Improve deduplication
- Improve enrichment
- Improve frontend
- Improve tests
- Fix publishing failures
- Implement approved platform changes
```

Never allow Codex to directly modify production `main` as its normal behaviour.

Preferred lifecycle:

```text
TASK
 ↓
Codex creates branch/worktree
 ↓
implement
 ↓
tests
 ↓
sample ingestion
 ↓
self-check
 ↓
PR
 ↓
independent verification
 ↓
approval/merge
 ↓
deployment
 ↓
post-deploy monitoring
```

---

# 11. SOURCE VERIFICATION AGENT

This agent verifies that a new or repaired source is actually useful.

For each source:

```text
- HTTP connectivity
- Feed/API validity
- Authentication
- Date validity
- Publication timestamps
- Content type
- Article extraction
- Relevant cybersecurity/AI content
- Duplicate rate
- Freshness
- Parser correctness
- False-positive rate
```

The source must produce genuine recent items before being promoted to `ACTIVE`.

Example:

```json
{
  "source_id": "example_source",
  "status": "validated",
  "feed_ok": true,
  "items_seen": 23,
  "recent_items": 8,
  "relevant_items": 7,
  "duplicate_rate": 0.12,
  "validated_at": "..."
}
```

---

# 12. EVENT VERIFICATION / CORRELATION AGENT

Mission:

Determine whether incoming reports describe:

```text
NEW_EVENT
UPDATE_EXISTING_EVENT
DUPLICATE
RELATED_BUT_DISTINCT
UNVERIFIED_SIGNAL
```

This agent should use evidence, entities, dates and source lineage.

Never rely only on title similarity.

Use a tiered strategy:

```text
1. Exact URL hash
2. Canonical URL / GUID
3. Normalised title hash
4. Token similarity
5. Entity overlap
6. Date proximity
7. Local embedding similarity for ambiguous cases
8. AI reasoning only when required
```

---

# 13. MATERIAL CHANGE DETECTION

When a new report matches an existing event, determine whether it contains genuinely new information.

Classify:

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

Only material changes should significantly refresh event prominence.

Do NOT reset freshness merely because another website repeated the same information.

---

# 14. SOURCE INDEPENDENCE

Do not treat multiple copied reports as multiple independent confirmations.

Detect source lineage.

Example:

```text
Vendor press release
    ↓
Reuters
    ↓
BleepingComputer
    ↓
The Hacker News
```

This may represent:

```text
1 primary assertion
+
3 downstream reports
```

not four independent confirmations.

Store:

```json
{
  "source_id": "bleepingcomputer",
  "lineage_id": "vendor-release-123",
  "independent": false
}
```

Maintain:

```text
distinct_sources
independent_confirmations
source_lineages
```

Use independent confirmations in confidence calculations.

---

# 15. EVIDENCE ENGINE

Every important AI-generated claim should have evidence.

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

Evidence types:

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

The UI should clearly distinguish verified facts from AI inference.

---

# 16. TAVILY ROLE

Tavily is primarily a **discovery and research engine**, not just another feed.

Use it for:

```text
- Finding missing sources
- Investigating emerging events
- Finding primary sources
- Cross-checking reports
- Researching developing incidents
- Finding evidence for claims
- Finding reports not available through RSS
```

Do not treat a single Tavily result as ground truth.

---

# 17. YOUTUBE

Treat YouTube as a first-class discovery/source type.

Pipeline:

```text
YouTube channel
 ↓
new video
 ↓
metadata
 ↓
transcript
 ↓
topic extraction
 ↓
event correlation
 ↓
evidence classification
```

A video can generate:

```text
EARLY_SIGNAL
```

but should not automatically equal verified fact.

---

# 18. SOCIAL SOURCES

X and community sources are signal sources.

Classify:

```text
AUTHORITATIVE
PRIMARY
SPECIALIST
NEWS
COMMUNITY
SOCIAL
```

Social sources are primarily useful for:

```text
- early signals
- researcher discovery
- event discovery
- emerging exploit discussion
- emerging threat actor discussion
```

They should generally require corroboration for high-confidence factual claims.

X API must remain optional and cost-gated.

If credentials are missing:

```text
SKIPPED
```

must be a valid state.

The rest of the pipeline must continue normally.

---

# 19. SOURCE REGISTRY

Sources must be data-driven.

Use:

```text
sources.yaml
```

or an equivalent structured registry.

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

  - id: x_security_search
    name: "X Security Search"
    type: x_api
    region: Global
    category: social
    priority: low
    enabled: false
```

Every source should support:

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

# 20. SOURCE CLASSES

The registry should support at minimum:

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

# 21. AUSTRALIAN SOURCE BASELINE

Start with the following categories and validate every endpoint before activation.

## Government / authoritative

```text
- ACSC Alerts
- ACSC Publications
- ACSC Advisories
- ACSC News
- ACSC Threats
- ASD
- DTA
- Australian government cybersecurity material
- relevant Australian regulators
```

## Australian security media

```text
- SecurityBrief Australia
- Cyber Daily
- Australian Cyber Security Magazine
- Australian Security Magazine
- Triskele Labs
```

Expand continuously through source discovery.

---

# 22. GLOBAL SOURCE BASELINE

Start with:

```text
- The Hacker News
- BleepingComputer
- Krebs on Security
- Dark Reading
- SecurityWeek
- The Record
- CyberWire
- Schneier on Security
- Graham Cluley
- SANS Internet Storm Center
- CISA
```

Also add major vendor threat-intelligence sources such as:

```text
- Microsoft Security / MSRC
- CrowdStrike
- Palo Alto Unit 42
- Malwarebytes Labs
```

Expand dynamically.

---

# 23. AI SOURCE BASELINE

Include:

```text
- Simon Willison
- Embrace The Red
- major AI lab security/safety publications
- major AI model releases
- AI agent security research
- MCP security research
- AI safety research
- AI red-team research
- AI threat research
```

AI labs may be included when the material is clearly relevant to AI, security, safety, agents, models, infrastructure, governance, or significant industry changes.

---

# 24. FAST / NORMAL / DEEP COLLECTION

Do not use one four-hour schedule for everything.

Use three operational cadences.

## FAST LANE

Approximately every 15–30 minutes.

Only high-signal sources:

```text
- ACSC
- CISA
- KEV
- major CERTs
- critical vendor advisories
- major zero-day disclosures
- critical AI security events
```

Purpose:

```text
THREAT RADAR
```

---

## NORMAL LANE

Every 4 hours.

```text
- RSS
- global news
- AU media
- vendor blogs
- AI news
- research
- GitHub advisories
- YouTube
- community sources
```

Purpose:

```text
MAIN INTELLIGENCE FEED
```

---

## DEEP LANE

Once per day.

```text
- Tavily discovery
- source discovery
- emerging researcher discovery
- long-form research
- trend analysis
- event follow-up
- source-quality analysis
```

Purpose:

```text
INTELLIGENCE IMPROVEMENT
```

Paperclip should coordinate these schedules.

---

# 25. EVENT DATA MODEL

The canonical object is:

```json
{
  "event_id": "evt-2026-000123",
  "first_seen": "2026-09-29T04:00:00Z",
  "last_seen": "2026-09-29T12:00:00Z",
  "last_material_update": "2026-09-29T12:00:00Z",
  "status": "active",

  "title": "Example vulnerability is being actively exploited",

  "summary": "Short original summary. Do not reproduce article text.",

  "domains": [
    "cybersecurity"
  ],

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
    "reasons": [
      "Australian critical infrastructure relevance"
    ],
    "sectors": [
      "government",
      "critical-infrastructure"
    ]
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

# 26. EVENT STATUS

Support:

```text
new
active
developing
monitoring
contained
resolved
archived
```

The follow-up agent owns transition monitoring.

---

# 27. TIMELINE

Each material event change should create a timeline entry.

Example:

```json
{
  "timestamp": "2026-09-29T13:00:00Z",
  "type": "EXPLOIT_CONFIRMED",
  "summary": "Independent researcher published proof of exploitation.",
  "sources": [
    "researcher_123"
  ]
}
```

This allows the frontend to show:

```text
09:00  Disclosure
11:00  Vendor confirmation
13:00  Exploit observed
17:00  KEV addition
Next day Patch released
```

---

# 28. AI ENRICHMENT

Use AI only where it provides value.

Deterministic functions should handle:

```text
- fetching
- parsing
- hashing
- deduplication
- regex CVE extraction
- source health
- scoring
- publishing
```

AI is used for:

```text
- summarisation
- semantic classification
- entity extraction
- event correlation when ambiguous
- severity judgment
- impact assessment
- MITRE mapping
- evidence reconciliation
- trend interpretation
```

---

# 29. MODEL TIERS

Use at least two logical model tiers.

```text
CHEAP / FAST
```

for:

```text
- classification
- entity extraction
- lightweight summaries
- source relevance
- simple event matching
```

and:

```text
STRONG / JUDGMENT
```

for:

```text
- severity
- complex correlation
- evidence reconciliation
- threat interpretation
- impact
- MITRE mapping
```

Do not hard-code provider-specific model IDs into business logic.

Use configuration/environment variables:

```text
OPENROUTER_FAST_MODEL
OPENROUTER_STRONG_MODEL
```

The cost guardian can dynamically change model tier.

---

# 30. CVE GUARDRAIL

Never allow the model to invent CVE data.

Process:

```text
Extract CVE ID
        ↓
lookup actual CVE
        ↓
lookup KEV
        ↓
lookup CVSS where available
        ↓
lookup CNA/vendor data where useful
        ↓
provide actual facts to model
        ↓
model reasons over retrieved evidence
```

If official scoring is missing:

```text
unknown
```

is valid.

Do not interpret:

```text
CVSS missing
```

as:

```text
low severity
```

---

# 31. MITRE GUARDRAIL

Use the actual cached MITRE dataset.

The model should choose from the cached list.

Store:

```json
{
  "id": "T1190",
  "name": "Exploit Public-Facing Application",
  "confidence": 0.84,
  "confidence_type": "ai_suggested",
  "evidence": []
}
```

Never silently present AI-selected MITRE mappings as official attribution.

Where AI mapping is used, label it:

```text
AI SUGGESTED
```

---

# 32. PROMINENCE AND DECAY

Keep severity and prominence conceptually separate.

Store:

```text
severity
urgency
confidence
AU relevance
AI relevance
novelty
corroboration
freshness
prominence
```

Use a configurable, versioned scoring system.

Baseline severity:

```text
critical = 4
high     = 3
medium   = 2
low      = 1
```

KEV-linked events receive an additional risk bonus.

Corroboration must use:

```text
independent confirmations
```

rather than raw source count.

Baseline recency half-life:

```text
low/medium              = 24 hours
high                    = 72 hours
critical                = 168 hours
critical + KEV          = 168 hours or configurable
```

The decay timer must reset on:

```text
material updates
```

not merely on duplicate reports.

---

# 33. EVENT ARCHIVING

Default:

```text
prominence < 0.05
OR
age > 30 days
```

moves an event out of the live feed.

Do not delete it from history.

Allow retention to be configurable.

Public history should contain compact event data, not raw article bodies.

---

# 34. TREND ENGINE

The system must answer:

```text
What is new?
```

and also:

```text
What is becoming important?
```

Track:

```text
topic_frequency
topic_velocity
severity_growth
source_count_growth
independent_confirmation_growth
geographic_spread
AU_relevance_growth
AI/cyber convergence
```

Produce:

```text
Emerging Topics
Emerging Vulnerabilities
Emerging Threat Actors
Emerging Malware
Emerging Techniques
Emerging AI Security Issues
```

Example:

```text
MCP security incidents
+210% week/week

AI phishing
+71%

Australian healthcare cyber incidents
+42%
```

Use actual data only.

Never fabricate percentages.

---

# 35. FOLLOW-UP AGENT

This agent monitors existing important events.

Do not let important events disappear merely because the original article became old.

Track:

```text
- new exploit
- PoC release
- KEV addition
- patch release
- vendor update
- victim disclosure
- new countries
- new threat actors
- new evidence
- regulatory response
- Australian impact
- resolution
```

The follow-up agent periodically asks:

```text
Did anything materially change?
```

---

# 36. COST / FINOPS AGENT

Create a dedicated OpenRouter API key for this project.

Do not share the key with unrelated agents.

Use provider-side spending limits as the hard backstop.

The cost agent tracks:

```text
- request count
- input tokens
- output tokens
- model
- per-call cost
- daily cost
- monthly cost
- cost per event
- cost per source
- cost by agent
- budget tier
```

Store:

```text
data/cost-log.json
```

or the equivalent database record plus public aggregate metrics.

---

# 37. COST DEGRADATION

Before each AI-heavy cycle:

```text
>50% remaining
    normal model tiering

20–50% remaining
    cheap model only

<20% remaining
    enrich only:
      - KEV-linked
      - critical
      - high severity
      - developing events

exhausted
    AI disabled
    publish deterministic/raw results
```

If an OpenRouter request returns a billing failure during a run:

```text
stop further AI calls
continue deterministic pipeline
publish with:
"pending_enrichment": true
```

---

# 38. COST TRANSPARENCY

Track:

```text
Today
Week
Month
Current budget
Estimated remaining capacity
```

The public site may expose only an intentionally limited transparency value such as:

```text
AI processing: operational
```

Do not expose secrets, API identifiers or internal limits.

---

# 39. PLATFORM WATCHDOG / SRE AGENT

Mission:

Make the system self-monitoring.

Monitor:

```text
Paperclip
Agents
Heartbeats
Collectors
Sources
Tavily
OpenRouter
GitHub
Database
Event pipeline
Publisher
GitHub Pages
JSON schema
```

Detect:

```text
- no collection
- repeated feed failure
- parser breakage
- source suddenly empty
- event count collapse
- duplicate explosion
- schema drift
- OpenRouter cost anomaly
- Tavily failure
- publication failure
- GitHub push failure
- invalid JSON
- site build failure
- stale data
```

---

# 40. SELF-HEALING LOOP

When the watchdog detects a problem:

```text
DETECT
 ↓
CREATE INCIDENT
 ↓
DIAGNOSE
 ↓
REPRODUCE
 ↓
CREATE ENGINEERING TASK
 ↓
CODEX FIXES
 ↓
TEST
 ↓
INDEPENDENT VERIFICATION
 ↓
MERGE
 ↓
DEPLOY
 ↓
MONITOR
```

For source failures:

```text
broken feed
 ↓
Source Agent diagnosis
 ↓
Codex source patch
 ↓
sample feed test
 ↓
real fetch
 ↓
verification
 ↓
activate
```

For systemic failures:

```text
incident
 ↓
root-cause analysis
 ↓
Codex patch
 ↓
full test suite
 ↓
staging validation
 ↓
review
 ↓
deployment
```

---

# 41. SELF-FIX SAFETY BOUNDARIES

Agents may automatically repair:

```text
- source parser
- source URL
- source configuration
- tests
- frontend bugs
- non-critical code defects
- data transformation bugs
```

Changes requiring stronger review:

```text
- secrets
- authentication
- permissions
- deployment infrastructure
- CI/CD
- package manager security
- network controls
- agent permissions
- budget limits
- production data deletion
- large-scale schema migration
```

Never allow an agent to expose credentials in source, logs, public JSON or Git history.

---

# 42. EDITORIAL QA AGENT

Every published event should be checked for:

```text
- unsupported claims
- hallucinated CVEs
- incorrect dates
- incorrect organisations
- incorrect severity
- duplicate event creation
- misleading summaries
- missing primary sources
- incorrect MITRE mapping
- excessive article text reproduction
```

The summary must be original and concise.

Do not reproduce complete articles.

Every public event should link back to the source.

---

# 43. PUBLICATION AGENT

The Publisher converts the canonical intelligence store into static public JSON.

Never expose:

```text
- raw credentials
- private API responses
- secrets
- internal prompts
- internal agent conversations
- raw article bodies
- private system diagnostics
```

Publish only intentionally selected structured data.

---

# 44. PUBLIC WEBSITE

Technology:

```text
HTML
CSS
JavaScript
SVG / Canvas where useful
No build step
```

Hosting:

```text
GitHub Pages
```

No runtime backend is required by the public site.

All intelligence is precomputed.

---

# 45. PUBLIC SITE STRUCTURE

Homepage:

```text
CYBER // AI INTELLIGENCE
```

Primary sections:

```text
1. AUSTRALIA NOW
2. GLOBAL CYBER
3. AI + CYBER
4. ACTIVE EXPLOITATION
5. DEVELOPING EVENTS
6. EMERGING THREATS
7. THREAT ACTORS
8. VULNERABILITIES
9. RESEARCH
10. POLICY / REGULATION
```

Australia should be visually prominent.

---

# 46. FILTERING

Tags are the filtering mechanism.

Users can click:

```text
severity
CVE
country
source
organisation
product
sector
threat actor
MITRE technique
AU
AI
category
```

and immediately filter the dataset.

Do not build an unnecessarily complicated search backend.

Client-side filtering is sufficient for the static site at first.

---

# 47. EVENT PAGE / EVENT DETAIL

An event should display:

```text
Title

Why it matters

Australia relevance

Severity

Urgency

Confidence

First seen

Last material update

Current status

Affected products

Organisations

Countries

Threat actors

CVEs

KEV

CVSS

EPSS

MITRE

Evidence

Timeline

Related events

Source reports
```

---

# 48. "WHY THIS MATTERS TO AUSTRALIA"

Every sufficiently AU-relevant event should provide a compact explanation:

```text
Why this matters to Australia
```

For example:

```text
- Australian organisations use the affected product
- Australian critical infrastructure exposure
- Australian government advisory
- Australian vendor involvement
```

This should be evidence-based.

---

# 49. COLLECTION VISUALISATION

Retain the cyberpunk network/pipeline visualization concept.

The visualisation shows:

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

Source nodes should reflect:

```text
healthy
degraded
broken
```

Use animated SVG/canvas flow lines.

Important:

GitHub Pages cannot display a genuinely live backend collection stream.

Therefore the public animation is a replay of the most recent completed run.

Do not falsely label it as real-time.

Example:

```text
LAST COMPLETED COLLECTION
2026-09-29 12:00 UTC
```

---

# 50. WORLD MAP

Use a lightweight SVG world map.

Shade or mark countries based on event counts and/or event severity.

Data comes from:

```text
entities.countries
```

Allow clicking a country to filter the events.

Australia should have special prominence.

---

# 51. HISTORY

Provide:

```text
Previous day
Next day
Jump to date
```

Use:

```text
data/index.json
```

as the history index.

History should be based on canonical events, not raw article duplication.

---

# 52. PUBLIC DATA LAYOUT

Recommended repository structure:

```text
repo/
│
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
│       ├── 2026-09-29.json
│       ├── 2026-09-28.json
│       └── ...
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
│   ├── au_intelligence/
│   ├── global_intelligence/
│   ├── ai_intelligence/
│   ├── source_discovery/
│   ├── verification/
│   ├── followup/
│   ├── watchdog/
│   └── editorial_qa/
│
├── workflows/
│   ├── research_graph.py
│   ├── verification_graph.py
│   └── event_followup_graph.py
│
├── schemas/
│   ├── source.py
│   ├── event.py
│   ├── evidence.py
│   ├── run.py
│   └── trend.py
│
├── tests/
│
├── Dockerfile
├── docker-compose.yml
├── README.md
└── .nojekyll
```

---

# 53. PRIVATE OPERATIONAL STATE

Use a persistent self-hosted application datastore.

Preferred:

```text
PostgreSQL
```

Use the operational database for:

```text
- canonical events
- event relationships
- source registry state
- source health
- evidence
- claims
- timelines
- trend metrics
- run metadata
- cost ledger
- agent integration state
```

Paperclip's own internal storage must remain logically separate from the application's event database.

Do not couple the cyber application schema to Paperclip's private implementation details.

---

# 54. RAW INGESTION CACHE

Raw fetched content should be kept outside the public GitHub repository.

Use local persistent storage for:

```text
- raw feed payload
- API response cache
- transcripts
- temporary research material
- diagnostic data
```

Do not commit raw article bodies or large provider responses to Git.

Public GitHub data contains compact canonical intelligence.

---

# 55. STRANDS ROLE

Use Strands as a **specialist workflow engine**, not as the overall company operating system.

Good Strands use cases:

```text
Research
Verification
Evidence reconciliation
Complex event correlation
Threat analysis
Follow-up investigation
```

Use Graph/Workflow style execution.

Prefer:

```text
typed deterministic workflow
```

over an unrestricted autonomous swarm.

Example:

```text
EVENT
 ↓
CVE lookup ──────────┐
                     │
KEV lookup ──────────┤
                     │
Vendor research ─────┤
                     │
Tavily research ─────┤
                     ▼
               Evidence merge
                     ↓
               conflict check
                     ↓
               confidence
                     ↓
              event update
```

Every major workflow stage should have a validated Pydantic schema.

---

# 56. DETERMINISTIC VS AGENTIC

Deterministic:

```text
- fetching
- parsing
- retries
- hashing
- deduplication
- CVE regex extraction
- database operations
- source health
- scoring
- exporting
- publishing
```

Agentic:

```text
- interpretation
- complex event correlation
- evidence reconciliation
- severity reasoning
- source discovery
- research
- editorial interpretation
```

Do not spend tokens where ordinary Python is sufficient.

---

# 57. HERMES

Hermes is no longer the primary orchestrator.

It may remain as:

```text
optional external runtime
optional operational integration
optional notification integration
optional fallback automation
```

The architecture must work without Hermes.

Paperclip is the primary control plane.

---

# 58. PROXMOX DEPLOYMENT

Default architecture:

```text
Proxmox
  └── Dedicated Cyber Intelligence LXC/VM
        ├── Paperclip
        ├── Application worker
        ├── PostgreSQL
        └── supporting services
```

Use Docker Compose within the dedicated environment where appropriate.

Prefer isolation from unrelated Hermes/Home Assistant workloads.

The public frontend remains on GitHub Pages.

---

# 59. SECRETS

Never commit:

```text
OPENROUTER_API_KEY
TAVILY_API_KEY
GITHUB_TOKEN
X_API_KEY
DATABASE_PASSWORD
```

Use environment variables or mounted secrets.

Agents receive only the secrets required for their assigned task.

Example:

```text
Source Discovery:
    Tavily only

Codex:
    GitHub only

Cost Agent:
    OpenRouter metrics only

Publisher:
    GitHub publication permissions

Collectors:
    source/API credentials required for collection
```

---

# 60. AGENT PERMISSION MODEL

Use least privilege.

Example:

```text
AU Intelligence
    read sources
    collect web data
    write events
    no GitHub merge permission

Source Discovery
    read external web
    propose sources
    create tasks
    no direct production modification

Codex
    repository read/write via branch
    create PR
    no secret access unless specifically required

Verification
    read proposed change
    run validation
    report pass/fail

Publisher
    update generated public data
    GitHub publication permission only

Watchdog
    read health
    create incidents/tasks
    cannot silently disable security controls

Cost Agent
    read usage
    update degradation state
    cannot raise budget limits automatically
```

---

# 61. SOURCE HEALTH

Every source fetch should generate:

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

Track source health historically.

Detect:

```text
sudden zero items
sudden volume spike
parser drift
stale timestamps
repeated timeout
HTTP failures
unexpected content structure
```

---

# 62. SOURCE QUALITY SCORE

Maintain a source-quality profile based on observed data:

```text
reliability
freshness
relevance
parser_stability
duplicate_rate
independent_reporting_value
```

Do not allow a low-quality source to dominate prominence merely because it publishes many articles.

---

# 63. SOURCE AUTO-DEGRADATION

Example:

```text
3 failures → warning

5 consecutive failures → degraded

persistent structural failure → create engineering task

source fixed → validation phase

validation successful → active
```

If a source suddenly starts returning irrelevant content:

```text
degrade
 ↓
investigate
 ↓
Codex/source agent
 ↓
repair or retire
```

---

# 64. DATA VALIDATION

Every generated JSON dataset must pass:

```text
- JSON schema validation
- required field validation
- timestamp validation
- event ID uniqueness
- CVE format validation
- enum validation
- URL validation
- no secret scan
- no unexpected null explosion
```

Publication must fail closed if critical validation fails.

---

# 65. GITHUB PUBLISHING

Prefer ordinary Git/GitHub CLI for simple deterministic publication.

Use Paperclip/Codex GitHub integration where agentic repository operations are necessary.

The publishing process must:

```text
generate
 ↓
validate
 ↓
test
 ↓
commit
 ↓
push
 ↓
verify GitHub Pages output
```

Never push known-invalid generated data.

---

# 66. GITHUB PAGES CONSTRAINT

The public site is static.

Therefore:

```text
No runtime API keys
No runtime OpenRouter
No runtime Tavily
No private database access
No server-side rendering required
```

All processing happens before publication.

---

# 67. NOTIFICATION SYSTEM

Create an abstract notification interface.

The system should support at least:

```text
daily intelligence digest
critical immediate alert
system incident alert
self-healing notification
source discovery notification
weekly trend summary
```

Examples:

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

System action:
Published / monitoring
```

And:

```text
SYSTEM SELF-HEALING

Problem:
ACSC parser failed

Detection:
Watchdog

Fix:
Codex PR #123

Verification:
Passed

Current state:
Healthy
```

Do not hard-code the notification provider into business logic.

---

# 68. DAILY INTELLIGENCE REPORT

The follow-up/director system should generate:

```text
AUSTRALIA
- critical events
- important events
- major developments

GLOBAL
- major cyber events
- active exploitation
- major breaches

AI
- major AI developments
- AI security
- AI threats

DEVELOPING
- events worth watching

EMERGING
- trend changes

SYSTEM
- source health
- collector health
- publishing health
- cost

FOLLOW-UP
- events requiring monitoring
```

---

# 69. SELF-IMPROVEMENT LOOP

The organisation should periodically ask:

```text
What are we missing?

Which sources are failing?

Which sources produce poor quality?

Which topics are underrepresented?

Are we duplicating events?

Are our AI costs increasing?

Are summaries accurate?

Are there emerging categories?

Are there new researchers?

Are there new authoritative sources?

Are AU events being correctly prioritised?
```

The answer should create tasks.

---

# 70. FEEDBACK LOOP FOR NEW SOURCES

When a source is discovered and activated:

```text
Day 0:
validate

Runs 1–3:
monitor precision

Runs 4–7:
evaluate reliability

After validation:
assign long-term source-quality score
```

If poor:

```text
degrade
```

If useful:

```text
promote priority
```

---

# 71. EVENT RELATIONSHIPS

Events should support:

```text
related_event
follow_up_to
caused_by
exploits
affects
targets
uses
attributed_to
mitigated_by
resolves
```

Example:

```text
CVE
 ↓
Exploit
 ↓
Threat actor
 ↓
Ransomware campaign
 ↓
Australian organisation
```

This creates the beginning of an intelligence graph.

---

# 72. SEARCH / DISCOVERY QUERIES

Discovery queries should cover:

```text
Australian cybersecurity
Australian cyber attack
Australian ransomware
Australian data breach
Australian critical infrastructure
Australian vulnerability
Australian zero day
Australian AI security
Australian AI policy
AI security
AI agent security
AI red teaming
prompt injection
MCP security
LLM vulnerability
AI attack
AI malware
AI phishing
agent hijacking
zero day
actively exploited
ransomware
APT
CVE exploit
supply chain attack
cloud security
identity attack
```

The discovery agent may dynamically generate narrower queries.

---

# 73. FRONTEND THEME

Cyberpunk, but professional.

Design:

```text
dark background
monospace elements
subtle terminal aesthetic
cyan/green primary accent
red/orange reserved for critical/KEV
```

Do not turn everything red.

Red should mean something.

Use monospace for:

```text
CVE IDs
MITRE IDs
timestamps
system status
technical indicators
```

---

# 74. FRONTEND INFORMATION HIERARCHY

Critical information should visually stand out through:

```text
severity
prominence
freshness
AU relevance
exploitation status
```

not merely through large fonts.

Cards should subtly decay visually with prominence.

Do not hide important historical events simply because their visual prominence is lower.

---

# 75. COLLECTION STATUS

Show:

```text
LAST COLLECTION
NEXT EXPECTED COLLECTION
ACTIVE SOURCES
DEGRADED SOURCES
BROKEN SOURCES
EVENTS PROCESSED
NEW EVENTS
UPDATED EVENTS
```

This is a static representation of the latest completed run.

---

# 76. PUBLIC STATUS MUST BE HONEST

Do not show:

```text
LIVE
```

when the public site is only showing cached/static data.

Use:

```text
LAST UPDATED:
2026-09-29 12:00 UTC
```

and optionally:

```text
COLLECTION REPLAY
```

for the animation.

---

# 77. AGENT STATUS

The private Paperclip operations view should show:

```text
Agent
Role
Status
Last heartbeat
Current task
Success/failure
Cost
Recent incidents
```

The public website can expose only sanitized operational status.

---

# 78. OBSERVABILITY

Collect metrics for:

```text
collection_duration
source_success_rate
source_failure_rate
events_created
events_updated
events_archived
duplicate_rate
AI_calls
AI_cost
Tavily_calls
Tavily_cost
publication_time
publication_failures
agent_heartbeats
agent_failures
self_heal_count
```

Create alerts for abnormal values.

---

# 79. TEST STRATEGY

Tests must include:

## Unit tests

```text
- parsing
- normalisation
- dedupe
- event matching
- scoring
- decay
- CVE extraction
- JSON schema
```

## Integration tests

```text
- RSS collection
- API collection
- CVE lookups
- GitHub advisories
- OpenRouter
- Tavily
- database
- publishing
```

## Agent workflow tests

```text
- source discovery
- verification
- event correlation
- follow-up
- self-healing
```

## Failure tests

Simulate:

```text
feed unavailable
feed schema changed
OpenRouter unavailable
Tavily unavailable
GitHub unavailable
database unavailable
bad model output
invalid JSON
duplicate storm
incorrect source data
```

---

# 80. FAILURE PHILOSOPHY

One broken component must not kill the whole system.

Example:

```text
Tavily fails
→ RSS collection continues

OpenRouter fails
→ deterministic ingestion continues

One source fails
→ other sources continue

YouTube fails
→ cyber intelligence continues

GitHub publish fails
→ generated output retained locally and retried

One agent fails
→ other agents continue
```

The system should degrade gracefully.

---

# 81. NO SINGLE POINT OF AI FAILURE

AI is an enrichment layer, not the source of truth.

If AI becomes unavailable:

```text
collect
dedupe
cross-reference
score deterministic fields
publish
```

with:

```text
pending_enrichment = true
```

Then enrich later.

---

# 82. NO SINGLE SOURCE OF TRUTH FOR NEWS

Never trust one source simply because it ranks highly.

Use:

```text
source reliability
+
evidence
+
independence
+
corroboration
+
primary-source access
```

A social post can initiate an investigation.

It cannot automatically prove an incident.

---

# 83. PRIVACY / SECURITY

The private system may contain:

```text
API credentials
provider responses
agent logs
cost data
internal diagnostics
```

Keep these private.

The public website contains only sanitized intelligence.

Perform a secret scan before every GitHub publication.

---

# 84. CONTENT / COPYRIGHT RULE

Do not reproduce news articles.

Store:

```text
title
short original summary
source
URL
date
structured claims
metadata
```

Do not publish large copied excerpts.

---

# 85. SELF-HEALING GOVERNANCE

A self-healing system must also know when to stop.

Create:

```text
CIRCUIT_BREAKER
```

Examples:

```text
same automatic fix fails 3 times
 ↓
stop automatic retries
 ↓
open incident
 ↓
notify owner
```

Also stop automated modification if:

```text
test suite repeatedly fails
schema migration fails
security scan fails
unexpected file modifications occur
multiple agents disagree on state
Git merge conflict cannot be resolved safely
```

---

# 86. VERSION EVERYTHING IMPORTANT

Version:

```text
source registry
schemas
scoring algorithm
prompt templates
AI model configuration
MITRE dataset
classification taxonomy
```

Every event should contain the enrichment/scoring version used.

Example:

```json
{
  "pipeline_version": "1.4.0",
  "schema_version": "2",
  "scoring_version": "3",
  "enrichment_version": "5"
}
```

---

# 87. REPRODUCIBILITY

Every run should have:

```text
run_id
start_time
end_time
pipeline_version
source versions
model versions
counts
errors
cost
```

Example:

```json
{
  "run_id": "2026-09-29T12:00:00Z",
  "new_events": 37,
  "updated_events": 82,
  "archived_events": 15,
  "sources_ok": 71,
  "sources_failed": 3,
  "ai_cost_usd": 0.84
}
```

---

# 88. ACCEPTANCE CRITERIA

The system is considered successful only when all of the following work.

## Intelligence

```text
✓ AU-first coverage
✓ Global cyber coverage
✓ AI coverage
✓ AI + cyber coverage
✓ Ground-truth CVE/KEV/MITRE enrichment
✓ Event-level deduplication
✓ Material-change detection
✓ Source independence
✓ Evidence-linked claims
✓ Follow-up monitoring
✓ Trend detection
```

## Autonomous operation

```text
✓ Paperclip schedules agents
✓ Agents have explicit ownership
✓ Agent heartbeats work
✓ Failed tasks are recoverable
✓ Source discovery works
✓ Codex can create source fixes
✓ Verification can independently validate fixes
✓ Watchdog detects failures
✓ Cost control works
✓ Self-healing circuit breaker works
```

## Public product

```text
✓ GitHub Pages
✓ Static only
✓ AU-first homepage
✓ Global feed
✓ AI feed
✓ active exploitation view
✓ event detail
✓ history navigation
✓ tag filtering
✓ map
✓ collection replay
✓ honest last-updated timestamp
✓ no exposed secrets
```

---

# 89. INITIAL PHASES

Build in these phases.

## Phase 1 — Foundation

```text
Paperclip
PostgreSQL
Python application
source registry
base source collectors
event schema
basic frontend
GitHub publication
```

## Phase 2 — Intelligence

```text
event correlation
deduplication
CVE/KEV/MITRE
AI enrichment
evidence
AU relevance
```

## Phase 3 — Autonomous organisation

```text
AU agent
Global agent
AI agent
source discovery
verification
Codex source engineering
watchdog
cost agent
```

## Phase 4 — Follow-up

```text
event monitoring
timeline
material change detection
trend engine
daily intelligence report
```

## Phase 5 — Self-improvement

```text
automatic source repair
automatic parser repair
automatic quality evaluation
source lifecycle management
self-healing
rollback
```

---

# 90. IMPORTANT IMPLEMENTATION RULE

Before writing production code:

1. Inspect the current Paperclip documentation/repository and use its current agent, scheduling, task, GitHub and budget APIs rather than assuming an older API.

2. Inspect the current Strands SDK documentation and use the currently supported Graph/Workflow API.

3. Verify every configured source URL/feed.

4. Verify the current OpenRouter API paths and usage/cost endpoints before implementing cost collection.

5. Do not blindly trust URLs, API shapes or SDK names from this design document.

6. Preserve the architectural intent even if implementation APIs have changed.

---

# 91. DECISION RULES

When deciding whether something belongs in the platform:

### Include if it is:

```text
cybersecurity
AI
AI security
AI + cyber
threat intelligence
vulnerability intelligence
security research
cyber policy
AI policy with meaningful security relevance
critical infrastructure security
security technology
```

### De-prioritise if:

```text
generic technology news
generic AI product marketing
celebrity/startup news
AI entertainment
irrelevant vendor marketing
```

unless it has clear security/intelligence relevance.

---

# 92. THE PLATFORM'S CORE QUESTION

Every new piece of information should ultimately answer one or more of:

```text
WHAT HAPPENED?

HOW IMPORTANT IS IT?

WHO IS AFFECTED?

WHO IS BEHIND IT?

WHAT TECHNOLOGY IS INVOLVED?

IS IT BEING EXPLOITED?

IS AUSTRALIA AFFECTED?

WHAT EVIDENCE SUPPORTS IT?

WHAT CHANGED SINCE THE LAST UPDATE?

WHAT SHOULD WE WATCH NEXT?
```

---

# 93. FINAL OPERATING MODEL

The completed system should behave like this:

```text
                    ┌───────────────────────┐
                    │      PAPERCLIP        │
                    │   CYBER INTEL CO      │
                    └───────────┬───────────┘
                                │
           ┌────────────────────┼────────────────────┐
           │                    │                    │
           ▼                    ▼                    ▼
       AU INTEL             GLOBAL INTEL         AI INTEL
           │                    │                    │
           └────────────────────┼────────────────────┘
                                │
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
                    ┌───────────┴───────────┐
                    ▼                       ▼
                NEW EVENT              EXISTING EVENT
                    │                       │
                    └───────────┬───────────┘
                                ▼
                      MATERIAL CHANGE TEST
                                │
                                ▼
                          EVIDENCE ENGINE
                                │
                                ▼
                         AI ENRICHMENT
                                │
                                ▼
                     CVE / KEV / MITRE / OSV
                                │
                                ▼
                       AU RELEVANCE ENGINE
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
                                ▼
                         DEVELOPING EVENT
                                │
                                └─────────────┐
                                              │
WATCHDOG ──→ DETECT FAILURE ──→ CODEX FIX ───┤
                                              │
VERIFICATION ──→ TEST ──→ MERGE ─────────────┤
                                              │
                                              ▼
                                           CONTINUE
```

---

# 94. END STATE

The final product should feel like:

```text
NOT:

"Here are today's cybersecurity articles."

BUT:

"I operate a continuously running cyber + AI intelligence organisation.
It discovers new information,
determines whether it is actually new,
correlates reports,
checks authoritative evidence,
understands Australian relevance,
tracks what changes,
follows important incidents,
finds missing sources,
repairs broken collectors,
keeps its costs under control,
publishes the intelligence,
and tells me when something important happens."
```

The system should be able to run for long periods with minimal human intervention while keeping all important changes auditable, testable, reversible and visible.

## PRIMARY ARCHITECTURAL DECISION

```text
Paperclip = autonomous organisation / control plane
Strands   = specialist reasoning workflow engine
Codex     = autonomous software engineer
Python    = deterministic system machinery
Postgres  = private operational intelligence state
GitHub    = code + public generated data
GitHub Pages = public intelligence product
```

Do not collapse these responsibilities into one agent or one framework.

Build the system as a **self-operating cyber intelligence organisation**, not merely an automated news scraper.
