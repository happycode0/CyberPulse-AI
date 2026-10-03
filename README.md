<div align="center">

# CyberPulse-AI

**An autonomous Australia-first Cybersecurity + AI Intelligence Organisation**

`DISCOVER → COLLECT → NORMALISE → CORRELATE → VERIFY → ENRICH → CROSS-REFERENCE`
`→ SCORE → PUBLISH → MONITOR → FOLLOW UP → SELF-DIAGNOSE → SELF-FIX → CONTINUE`

</div>

---

## What this is

Not a news aggregator. A small, permanently-running intelligence organisation staffed by
fifteen named agents who each own a beat.

Deterministic Python collects from ~60 live-validated sources on three cadences and
resolves many reports into **one canonical event**. Ground truth comes from CISA KEV,
CVE.org, CISA Vulnrichment, EPSS, OSV and MITRE ATT&CK — never from a model's memory. AI is
an enrichment layer that judges severity, extracts entities, reasons about Australian
relevance and reconciles conflicting evidence, always from retrieved facts.

[Paperclip](https://github.com/paperclipai/paperclip) is the control plane where the agents
live, wake, delegate and spend budget. [Strands](https://strandsagents.com) runs the
multi-step specialist workflows. When a source breaks, the watchdog opens an incident, an
engineer agent fixes the parser on a branch, an independent auditor verifies it on a
different model, and **you** approve the merge.

The public face is a cyberpunk HUD on GitHub Pages that publishes only canonical,
evidence-linked intelligence — and is honest that it is a snapshot, not a live feed.

> **📖 [`PLAN.md`](PLAN.md) is the authoritative design.** It includes the full end-to-end
> validation, the agent roster, the data model, the UI spec and the staged build. Read it
> before changing anything architectural.

---

## Design at a glance

```text
        OWNER ──(NetBird VPN only)──► PAPERCLIP  control plane
                                       127.0.0.1:3100
                                          │
         ┌────────────────────────────────┼────────────────────────────────┐
         ▼                                ▼                                ▼
   INTELLIGENCE                     ENGINEERING                      OPERATIONS
   MORPHEUS · ZION                  WHEELJACK · TRON                 TELETRAAN · ROGUE
   BLASTER · WINTERMUTE                                              LINK · SERAPH
   TACHIKOMA · DECKARD                                               LIBRARIAN · PROWL
   VOIGHT                                                            RIPPERDOC
         └────────────────────────────────┼────────────────────────────────┘
                                          ▼
                              CYBER WORKER  (Python 3.13)
                              own scheduler: FAST 1h / NORMAL 4h
                                          │
                        ┌─────────────────┼─────────────────┐
                        ▼                 ▼                 ▼
                 OpenRouter          Tavily         PostgreSQL 17
                 fast / strong       discovery      + pgvector
                                          │
                                          ▼
                    git → orphan `data` branch → Actions → GitHub Pages
```

| | |
|---|---|
| **Control plane** | Paperclip (agents, issues, delegation, heartbeats, budgets, approvals, audit) |
| **Deterministic layer** | Python 3.13 worker — collection, resolution, scoring, publishing, scheduling |
| **Specialist workflows** | Strands Agents 1.57.1 Graph |
| **Engineer agent** | OpenCode CLI via OpenRouter, branch + PR only |
| **Inference** | OpenRouter — free tier first, then two paid tiers. **US$20/month hard cap, output ≤ US$1/M** |
| **Discovery** | Tavily (free tier, ~30 searches/day) |
| **State** | PostgreSQL 17 + pgvector — one instance, two databases |
| **Host** | One Debian 13 QEMU VM on Proxmox VE 9 · 6 vCPU / 12 GB / 150 GB |
| **Public site** | Static HTML/CSS/JS on GitHub Pages — no build step, no secrets |

---

## The crew

Personas are presentation, not authority — that comes from each agent's Paperclip role,
permissions and budget. **Deterministic agents cost zero tokens**, which is the whole point
of the split. Full task definitions in [`PLAN.md` §4](PLAN.md).

| Callsign | Origin | Beat | Runtime |
|---|---|---|---|
| **MORPHEUS** · *"I can only show you the door."* | Matrix | Intelligence Director / Chief Editor | LLM, strong, 1×/day |
| **ZION** · *"Home ground. Our watch."* | Matrix | Australian desk | LLM, fast |
| **BLASTER** · *"I'm picking up chatter on every band."* | Transformers | Global cyber desk | LLM, fast |
| **WINTERMUTE** · *"The model is the attack surface."* | Neuromancer | AI + cyber↔AI convergence | LLM, fast |
| **TACHIKOMA** · *"Ooh — what's this one?"* | Ghost in the Shell | Source discovery | LLM + Tavily |
| **SERAPH** · *"I had to be sure."* | Matrix | Source verification gate | Deterministic |
| **PROWL** · *"One event. Many threads."* | Transformers | Event correlation, material change | Deterministic + Strands |
| **LIBRARIAN** · *"Cite it or it didn't happen."* | Snow Crash | KEV / CVE / EPSS / OSV / ATT&CK | Deterministic |
| **DECKARD** · *"The case stays open until it's patched."* | Blade Runner | Follow-up, developing events | LLM + Strands |
| **VOIGHT** · *"Says who?"* | Blade Runner | Editorial QA, publication veto | LLM, strong, gated |
| **LINK** · *"Transmission clean. Here's the diff."* | Matrix | Publishing + notifications | Deterministic |
| **ROGUE** · *"Nothing in this city is free."* | Cyberpunk 2077 | Cost / FinOps, degradation tiers | Deterministic |
| **RIPPERDOC** · *"Better chrome just came in."* | Cyberpunk 2077 | Model scout — cheaper/better models, free first | Deterministic + tier 1 |
| **TELETRAAN** · *"Anomaly detected on the grid."* | Transformers | Watchdog / SRE, self-healing | Deterministic + LLM on incident |
| **WHEELJACK** · *"She'll be right — after the tests pass."* | Transformers | Source & platform engineer | OpenCode, incident-driven |
| **TRON** · *"I fight for the users."* | Tron | Independent code verification | LLM, different family |

---

## Build stages

🔴 needs you · 🟢 I can do it

| Stage | What | Who | Status |
|---|---|---|---|
| **0** | Prerequisites: accounts, keys, repo visibility, optional VM | 🔴 | ✅ done |
| **1** | Foundation: schema, collectors, event resolution, scoring, publisher, **full site** | 🟢 | ✅ done, live on the VM |
| **2** | Ground truth + AI enrichment + AU relevance + evidence + cost ledger | 🟢 | ✅ done, live on the VM |
| **3** | Material change, source lineage, trends, decay, extra adapters | 🟢 | ✅ done, live on the VM |
| **4** | Proxmox VM + Paperclip + first agents | 🔴🟢 | ▶ in progress |
| **5** | Full crew + follow-up + Telegram + public crew page | 🟢🔴 | ▶ built, live on the VM; agents wait for you to resume them |
| **6** | Self-healing: watchdog → incident → fix → audit → your approval | 🟢🔴 | ▶ watchdog and breaker live on the VM; the crew's part waits for your token and routine |
| **7** | Hardening: backups, observability, failure injection, runbooks | 🟢🔴 | ✅ restore rehearsed, 53 failure tests pass on the VM; the off-host backup target is yours |

**Stages 1–3 run entirely on your laptop or WSL.** No Proxmox needed until Stage 4, so you
get a working public intelligence site before touching hardware.

---

# Deployment

## Stage 0 — Prerequisites 🔴

Everything here needs a human. Work through it in order.

### 0.1 Make the repository public 🔴

**Required.** GitHub Pages needs a public repository on the Free plan, and
`happycode0/CyberPulse-AI` is currently private.

1. <https://github.com/happycode0/CyberPulse-AI/settings> → **Danger Zone** → **Change repository visibility** → **Make public**.

Before you do, confirm nothing sensitive is in the history:

```bash
git log --all --oneline
git log --all --name-only --pretty=format: | sort -u   # every path ever committed
```

The repo currently holds only markdown, so this should be clean. If you would rather keep
the code private, tell me and I will switch the design to a separate public site repo
instead (see `PLAN.md` §1, decision 4).

> **Understand the trade:** GitHub's docs state *"GitHub Pages sites are publicly available
> on the internet, even if the repository for the site is private."* The site is public
> either way. Everything secret lives in `.env`, which is git-ignored, and LINK runs a
> secret scan before every publish.

### 0.2 OpenRouter — the AI budget 🔴

1. Sign up at <https://openrouter.ai> and add **US$20** of credit.
2. <https://openrouter.ai/keys> → **Create key**.
   - Name: `cyberpulse-worker`
   - **Credit limit: `20`** ← this is the hard stop; the platform cannot exceed it
   - **Limit reset: `monthly`**
3. Copy the key (shown once) into `.env` as `OPENROUTER_API_KEY`.

> Create a **second** key named `cyberpulse-agents` with a `5` limit if you want agent
> spend capped separately from pipeline spend. Recommended once you reach Stage 4.
>
> Do **not** create a management key. The platform only needs `GET /api/v1/key`, which works
> with the normal key, so it never holds admin-level authority over your account.

### 0.3 Tavily — discovery 🔴

1. Sign up at <https://tavily.com> — the free tier gives **1,000 credits/month**, no card required.
2. Copy the key into `.env` as `TAVILY_API_KEY`.

Basic search costs 1 credit. We pin `search_depth: basic` and `auto_parameters: false` and
cap the daily discovery lane at ~30 searches, so the free tier is sufficient. The dev key is
limited to 100 requests/minute, which is ample.

### 0.4 NVD API key — optional but recommended 🔴

Free, and raises the rate limit from 5 to 50 requests per 30 seconds:
<https://nvd.nist.gov/developers/request-an-api-key> → `.env` as `NVD_API_KEY`.

Note from validation: NVD now defers most new CVEs and authors almost no CVSS scores, so we
take severity from the CNA record and CISA Vulnrichment instead. NVD is a fallback only —
leaving this blank is fine.

### 0.5 GitHub tokens 🔴

<https://github.com/settings/personal-access-tokens/new> — **fine-grained**, scoped to
`happycode0/CyberPulse-AI` only.

**Publisher token** (`CYBERPULSE_PUBLISH_TOKEN`) — Stage 1:

| Permission | Level |
|---|---|
| Contents | Read and write |
| Metadata | Read (automatic) |

**Engineer token** (`CYBERPULSE_ENGINEER_TOKEN`) — Stage 6, create it later:

| Permission | Level |
|---|---|
| Contents | Read and write |
| Pull requests | Read and write |
| Metadata | Read (automatic) |

Set a **90-day expiry** on both and put a calendar reminder to rotate. GitHub caps
fine-grained tokens at 50 per user and recommends a GitHub App for long-lived automation —
worth revisiting in Stage 7.

### 0.6 Telegram notifications 🔴

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts → copy the token.
2. Send your new bot any message, then visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy `result[0].message.chat.id`.
3. `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

### 0.7 Enable GitHub Pages 🔴

<https://github.com/happycode0/CyberPulse-AI/settings/pages> → **Source: GitHub Actions**.

Do **not** choose "Deploy from a branch". A custom Actions workflow avoids the soft limit of
10 builds per hour — GitHub's docs confirm *"this limit does not apply if you build and
publish your site with a custom GitHub Actions workflow."*

### 0.8 Create your `.env` 🔴

```bash
cp .env.example .env
$EDITOR .env
chmod 600 .env
```

`.env` is git-ignored and must never be committed, logged, pasted into a prompt, or included
in a screenshot.

### 0.9 Local prerequisites 🔴

Docker Engine with the Compose plugin, following the official Debian instructions
(<https://docs.docker.com/engine/install/debian/>). On WSL, Docker Desktop with WSL
integration works. Verify:

```bash
docker --version && docker compose version
```

---

## Stage 1 — Run it locally 🟢

Once `.env` exists:

```bash
docker compose up -d db          # PostgreSQL 17 + pgvector
docker compose run --rm worker python -m worker.db.migrate
docker compose run --rm worker python -m worker.main --lane fast --once
docker compose run --rm worker python -m worker.publish.build
python -m http.server 8000 --directory site   # → http://localhost:8000
```

Then publish for real:

```bash
docker compose run --rm worker python -m worker.publish.push
```

Your site appears at **https://happycode0.github.io/CyberPulse-AI/**.

Run the tests:

```bash
docker compose run --rm worker pytest -q
```

---

## Stage 4 — Proxmox VM 🔴

Only needed when you want the agent organisation running 24/7.

### 4.1 Why a VM and not an LXC 🔴

The source design called for an LXC. Validation changed that — the details are in
[`PLAN.md` §2.1](PLAN.md), but in short:

- Proxmox's own docs: *"nesting containers inside a Proxmox QEMU VM remains a recommended practice"*, and running application containers directly is *"currently a tech preview"*.
- PVE 9.0 release notes list **Docker-inside-LXC as a known issue (bug #6538)**.
- Docker in LXC needs `nesting=1`, which *"will expose procfs and sysfs contents of the host to the guest"* — a poor trade on a box running an autonomous code agent.
- **Debian 12's regular security support ended 2026-07-11.** Debian 13 is current stable.

### 4.2 Create the VM 🔴

**Check the host first — these numbers are sized for the host, not copied blind:**

```bash
lscpu | grep -E '^(CPU\(s\)|Core|Thread|Model name)'
free -g | head -2
pvesm status            # free space on the storage you will import into
```

The commands below assume a host with roughly **8 threads, 32 GB RAM and 90 GB of free
storage** (the box this was commissioned on: 4-core/8-thread i7-7700HQ, 31 GiB, a single
94 GB disk). Two of the figures are the ones to adjust:

- **`--cores`** — 4, not 6. The work is IO-bound HTTP collection plus Postgres, so cores
  buy little here, and leaving half a 4-core host to PVE itself costs nothing in throughput.
- **`scsi0` size** — 60G. Postgres never stores raw article bodies (§3), so the database
  stays small; the raw cache is a rotating filesystem cache. On **thin** storage
  (`local-lvm` is thin by default) an over-sized disk is accepted at creation and then
  fills silently until the pool wedges — which takes the database down hard. Size it to
  fit the pool with headroom, and grow it later with `qm disk resize` if you need to.

```bash
# Debian 13 "trixie" cloud image
cd /var/lib/vz/template/iso
wget https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2

qm create 200 --name cyberpulse \
  --memory 12288 --balloon 8192 \
  --cores 4 --cpu host \
  --net0 virtio,bridge=vmbr0 \
  --scsihw virtio-scsi-single \
  --agent enabled=1 \
  --ostype l26

qm importdisk 200 debian-13-genericcloud-amd64.qcow2 local-lvm
qm set 200 --scsi0 local-lvm:vm-200-disk-0
qm disk resize 200 scsi0 60G
qm set 200 --boot order=scsi0 --ide2 local-lvm:cloudinit --serial0 socket --vga serial0
qm set 200 --ciuser YOURUSER --sshkeys ~/.ssh/id_ed25519.pub --ipconfig0 ip=dhcp
qm start 200
```

`--agent enabled=1` matters: the QEMU guest agent lets `vzdump` snapshot backups use
`fsfreeze` for a consistent database backup.

> **A single-disk host cannot back itself up.** `vzdump` to local storage is not a backup:
> it dies with the disk it sits on. If the host has one disk, §4.6 needs an external
> target — a NAS over NFS/SMB, a USB disk, or another machine running PBS.

### 4.3 Prepare the guest 🔴

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y qemu-guest-agent git curl ca-certificates
sudo systemctl enable --now qemu-guest-agent

# Docker, per docs.docker.com/engine/install/debian
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER   # log out and back in
```

Cap the logs, or they grow without limit — Docker's `json-file` driver defaults to
unlimited:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "log-driver": "local", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
sudo systemctl restart docker
```

> **Note:** published Docker ports bypass `ufw` and `firewalld`. Every service in this
> stack binds to `127.0.0.1`, so nothing is exposed even without a host firewall.

### 4.4 NetBird 🔴

Join the VM to your existing NetBird mesh so the Paperclip dashboard is reachable from your
devices and nowhere else. Install per <https://docs.netbird.io/how-to/getting-started>, then
confirm the VM's NetBird IP and reach Paperclip at `http://<netbird-ip>:3100`.

Paperclip binds to `127.0.0.1:3100` by default. To expose it on the NetBird interface only,
set `PAPERCLIP_BIND=custom` and `PAPERCLIP_BIND_HOST=<netbird-ip>`. **Never** bind `0.0.0.0`
or forward a router port.

### 4.5 Claim the Paperclip instance 🔴

Deployment mode is `authenticated` + `private`, so the first person to sign in claims the
instance. Do this immediately after the first start, while only you can reach it.

```bash
docker compose up -d
docker compose logs -f server   # wait for migrations to finish
```

Open `http://<netbird-ip>:3100`, sign in, and choose **Claim this instance**. Then, in
Company Settings, harden the defaults — validation found several are permissive:

- [ ] **Require board approval for new agents** — defaults to **off**
- [ ] **Disable sign-up** (`PAPERCLIP_AUTH_DISABLE_SIGN_UP=true`) once you have your account
- [ ] Confirm secrets **strict mode** is on
- [ ] Confirm telemetry and announcements are off (both default to on)

### 4.6 Back up the two things that matter 🔴

**You need both the database and the secrets master key. Neither is sufficient alone.**

```bash
# in the VM
docker compose exec -T db pg_dump -U cyberpulse cyber_intel | gzip > /backup/cyber_intel.sql.gz
docker compose exec -T db pg_dump -U cyberpulse paperclip  | gzip > /backup/paperclip.sql.gz
docker compose cp server:/paperclip/instances/default/secrets/master.key /backup/master.key
```

On the Proxmox host, add a `vzdump` job in **snapshot** mode targeting Proxmox Backup
Server on a *separate* machine, with `prune-backups` set to
`keep-daily=7,keep-weekly=4,keep-monthly=3` and **Repeat missed** enabled.

> Do not rely on bind mounts for backup coverage. Proxmox's docs are explicit: *"The
> contents of bind mount points are not backed up when using vzdump."* This stack uses named
> Docker volumes on the VM's own disk, which `vzdump` does capture.

---

## Operations

### Everyday commands

```bash
docker compose ps                                  # what's running
docker compose logs -f worker                      # follow the pipeline
docker compose run --rm worker python -m worker.main --lane fast --once
docker compose run --rm worker python -m worker.ops.health
docker compose run --rm worker python -m worker.ops.cost --month     # ROGUE's ledger
docker compose run --rm worker python -m worker.ops.sources --status # SERAPH's view
docker compose run --rm worker pytest -q
```

### Cost control

The hard stop is the **OpenRouter key limit**, set at the provider. The platform cannot
exceed it regardless of any bug on our side. Inside that, ROGUE degrades progressively:

| Budget remaining | Behaviour |
|---|---|
| > 50% | Full ladder: free tier → cheap → strong, by task |
| 20–50% | Strong tier restricted to critical and KEV-linked |
| < 20% | Free tier only, and only critical / high / KEV-linked / developing |
| Free daily cap hit | Falls through to the cheap paid tier |
| Exhausted | Paid AI off; free tier and deterministic collection continue; events marked `pending_enrichment` |

### Secret hygiene

Non-negotiable, and enforced in code:

- `.env` is git-ignored — never committed, logged, or pasted into any prompt
- Secrets never appear in Python, YAML, JSON, Dockerfiles, frontend JS, published data or README examples
- Each agent gets only the credentials its job requires ([`PLAN.md` §4.4](PLAN.md))
- WHEELJACK, the engineer agent, holds **exactly one** credential: a branch-scoped GitHub token
- LINK secret-scans every payload before publishing and **fails closed**
- Ingested article text is treated as untrusted input, given to models as data with no tools available, and never forwarded to the engineer agent

### Recovery

```bash
./ops/backup.sh /mnt/backup                                     # databases, Paperclip's keys, .env
./ops/restore.sh rehearse /mnt/backup/cyberpulse-<UTC time>     # restore into a throwaway, compare
./ops/restore.sh fresh /mnt/backup/cyberpulse-<UTC time> --yes  # rebuild onto a new, empty VM
```

A backup you have never restored is a hypothesis. This one was rehearsed on the VM on
2026-10-03: every table's row count matched ([Stage 7](docs/wiki/stage-7-hardening.md)).

---

## Honest limitations

Stated plainly, because an intelligence product that oversells itself is worthless:

- **The public site is not live.** It shows the last completed collection run, labelled with its timestamp. The pipeline animation is a replay, and says so.
- **AI severity and MITRE mappings are labelled `AI SUGGESTED`.** They are not official attribution. CVSS, KEV status and CVE facts always come from authoritative sources, or are recorded as `unknown`.
- **Missing data is `unknown`, never "low".** Absent CVSS or EPSS scores are common for new CVEs and are not evidence of low risk.
- **Coverage is incomplete by construction.** SecurityWeek blocks automated access; ASD, the MSRC blog and Anthropic publish no feed. Known gaps are tracked in `config/sources.yaml` with reasons.
- **Self-healing is bounded.** Agents may repair parsers, adapters, transformations, tests and frontend defects. Auth, secrets, permissions, deployment, budgets and schema migrations require your approval. Circuit breakers halt after 3 failed attempts.
- **Summaries are original and short.** We do not reproduce articles. Every event links to its sources.
- **GitHub Pages is not for commercial use**, per GitHub's prohibited-uses policy. This is a personal intelligence product.

---

## Documentation

| File | What |
|---|---|
| [`PLAN.md`](PLAN.md) | **Authoritative design** — validation, crew, data model, UI spec, stages |
| [`docs/superpowers/plans/`](docs/superpowers/plans/) | Task-by-task implementation plans |
| [`config/sources.yaml`](config/) | Source registry with live-validation status |
| [`schemas/`](schemas/) | Published JSON Schema for the public data |
| [`ops/runbooks/`](ops/) | Incident runbooks |

---

## Contributing rule

**Always check current official documentation before implementing against any external
dependency.** This project was designed that way and it changed the architecture
substantially — see [`PLAN.md` §2](PLAN.md) for eighteen findings that contradicted
assumptions, including a security default that would have exposed every secret in the stack.

Docs beat this README. This README beats memory.

---

<div align="center">

*Built for Australian defenders. Cite it or it didn't happen.*

</div>
