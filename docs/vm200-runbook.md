# the VM build runbook — every command, and who runs it

A single ordered record of the commands that build the CyberPulse-AI host: the ones already run,
the ones still to run, and the ones that only the owner can run. README §4 is the reference
design; this is the log of applying it to the actual box, with the real values filled in.

**Read the legend before anything else — the two columns are not interchangeable.**

| Mark | Who | Where |
|---|---|---|
| 🔴 | **Owner, by hand** | Proxmox host shell, or a browser. Claude has no access to the PVE host — only to the guest. |
| 🟢 | **Claude, over SSH** | Inside the guest, as `deploy-user@10.0.0.0`. |

## The box as it actually is

Measured on the VM, 2026-10-01, not copied from the design table:

| | |
|---|---|
| VM | 200 `cyberpulse`, Debian GNU/Linux 13 (trixie), kernel 6.12.107+deb13-cloud-amd64 |
| Address | `10.0.0.0/24` on `eth0` (DHCP), reachable from the WSL box |
| Login | `deploy-user`, passwordless sudo, key-only (`~/.ssh/cyberpulse_vm_ed25519`) |
| CPU / RAM | 4 vCPU / 11 GiB usable (created with `--memory 12288 --balloon 8192`) |
| Disk | 59 GB usable on `/dev/sda1`, 55 GB free after Docker and the images (resized — see Part 2) |
| Docker | 29.8.2, Compose v5.5.1, `local` log driver capped at 3 × 10 MB |
| Status | **Stage 1 is running.** FAST every 15 min, NORMAL every 4 h, publishing after each run. |

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

## Part 3 🟢 — Prepare the guest

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
- **No `CYBERPULSE_PUBLISH_TOKEN` yet.** It is only needed by the publish step (Part 5d). A
  collection-only host has no reason to hold a token that can write to a public repository, so it
  is added when publishing starts and not before.
- **`.env` is git-ignored and must stay so.** The repository is public. Verified with
  `git check-ignore -v .env`, which reported `.gitignore:2`.

Validate with `docker compose config -q`, not `docker compose config`. The unquiet form prints the
fully resolved file, `POSTGRES_PASSWORD` included, into your terminal and scrollback. `-q` exits 0
on success and says nothing.

---

## Part 5 🟢 — Bring up Stage 1 *(done; 5d is blocked on the owner)*

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

**Expect every severity to be `unknown` at this point, and do not treat it as a fault.**

```
severity | severity_source | count          counts in live.json
---------+-----------------+------          critical 0   high 0   medium 0   low 0
unknown  | unknown         |  1742          unknown 20   pending_enrichment 20
```

Severity arrives from the Stage 2 ground-truth chain (CNA → CISA-ADP → NVD), which is written but
not yet wired into the pipeline, so `unknown` is the honest answer and PLAN.md §2 forbids inventing
anything else. The same is true of `au_relevance`, which is NULL rather than 0.

One visible consequence: `urgency` is **0.273 for every single event**, because
`worker/pipeline/score.py:121` computes it from the severity weight plus a KEV bonus, and with
severity unknown and no CVE yet flagged as KEV-listed every event gets the identical number. Flat
urgency is the symptom of the missing ground truth, not a scoring bug — it is the single clearest
argument for wiring Stage 2 in next.

**d. Publish.** Writes `data/*.json` against the JSON Schemas, secret-scans the output and fails
closed if anything matches:

```bash
docker compose run --rm -T worker python -m worker.main --publish </dev/null
```

Wrote **501 files** — `live.json`, `index.json`, `source-health.json`, `system-status.json` and 497
under `data/history/`. `--publish` only builds and writes; pushing is a separate module, so this is
safe to run on a host holding no token.

🔴 **This is where the owner is needed.** Pushing the `data` branch needs a token with
`contents: write` on this repo and **nothing else** — specifically **not** `workflow` scope, since a
worker able to rewrite `.github/workflows/**` could bypass Stage 6's approval gates. Add it to
`.env` as `CYBERPULSE_PUBLISH_TOKEN`. **Until that exists the live site keeps serving the synthetic
fixture events and its AWAITING DATA banners**, because the VM is producing correct files that
nothing is allowed to push.

**e. Run continuously.** Replaces the one-shot with the scheduler the design intends — FAST every
15 minutes, NORMAL every 4 hours, owned by the worker so collection survives the control plane
being down:

```bash
docker compose up -d
docker compose ps
docker compose logs -f worker
```

