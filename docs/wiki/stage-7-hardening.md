# Stage 7 — Hardening + operations

[← Stage 6 — Self-healing](stage-6-self-healing.md) · [Wiki home](README.md)

**Status: ▶ built; a restore was rehearsed on the VM on 2026-10-03.** What is left is yours:
somewhere off the host to keep the backups, and a schedule. Plan:
[PLAN.md §9, Stage 7](../../PLAN.md#9-stages) · Build record: [runbook Part 7](../vm200-runbook.md)

---

## What this stage gives you

Confidence that it survives: a backup that has actually been restored, failures injected on
purpose and handled, and the threat model reviewed against what was really built.

## What's left

| Who | Step |
|---|---|
| 🔴 | **An off-host backup target:** a NAS over NFS or SMB, a USB disk, or a Proxmox Backup Server. **The Proxmox host has a single disk**, and a backup on it dies with it |
| 🔴 | **Schedule `ops/backup.sh`** to that target: [the crontab line below](#schedule-it) |
| 🔴 | On the Proxmox host: a `vzdump` job for the VM, **snapshot** mode, `keep-daily=7,keep-weekly=4,keep-monthly=3`, *Repeat missed* on |
| 🔴 | **Read the open risks** in the [threat model](../threat-model.md#open-risks-ranked), the first one above all |
| ✅ | `ops/backup.sh`: both databases, Paperclip's keys and `.env`, checked and checksummed: [below](#backups) |
| ✅ | `ops/restore.sh rehearse`, run for real on 2026-10-03: [below](#restore-rehearsal) |
| ✅ | `ops/restore.sh fresh`, to rebuild on a new VM: [below](#rebuilding-on-a-new-vm) |
| ✅ | A failure-injection suite, one test per failure in PLAN.md §11: [below](#failure-injection-tests) |
| ✅ | The [threat model](../threat-model.md), reviewed against what was built, and [runbooks](../runbooks/README.md) for the common failures |

## Backups

```bash
cd ~/CyberPulse-AI
./ops/backup.sh /mnt/backup                     # everything: both databases, Paperclip's keys, .env
./ops/backup.sh --databases-only /mnt/backup    # the two databases only
./ops/backup.sh --keep 30 /mnt/backup           # keep the newest 30 (the default is 14)
```

Each run writes `/mnt/backup/cyberpulse-<UTC time>/`, readable only by you:

| File | What it is |
|---|---|
| `cyber_intel.dump`, `paperclip.dump` | Both databases (`pg_dump -Fc`), each read back before the backup counts |
| `paperclip-secrets/` | The keys Paperclip signs and encrypts with. Without them its stored secrets cannot be read |
| `env` | A copy of `.env`. **Keep the target private**: this file holds every key |
| `manifest.txt` | When, which commit, each file's size, and every table's row count |
| `SHA256SUMS` | Checksums of all of the above |
| `COMPLETE` | Written last. A folder without it is a backup that failed, and is ignored |

The dumps and their row counts come from one database snapshot, so they agree exactly even
while the worker keeps writing. A backup takes about 2 seconds today (4.4 MB). Each part is
useless without the others: the dumps need the keys, and the keys need the dumps.

`--keep` only ever deletes older complete backups in that folder. It never touches anything
else, and never a failed one.

Paperclip also dumps its own database every 60 minutes (kept 7 days) inside the
`paperclip-data` volume. Useful for undoing a bad change, but it is on the same disk, so it
does not count as the backup. Keep the Docker volumes as named volumes: `vzdump` captures those,
and Proxmox does not back up bind mounts.

### Schedule it

Once the target is mounted at, say, `/mnt/backup`, run `crontab -e` on the VM as `deploy-user` and
add:

```cron
10 16 * * * cd ~/CyberPulse-AI && ./ops/backup.sh /mnt/backup >> ~/backup.log 2>&1
```

That is 16:10 in the VM's time zone. Pick any minute clear of the collection runs (:00, :15,
:30, :45 UTC). Then check `~/backup.log` the next day for `complete:`.

## Restore rehearsal

A backup you have never restored is a hypothesis. This proves one without touching the stack:

```bash
./ops/restore.sh rehearse /mnt/backup/cyberpulse-<UTC time>
```

It checks `COMPLETE` and the checksums, starts a throwaway Postgres of the same image with no
network and no ports, restores both dumps into it, and compares every table's row count with
the manifest. The container is removed however the run ends.

The rehearsal on the VM, 2026-10-03:

| Database | Tables | Rows | Result |
|---|---|---|---|
| `cyber_intel` | 43 | 44,084 | PASS, as in the manifest |
| `paperclip` | 215 | 2,153 | PASS, as in the manifest |

Backup 2 seconds, rehearsal 9 seconds, nothing left behind. Repeat it once a month, and after
any Postgres image change.

## Rebuilding on a new VM

If the VM is lost: build a new one ([runbook Parts 1–4](../vm200-runbook.md)), clone the repo,
then:

```bash
cd ~/CyberPulse-AI
cp -p /mnt/backup/cyberpulse-<UTC time>/env .env && chmod 600 .env
docker compose up -d db                  # the database only: not the server yet
./ops/restore.sh fresh /mnt/backup/cyberpulse-<UTC time> --yes
docker compose up -d
docker compose exec -T worker python -m worker --migrate
```

`fresh` refuses unless both databases are empty and the Paperclip server is stopped, so it can
never overwrite live data. It restores both databases in one transaction each, puts Paperclip's
keys back, and checks the row counts against the manifest.

## Failure-injection tests

`tests/failure/` injects each failure in [PLAN.md §11](../../PLAN.md#11-failure-model) and checks
it is handled the way the plan says. The map from each failure to its tests, and anything not
yet handled, is in [tests/failure/README.md](../../tests/failure/README.md).

```bash
python -m pytest -q tests/failure
```

## Threat model and runbooks

- [The threat model](../threat-model.md): each trust boundary, the controls that are really in
  place, what is left, and the open risks ranked.
- [The runbooks](../runbooks/README.md): one per watchdog incident, plus the database or
  Paperclip down, the AI budget spent, token rotation, a bad deploy and a restore. Each says
  how to check, how to fix, and when to stop and decide yourself.

## Not built, on purpose

- **OpenTelemetry.** On one VM the watchdog, `job_runs`, the ops API and the container logs
  already answer "is it working, and since when". A collector and a backend would be more to run
  and secure than what they would show. Revisit if this ever runs on more than one host.
- **More notification channels.** Telegram only. The notices go through one interface
  (`worker/notify/`), so another channel is a small addition when you want one.

## Optional (🔴 you)

- **NetBird**, to reach the dashboard from outside the house without opening anything to the
  internet (runbook Part 6).

## Done when

- [x] A restore from backup has been rehearsed successfully (2026-10-03, the VM)
- [ ] Every failure-injection test passes

---

[← Stage 6](stage-6-self-healing.md) · [Wiki home](README.md) · **That's the last stage.** Back to
[the start page](README.md) to see what's next.
