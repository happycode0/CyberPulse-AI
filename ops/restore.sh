#!/usr/bin/env bash
# Prove a backup made by ops/backup.sh restores, or restore it onto a rebuilt VM
# (docs/wiki/stage-7-hardening.md#backups).
#
#   ./ops/restore.sh rehearse <backup-dir>     restore into a throwaway Postgres and compare
#   ./ops/restore.sh fresh <backup-dir> --yes  restore into this checkout's empty compose db
#
# Both start by checking the COMPLETE marker and SHA256SUMS, and stop on a damaged backup.
#
# `rehearse` leaves the stack alone. It starts a throwaway container of the compose db's image,
# with no network and no published ports, restores both dumps into it and compares every
# table's row count with the manifest: PASS or FAIL per database, and a non-zero exit on any
# mismatch. The container is removed however the run ends.
#
# `fresh` is for a rebuilt VM. Before it: clone the repo, restore .env by hand from the backup's
# env, `docker compose up -d db`, and do not start the server yet: Paperclip sets up an empty
# database as soon as it starts. `fresh` refuses unless cyber_intel and paperclip have no tables,
# so it never overwrites a database. It copies paperclip-secrets/ into the server container
# (creating it, not started, if there is none), restores both databases, checks their row counts
# and prints the steps that are left to do by hand.

set -euo pipefail
export LC_ALL=C
umask 077

die() { echo "restore: $*" >&2; exit 1; }
usage() { awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"; }
bad_usage() { [[ -z ${1:-} ]] || echo "restore: $1" >&2; usage >&2; exit 2; }

DATABASES=(cyber_intel paperclip)
SECRETS=/paperclip/instances/default/secrets

# Every table's exact row count, one "<rows> <schema>.<table>" line each, in C order.
# ops/backup.sh runs the same query; tests/unit/test_ops_scripts.py keeps the two identical.
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

TABLES_SQL="SELECT count(*) FROM pg_class cl JOIN pg_namespace ns ON ns.oid = cl.relnamespace
 WHERE cl.relkind IN ('r', 'p') AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
   AND ns.nspname !~ '^pg_'"

# As the databases were first made: cyber_intel by the db image (POSTGRES_DB), paperclip by
# docs/wiki/stage-4a-paperclip-setup.md step 1.
create_sql() {
  case $1 in
    paperclip) echo "CREATE DATABASE paperclip ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0" ;;
    *) echo "CREATE DATABASE $1" ;;
  esac
}

cmd=${1:-}
case $cmd in
  rehearse | fresh) shift ;;
  -h | --help | "") usage; exit 0 ;;
  *) bad_usage "unknown command $cmd" ;;
esac
backup= yes=no
for arg in "$@"; do
  case $arg in
    --yes) [[ $cmd == fresh ]] || bad_usage "--yes is for fresh only"; yes=yes ;;
    -*) bad_usage "unknown option $arg" ;;
    *) [[ -z $backup ]] || bad_usage "one backup directory only"; backup=$arg ;;
  esac
done
[[ -n $backup ]] || bad_usage "which backup? $cmd needs a backup directory"
[[ -d $backup ]] || die "$backup is not a directory"
backup=$(cd "$backup" && pwd -P)

cd "$(dirname "$0")/.."
[[ -f docker-compose.yml ]] || die "no docker-compose.yml in $PWD"

