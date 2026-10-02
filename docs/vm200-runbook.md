# the VM build runbook — every command, and who runs it

[Wiki home](wiki/README.md) — the owner's step-by-step guide and "what's next". This file is the
build record behind it; each wiki stage links to its Part here.

A single ordered record of the commands that build the CyberPulse-AI host: the ones already run,
the ones still to run, and the ones that only the owner can run. README §4 is the reference
design; this is the log of applying it to the actual box, with the real values filled in.

**Read the legend before anything else — the two columns are not interchangeable.**

| Mark | Who | Where |
|---|---|---|
| 🔴 | **Owner, by hand** | Proxmox host shell, or a browser. Claude has no access to the PVE host — only to the guest. |
| 🟢 | **Claude, over SSH** | Inside the guest, as `deploy-user@10.0.0.0`. |

## The box as it actually is

Measured on the VM, 2026-10-01 (disk and status re-measured 2026-10-02), not copied from the
design table:

| | |
|---|---|
| VM | 200 `cyberpulse`, Debian GNU/Linux 13 (trixie), kernel 6.12.107+deb13-cloud-amd64 |
| Address | `10.0.0.0/24` on `eth0` (DHCP), reachable from the WSL box |
| Login | `deploy-user`, passwordless sudo, key-only (`~/.ssh/cyberpulse_vm_ed25519`) |
| CPU / RAM | 4 vCPU / 11 GiB usable (created with `--memory 12288 --balloon 8192`) |
| Disk | 59 GB usable on `/dev/sda1`, 42 GB free with all three images pulled, Paperclip's included (resized — see Part 2) |
| Docker | 29.8.2, Compose v5.5.1, `local` log driver capped at 3 × 10 MB |
| Status | **Stage 1 is running:** FAST every 15 min, NORMAL every 4 h, ground truth every 6 h, the `data/*.json` files rebuilt after each run — but **not pushed to GitHub yet** (Part 5d). **Paperclip is running and claimed** (Part 6). |

An SSH alias is configured on the WSL box, so every 🟢 command below is reachable as
`ssh cyberpulse-vm '<command>'`:

```sshconfig
Host cyberpulse-vm
  HostName 10.0.0.0
  User deploy-user
  IdentityFile ~/.ssh/cyberpulse_vm_ed25519
  IdentitiesOnly yes
```

---

## Part 1 🔴 — Create the VM *(done)*

Recorded for reproducibility. Run on the Proxmox host as root. These are README §4.2's commands
with this host's corrections already applied: `--cores 4` (not 6) and a **60G** disk (not 150G),
because `local-lvm` is thin-provisioned and an oversized disk is accepted at creation and then
fills silently until the pool wedges, which takes Postgres down hard.

```bash
# Pre-flight
qm list                           # confirm 200 is free
pvesm status                      # confirm the storage name and free space
ls -l /root/cyberpulse-keys.pub   # must hold BOTH the owner's key and Claude's

# 1. Debian 13 "trixie" cloud image
cd /var/lib/vz/template/iso
wget https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2

# 2. Create
qm create 200 --name cyberpulse \
  --memory 12288 --balloon 8192 \
  --cores 4 --cpu host \
  --net0 virtio,bridge=vmbr0 \
  --scsihw virtio-scsi-single \
  --agent enabled=1 \
  --ostype l26

# 3. Import the image (read the output — it prints the volume id it created)
qm importdisk 200 debian-13-genericcloud-amd64.qcow2 local-lvm

# 4. Attach it
qm set 200 --scsi0 local-lvm:vm-200-disk-0

# 5. Grow to 60G  <-- THIS ONE DID NOT TAKE EFFECT. See Part 2.
qm disk resize 200 scsi0 60G

# 6. Boot order, cloud-init drive, serial console
qm set 200 --boot order=scsi0 --ide2 local-lvm:cloudinit --serial0 socket --vga serial0

# 7. User + keys + DHCP
qm set 200 --ciuser deploy-user --sshkeys /root/cyberpulse-keys.pub --ipconfig0 ip=dhcp

# 8. Start
qm start 200
```

Notes worth keeping, both learned the hard way:

- Step 7 fails with `Configuration file 'nodes/pve/qemu-server/200.conf' does not exist` if run
  before step 2. It is the seventh command, not the first.
