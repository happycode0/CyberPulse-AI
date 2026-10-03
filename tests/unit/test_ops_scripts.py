"""ops/backup.sh and ops/restore.sh, run against a fake docker that logs its argv and pretends.

No real docker, database or .env is touched: each test copies the scripts into tmp_path with a
minimal docker-compose.yml and an obviously fake .env, and puts the fake docker first on PATH.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from worker import scheduler
from worker.cadence import fast_minutes

OPS = Path(__file__).resolve().parents[2] / "ops"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="the ops scripts need bash")

FAKE_ENV = "FAKE_SETTING=fake-env-TESTONLY\n"
FAKE_KEY = "fake-key-TESTONLY"
DB_IMAGE = "example.invalid/pgvector:fake-TESTONLY"
COMPOSE = f"""services:
  db:
    image: {DB_IMAGE}
    environment:
      POSTGRES_USER: ${{POSTGRES_USER}}
  server:
    image: example.invalid/paperclip:fake-TESTONLY
"""
STAMP = re.compile(r"^cyberpulse-\d{8}T\d{6}Z$")

# The fake docker. It answers the handful of calls the scripts make: dumps are "PGDMP" plus a
# line of text, row counts come from ROWS (or $FAKE_DOCKER_ROWS), and any call whose argv
# matches the regex $FAKE_DOCKER_FAIL exits 1.
FAKE_DOCKER = r"""#!@PYTHON@
import json, os, re, stat, sys

argv = sys.argv[1:]
joined = " ".join(argv)
st = os.fstat(0)
null = stat.S_ISCHR(st.st_mode) and st.st_rdev == os.stat("/dev/null").st_rdev
entry = {
    "argv": argv,
    "stdin": "null" if null else "data",
    "password_env": bool(os.environ.get("POSTGRES_PASSWORD")),
}
if argv[:2] == ["compose", "cp"] and argv[3].startswith("server:"):
    base = os.path.dirname(argv[2].rstrip("/"))
    entry["copied"] = sorted(
        os.path.relpath(os.path.join(d, f), base) for d, _, fs in os.walk(argv[2]) for f in fs
    )
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")

fail = os.environ.get("FAKE_DOCKER_FAIL")
if fail and re.search(fail, joined):
    sys.stderr.write("fake docker: failing on purpose\n")
    sys.exit(1)

ROWS = {"cyber_intel": ["3 public.events", "5 public.sources"], "paperclip": ["2 public.agents"]}
ROWS.update(json.loads(os.environ.get("FAKE_DOCKER_ROWS", "{}")))


def read_dump():
    if not sys.stdin.buffer.read().startswith(b"PGDMP"):
        sys.stderr.write("fake docker: that is not a dump\n")
        sys.exit(1)


def psql(db):
    seen = ""
    for line in iter(sys.stdin.readline, ""):
        seen += line
        if "pg_export_snapshot" in line:
            print("00000003-0000001B-1", flush=True)
    if "query_to_xml" in seen:
        for row in ROWS.get(db, []):
            print(row)


if argv[:2] == ["compose", "exec"]:
    rest = argv[argv.index("db") + 1 :]
    if rest[0] == "sh":
        script, args = rest[2], rest[4:]
        if "pg_dump" in script:
            sys.stdout.buffer.write(b"PGDMP fake dump of " + args[0].encode() + b"\n")
        elif "pg_restore" in script:
            read_dump()
        elif '-c "$2"' in script:
            if "pg_database" in args[1]:
                print(0 if args[1].split("'")[1] in os.environ.get("FAKE_DOCKER_MISSING", "") else 1)
            elif "relkind" in args[1]:
                print(os.environ.get("FAKE_DOCKER_TABLES", "0"))
        else:
            psql(args[0])
    elif rest[0] == "pg_restore":
        read_dump()
        print("; fake table of contents")
