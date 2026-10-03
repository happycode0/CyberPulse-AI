#!/usr/bin/env bash
# Roll the worker back to an earlier commit of main, and forward again
# (docs/wiki/stage-6-self-healing.md#rollback).
#
#   ./ops/rollback.sh status              what is running, against main's tip
#   ./ops/rollback.sh pin <sha>           run an earlier commit of main now (on the VM)
#   ./ops/rollback.sh unpin               back to main's tip, migrated (on the VM)
#   ./ops/rollback.sh revert <merge-sha>  open a pull request that undoes one merge
#
# `pin` is the fast way out: it checks out a commit main has already had and restarts the
# worker, which runs the checkout it mounts. Migrations only ever add, so an earlier commit
# runs on the newer schema and nothing is rolled back in the database.
#
# `revert` is the lasting fix. It works in a temporary worktree, so the checkout it runs in
# is never touched, and it pushes a branch and opens a pull request that a human merges.
# Run it where you push from, not on the VM. Once the revert is merged, `unpin`.
#
# A deploy restarts the worker, and a restart across a job's minute loses that run. Run `pin`
# and `unpin` between :10 and :45 past the hour: the fast run is at :00 UTC, its alerts and
# enrichment follow at :03 and :05, and the source gate runs at :50.

set -euo pipefail
cd "$(dirname "$0")/.."

die() { echo "rollback: $*" >&2; exit 1; }

clean() {
  [[ -z "$(git status --porcelain)" ]] || die "the checkout has local changes; commit or remove them first"
}

on_main() {
  git rev-parse -q --verify "$1^{commit}" >/dev/null || die "$1 is not a commit here"
  git merge-base --is-ancestor "$1" origin/main || die "$1 is not on main"
}

restart() {
  docker compose restart worker </dev/null
  echo "worker restarted on $(git rev-parse --short HEAD)"
}

cmd=${1:-}
case "$cmd" in
  status)
    git fetch -q origin
    head=$(git rev-parse --short HEAD)
    tip=$(git rev-parse --short origin/main)
    branch=$(git symbolic-ref -q --short HEAD || echo "detached: pinned")
    echo "running $head ($branch); main is at $tip"
    [[ "$head" == "$tip" ]] || echo "main has $(git rev-list --count HEAD..origin/main) commit(s) this checkout does not run"
    ;;
  pin)
    sha=${2:-}
    [[ -n "$sha" ]] || die "usage: $0 pin <sha>"
    clean
    git fetch -q origin
    on_main "$sha"
    git checkout -q --detach "$sha"
    restart
    echo "pinned. When main is fixed: $0 unpin"
    ;;
  unpin)
    clean
    git checkout -q main
    git pull -q --ff-only
    docker compose exec -T worker python -m worker --migrate </dev/null
    restart
    ;;
  revert)
    sha=${2:-}
    [[ -n "$sha" ]] || die "usage: $0 revert <merge-sha>"
    command -v gh >/dev/null || die "the GitHub CLI (gh) is needed to open the pull request"
    git fetch -q origin
    on_main "$sha"
    short=$(git rev-parse --short "$sha")
    branch="revert/$short"
    subject=$(git log -1 --format=%s "$sha")
    work=$(mktemp -d)
    trap 'git worktree remove --force "$work" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
    git worktree add -q -b "$branch" "$work" origin/main
    parents=$(git rev-list --parents -n 1 "$sha" | wc -w)
    if (( parents > 2 )); then
      git -C "$work" revert --no-edit -m 1 "$sha"
    else
      git -C "$work" revert --no-edit "$sha"
    fi
    git -C "$work" push -q -u origin "$branch"
    (cd "$work" && gh pr create --base main --head "$branch" \
      --title "Revert: $subject" \
      --body "Undoes $short ($subject), made with ops/rollback.sh. Review it, merge it, then run ops/rollback.sh unpin on the VM.")
    ;;
  *)
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    [[ -z "$cmd" ]] || exit 2
    ;;
esac