container=
work=$(mktemp -d)
finish() {
  local rc=$?
  if [[ -n $container ]]; then
    docker rm -f "$container" </dev/null >/dev/null 2>&1 || true
    echo "removed the throwaway container $container"
  fi
  rm -rf "$work"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

check_backup() {
  local f db
  [[ -f $backup/COMPLETE ]] || die "$backup has no COMPLETE marker: that backup did not finish"
  for f in SHA256SUMS manifest.txt cyber_intel.dump paperclip.dump; do
    [[ -f $backup/$f ]] || die "$backup has no $f"
  done
  for f in manifest.txt cyber_intel.dump paperclip.dump; do
    grep -qE "^[0-9a-f]{64} [ *]$f\$" "$backup/SHA256SUMS" || die "SHA256SUMS does not cover $f"
  done
  (cd "$backup" && sha256sum --check --quiet --strict SHA256SUMS) >&2 \
    || die "$backup does not match its SHA256SUMS: the backup is damaged or was changed"
  for db in "${DATABASES[@]}"; do
    grep -qE "^tables $db [0-9]+\$" "$backup/manifest.txt" \
      || die "manifest.txt has no row counts for $db"
  done
  echo "checked $backup: COMPLETE, SHA256SUMS match" \
    "(made $(awk '$1 == "created_utc" { print $2 }' "$backup/manifest.txt")," \
    "commit $(awk '$1 == "git_head" { print substr($2, 1, 12) }' "$backup/manifest.txt"))"
}

# Lines of pg_restore's stderr worth showing. DETAIL and CONTEXT lines can quote row data.
show_errors() {
  grep -Ev '^(DETAIL|CONTEXT|HINT): ' "$1" | head -n 20 >&2 || true
}

# compare <db> <restored-counts-file>: one summary line, plus the tables that differ.
compare() {
  local db=$1 got=$2 want=$work/$1.manifest tables
  awk -v db="$db" '$1 == "rows" && $2 == db { sub(/^rows [^ ]+ /, ""); print }' \
    "$backup/manifest.txt" >"$want"
  tables=$(awk -v db="$db" '$1 == "tables" && $2 == db { print $3 }' "$backup/manifest.txt")
  awk -v db="$db" -v tables="$tables" '
    { n = $1; sub(/^[^ ]+ /, "") }
    FILENAME == ARGV[1] { want[$0] = n; nwant++; total += n; next }
    { got[$0] = n }
    END {
      for (t in want)
        if (!(t in got)) diff[++bad] = sprintf("  %s: %s rows in the manifest, missing after restore", t, want[t])
        else if (got[t] != want[t]) diff[++bad] = sprintf("  %s: %s rows in the manifest, %s after restore", t, want[t], got[t])
      for (t in got)
        if (!(t in want)) diff[++bad] = sprintf("  %s: not in the manifest, %s rows after restore", t, got[t])
      if (nwant != tables) diff[++bad] = sprintf("  manifest.txt lists %d tables but says %s", nwant, tables)
      if (bad) {
        printf "%-12s FAIL  %d difference(s):\n", db, bad
        for (i = 1; i <= bad; i++) print diff[i]
        exit 1
      }
      printf "%-12s PASS  %d tables, %d rows, as in the manifest\n", db, nwant, total
    }' "$want" "$got"
}

db_image() {
  awk '
    /^services:/ { services = 1; next }
    services && /^[^ #]/ { exit }
    services && /^  db:/ { db = 1; next }
    db && /^  [^ #]/ { exit }
    db && /^    image:/ {
      sub(/^    image:[ ]*/, ""); sub(/[ ]+#.*$/, ""); gsub(/["\047]/, ""); print; exit
    }' docker-compose.yml
}

rehearse() {
  local image pw db i ok=yes
  check_backup
  image=$(db_image)
  [[ $image =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]] \
    || die "could not read the db service's image from docker-compose.yml"
  pw=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
  [[ ${#pw} -eq 48 ]] || die "could not generate a password"

  container=cyberpulse-restore-$(date -u +%Y%m%dT%H%M%SZ)
  echo "starting $container from $image (no network, no ports)"
  # -e with no value: docker reads the password from its environment, so it is not in argv.
  POSTGRES_PASSWORD=$pw docker run -d --rm --network none --shm-size 256m \
    --name "$container" -e POSTGRES_PASSWORD "$image" </dev/null >/dev/null \
    || die "could not start the throwaway container"
  pw=

  # Over TCP: the image's first-boot setup server listens on the socket only, then restarts.
  for ((i = 0; ; i++)); do
    if docker exec "$container" pg_isready -q -h 127.0.0.1 -U postgres </dev/null >/dev/null 2>&1; then
      break
    fi
    ((i < 120)) || die "the throwaway postgres did not come up in 2 minutes"
    sleep 1
  done

  for db in "${DATABASES[@]}"; do
    echo "restoring $db.dump"
    if ! docker exec -i "$container" pg_restore -U postgres -d postgres --create --no-owner \
      --no-privileges --exit-on-error <"$backup/$db.dump" 2>"$work/$db.err"; then
      show_errors "$work/$db.err"
      printf '%-12s FAIL  pg_restore failed\n' "$db"
      ok=no
      continue
    fi
    if ! printf '%s\n' 'BEGIN TRANSACTION READ ONLY;' "$ROW_COUNTS_SQL" 'COMMIT;' \
      | docker exec -i "$container" psql -X -q -A -t -v ON_ERROR_STOP=1 -U postgres -d "$db" \
        >"$work/$db.restored"; then
      printf '%-12s FAIL  could not count its rows\n' "$db"
      ok=no
      continue
    fi
    compare "$db" "$work/$db.restored" || ok=no
  done

  [[ $ok == yes ]] || die "rehearsal FAILED: $backup does not restore to what its manifest says"
  echo "rehearsal PASSED: both databases restore to the row counts in the manifest"
}

# db_sql <database> <sql>: one statement in the compose db; unaligned, no headers.
db_sql() {
  docker compose exec -T db sh -c \
    'psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -c "$2"' sh "$1" "$2" \
    </dev/null
}

fresh() {
  local db n restored=() missing=" " stage server user ok=yes
  [[ $yes == yes ]] || die "fresh restores into this checkout's compose db; add --yes to go ahead"
  check_backup
  [[ -d $backup/paperclip-secrets ]] \
    || die "$backup has no paperclip-secrets (made with --databases-only?); fresh needs a full backup"

  docker compose exec -T db pg_isready -q </dev/null \
    || die "the compose db is not up. Restore .env, then: docker compose up -d db"
  [[ -z $(docker compose ps -q --status running server </dev/null) ]] \
    || die "the server is running and may set up an empty paperclip database; docker compose stop server"

  for db in "${DATABASES[@]}"; do
    n=$(db_sql postgres "SELECT count(*) FROM pg_database WHERE datname = '$db'") \
      || die "could not query the compose db"
    if [[ $n == 0 ]]; then
      missing+="$db "
      continue
    fi
    n=$(db_sql "$db" "$TABLES_SQL") || die "could not count the tables in $db"
    [[ $n =~ ^[0-9]+$ ]] || die "could not count the tables in $db"
    ((n == 0)) || die "refusing: $db already has $n tables. fresh only restores into empty databases"
  done
  echo "cyber_intel and paperclip have no tables yet"

  if [[ -z $(docker compose ps -a -q server </dev/null) ]]; then
    echo "creating the server container (not starting it)"
    docker compose create --no-recreate server </dev/null || die "could not create the server container"
  fi
  server=$(docker compose ps -a -q server </dev/null)
  [[ -n $server ]] || die "there is no server container to copy paperclip-secrets into"

  # Copied as instances/default/secrets, so parent folders a new volume lacks are made too.
  stage=$work/stage
  mkdir -p "$stage/instances/default"
  chmod 755 "$stage/instances" "$stage/instances/default"
  cp -Rp "$backup/paperclip-secrets" "$stage/instances/default/secrets"
  docker compose cp "$stage/instances" server:/paperclip </dev/null >/dev/null \
    || die "could not copy paperclip-secrets into the server container"
  rm -rf "$stage"
  echo "copied paperclip-secrets to $SECRETS in the server container"

  # docker cp leaves what it copies in owned by root, and the keys are 0600. If Paperclip runs as
  # another user, hand the folders to that user from a one-off root container of the service.
  user=$(docker inspect --format '{{.Config.User}}' "$server" </dev/null) \
    || die "could not inspect the server container"
  if [[ -n $user && ! $user =~ ^(root|0)(:(root|0))?$ ]]; then
    docker compose run --rm --no-deps -T --user 0:0 --entrypoint chown server \
      -R "$user" /paperclip/instances </dev/null >/dev/null \
      || die "could not give /paperclip/instances to $user, the user Paperclip runs as"
    echo "gave /paperclip/instances to $user, the user Paperclip runs as"
  fi

  for db in "${DATABASES[@]}"; do
    if [[ $missing == *" $db "* ]]; then
      db_sql postgres "$(create_sql "$db")" >/dev/null || die "could not create database $db"
      echo "created database $db"
    fi
    echo "restoring $db.dump"
    # One transaction: a failed restore leaves the database empty, so fresh can run again.
    if ! docker compose exec -T db sh -c \
      'pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --single-transaction --exit-on-error' \
      sh "$db" <"$backup/$db.dump" 2>"$work/$db.err"; then
      show_errors "$work/$db.err"
      die "restoring $db failed and was rolled back. Restored before it: ${restored[*]:-nothing}"
    fi
    restored+=("$db")
  done

  for db in "${DATABASES[@]}"; do
    printf '%s\n' 'BEGIN TRANSACTION READ ONLY;' "$ROW_COUNTS_SQL" 'COMMIT;' \
      | docker compose exec -T db sh -c \
        'psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"' sh "$db" \
        >"$work/$db.restored" || die "could not count the rows of the restored $db"
    compare "$db" "$work/$db.restored" || ok=no
  done
  [[ $ok == yes ]] || die "restored, but the row counts differ from the manifest (above)"

  cat <<EOF

Restored both databases and Paperclip's secrets. Left to do by hand:
  1. .env: this script never copies it. If .env is not the backup's yet:
       cp -p $backup/env .env && chmod 600 .env
     keeping the POSTGRES_PASSWORD this db volume was set up with.
  2. docker compose up -d
  3. docker compose exec -T worker python -m worker --migrate
EOF
}

"$cmd"