elif argv[:2] == ["compose", "cp"] and argv[2].startswith("server:"):
    os.makedirs(argv[3])
    for name in ("decision-signing.key", "master.key"):
        path = os.path.join(argv[3], name)
        with open(path, "w") as f:
            f.write("fake-key-TESTONLY\n")
        os.chmod(path, 0o644)
    os.chmod(argv[3], 0o755)
elif argv[:2] == ["compose", "create"]:
    open(os.environ["FAKE_DOCKER_LOG"] + ".created", "w").close()
elif argv[:2] == ["compose", "ps"]:
    created = os.path.exists(os.environ["FAKE_DOCKER_LOG"] + ".created")
    if "--status" in argv:
        if os.environ.get("FAKE_DOCKER_SERVER_RUNNING"):
            print("fake-server-id")
    elif created or not os.environ.get("FAKE_DOCKER_NO_SERVER"):
        print("fake-server-id")
elif argv[0] == "run":
    print("fake-container-id")
elif argv[0] == "inspect":
    print(os.environ.get("FAKE_DOCKER_USER", "node"))
elif argv[0] == "exec":
    if "pg_restore" in argv:
        read_dump()
    elif "psql" in argv:
        psql(argv[argv.index("-d") + 1])
"""


class Stack:
    """A copy of the scripts in tmp_path, a fake docker on PATH, and a backup target."""

    def __init__(self, root: Path):
        self.root = root
        self.repo = root / "repo"
        (self.repo / "ops").mkdir(parents=True)
        for name in ("backup.sh", "restore.sh"):
            shutil.copy(OPS / name, self.repo / "ops" / name)
        (self.repo / "docker-compose.yml").write_text(COMPOSE)
        (self.repo / ".env").write_text(FAKE_ENV)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER.replace("@PYTHON@", sys.executable))
        docker.chmod(0o755)
        self.log = root / "docker.log"
        self.target = root / "backups"
        self.target.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "POSTGRES"))}
        self.env.update(
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", FAKE_DOCKER_LOG=str(self.log)
        )

    def run(self, script: str, *args, **env: str) -> subprocess.CompletedProcess:
        self.log.write_text("")
        return subprocess.run(
            [BASH, str(self.repo / "ops" / script), *map(str, args)],
            cwd=self.root,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def backup(self, *args, **env: str) -> Path:
        result = self.run("backup.sh", *args, self.target, **env)
        assert result.returncode == 0, result.stderr
        made = [d for d in self.target.iterdir() if STAMP.match(d.name)]
        return max(made)


@pytest.fixture
def stack(tmp_path):
    return Stack(tmp_path)


def joined(call: dict) -> str:
    return " ".join(call["argv"])


def needs_stdin(call: dict) -> bool:
    text = joined(call)
    return "pg_restore" in text or ("psql" in text and '-c "$2"' not in text)


def assert_safe_docker_use(calls: list[dict]) -> None:
    for call in calls:
        argv = call["argv"]
        # Nothing that removes volumes: no `compose down` at all, no `volume`, and `rm -v` only
        # for the rehearsal's own throwaway container, whose anonymous volume goes with it.
        assert "down" not in argv and argv[0] != "volume", call
        if argv[0] == "rm" and {"-v", "--volumes"} & set(argv):
            assert argv[-1].startswith("cyberpulse-restore-"), call
        if not needs_stdin(call):
            assert call["stdin"] == "null", f"no </dev/null: {joined(call)}"


def output(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


def complete_backup(path: Path) -> Path:
    path.mkdir()
    (path / "COMPLETE").write_text("")
    return path


# ─── both scripts ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("script", sorted(OPS.glob("*.sh")), ids=lambda p: p.name)
def test_every_ops_script_parses(script):
    result = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_backup_and_restore_count_rows_with_the_same_query():
    pattern = re.compile(r"ROW_COUNTS_SQL=\$\(cat <<'SQL'\n(.*?)\nSQL\n\)", re.DOTALL)
    queries = [pattern.search((OPS / name).read_text()) for name in ("backup.sh", "restore.sh")]
    assert all(queries)
    assert queries[0].group(1) == queries[1].group(1)
    assert "READ ONLY" in (OPS / "backup.sh").read_text()


def test_the_safe_minutes_to_restart_miss_the_busy_ones():
    """A restart across a job's minute loses that run. The window rollback.sh gives must miss each
    fast run, the alerts and the enrichment pass right after it, and the gate. A new fast cadence
    needs a new window here and in the runbooks."""
    window = re.compile(r"between :(\d\d) and :(\d\d)")
    found = window.search((OPS / "rollback.sh").read_text())
    assert found
    runbooks = (OPS.parent / "docs" / "runbooks" / "README.md").read_text()
    assert found.groups() in {m.groups() for m in window.finditer(runbooks)}
    safe = set(range(int(found[1]), int(found[2]) + 1))
    busy = {int(scheduler.GATE_SCHEDULE.split()[0])}
    for fast in fast_minutes():
        busy |= {fast, fast + scheduler.ALERT_AFTER_COLLECTION}
        busy |= {next((m for m in sorted(scheduler.ENRICH_MINUTES) if m > fast), fast)}
    assert not safe & busy
    assert len(safe) >= 30  # room to work in


@pytest.mark.parametrize(
    "script, args, code, shows",
    [
        ("restore.sh", [], 0, "rehearse <backup-dir>"),
        ("restore.sh", ["--help"], 0, "fresh <backup-dir> --yes"),
        ("restore.sh", ["restart"], 2, "unknown command restart"),
        ("restore.sh", ["rehearse"], 2, "needs a backup directory"),
        ("restore.sh", ["rehearse", "--yes", "x"], 2, "for fresh only"),
        ("backup.sh", [], 2, "[--databases-only] [--keep N] <target-dir>"),
        ("backup.sh", ["--help"], 0, "--keep N"),
        ("backup.sh", ["--bogus", "x"], 2, "unknown option --bogus"),
        ("backup.sh", ["--keep", "0", "x"], 2, "--keep needs a whole number"),
        ("backup.sh", ["a", "b"], 2, "one target directory only"),
    ],
)
def test_usage(stack, script, args, code, shows):
    result = stack.run(script, *args)
    assert result.returncode == code
    assert shows in output(result)
    assert "set -euo pipefail" not in output(result)
    assert stack.calls() == []


# ─── backup.sh ───────────────────────────────────────────────────────────────


def test_backup_writes_a_private_complete_backup(stack):
    result = stack.run("backup.sh", "backups")  # relative to the caller's directory
    assert result.returncode == 0, result.stderr
    [made] = stack.target.iterdir()
    assert STAMP.match(made.name)

    assert stat.S_IMODE(made.stat().st_mode) == 0o700
    for path in made.rglob("*"):
        want = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == want, path
    files = sorted(str(p.relative_to(made)) for p in made.rglob("*") if p.is_file())
    assert files == [
        "COMPLETE",
        "SHA256SUMS",
        "cyber_intel.dump",
        "env",
        "manifest.txt",
        "paperclip-secrets/decision-signing.key",
        "paperclip-secrets/master.key",
        "paperclip.dump",
    ]
    assert (made / "env").read_text() == FAKE_ENV
    assert (made / "cyber_intel.dump").read_bytes().startswith(b"PGDMP")

    manifest = (made / "manifest.txt").read_text().splitlines()
    assert any(
        re.fullmatch(r"created_utc \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line) for line in manifest
    )
    assert "git_head unknown" in manifest  # tmp_path is not a git checkout
    assert "contents full" in manifest
    for name in ("cyber_intel.dump", "paperclip.dump", "env", "paperclip-secrets/master.key"):
        assert f"file {(made / name).stat().st_size} {name}" in manifest
    assert [line for line in manifest if line.startswith(("tables ", "rows "))] == [
        "tables cyber_intel 2",
        "rows cyber_intel 3 public.events",
        "rows cyber_intel 5 public.sources",
        "tables paperclip 1",
        "rows paperclip 2 public.agents",
    ]

    sums = dict(
        reversed(line.split("  ", 1)) for line in (made / "SHA256SUMS").read_text().splitlines()
    )
    assert sorted(sums) == [f for f in files if f not in ("COMPLETE", "SHA256SUMS")]
    for name, digest in sums.items():
        assert hashlib.sha256((made / name).read_bytes()).hexdigest() == digest

    # COMPLETE is empty and written after everything else.
    assert (made / "COMPLETE").read_bytes() == b""
    last = max(p.stat().st_mtime_ns for p in made.rglob("*") if p.name != "COMPLETE")
    assert (made / "COMPLETE").stat().st_mtime_ns >= last

    assert "fake-env-TESTONLY" not in output(result)
    assert FAKE_KEY not in output(result)
    assert FAKE_KEY not in (made / "manifest.txt").read_text()

    calls = stack.calls()
    assert_safe_docker_use(calls)
    dumps = [c for c in calls if "pg_dump" in joined(c)]
    assert [c["argv"][-2:] for c in dumps] == [
        ["cyber_intel", "00000003-0000001B-1"],
        ["paperclip", "00000003-0000001B-1"],
    ]
    assert all("--snapshot=" in joined(c) for c in dumps)
    assert sum("pg_restore -l" in joined(c) for c in calls) == 2
    assert [c["argv"][2] for c in calls if c["argv"][:2] == ["compose", "cp"]] == [
        "server:/paperclip/instances/default/secrets"
    ]


def test_databases_only_leaves_out_env_and_secrets(stack):
    made = stack.backup("--databases-only")
    assert sorted(p.name for p in made.iterdir()) == [
        "COMPLETE",
        "SHA256SUMS",
        "cyber_intel.dump",
        "manifest.txt",
        "paperclip.dump",
    ]
    assert "contents databases-only" in (made / "manifest.txt").read_text().splitlines()
    assert not any(c["argv"][:2] == ["compose", "cp"] for c in stack.calls())


@pytest.mark.parametrize(
    "fail, says",
    [
        ("exec psql", "could not open a transaction in cyber_intel"),
        ("pg_dump.* paperclip ", "pg_dump of paperclip failed"),
        ("pg_restore -l", "cyber_intel.dump does not read back with pg_restore -l"),
        ("compose cp", "could not copy /paperclip/instances/default/secrets"),
    ],
)
def test_a_failure_part_way_leaves_no_complete_marker(stack, fail, says):
    result = stack.run("backup.sh", stack.target, FAKE_DOCKER_FAIL=fail)
    assert result.returncode != 0
    assert says in result.stderr
    assert "FAILED" in result.stderr
    [made] = stack.target.iterdir()
    assert not (made / "COMPLETE").exists()
    assert not (made / "SHA256SUMS").exists()


def test_keep_deletes_only_old_complete_backups(stack):
    oldest = complete_backup(stack.target / "cyberpulse-20250101T000000Z")
    older = complete_backup(stack.target / "cyberpulse-20250102T000000Z")
    unfinished = stack.target / "cyberpulse-20240101T000000Z"
    unfinished.mkdir()
    oddly_named = complete_backup(stack.target / "cyberpulse-manual")
    other = complete_backup(stack.target / "other-20200101T000000Z")
    note = stack.target / "cyberpulse-20200101T000000Z.txt"
    note.write_text("not a backup")

    made = stack.backup("--databases-only", "--keep", "2")

    assert not oldest.exists()
    for kept in (older, made, unfinished, oddly_named, other, note):
        assert kept.exists(), kept


def test_the_default_keep_is_fourteen(stack):
    for day in range(1, 16):
        complete_backup(stack.target / f"cyberpulse-202501{day:02d}T000000Z")
    stack.backup("--databases-only")
    left = sorted(d.name for d in stack.target.iterdir())
    assert len(left) == 14
    assert left[0] == "cyberpulse-20250103T000000Z"


# ─── restore.sh rehearse ─────────────────────────────────────────────────────


def test_rehearse_passes_and_always_removes_its_container(stack):
    made = stack.backup()
    result = stack.run("restore.sh", "rehearse", made)
    assert result.returncode == 0, result.stderr
    assert re.search(r"^cyber_intel +PASS +2 tables, 8 rows", result.stdout, re.MULTILINE)
    assert re.search(r"^paperclip +PASS +1 tables, 2 rows", result.stdout, re.MULTILINE)
    assert "rehearsal PASSED" in result.stdout

    calls = stack.calls()
    assert_safe_docker_use(calls)
    [run] = [c for c in calls if c["argv"][0] == "run"]
    argv = run["argv"]
    name = argv[argv.index("--name") + 1]
    assert re.fullmatch(r"cyberpulse-restore-\d{8}T\d{6}Z", name)
    assert {"-d", "--rm"} <= set(argv)
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[-1] == DB_IMAGE
    assert not {"-p", "--publish", "-P", "--publish-all"} & set(argv)
    # The password reaches docker through its environment only: never argv, never printed.
    assert run["password_env"]
    assert argv[argv.index("-e") + 1] == "POSTGRES_PASSWORD"
    assert not any(re.search(r"[0-9a-f]{48}", joined(c)) for c in calls)
    assert not re.search(r"[0-9a-f]{48}", output(result))

    restores = [c for c in calls if c["argv"][0] == "exec" and "pg_restore" in c["argv"]]
    assert len(restores) == 2 and all(name in c["argv"] for c in restores)
    assert calls[-1]["argv"] == ["rm", "-f", "-v", name]


def test_rehearse_fails_when_row_counts_differ(stack):
    made = stack.backup("--databases-only")
    result = stack.run(
        "restore.sh", "rehearse", made, FAKE_DOCKER_ROWS='{"paperclip": ["3 public.agents"]}'
    )
    assert result.returncode == 1
    assert re.search(r"^cyber_intel +PASS", result.stdout, re.MULTILINE)
    assert re.search(r"^paperclip +FAIL", result.stdout, re.MULTILINE)
    assert "public.agents: 2 rows in the manifest, 3 after restore" in result.stdout
    assert "rehearsal FAILED" in result.stderr
    assert stack.calls()[-1]["argv"][:2] == ["rm", "-f"]


def test_rehearse_fails_on_a_sha256sums_mismatch(stack):
    made = stack.backup("--databases-only")
    with open(made / "paperclip.dump", "ab") as dump:
        dump.write(b"one more byte")
    result = stack.run("restore.sh", "rehearse", made)
    assert result.returncode == 1
    assert "does not match its SHA256SUMS" in result.stderr
    assert "paperclip.dump: FAILED" in result.stderr
    assert stack.calls() == []


def test_rehearse_refuses_a_backup_without_complete(stack):
    made = stack.backup("--databases-only")
    (made / "COMPLETE").unlink()
    result = stack.run("restore.sh", "rehearse", made)
    assert result.returncode == 1
    assert "no COMPLETE marker" in result.stderr
    assert stack.calls() == []


def test_rehearse_fails_when_a_dump_will_not_restore(stack):
    made = stack.backup("--databases-only")
    result = stack.run("restore.sh", "rehearse", made, FAKE_DOCKER_FAIL="^exec -i .* pg_restore")
    assert result.returncode == 1
    assert re.search(r"^cyber_intel +FAIL +pg_restore failed", result.stdout, re.MULTILINE)
    assert stack.calls()[-1]["argv"][:2] == ["rm", "-f"]


# ─── restore.sh fresh ────────────────────────────────────────────────────────


def test_fresh_refuses_without_yes(stack):
    made = stack.backup()
    result = stack.run("restore.sh", "fresh", made)
    assert result.returncode == 1
    assert "add --yes" in result.stderr
    assert stack.calls() == []


def test_fresh_refuses_when_tables_exist(stack):
    made = stack.backup()
    result = stack.run("restore.sh", "fresh", made, "--yes", FAKE_DOCKER_TABLES="7")
    assert result.returncode == 1
    assert "refusing: cyber_intel already has 7 tables" in result.stderr
    texts = [joined(c) for c in stack.calls()]
    assert not any("pg_restore" in t or "CREATE DATABASE" in t or "compose cp" in t for t in texts)


def test_fresh_refuses_while_the_server_runs(stack):
    made = stack.backup()
    result = stack.run("restore.sh", "fresh", made, "--yes", FAKE_DOCKER_SERVER_RUNNING="1")
    assert result.returncode == 1
    assert "docker compose stop server" in result.stderr
    assert not any("pg_restore" in joined(c) for c in stack.calls())


def test_fresh_needs_a_full_backup(stack):
    made = stack.backup("--databases-only")
    result = stack.run("restore.sh", "fresh", "--yes", made)
    assert result.returncode == 1
    assert "has no paperclip-secrets" in result.stderr


def test_fresh_restores_secrets_then_both_databases(stack):
    made = stack.backup()
    result = stack.run(
        "restore.sh",
        "fresh",
        made,
        "--yes",
        FAKE_DOCKER_MISSING="paperclip",
        FAKE_DOCKER_NO_SERVER="1",
    )
    assert result.returncode == 0, result.stderr
    calls = stack.calls()
    assert_safe_docker_use(calls)
    texts = [joined(c) for c in calls]

    assert any(t.startswith("compose create --no-recreate server") for t in texts)
    [copy] = [c for c in calls if c["argv"][:2] == ["compose", "cp"]]
    assert copy["argv"][-1] == "server:/paperclip"
    assert copy["copied"] == [
        "instances/default/secrets/decision-signing.key",
        "instances/default/secrets/master.key",
    ]
    # docker cp leaves the copy owned by root; it goes to the user the container runs as.
    [chown] = [c for c in calls if c["argv"][:2] == ["compose", "run"]]
    assert chown["argv"][-4:] == ["server", "-R", "node", "/paperclip/instances"]
    assert {"--rm", "--no-deps"} <= set(chown["argv"])
    assert chown["argv"][chown["argv"].index("--user") + 1] == "0:0"
    assert chown["argv"][chown["argv"].index("--entrypoint") + 1] == "chown"
    assert texts.index(joined(copy)) < texts.index(joined(chown))
    creates = [t for t in texts if "CREATE DATABASE" in t]
    assert len(creates) == 1 and "CREATE DATABASE paperclip" in creates[0]
    restores = [
        c for c in calls if c["argv"][:2] == ["compose", "exec"] and "pg_restore" in joined(c)
    ]
    assert [c["argv"][-1] for c in restores] == ["cyber_intel", "paperclip"]
    assert all("--single-transaction" in joined(c) for c in restores)
    assert texts.index(joined(copy)) < texts.index(joined(restores[0]))

    assert re.search(r"^cyber_intel +PASS", result.stdout, re.MULTILINE)
    assert re.search(r"^paperclip +PASS", result.stdout, re.MULTILINE)
    assert "docker compose up -d" in result.stdout
    assert "docker compose exec -T worker python -m worker --migrate" in result.stdout
    assert "fake-env-TESTONLY" not in output(result) and FAKE_KEY not in output(result)
    assert not any(t.startswith(("compose up", "compose start", "compose restart")) for t in texts)


@pytest.mark.parametrize("user", ["", "root", "0:0"])
def test_fresh_leaves_ownership_alone_when_the_server_runs_as_root(stack, user):
    made = stack.backup()
    result = stack.run("restore.sh", "fresh", made, "--yes", FAKE_DOCKER_USER=user)
    assert result.returncode == 0, result.stderr
    assert not any(c["argv"][:2] == ["compose", "run"] for c in stack.calls())