This is the step that makes the VM worth having over the laptop: a 15-minute cadence needs a
machine that is always on. The worker logs `scheduler started: lane-fast, lane-normal`, then
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

Three, all of which only appear when the stack actually runs in containers, which is why the laptop
never showed them. Recorded because each is the kind that hides rather than announces itself.

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

---

## Part 6 — Paperclip

**Status: not possible from this repository yet, and the README is ahead of the code here.**

Being specific, because this is the part most likely to waste an evening:
`docker-compose.yml` defines exactly two services, `db` and `worker`. There is **no Paperclip
service, image, or configuration in the repo** — the only mention is a docstring in
`worker/scheduler.py` noting that the DEEP lane is a Paperclip routine. README §4.5's
`docker compose up -d` and `docker compose logs -f server` refer to a `server` service that Stage 4
adds and that does not exist today. Running them now brings up the worker stack, not Paperclip.

`.env.example` already reserves the variable names, which is why it looks ready:

```
BETTER_AUTH_SECRET                     PAPERCLIP_AUTH_BASE_URL_MODE
PAPERCLIP_AGENT_JWT_SECRET             PAPERCLIP_AUTH_DISABLE_SIGN_UP
PAPERCLIP_TOOL_ACTION_SIGNING_SECRET   PAPERCLIP_TELEMETRY_DISABLED
PAPERCLIP_PUBLIC_URL                   PAPERCLIP_ANNOUNCEMENTS_ENABLED
PAPERCLIP_DEPLOYMENT_MODE              PAPERCLIP_SECRETS_STRICT_MODE
PAPERCLIP_DEPLOYMENT_EXPOSURE          CYBERPULSE_ENGINEER_TOKEN
```

### 🔴 When Stage 4 adds the service, these steps are owner-only

Claimed from a browser, so they cannot be automated:

1. **NetBird first** (README §4.4). Join the VM to the existing mesh, so the dashboard is
   reachable from the owner's devices and nowhere else. Paperclip binds `127.0.0.1:3100` by
   default; to expose it on the NetBird interface only, set `PAPERCLIP_BIND=custom` and
   `PAPERCLIP_BIND_HOST=<netbird-ip>`. **Never** bind `0.0.0.0` or forward a router port.
   Published Docker ports bypass `ufw` and `firewalld`, so a host firewall will not save a
   mistake here.
2. **Claim the instance immediately** after first start, while only the owner can reach it.
   Deployment mode is `authenticated` + `private`, so the first account to sign in claims it.
   Open `http://<netbird-ip>:3100` → sign in → **Claim this instance**.
3. **Harden the defaults.** Validation found several permissive, so this is a checklist and not a
   formality:
   - [ ] Require board approval for new agents — defaults to **off**
   - [ ] `PAPERCLIP_AUTH_DISABLE_SIGN_UP=true`, once the owner's account exists
   - [ ] Secrets strict mode on
   - [ ] Telemetry off (defaults to **on**)
   - [ ] Announcements off (defaults to **on**)

---

## Part 7 🔴 — Backups

```bash
# in the VM, once Paperclip exists
docker compose exec -T db pg_dump -U cyberpulse cyber_intel | gzip > /backup/cyber_intel.sql.gz
docker compose exec -T db pg_dump -U cyberpulse paperclip  | gzip > /backup/paperclip.sql.gz
docker compose cp server:/paperclip/instances/default/secrets/master.key /backup/master.key
```

The database and the secrets master key are both required; neither restores without the other.

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
- [ ] **Part 5d — the publish token.** `contents: write` only, **no `workflow` scope**. This is the
      one thing standing between a VM producing correct data and a site showing it. Nothing else is
      blocked on it.
- [ ] Part 7 — provide an off-host backup target. There is now real data to lose.
- [ ] Part 6 — NetBird, claiming Paperclip, and the five hardening toggles. Not yet actionable.

## Commands the README lists that do not exist yet

Flagged so they are not mistaken for breakage. `worker/ops/` is not in the tree; these arrive with
Stage 2 and Stage 4:

```
docker compose run --rm worker python -m worker.ops.health
docker compose run --rm worker python -m worker.ops.cost --month      # ROGUE's ledger
docker compose run --rm worker python -m worker.ops.sources --status  # SERAPH's view
```

The worker CLI today is exactly four flags: `--migrate`, `--lane {fast,normal,deep}`, `--once`,
`--publish`.
