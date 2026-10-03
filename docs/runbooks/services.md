# Services: the database and Paperclip

[Runbooks](README.md) · [Watchdog incidents](watchdog-incidents.md) ·
[VM 200 runbook](../vm200-runbook.md)

Three containers run on the VM: `db` (Postgres, holding `cyber_intel` and `paperclip`), `worker`
and `server` (Paperclip). Every entry assumes the `q` helper from
[Before you start](README.md#before-you-start).

The worker needs the database. Paperclip needs the database. The worker does **not** need
Paperclip: with the server stopped or broken, collection and publishing go on
(docker-compose.yml:38-39).

---

## Database down

**Symptom.** Telegram:

```text
CyberPulse-AI · system failure

The worker has not reached its database since 03:12 Sydney time, …. Nothing is collected or
published until it does, and the watchdog cannot record incidents.
```

The watchdog sends this after 3 passes without the database, about 15 minutes. It can only send
it while the worker runs, so total silence from Telegram can mean the same thing. With the database
down, the watchdog judges nothing else: no other incident opens or resolves.

**After a reboot, this is the first thing to check.** `worker` and `server` have
`restart: unless-stopped`, but `db` has no restart policy (docker-compose.yml:2-15). Compose's
default is not to restart it, so after the VM or Docker restarts, the database stays stopped.

**Check.**

```bash
docker compose ps -a
docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} restart={{.HostConfig.RestartPolicy.Name}}' $(docker compose ps -aq db)
docker compose logs db --tail 100
docker compose logs worker --since 30m | grep -iE "password authentication|could not connect|connection refused|database" | tail -5
uptime
df -h /
free -h
```

**Fix.**

- **`exited` with `exit=0`, or after a reboot:** start it. Nothing is running while it is down, so
  there is no need to wait for a safe minute.

  ```bash
  docker compose up -d db
  docker compose ps                  # db "(healthy)", worker and server "Up"
  docker compose logs -f worker      # the next run logs "done: ok=" and "published"
  ```

  The worker reconnects by itself. If the worker or server is not `Up`, start them too:
  `docker compose up -d worker server`. Telegram says `CyberPulse-AI · system recovered`.
- **`password authentication failed for user "cyberpulse"`:** `POSTGRES_PASSWORD` in `.env` is no
  longer the one the database was set up with. Postgres reads that variable only when its volume
  is first created. Follow [4a troubleshooting](../wiki/stage-4a-paperclip-setup.md#troubleshooting):
  put the old password back, or set the database to the new one.
- **`oom=true`:** the VM ran out of memory. See what is using it with `docker stats --no-stream`.
  If Paperclip's agents were running, stop the server for now (`docker compose stop server`), then
  `docker compose up -d db`.
- **The disk is full:** [Disk full](#disk-full).

**Stop and decide yourself** if the database log shows `PANIC`, `invalid page`, `could not read
block` or a checksum failure, or if it crashes again as soon as it starts. Do not run
`pg_resetwal`, and do not delete or move anything inside the volume. The way back is a restore from
backup ([Backup and restore](backup-and-restore.md)), and choosing that is your call.

### Disk full

**Symptom.** `df -h /` shows 100% use, or close to it. The database stops, publishing fails, or
`docker compose build` fails. The VM has 59 GB usable.

**Check.**

```bash
df -h /
docker system df
sudo du -xh --max-depth=1 /var/lib/docker 2>/dev/null | sort -h | tail -8
sudo journalctl --disk-usage
du -sh ~/* 2>/dev/null | sort -h | tail -8
q "select datname, pg_size_pretty(pg_database_size(datname)) from pg_database order by pg_database_size(datname) desc"
docker compose exec -T worker du -sh /var/cache/cyberpulse </dev/null
```

**Fix.** Take back space that nothing needs:

```bash
docker image prune                 # images no container uses and no tag names; it asks first
docker builder prune               # the build cache; it asks first
sudo journalctl --vacuum-size=200M
```

After a Paperclip image bump, the old image stays: 7.3 GB unpacked. `docker image ls` lists it;
remove it with `docker image rm <its image id>` once the new one is running.

**Never** `docker volume prune` or `docker system prune --volumes`. With the stack stopped, every
volume counts as unused, the database included. Container logs are capped already (3 × 10 MB per
container, vm200-runbook.md:29).

**Stop and decide yourself** before deleting anything from the raw cache or the database. The raw
cache holds the bytes each event came from, and old ones cannot be fetched again. If there is
nothing left to take back, grow the disk on the Proxmox host
([VM 200 runbook, Part 2](../vm200-runbook.md)).

---

## Paperclip down

**Symptom.** `paperclip-down` (high): "Paperclip has not answered 3 checks in a row". Or the
dashboard at `http://192.168.128.39:3100` does not load. The crew is not told. The worker carries
on.

**Check.**

```bash
docker compose ps -a server
docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} health={{.State.Health.Status}}' $(docker compose ps -aq server)
docker compose logs server --since 30m --tail 100
curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.128.39:3100/api/health
ss -ltn | grep 3100                # should show only 192.168.128.39:3100
ip -4 addr show eth0 | grep inet   # the VM should still be 192.168.128.39
free -h
```

The watchdog probes `http://server:3100/api/health` from the worker, with no credentials. Any
answer below 500 counts as up.

**Fix.**

- **The database is down:** [Database down](#database-down) first. The server waits for it.
- **`exited`:** start it, and wait for `(healthy)`. It is allowed 2 minutes to start.

  ```bash
  docker compose up -d server
  docker compose ps server
  ```

- **`database "paperclip" does not exist`:** the database was never created on this volume, as
  after a rebuild. [4a step 1](../wiki/stage-4a-paperclip-setup.md#1--start-paperclip) has the
  one-off command. After a restore, [Backup and restore](backup-and-restore.md) covers it.
- **The VM's address changed:** the server binds to `192.168.128.39` only, so it cannot start on
  another address. That is the safe failure. Put the address back with a DHCP reservation on the
  router. **Never** change the `ports:` line to `0.0.0.0` or forward a router port to get it back.
- **It answers but is stuck, or `oom=true`:** restart it. An agent run in progress is cut off, so
  check the agents' pages first if Paperclip still loads. After a change to `.env`, recreate it
  instead, since a restart keeps the old environment:

  ```bash
  docker compose restart server
  docker compose up -d --force-recreate server      # only after editing .env
  ```

  If memory ran out while agents were working, pause the AI agents once it is back.
- **The phone cannot reach it, but the VM can:** the phone is not on the home Wi-Fi
  ([4a troubleshooting](../wiki/stage-4a-paperclip-setup.md#troubleshooting)).

**Stop and decide yourself:**

- **A different Paperclip image.** It is pinned by tag and digest (docker-compose.yml:42). An
  upgrade is a pull request that changes both, merged after a backup. Paperclip may change its
  database when a new version starts, so going back to the old image afterwards may not work. The
  way back is then a restore.
- **Paperclip's own hourly backups** (in the `paperclip-data` volume, kept 7 days) can undo a bad
  change inside Paperclip. Using one rolls back everything in Paperclip since then.
- **Any change to the `paperclip` database by hand.** Backup first, and your decision.

---

[Runbooks](README.md) · [Watchdog incidents](watchdog-incidents.md) · [Backup and restore](backup-and-restore.md)
