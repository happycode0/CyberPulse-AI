# Stage 6 — Self-healing

[← Stage 5 — Full crew](stage-5-full-crew.md) · [Wiki home](README.md) ·
[Stage 7 — Hardening →](stage-7-hardening.md)

**Status: ⬜ not started.** Plan: [PLAN.md §9, Stage 6](../../PLAN.md#9-stages) · How the loop
runs: [4c, the repair loop](stage-4c-how-the-crew-works.md#6-repair-loop--stage-6)

---

## What this stage gives you

When a source breaks (a changed feed, a parser that stops matching), the crew notices, finds the
cause, writes a fix with a test, and has a second agent check it. **Then you approve the merge.**
No code reaches production without you.

## Switched on in this stage

| Agent | Does | Model |
|---|---|---|
| WHEELJACK | Writes the fix on a branch, with a regression test, and opens a PR | CODE tier |
| TRON | Independently verifies it: PASS or FAIL | AUDIT tier, **a different vendor** from WHEELJACK's |

## What's left

| Who | Step |
|---|---|
| 🔴 | **A second fine-grained token**, `CYBERPULSE_ENGINEER_TOKEN`: this repo only, 90-day expiry, **Contents** and **Pull requests** read and write, nothing else. It is the only credential WHEELJACK ever gets |
| 🔴 | **Branch protection on `main`:** require a pull request and your review before merging |
| 🔴 | **Approve the un-pause** of WHEELJACK and TRON |
| 🟢 | TELETRAAN's full detection suite, including feeds that look healthy but have gone stale |
| 🟢 | The incident model; WHEELJACK's worktree workflow and sandbox |
| 🟢 | Circuit breaker: the same fix failing 3 times stops everything and waits for you; rollback |

## Done when

- [ ] A deliberately broken parser is detected, diagnosed, fixed on a branch, verified
      independently, and merged after your approval
- [ ] The circuit breaker is proven to stop after 3 failures

---

[← Stage 5](stage-5-full-crew.md) · [Wiki home](README.md) ·
**Next:** [Stage 7 — Hardening + operations →](stage-7-hardening.md)