- Step 6 is not optional. The genericcloud image has no graphical console, so without
  `--serial0 socket --vga serial0` the web console is a black screen with no way in. Attach with
  `qm terminal 200` (escape: `Ctrl-O`).
- `--agent enabled=1` is what lets `vzdump` use `fsfreeze` for a consistent database backup.
- If `qm importdisk` reports itself deprecated on PVE 9.2, steps 3–4 collapse into
  `qm set 200 --scsi0 local-lvm:0,import-from=/var/lib/vz/template/iso/debian-13-genericcloud-amd64.qcow2`.

---

## Part 2 🔴 — Resize the disk *(done)*

`lsblk` on the guest reported `sda` as **3G**, which is the cloud image's native size: step 5 above
never took effect. The root filesystem was 2.8 GB with ~1.6 GB free.

That is not enough to continue, and the failure mode if we try is bad rather than merely
inconvenient. Docker Engine is roughly 400 MB installed; `pgvector/pgvector:0.8.6-pg17-trixie`
unpacks to several hundred more; the worker image adds a Python base plus dependencies; and then
Postgres needs room to actually store data. Filling the root filesystem of a database host
corrupts, it does not politely stop.

**On the Proxmox host:**

```bash
qm disk resize 200 scsi0 60G
```

This works on a running VM — growing a disk needs no shutdown. It only enlarges the *virtual*
disk; the partition and filesystem inside stay their old size until grown.

**Then 🟢 Claude grows the partition and filesystem online, no reboot:**

```bash
sudo growpart /dev/sda 1     # growpart is already present at /usr/bin/growpart
sudo resize2fs /dev/sda1
df -h /                      # expect ~59G
```

`/dev/sda1` starts at sector 262144, after `sda14` and `sda15`, so it is the last partition on the
disk and can grow into the new space directly.

Both ran cleanly with no reboot: `growpart` moved the partition end from sector 6289407 to
125829086, `resize2fs` took the filesystem to 15,695,867 4k blocks, and the guest had already seen
the larger disk so no SCSI rescan was needed. `df -h /` now reports 59G.

---

## Part 3 🟢 — Prepare the guest *(done)*

Already done:

```bash
sudo apt-get update
sudo apt-get install -y qemu-guest-agent git ca-certificates
sudo systemctl enable --now qemu-guest-agent
git clone https://github.com/happycode0/CyberPulse-AI.git ~/CyberPulse-AI
```

`qemu-guest-agent` is **active**. `systemctl is-enabled` reports `static`, which is correct and
not a failure: the unit is activated by the virtio device rather than by a `WantedBy`, so there is
nothing to enable. Verify with `systemctl is-active qemu-guest-agent`, and from the Proxmox host
with `qm guest cmd 200 network-get-interfaces`.

The system had 0 pending upgrades, so README §4.3's `apt upgrade` is a no-op on this image today.

### Docker (README §4.3) *(done)*

```bash
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
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy-user
```

