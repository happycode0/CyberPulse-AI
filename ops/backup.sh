#!/usr/bin/env bash
# Back up both databases, Paperclip's secrets folder and .env into a new directory that only
# its owner can read (docs/runbooks/backup-and-restore.md).
#
#   ./ops/backup.sh [--databases-only] [--keep N] <target-dir>
#
#   --databases-only  the two database dumps only: no paperclip-secrets/, no env
#   --keep N          after a good backup, delete the oldest complete backups in <target-dir>
#                     beyond the newest N (default 14). Nothing else there is ever deleted.
#
# Run it in the checkout on the VM, with the stack up. <target-dir> must already exist: point it
# at the off-host target (NAS, USB disk), because a backup on the VM's own disk dies with it.
# Each run writes <target-dir>/cyberpulse-<YYYYmmddTHHMMSSZ>/:
#
#   cyber_intel.dump  paperclip.dump  pg_dump -Fc, each read back with pg_restore -l
#   paperclip-secrets/                the keys Paperclip signs and encrypts with
#   env                               a copy of .env
#   manifest.txt                      time, commit, file sizes, every table's row count
#   SHA256SUMS                        of all of the above
#   COMPLETE                          empty, written last. No COMPLETE, no backup.
#
# Each dump and its row counts come from one snapshot, so `ops/restore.sh rehearse` can compare
# them exactly while the worker and Paperclip keep writing.

set -euo pipefail
export LC_ALL=C
umask 077

die() { echo "backup: $*" >&2; exit 1; }
usage() { awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"; }
bad_usage() { [[ -z ${1:-} ]] || echo "backup: $1" >&2; usage >&2; exit 2; }

DATABASES=(cyber_intel paperclip)
SECRETS=/paperclip/instances/default/secrets

# Every table's exact row count, one "<rows> <schema>.<table>" line each, in C order.
# ops/restore.sh runs the same query; tests/unit/test_ops_scripts.py keeps the two identical.
ROW_COUNTS_SQL=$(cat <<'SQL'
SELECT (xpath('/row/n/text()', query_to_xml(
          format('SELECT count(*) AS n FROM %I.%I', ns.nspname, cl.relname),
          false, true, '')))[1]::text || ' ' || format('%I.%I', ns.nspname, cl.relname)
  FROM pg_class cl JOIN pg_namespace ns ON ns.oid = cl.relnamespace
 WHERE cl.relkind = 'r' AND cl.relpersistence <> 't'
   AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
 ORDER BY format('%I.%I', ns.nspname, cl.relname) COLLATE "C";
SQL
)

keep=14 dbonly=no target=
while (($#)); do
  case $1 in
    --databases-only) dbonly=yes ;;
    --keep)
      [[ ${2:-} =~ ^[1-9][0-9]*$ ]] || bad_usage "--keep needs a whole number, 1 or more"
      keep=$2
      shift
      ;;
    -h | --help) usage; exit 0 ;;
    -*) bad_usage "unknown option $1" ;;
    *) [[ -z $target ]] || bad_usage "one target directory only"; target=$1 ;;
  esac
  shift
done
[[ -n $target ]] || bad_usage
[[ -d $target ]] || die "$target is not a directory (is the backup target mounted?)"
target=$(cd "$target" && pwd -P)

cd "$(dirname "$0")/.."
[[ -f docker-compose.yml ]] || die "no docker-compose.yml in $PWD"
[[ $dbonly == yes || -f .env ]] || die "no .env in $PWD (or use --databases-only)"

