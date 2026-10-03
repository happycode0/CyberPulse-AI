# Backup and restore

[Runbooks](README.md) · [Stage 7, Backups](../wiki/stage-7-hardening.md#backups--do-this-one-early) ·
[Threat model, B8](../threat-model.md#b8-backups-on-a-single-disk-host)

The Proxmox host has one disk. A backup on that disk dies with it, so only an **off-host target**
counts: a NAS, a USB disk, or a Proxmox Backup Server. There are three kinds of copy:

| Copy | Made by | Holds | Counts as a backup |
|---|---|---|---|
| `vzdump` of the VM | Proxmox, on the host | The whole VM: every volume, `.env`, the raw cache | Yes, on an off-host target |
| `ops/backup.sh` | You or cron, on the VM | Both databases, Paperclip's secrets folder, `.env` | Yes, on an off-host target |
| Paperclip's hourly dumps | Paperclip, inside its volume | The `paperclip` database only, kept 7 days | No: same disk. Good for undoing a change inside Paperclip |

**A backup holds every credential.** Its `env` and `paperclip-secrets/` are the keys to
everything on the [Tokens](tokens.md) page. Keep the target private, and encrypted if you can.
Never put a backup inside the checkout (`~/CyberPulse-AI`): the worker mounts it, and every image
build copies it.

---

## Take a backup

**What it writes.** Each run makes `<target>/cyberpulse-<UTC time>/`, such as
`cyberpulse-20261003T021000Z/`, readable by `deploy-user` only:

| File | What |
|---|---|
| `cyber_intel.dump`, `paperclip.dump` | `pg_dump` custom format, each read back with `pg_restore -l` |
| `paperclip-secrets/` | The keys Paperclip signs and encrypts with |
| `env` | A copy of `.env` |
| `manifest.txt` | When, which commit, each file's size, every table's row count. No secrets |
| `SHA256SUMS` | A checksum of each file above |
| `COMPLETE` | Empty, written last. **No `COMPLETE`, no backup** |

Not in it: the raw cache and the rest of the `paperclip-data` volume. `vzdump` has those.

**Run it** on the VM, in the checkout, with the stack up. It is not a restart, so any minute
will do. Each dump comes from one snapshot, so the worker and Paperclip keep writing.

```bash
mountpoint /mnt/backup             # "/mnt/backup is a mountpoint". Anything else: stop
./ops/backup.sh /mnt/backup
```

`/mnt/backup` stands for wherever you mount the target. The script refuses a directory that does
not exist, but an empty mount point does exist: without the `mountpoint` check, a backup can land
on the VM's own disk.

It ends with `complete: /mnt/backup/cyberpulse-… (<size>)`, then `removed old backup …` for each
one past the newest 14.

| Option | Does |
|---|---|
| `--keep N` | Keep the newest `N` complete backups in the target (default 14). It only ever deletes complete `cyberpulse-…` directories |
| `--databases-only` | The two dumps only: no `paperclip-secrets/`, no `env`. Safer on a target you trust less, but it cannot rebuild a VM: `restore.sh fresh` needs a full backup |

**If it fails,** it says `backup: FAILED. <dir> has no COMPLETE marker, so restore.sh and --keep
ignore it.`, after the reason. Fix the reason, and run it again. `--keep` never removes the
unfinished directory; delete it yourself once a new backup has completed.

## Check a backup

```bash
d=/mnt/backup/cyberpulse-20261003T021000Z      # the one to check
ls -l "$d"                                      # COMPLETE is there
(cd "$d" && sha256sum -c --quiet SHA256SUMS) && echo "checksums OK"
head -4 "$d/manifest.txt"                       # created_utc, git_head, contents: full
grep '^tables' "$d/manifest.txt"
```

Never `cat` the backup's `env`. It is `.env`.

## Schedule it

As `deploy-user`, `crontab -e`, and add one line. It runs at 02:10 by the VM's clock:

```cron
10 2 * * * cd ~/CyberPulse-AI && mountpoint -q /mnt/backup && ./ops/backup.sh --keep 14 /mnt/backup >> ~/backup.log 2>&1
```

**Nothing tells you when it fails.** The watchdog does not watch backups. Once a week:

```bash
tail -3 ~/backup.log               # the last line: "complete: …" with a recent date in its name
ls /mnt/backup
```

No new `complete:` line means the target was not mounted or the run failed.

**On the Proxmox host,** add the `vzdump` job too: **Datacenter → Backup → Add**, the VM, mode
**snapshot**, storage the off-host target, retention `keep-daily=7,keep-weekly=4,keep-monthly=3`,
**Repeat missed** on ([the VM runbook](../vm200-runbook.md), Part 7).

Once a scheduled backup has run and one rehearsal has passed, delete the loose copy of `.env` left
from the sign-up change: `rm ~/env-backup-20261002-signup`.

## Rehearse a restore

**Once a month,** and after any change to the database or to Paperclip's version. It leaves the
stack alone: it starts a throwaway Postgres from the same image, with no network and no ports,
restores both dumps into it and compares every table's row count with the manifest.

```bash
df -h /                            # room for one more copy of both databases
./ops/restore.sh rehearse /mnt/backup/cyberpulse-20261003T021000Z
```

It checks `COMPLETE` and the checksums first. Then, per database:

```text
cyber_intel  PASS  <n> tables, <n> rows, as in the manifest
paperclip    PASS  <n> tables, <n> rows, as in the manifest
rehearsal PASSED: both databases restore to the row counts in the manifest
removed the throwaway container cyberpulse-restore-…
```

It proves the dumps restore. It does not prove the secrets or `env` work: only a rebuild, below, on
a scratch VM does that.

**Stop and decide yourself** on `rehearsal FAILED` or any `FAIL` line. That backup is no good. Try
the one before it, and find out why before you rely on any of them.

## Restore onto a rebuilt VM

**When.** the VM or its disk is lost. If there is a good `vzdump` on the off-host target, restoring
the whole VM in Proxmox is quicker and brings back everything, the raw cache included. This is the
way when there is no `vzdump`, or to rehearse on a scratch VM.

`ops/restore.sh fresh` only restores into **empty** databases, so it can never overwrite one.

1. **Build the VM:** [the VM runbook](../vm200-runbook.md), Parts 1 to 3: the VM, its disk,
   Docker, and the clone of the repository. Skip Part 4: `.env` comes from the backup. To replace
   the VM, give the new VM the same address, `10.0.0.0`, with a DHCP reservation. The
   server binds to that address only and does not start on another.
2. **Mount the target, and put `.env` in place** from the backup:

   ```bash
   cd ~/CyberPulse-AI
   d=/mnt/backup/cyberpulse-20261003T021000Z      # the newest complete one
   install -m 600 "$d/env" .env
   ls -l .env                                      # -rw------- deploy-user
   docker compose config -q
   ```

3. **Start the database only:**

   ```bash
   docker compose up -d db
   docker compose ps db               # "(healthy)"
   ```

   Do not start the server or the worker. Paperclip sets up an empty database as soon as it
   starts, and then `fresh` refuses.
4. **Rehearse it here first:** `./ops/restore.sh rehearse "$d"` must end `rehearsal PASSED`.
5. **Restore:**

   ```bash
   ./ops/restore.sh fresh "$d" --yes
   ```

   It checks the backup again, and refuses if the database is not up, if the server is running, or
   if either database has tables. It creates the `paperclip` database and the server container,
   without starting it. That pulls the Paperclip image, 1.8 GB, the first time. It copies the
   secrets folder in, restores each database in one transaction, and compares the row counts. It
   ends with `Left to do by hand:`. `.env` is done already, in step 2.
6. **Start everything,** and bring the schema up to the checkout's code:

   ```bash
   docker compose up -d               # builds the worker image first
   docker compose exec -T worker python -m worker --migrate </dev/null
   docker compose ps                  # db and server "(healthy)", worker "Up"
   docker compose logs -f worker      # the next run logs "done: ok=" and "published"
   ```

7. **Check:** sign in at `http://10.0.0.0:3100` with your own account, and see the agents as
   they were, paused ones still paused. `ops /ops/incidents` answers. The site updates after the
   next run.

**On a scratch VM, for a rehearsal,** the backup's `.env` holds the real keys. Started in full, it
would publish to the real site, send Telegram messages and spend on the real OpenRouter key. Stop
after step 5, or blank `CYBERPULSE_PUBLISH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY` and
`PAPERCLIP_INCIDENT_WEBHOOK_URL` in its `.env` before step 6. Delete the scratch VM afterwards: it
holds every key.

**Stop and decide yourself:**

- **No `COMPLETE`, a checksum failure, or a `FAIL` row count.** That backup is no good. Use the
  one before it. Never edit a backup's files or its `SHA256SUMS` to get past a check.
- **`refusing: <db> already has N tables`.** Never empty a database to get past it. On a rebuilt
  VM, something started too early. On the VM itself, it is live data. Look at what is there first.
- **Restoring over the VM's live databases.** These scripts never do that. It is a decision, with a
  backup of the current state taken first.
- **What is lost.** Everything since the backup, and the raw cache unless `vzdump` has it.
  Collection catches up from the sources; what they no longer carry is gone.
- **If the old VM comes back,** keep it off. Two workers with the same keys would both publish and
  both alert.

---

[Runbooks](README.md) · [Tokens](tokens.md) · [Services](services.md)