Then cap the logs. Docker's default `json-file` driver grows without limit, which on a 60 GB disk
is a slow-motion outage:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "log-driver": "local", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
sudo systemctl restart docker
```

The `usermod` needs a new login to take effect; over SSH each command is a fresh session, so this
resolves itself — `docker version` as `deploy-user` without `sudo` confirmed it.

Write that script to the guest by piping it, not by quoting it:

```bash
ssh cyberpulse-vm 'bash -s' <<'REMOTE'
...
REMOTE
```

The `sudo tee ... <<EOF` heredocs are nested inside, and passing the whole thing as a quoted `ssh`
argument breaks on the inner quoting. Note also that the `Suites:` line must stay unquoted so
`$VERSION_CODENAME` expands on the guest — it resolved to `trixie`.

---

## Part 4 🟢 — The `.env` *(done)*

`worker/settings.py` requires exactly one variable: `database_url`. Everything else is optional
with a default, so collection needs only the database block. The Postgres password is generated
on the VM and exists nowhere else.

```bash
cd ~/CyberPulse-AI
PGPASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)"
umask 077                     # so the file is never briefly world-readable between > and chmod
cat > .env <<EOF
POSTGRES_USER=cyberpulse
POSTGRES_PASSWORD=${PGPASS}
POSTGRES_DB=cyber_intel
DATABASE_URL=postgresql://cyberpulse:${PGPASS}@db:5432/cyber_intel
TZ=Australia/Sydney
LOG_LEVEL=INFO
EOF
chmod 600 .env
```

Three deliberate choices here:

- **Fresh credentials, not copied.** The laptop's `.env` is not transferred. It lives on a
  `/mnt/c` Windows mount where it is `-rwxrwxrwx` and cannot be `chmod`-ed; copying it would
  spread that exposure rather than contain it. On the VM, `chmod 600` actually holds.
- **No `CYBERPULSE_PUBLISH_TOKEN` at first.** It is only needed by the publish step (Part 5d). A
  collection-only host has no reason to hold a token that can write to a public repository, so it
  was added when publishing started (2026-10-02) and not before.
- **`.env` is git-ignored and must stay so.** The repository is public. Verified with
  `git check-ignore -v .env`, which reported `.gitignore:2`.

**The file has grown since, on 2026-10-02:** the owner added the API keys and the publish token
(wiki [Stage 0](wiki/stage-0-prerequisites.md)), and the Paperclip section was appended (wiki
[4a step 0](wiki/stage-4a-paperclip-setup.md#0--add-the-paperclip-settings-to-env-once)). The
owner also set a new `POSTGRES_PASSWORD`; Postgres only reads that when its volume is first
created, so the database role was brought into line with `ALTER ROLE` (Part 6). The block above is
the original six lines, not the current file. Check what is there by name only:
`grep -oE '^[A-Z0-9_]+' .env`.

Validate with `docker compose config -q`, not `docker compose config`. The unquiet form prints the
fully resolved file, `POSTGRES_PASSWORD` included, into your terminal and scrollback. `-q` exits 0
on success and says nothing.

---

## Part 5 🟢 — Bring up Stage 1 *(done; the push to GitHub is not scheduled yet)*

```bash
cd ~/CyberPulse-AI
docker compose config -q              # validates .env resolves — Stage 0's exit criterion
docker compose build                  # builds the worker image from Dockerfile.worker
docker compose up -d db               # Postgres with pgvector; waits for its own healthcheck
```

> **Add `</dev/null` to every `docker compose run` and `exec` inside a piped script.** Both attach
> the caller's stdin, so when the script itself arrived on stdin (`ssh host 'bash -s' <<'EOF'`) the
> container eats the remaining lines and the rest of the script silently never runs. `-T` disables
> the TTY but does *not* detach stdin. This cost two confusing half-executed runs here.

The db came up healthy in ~12s and carries both required extensions: `pg_trgm 1.6` and
`vector 0.8.6`.

**a. Migrate.** Creates the schema and the required extensions (`pg_trgm` and `vector` — the
first is not optional, `worker/db/events.py:124` uses `similarity()` for trigram event resolution):

```bash
docker compose run --rm -T worker python -m worker.main --migrate </dev/null
```

Applied `001_initial.sql`, `002_source_health_lifecycle.sql`, `003_source_fetch_state.sql` →
**28 tables**, with `pg_trgm` and `vector` both installed.

**b. Collect once.** The FAST lane, a single pass, against the real ACSC/CISA feeds:

```bash
docker compose run --rm -T worker python -m worker.main --lane fast --once </dev/null
docker compose run --rm -T worker python -m worker.main --lane normal --once </dev/null
```

Measured, 2026-10-01:

| Lane | Sources | ok | failed | stale | Items | New events |
|---|---|---|---|---|---|---|
| FAST | 11 | 8 | 0 | 3 | 1877 | 1742 |
| NORMAL | 22 | 19 | 1 | 2 | 415 | 409 |

**`stale` is not an error.** It is §2.6 working: `acsc_publications` genuinely has nothing newer
than 14 days, and the run says so rather than presenting an old item as current. The one NORMAL
failure was `sophos_labs: timeout: ReadTimeout after 30s` — transient, and the other 21 sources
were unaffected, which is §11's one-source-fails row behaving as designed.

A second FAST pass immediately afterwards returned `items=90 new=0 dup=90`: seven sources answered
**304 Not Modified** against the `etag`/`last_modified` stored in `source_fetch_state`, and every
re-ingested item resolved onto the event it already was. Both halves of that are worth keeping an
eye on — it is the cheapest evidence that conditional fetching and deduplication are working.

**c. Verify it is real data.** The point of the whole exercise, and worth doing before publishing
anything — the site currently shows synthetic fixtures:

```bash
docker compose exec -T db psql -U cyberpulse -d cyber_intel </dev/null \
  -c "select count(*) from events;" \
  -c "select source_id, count(*) from event_sources group by 1 order by 2 desc limit 10;" \
  -c "select left(title,60), severity, first_seen::date from events order by first_seen desc limit 5;"
