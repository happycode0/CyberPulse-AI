# Stage 7 — Hardening + operations

[← Stage 6 — Self-healing](stage-6-self-healing.md) · [Wiki home](README.md)

**Status: ⬜ not started, but one part of it should not wait: backups.** Plan:
[PLAN.md §9, Stage 7](../../PLAN.md#9-stages) · Build record: [runbook Part 7](../vm200-runbook.md)

---

## What this stage gives you

Confidence that it survives: a backup that has actually been restored, failures injected on
purpose and handled, and the threat model reviewed against what was really built.

## Backups — do this one early

There is real data on the VM now, and **the Proxmox host has a single disk**. A backup on that
disk dies with it.

| Who | Step |
|---|---|
| 🔴 | **Provide an off-host target:** a NAS over NFS or SMB, a USB disk, or a Proxmox Backup Server |
| 🔴 | On the Proxmox host: a `vzdump` job for VM 200, **snapshot** mode, `keep-daily=7,keep-weekly=4,keep-monthly=3`, *Repeat missed* on |
| 🟢 | A scheduled dump of both databases plus Paperclip's secrets folder to that target |

What has to be in a backup. Each part is useless without the others:

```bash
cd ~/CyberPulse-AI
docker compose exec -T db pg_dump -U cyberpulse cyber_intel | gzip > /backup/cyber_intel.sql.gz
docker compose exec -T db pg_dump -U cyberpulse paperclip   | gzip > /backup/paperclip.sql.gz
docker compose cp server:/paperclip/instances/default/secrets /backup/paperclip-secrets
cp -p .env /backup/env                                       # keep this copy private
```

The secrets folder holds the keys Paperclip signs and encrypts with. On 2026-10-02 it contained
`decision-signing.key`; Paperclip adds `master.key` there when it first stores a secret. Copy the
whole folder so whatever is there gets saved.

Paperclip also dumps its own database every 60 minutes (kept 7 days) into
`/paperclip/instances/default/data/backups` inside the `paperclip-data` volume. Useful for undoing
a bad change, but it lives on the same disk, so it does not count as the backup.

Keep the Docker volumes as named volumes. `vzdump` captures those, but Proxmox does not back up
bind mounts.

## The rest of the stage (🟢 Claude)

1. A restore rehearsal: a scratch VM rebuilt from the backup alone.
2. Observability and OpenTelemetry; cost tuning.
3. More notification channels.
4. A failure-injection test suite.
5. A threat-model review, and runbooks for the common failures.

## Optional (🔴 you)

- **NetBird**, to reach the dashboard from outside the house without opening anything to the
  internet (runbook Part 6).

## Done when

- [ ] A restore from backup has been rehearsed successfully
- [ ] Every failure-injection test passes

---

[← Stage 6](stage-6-self-healing.md) · [Wiki home](README.md) · **That's the last stage.** Back to
[the start page](README.md) to see what's next.