dir= session=
work=$(mktemp -d)
finish() {
  local rc=$?
  if [[ -n $session ]]; then kill "$session" 2>/dev/null || true; fi
  rm -rf "$work"
  if ((rc != 0)) && [[ -n $dir && -d $dir && ! -e $dir/COMPLETE ]]; then
    echo "backup: FAILED. $dir has no COMPLETE marker, so restore.sh and --keep ignore it." >&2
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Write lines to the open psql session. A session that has gone away is an error here, not a
# SIGPIPE that kills the script before it can say what failed.
send() {
  local fd=$1 rc=0
  shift
  trap '' PIPE
  printf '%s\n' "$@" >&"$fd" || rc=$?
  trap - PIPE
  return "$rc"
}

# One READ ONLY, REPEATABLE READ transaction exports its snapshot, pg_dump dumps from that
# snapshot, and the row counts are taken in the same transaction afterwards. Counted
# separately, rows written in between would show up as a mismatch in the rehearsal.
dump_database() {
  local db=$1 snap to from rc=0
  rm -f "$work/sql" "$work/out"
  mkfifo "$work/sql" "$work/out"
  docker compose exec -T db sh -c \
    'exec psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"' sh "$db" \
    <"$work/sql" >"$work/out" &
  session=$!
  exec {to}>"$work/sql" {from}<"$work/out"

  # psql flushes after every result, so the snapshot name arrives while the session stays open.
  send "$to" 'BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;' \
    'SELECT pg_export_snapshot();' || die "could not open a transaction in $db"
  if ! read -r -t 120 snap <&"$from" || [[ ! $snap =~ ^[0-9A-F-]+$ ]]; then
    die "could not open a transaction in $db"
  fi

  docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc --snapshot="$2" "$1"' \
    sh "$db" "$snap" </dev/null >"$dir/$db.dump" || die "pg_dump of $db failed"
  [[ -s $dir/$db.dump ]] || die "pg_dump of $db wrote nothing"

  send "$to" "$ROW_COUNTS_SQL" 'COMMIT;' || die "counting the rows of $db failed"
  exec {to}>&-
  cat <&"$from" >"$work/$db.rows"
  exec {from}<&-
  wait "$session" || rc=$?
  session=
  ((rc == 0)) || die "counting the rows of $db failed"
  if grep -Evq '^[0-9]+ .+$' "$work/$db.rows"; then
    die "counting the rows of $db gave unexpected output"
  fi
  echo "dumped $db: $(awk 'END { print NR }' "$work/$db.rows") tables"
}

read -r stamp created < <(date -u '+%Y%m%dT%H%M%SZ %Y-%m-%dT%H:%M:%SZ')
dir=$target/cyberpulse-$stamp
mkdir -m 700 "$dir" || die "could not create $dir"
chmod 700 "$dir"
echo "backing up to $dir"

for db in "${DATABASES[@]}"; do
  dump_database "$db"
done

# pg_restore -l reads the whole table of contents: a truncated or garbled dump fails here.
for db in "${DATABASES[@]}"; do
  docker compose exec -T db pg_restore -l <"$dir/$db.dump" >/dev/null \
    || die "$db.dump does not read back with pg_restore -l"
done
echo "both dumps read back with pg_restore -l"

if [[ $dbonly == no ]]; then
  docker compose cp "server:$SECRETS" "$dir/paperclip-secrets" </dev/null >/dev/null \
    || die "could not copy $SECRETS out of the server container (or use --databases-only)"
  [[ -d $dir/paperclip-secrets ]] || die "the server container has no $SECRETS"
  [[ -n $(find "$dir/paperclip-secrets" -type f -print -quit) ]] \
    || die "$SECRETS in the server container is empty"
  find "$dir/paperclip-secrets" -type d -exec chmod 700 {} +
  find "$dir/paperclip-secrets" -type f -exec chmod 600 {} +
  echo "copied paperclip-secrets ($(find "$dir/paperclip-secrets" -type f | wc -l) files)"

  cp .env "$dir/env" || die "could not copy .env"
  chmod 600 "$dir/env"
  echo "copied .env"
fi

{
  echo "# CyberPulse-AI backup manifest, written by ops/backup.sh. Sizes are in bytes."
  echo "created_utc $created"
  echo "git_head $(git rev-parse --verify -q HEAD 2>/dev/null || echo unknown)"
  echo "contents $([[ $dbonly == yes ]] && echo databases-only || echo full)"
  (cd "$dir" && find . -type f ! -path ./manifest.txt -printf 'file %s %P\n' | sort -k3)
  for db in "${DATABASES[@]}"; do
    echo "tables $db $(awk 'END { print NR }' "$work/$db.rows")"
    awk -v db="$db" '{ print "rows " db " " $0 }' "$work/$db.rows"
  done
} >"$dir/manifest.txt"

(cd "$dir" && find . -type f ! -path ./SHA256SUMS ! -path ./COMPLETE -printf '%P\0' \
  | sort -z | xargs -0 -r sha256sum --) >"$dir/SHA256SUMS" || die "could not write SHA256SUMS"

: >"$dir/COMPLETE"
echo "complete: $dir ($(du -sh "$dir" | cut -f1))"

# Retention: only directories named like ours, that finished, and never the one just written.
shopt -s nullglob
complete=()
for old in "$target"/cyberpulse-*; do
  if [[ ${old##*/} =~ ^cyberpulse-[0-9]{8}T[0-9]{6}Z$ && -d $old && ! -L $old && -f $old/COMPLETE ]]; then
    complete+=("$old")
  fi
done
for ((i = 0; i < ${#complete[@]} - keep; i++)); do
  [[ ${complete[i]} != "$dir" ]] || continue
  rm -rf -- "${complete[i]}"
  echo "removed old backup ${complete[i]##*/} (keeping $keep)"
done