```

(`events` has no `published` column and there is no `raw_items` table — the columns are
`first_seen` / `last_seen` / `last_material_update`, and per-source attribution lives in
`event_sources`.)

It is real: 2151 events, 1813 CVEs, titles such as *"Exclusive: WA's St James' Anglican School
investigating cyber…"* and *"Critical alert: Aussie organisations targeted in Citrix NetScaler…"*.
`cisa_kev` contributed 1730 — which matches the live KEV catalogue size independently measured
while building `worker/groundtruth/kev.py`, a useful cross-check that nothing was dropped.

**Expect every severity to be `unknown` until the ground-truth sync has run, and do not treat it as
a fault.** A collection run on its own produces:

```
severity | severity_source | count          counts in live.json
---------+-----------------+------          critical 0   high 0   medium 0   low 0
unknown  | unknown         |  1742          unknown 20   pending_enrichment 20
```

Severity comes from the Stage 2 ground-truth chain (CNA → CISA-ADP → NVD), which nothing in a lane
run reads, so `unknown` is the honest answer and PLAN.md §2 forbids inventing anything else. The
same is true of `au_relevance`, which is NULL rather than 0.

One visible consequence: `urgency` is **0.273 for every single event**, because
`worker/pipeline/score.py:121` computes it from the severity weight plus a KEV bonus, and with
severity unknown and no CVE flagged as KEV-listed every event gets the identical number. Flat
urgency is the symptom of missing ground truth, not a scoring bug.

**c-bis. Fill in the ground truth.** This is the step that turns that flat column into a ranking —
see *Part 5f* below. Run it once by hand after the first collection; after that the scheduler owns
it.

**d. Publish.** Writes `data/*.json` against the JSON Schemas, secret-scans the output and fails
closed if anything matches:

```bash
docker compose run --rm -T worker python -m worker.main --publish </dev/null
```

Wrote **501 files** — `live.json`, `index.json`, `source-health.json`, `system-status.json` and 497
under `data/history/`. `--publish` only builds and writes; pushing is a separate module, so this is
safe to run on a host holding no token.

🔴 **This is where the owner was needed** *(done 2026-10-02)*. Pushing the `data` branch needs a
token with `contents: write` on this repo and **nothing else** — specifically **not** `workflow`
scope, since a worker able to rewrite `.github/workflows/**` could bypass Stage 6's approval gates.
It is in `.env` as `CYBERPULSE_PUBLISH_TOKEN`, and a dry run authenticated:

```bash
docker compose exec -T worker python -m worker.publish.push --dry-run </dev/null
```

**The live site still serves the synthetic fixture events and AWAITING DATA banners**, for three
reasons, all in the wiki's [Stage 1, "What's left"](wiki/stage-1-foundation.md#whats-left):

1. Nothing runs the push yet. The scheduler rebuilds `data/*.json` after every run, but
   `worker.publish.push` is only a command.
2. A push to `data` does not redeploy Pages: a push runs the workflow file inside the pushed
   commit, and the `data` branch has none, on purpose.
3. Pages builds from `main`, and `stage-1-foundation` is not merged into it.

**e. Run continuously.** Replaces the one-shot with the scheduler the design intends — FAST every
15 minutes, NORMAL every 4 hours, owned by the worker so collection survives the control plane
being down:

```bash
docker compose up -d
docker compose ps
docker compose logs -f worker
```

Since Part 6, `docker compose up -d` starts Paperclip's `server` as well; the first time, it
downloads a 1.8 GB image before starting anything. To bring up only this stage, use
`docker compose up -d --no-deps db worker`.

This is the step that makes the VM worth having over the laptop: a 15-minute cadence needs a
machine that is always on. The worker logs `scheduler started: lane-fast, lane-normal,
groundtruth-sync` (the third job arrived with Part 5f), then
publishes after each run — collection and publishing are one unit now, so `data/*.json` cannot
describe an older collection than the database holds.

Confirmed unattended at 12:00 UTC. Both lanes fired on the same tick (`*/15` and `0 */4` coincide
every four hours, which is why publishes hold a lock), and each published afterwards:

```
22:00:00 Running job "fast lane" ... (scheduled at 2026-10-01 12:00:00+00:00)
22:00:00 Running job "normal lane" ... (scheduled at 2026-10-01 12:00:00+00:00)
22:00:01 run ...-b072e3 done: ok=8 failed=0 stale=3 items=90 new=0 updated=0 dup=90
22:00:13 published 531 files after the fast lane
22:01:34 run ...-686f8a done: ok=19 failed=1 stale=2 items=320 new=2 updated=0 dup=318
22:01:42 published 531 files after the normal lane
```

**f. The ground-truth sync.** The lanes collect; this is what gives the collected CVEs a severity,
an exploitation status and an exploitation probability. It reads three registers — CISA KEV, the
EPSS daily snapshot, and the CVE.org record for one CVE at a time — writes what changed, re-bands
every affected event and rescores it.

```bash
docker compose exec -T worker python -m worker.main --groundtruth --cvss-batch 400 </dev/null
```

It is also a scheduler job (`25 */6 * * *` — four times a day, off the lane hours so a sync and a
collection do not contend for the same tables), so `docker compose up -d` covers it from then on.
`--cvss-batch 0` refreshes only the two bulk registers, which is the quick form.

First live run on this box, against 5,013 collected CVEs and 2,199 events:

```
kev=KevResult(updated=1731, delisted=0, unchanged=3282, delist_withheld=0)
epss=ScoreResult(recorded=5013, unchanged=0)
cvss=CvssTally(scored=400, unscored=0, absent=0, errored=0, recorded=400, backed_off=False)
severity_changed=367 rescored=1665 errors=0
```

Took 23 seconds, and the column it was built for stopped being a constant:

```
severity | severity_source | count        urgency: 6 distinct values, 0.273 → 1.000
---------+-----------------+------        (was: 1 distinct value, 0.273 for all 2,199)
unknown  | unknown         |  1832
high     | cisa_adp        |   237
critical | cisa_adp        |    84
medium   | cisa_adp        |    32
high     | cna             |     5
critical | cna             |     4
medium   | cna             |     3
low      | cisa_adp        |     2
```

Four things in that table are worth reading deliberately, because each one is a design decision
visible in production rather than a number:

- **`cisa_adp` 355 to `cna` 12.** PLAN.md §2.5 ordered the chain CNA → CISA-ADP → NVD on the
  strength of a 300-CVE sample that found *zero* NVD-authored CVSS and most scoring coming from
  CISA's ADP enrichment. The live ratio is that sample holding at scale. Had the chain been built
  NVD-first, as the obvious reading of "use NVD for CVSS" would suggest, 355 of these 367 events
  would still read `unknown`.
- **Most events still `unknown`, and that is correct.** `--cvss-batch 400` resolved 400 of 5,013
  CVEs by design: one HTTP request per CVE, so an uncapped first run would fire five thousand at a
  free public register. The backfill drains over about three days at four passes a day, and
  `RECHECK_HOURS` then idles it. An event with no scored CVE is **left alone** rather than written
  to `unknown` — a missing lookup is not evidence, so it must not overwrite a vendor rating.
- **19 EPSS rows written with a NULL score and `status='unknown'`.** Those are CVEs the EPSS model
  does not cover. The row is the whole point: it distinguishes "EPSS has not modelled this" from
  "nobody has looked", and stops a gap being read as 0% on the site. `config/scoring.yaml` weights
  `unknown: 1.5` above `low: 1` for the same reason — unrated is not the same as harmless.
- **`delisted=0, delist_withheld=0`.** The catalogue's declared `count` matched what was parsed, so
  absence was trusted. Had the download been truncated — valid JSON, short array, indistinguishable
  from a shrunken catalogue — `delist_withheld` would be non-zero and **no listing would have been
  removed**. That is the guard worth knowing about: a truncated KEV response acted on naively would
  report thousands of known-exploited CVEs as unexploited.

**Two passes were needed, and the reason is worth knowing.** After the first pass the database held
367 banded events and `live.json` still read `{unknown: 152}` — not one visible event had a severity.
The batch had ordered never-checked CVEs by `cve_id`, so it spent all 400 lookups between
CVE-2002-0367 and CVE-2018-19953: the oldest ids in the table, and the least likely to be on the
front page. It was banding the archive. The ordering now tie-breaks on the newest event mentioning
each CVE — only 73 of the 5,013 belong to events first seen in the preceding week, so one batch
covers the whole visible site and the backlog fills in behind it. A second pass, same batch size:

```
                   events   critical   high   unknown
