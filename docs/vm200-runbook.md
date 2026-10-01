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
| **Disk** | **3 GB — this is wrong and blocks Docker. See Part 2.** |

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

## Part 2 🔴 — Resize the disk *(blocking, not yet done)*

`lsblk` on the guest reports `sda` as **3G**, which is the cloud image's native size: step 5 above
never took effect. The root filesystem is 2.8 GB with ~1.6 GB free.

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

### Still to run, after Part 2 — Docker (README §4.3)

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
resolves itself.

---

## Part 4 🟢 — The `.env`

`worker/settings.py` requires exactly one variable: `database_url`. Everything else is optional
with a default, so collection needs only the database block. The Postgres password is generated
on the VM and exists nowhere else.

```bash
cd ~/CyberPulse-AI
PGPASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)"
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
- **`.env` is git-ignored and must stay so.** The repository is public.

---

## Part 5 🟢 — Bring up Stage 1

```bash
cd ~/CyberPulse-AI
docker compose config                 # validates .env resolves — Stage 0's exit criterion
docker compose build                  # builds the worker image from Dockerfile.worker
docker compose up -d db               # Postgres with pgvector; waits for its own healthcheck
```

**a. Migrate.** Creates the schema and the required extensions (`pg_trgm` and `vector` — the
first is not optional, `worker/db/events.py:124` uses `similarity()` for trigram event resolution):

```bash
docker compose run --rm worker python -m worker.main --migrate
```

**b. Collect once.** The FAST lane, a single pass, against the real ACSC/CISA feeds:

```bash
docker compose run --rm worker python -m worker.main --lane fast --once
```

**c. Verify it is real data.** The point of the whole exercise, and worth doing before publishing
anything — the site currently shows synthetic fixtures:

```bash
docker compose exec -T db psql -U cyberpulse -d cyber_intel \
  -c "select count(*) from events;" \
  -c "select source_id, count(*) from raw_items group by 1 order by 2 desc limit 10;" \
  -c "select title, published from events order by published desc limit 5;"
```

**d. Publish.** Writes `data/*.json` against the JSON Schemas, secret-scans the output and fails
closed if anything matches:

```bash
docker compose run --rm worker python -m worker.main --publish
```

Pushing the `data` branch needs a token with `contents: write` on this repo and **nothing else**.
Specifically **not** `workflow` scope: a worker able to rewrite `.github/workflows/**` could bypass
Stage 6's approval gates. Add it to `.env` as `CYBERPULSE_PUBLISH_TOKEN` at this point.

**e. Run continuously.** Replaces the one-shot with the scheduler the design intends — FAST every
15 minutes, NORMAL every 4 hours, owned by the worker so collection survives the control plane
being down:

```bash
docker compose up -d
docker compose ps
docker compose logs -f worker
```

This is the step that makes the VM worth having over the laptop: a 15-minute cadence needs a
machine that is always on.

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

- [ ] **Part 2 — `qm disk resize 200 scsi0 60G`** on the Proxmox host. Blocking; nothing else can
      proceed.
- [ ] Part 5d — create the publish token (`contents: write` only, **no `workflow` scope**) when
      publishing starts.
- [ ] Part 7 — provide an off-host backup target.
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
