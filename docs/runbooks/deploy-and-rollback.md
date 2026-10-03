# Deploy and rollback

[Runbooks](README.md) · [Stage 6, Rollback](../wiki/stage-6-self-healing.md#rollback) ·
[ops/rollback.sh](../../ops/rollback.sh)

The worker runs whatever the VM's checkout holds: `~/CyberPulse-AI` is mounted at `/app`
(docker-compose.yml:26). So a merge on GitHub changes nothing until you pull it on the VM, and
going back is a checkout of an earlier commit. `ops/rollback.sh` does both:

```bash
./ops/rollback.sh status              # what is running, against main's tip
./ops/rollback.sh pin <sha>           # run an earlier commit of main now, restart the worker
./ops/rollback.sh unpin               # back to main's tip: pull, migrate, restart
./ops/rollback.sh revert <merge-sha>  # where you push from: a pull request that undoes one merge
```

Migrations only ever add, so an earlier commit runs on the newer schema. The database is never
rolled back. None of this touches the `server`: Paperclip runs from its pinned image.

---

## A normal deploy

**When.** After you merge a pull request on GitHub. At a safe minute: `date -u +%M`, between :10
and :45.

**Check** first what is coming, without changing anything:

```bash
./ops/rollback.sh status
git log --oneline HEAD..origin/main
git diff --stat HEAD origin/main -- requirements.txt Dockerfile.worker docker-compose.yml .env.example
```

`status` fetches from GitHub, then prints `running <sha> (main); main is at <sha>`.

**Fix.**

```bash
./ops/rollback.sh unpin
docker compose logs -f worker      # wait for "done: ok=" and "published"; Ctrl-c stops watching
```

`unpin` checks out `main`, pulls, runs the migrations, and restarts the worker. It prints `worker
restarted on <sha>`. Then:

```bash
ops /ops/incidents                 # nothing new after the next pass
```

The `git diff --stat` above decides whether there is more to do:

- **`requirements.txt` or `Dockerfile.worker` changed:** the image needs a rebuild, which `unpin`
  does not do. After `unpin`:

  ```bash
  docker compose build worker
  docker compose up -d --force-recreate worker
  docker image prune                 # the old image holds an old copy of .env; it asks first
  ```

- **`.env.example` changed:** a new setting may be needed. Read the diff, add any new line to
  `.env` with `nano`, `docker compose config -q`, then recreate the worker.
- **`docker-compose.yml` changed:** `docker compose config -q`, then `docker compose up -d`. It
  recreates only the services whose settings changed. If the change is the Paperclip image, stop
  first (below).

**Stop and decide yourself:**

- **A change to the Paperclip image** (its tag and digest, docker-compose.yml:42). Take a backup
  first ([Backup and restore](backup-and-restore.md)), with the AI agents paused. Then
  `docker compose up -d server` pulls it and starts it. Paperclip may change its database when a
  new version starts, so the old image may not work afterwards. The way back is then a restore.
- **`git diff --stat` shows a file you did not expect** in a pull request you merged. Read it
  first.

## A bad deploy

**Symptom.** Right after a deploy: the worker crashes or restarts in a loop, `no-collection` or
`publish-failure` opens, or a run logs a traceback it never did before.

**Check.**

```bash
./ops/rollback.sh status
docker compose ps
docker compose logs worker --since 30m | grep -E "Traceback|Error|done: ok=" | tail -20
git log --oneline --first-parent -10 origin/main
```

The last command lists main's merges, newest first. The bad one is usually the top. The commit
below it is the last good one.

**Fix.**

1. **Pin the last good commit,** at a safe minute:

   ```bash
   ./ops/rollback.sh pin <last good sha>
   ```

   It prints `worker restarted on <sha>`, then `pinned. When main is fixed: ./ops/rollback.sh
   unpin`. `status` now shows `(detached: pinned)`.
2. **Check it took:** the next run logs `done: ok=` and `published`, and the incidents resolve
   about 15 minutes after their faults stop.
3. **Undo the merge on GitHub,** in your own checkout where you push from (the WSL box), not on
   the VM. This needs the GitHub CLI, `gh`, signed in:

   ```bash
   ./ops/rollback.sh revert <merge sha>
   ```

   It makes the revert in a temporary worktree, pushes the branch `revert/<short sha>`, and opens
   the pull request `Revert: <the merge's title>`. Review it and merge it like any other.
4. **Back on the VM, unpin,** at a safe minute: `./ops/rollback.sh unpin`. That is
   [a normal deploy](#a-normal-deploy) of main with the revert in it.

While pinned, the worker stays on that commit. A merge on GitHub reaches it only at `unpin`.

**If the bad merge changed `requirements.txt` or `Dockerfile.worker`,** pinning moves the code
back but not the image. After `pin`, rebuild as in [a normal deploy](#a-normal-deploy).

**`rollback: the checkout has local changes; commit or remove them first`.** `pin` and `unpin`
refuse a checkout that differs from git. Nothing should edit files on the VM, so find out what
changed and who changed it:

```bash
git status
git diff --stat
```

Do not commit them on the VM. If you made the change, save it outside the checkout, then put the
file back:

```bash
git diff > ~/vm-changes-$(date -u +%Y%m%dT%H%M%SZ).patch
git restore <file>
```

Move an untracked file out of the checkout rather than deleting it. If you did not make the
change, stop: the worker can write to the checkout
([threat model, B2](../threat-model.md#b2-untrusted-feed-content-into-the-worker-and-into-model-prompts)),
so treat it as a security incident. Keep the files as they are and look at them first.

**`unpin` stops on `git pull`:** the VM's `main` has commits GitHub's does not. Nobody should
commit on the VM. Run `git log --oneline origin/main..main` to see them, and stop there.

**Stop and decide yourself:**

- **Pinning does not help:** the same error on the last good commit means the fault is not in the
  code. Look at the database, the disk and the network
  ([Watchdog incidents](watchdog-incidents.md#no-collection)).
- **A migration did the damage,** such as a column the old code cannot read, or rows changed. Code
  can go back, but the database cannot, by design. The way back is a restore
  ([Backup and restore](backup-and-restore.md)), and that is your decision.
- **Going back to an older Paperclip image.** See
  [Services, Paperclip down](services.md#paperclip-down).

---

[Runbooks](README.md) · [Watchdog incidents](watchdog-incidents.md) · [Backup and restore](backup-and-restore.md)