first pass            152          0      0       152
second pass           199         49     16       134
```

Published per-CVE coverage after the two passes: 152 CVE references, 142 with a CVSS score, 60
KEV-listed, 137 with an EPSS probability and **15 carrying `{"score": null, "status": "unknown"}`** —
which is what honest absence looks like once it reaches the wire.

> **A sync that logs an error is not a failed sync.** Each register is read and written
> independently, so KEV being down does not stop EPSS, and neither stops the CVSS batch. The CLI
> exits 0 on a register failure on purpose — "CISA was unreachable for ten minutes" is not a reason
> for a container to exit non-zero and be restarted into trying again immediately. What never
> happens on a failure is a *default* being written: every answer that register would have given
> stays as it was.

### Expect sources to go `degraded`, and do not treat it as breakage

The same tick logged `source cisa_news: lifecycle active -> degraded`, and the same for
`acsc_alerts` and `acsc_publications`. This is correct:

- `worker/pipeline/health.py:49` counts `STALE` among the failure statuses, and `DEGRADE_AFTER = 5`
  demotes an ACTIVE source after five consecutive ones. At a 15-minute cadence a genuinely quiet
  feed crosses that in about 75 minutes. `acsc_publications` has published nothing for 14 days
  against a 7-day expectation, so it is being reported accurately.
- **A degraded source is still collected.** `sources_for_lane` filters on lane and `enabled` only,
  never on lifecycle state, so demotion cannot create a catch-22 where a quiet source stops being
  polled and therefore can never recover. One OK fetch moves it to TESTING.
- It reaches the site rather than hiding in a log: `data/source-health.json` carries
  `lifecycle_state: "degraded"` for each, alongside `{ok: 27, stale: 5, timeout: 1, no_data: 17}`.

So the signal to act on is a source going degraded that you expected to be busy — not the fact that
any source is degraded at all.

---

## Defects this build surfaced

Four. The first three only appear when the stack actually runs in containers, which is why the laptop
never showed them; the fourth only appears at production data volumes. Recorded because each is the
kind that hides rather than announces itself.

**1. The worker image could not start at all.** `--migrate` died on
`ModuleNotFoundError: No module named 'yaml'`. PyYAML was never in `requirements.txt`, and
`worker/sources/registry.py` imports it at module load to read `config/sources.yaml` — so *no*
worker subcommand worked in a container, not just `--migrate`. The laptop was unaffected because its
`.venv` happens to carry `yaml 6.0.3`, pulled in by something else. `referencing` was the same
problem one step removed: `worker/publish/validate.py` imports it directly but it reached the image
only as a dependency of `jsonschema`. Both are now declared.

**2. No provenance was being written.** Every source logged
`raw cache write failed … Permission denied: /var/cache/cyberpulse/<source>` — and the run still
reported `ok=8 failed=0`. `/var/cache/cyberpulse` was not in the image, so Docker invented the
`rawcache` mount point as `root:root` while the container runs as `cyberpulse` (uid 1000).
`worker/pipeline/run.py:181` only warns, so this host could have collected for weeks with a complete
database and an empty evidence store and nothing would have said so. The cache holds the raw bytes
every published event was derived from, keyed by sha256 — it is what can show an event was not
invented. Fixed by creating the directory in `Dockerfile.worker` before `USER`, since Docker
initialises an empty named volume from the image's content at the mount point, ownership included.

After fixing it, the first run's bodies were still missing, and conditional fetching meant they
would not be re-downloaded until they changed. Backfilled with:

```bash
docker compose exec -T db psql -U cyberpulse -d cyber_intel -c "delete from source_fetch_state;" </dev/null
docker compose run --rm -T worker python -m worker.main --lane fast --once </dev/null
```

All 11 FAST sources now hold provenance, and a spot check confirms the filename really is the
payload's hash: `41a998d171e4b29a.raw` ⇄ `sha256sum | cut -c1-16` = `41a998d171e4b29a`.

**3. The scheduler never published.** It ran lanes only, so the database would advance every 15
minutes while `data/*.json` kept describing whichever collection was last published by hand — a
stale snapshot presented as current, which is exactly what §2.6 exists to prevent, and it
contradicts §11's *"keeps collecting and publishing"*. `worker/publish/run.py` now supplies the
connection and output directory, both `--publish` and the scheduler go through it, publishes are
serialised with a lock (the two cadences coincide at 00:00, 04:00, 08:00), and the build runs off
the event loop so a slow publish cannot look like a stalled collection.

**4. The ground-truth summary line was 140 KB.** `KevResult` carries `changed_cves` so the sync can
name which events need rescoring, and both `sync_groundtruth` and `worker.main` log the result as a
whole dataclass. On the first live run that was 1,731 CVE ids, printed twice, in what is meant to be
a one-line summary. Every test had passed — the unit fixtures move one or two CVEs, so the repr was
a normal length at test scale and only misbehaved at catalogue scale. `changed_cves` is now
`repr=False`; `len(changed_cves)` is `updated + delisted` and both are still in the repr, so nothing
diagnostic was lost. The general shape is worth remembering: a field that is small in every fixture
and large in production will not be caught by a test that only checks behaviour.

---

## Part 6 — Paperclip *(running and claimed; company setup is the owner's)*

> The owner's step-by-step guide is the wiki's
> [Stage 4 pages](wiki/stage-4-paperclip.md): opening and claiming it, the 16 agents with their
> prompts, the routines, and how the crew hands work between them.

**2026-10-02, what was done:**

- The `server` service was added to `docker-compose.yml` (commit 184bb0d). It runs
  `ghcr.io/paperclipai/paperclip:2026.1001.0`, pinned by digest, with upstream's
  `pids_limit: 2048` and no `init` (the image runs tini itself). Its environment is an
  explicit list, so it never sees the publish token or the API keys.
- **It is published on `10.0.0.0:3100` only.** The owner chose the home network over
  NetBird for now. Published Docker ports bypass `ufw` and `firewalld`, so that bind address is
  the only thing limiting exposure. **Never** `0.0.0.0`, never a router port forward.
  Confirmed with `ss -ltn`.
- Its own database, `paperclip`, sits next to `cyber_intel` in the shared Postgres. **It must be
  created by hand**: the image only creates its database when running its embedded Postgres.
  Command: wiki 4a step 1.
- A DB login fault came up first. `POSTGRES_PASSWORD` in `.env` had been changed after the volume
  was created, so the worker had been failing since 19:27 Sydney. The role was set to the `.env`
  value with `ALTER ROLE` from inside the db container, so no value was shown (wiki 4a
  troubleshooting).
- The first pull is 1.8 GB compressed, 7.3 GB unpacked. Mid-pull, a `docker compose down`
  removed `db` and `worker`, and `up -d` then waited for the image before starting anything. They
  were started on their own with `docker compose up -d --no-deps db worker`.
- Result at 20:15 Sydney: `server` `(healthy)`; `/api/health` reports `authenticated` /
  `private` / `bootstrap_pending`; migrations applied; the worker's 20:15 run `ok=7 failed=0`.
- 20:21 Sydney: the owner created their account and claimed the instance. Checked in the
  `paperclip` database: one user, holding `instance_admin`. `PAPERCLIP_AUTH_DISABLE_SIGN_UP` was
  then set to `true` (`.env` copied to `~/env-backup-20261002-signup` first; only that line
  changed) and the `server` alone was recreated. A test sign-up now gets HTTP 400 and the user
  count stays at one.
- Paperclip runs its own database backup every 60 minutes, kept 7 days, into
  `/paperclip/instances/default/data/backups` in the `paperclip-data` volume. Same disk, so it
  does not replace Part 7.

**Lesson kept from the earlier hand-started test:** started from `~/CyberPulse-AI` with `npx`,
Paperclip auto-loads that folder's `.env`, inherits `DATABASE_URL=…@db…`, and fails with
`getaddrinfo ENOTFOUND db`, even though `doctor` passes. Only run it as the Docker service.

### 🔴 Owner-only, from a browser

1. [x] **Claim the instance**, while only the home network can reach it. In
   `authenticated` + `private` mode the first signed-in account to click **Claim this instance**
   becomes the admin. Fallback: `docker compose exec server pnpm paperclipai auth bootstrap-ceo`.
   Done 2026-10-02.
2. **Harden the defaults.** Four of the five toggles are set in `.env` already:
   - [ ] Require board approval for new hires (company settings, defaults to **off**; still off)
   - [x] `PAPERCLIP_AUTH_DISABLE_SIGN_UP=true`, straight after the claim
   - [x] Secrets strict mode on
   - [x] Telemetry off (defaults to **on**)
   - [x] Announcements off (defaults to **on**)

---

## Part 7 🔴 — Backups

```bash
# in the VM
docker compose exec -T db pg_dump -U cyberpulse cyber_intel | gzip > /backup/cyber_intel.sql.gz
docker compose exec -T db pg_dump -U cyberpulse paperclip  | gzip > /backup/paperclip.sql.gz
docker compose cp server:/paperclip/instances/default/secrets /backup/paperclip-secrets
cp -p .env /backup/env                                       # keep this copy private
```

The database and Paperclip's secrets folder are both required; neither restores without the
other. Copy the whole folder, not a single file. On 2026-10-02 it held only
`decision-signing.key`; `master.key` appears there once Paperclip first stores a secret.

On the Proxmox host, add a `vzdump` job in **snapshot** mode with `prune-backups` set to
`keep-daily=7,keep-weekly=4,keep-monthly=3` and *Repeat missed* enabled.

**This host has a single 94 GB disk, so `vzdump` to local storage is not a backup — it dies with
the disk.** It needs an external target: a NAS over NFS/SMB, a USB disk, or another machine
running Proxmox Backup Server. Cheap to arrange now, much less so after there is data worth
keeping.

This stack uses named Docker volumes on the VM's own disk, which `vzdump` does capture. Do not
convert them to bind mounts: Proxmox's docs are explicit that *"the contents of bind mount points
are not backed up when using vzdump"*.

---

## Owner checklist — the short version

Everything that needs a human, in order:

- [x] **Part 2 — `qm disk resize 200 scsi0 60G`** on the Proxmox host. Done 2026-10-01; everything
      after it followed.
- [x] **Part 5d — the publish token.** `contents: write` only, **no `workflow` scope**. In `.env`
      since 2026-10-02; a dry-run push authenticated. The push is not scheduled yet, and a push to
      `data` does not redeploy Pages (wiki Stage 1, "What's left").
- [x] **Part 6 — claim Paperclip.** Done 2026-10-02; sign-up turned off straight after.
- [ ] Part 6 — board approval for new hires, then the company mission and budget (wiki 4a steps
      4–5). Actionable now.
- [ ] Approve merging `stage-1-foundation` into `main`, which Pages builds from.
- [ ] Part 7 — provide an off-host backup target. There is now real data to lose.
- [ ] Later: NetBird, for the dashboard from outside the house.

## Commands the README lists that do not exist yet

Flagged so they are not mistaken for breakage. `worker/ops/` is not in the tree; these arrive with
Stage 2 and Stage 4:

```
docker compose run --rm worker python -m worker.ops.health
docker compose run --rm worker python -m worker.ops.cost --month      # ROGUE's ledger
docker compose run --rm worker python -m worker.ops.sources --status  # SERAPH's view
```

The worker CLI today is exactly six flags: `--migrate`, `--lane {fast,normal,deep}`, `--once`,
`--groundtruth`, `--cvss-batch N` and `--publish`. The push to GitHub is a separate command,
`python -m worker.publish.push [--dry-run]`.
