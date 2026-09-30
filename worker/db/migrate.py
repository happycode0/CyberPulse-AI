"""Forward-only SQL migration runner. Usage: `python -m worker.db.migrate`."""

import re
from pathlib import Path

from sqlalchemy import Engine, inspect, text

from worker.db.session import get_engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LEDGER = "schema_migrations"
# Arbitrary constant: serialises concurrent runners on one database.
_LOCK_KEY = 7_262_026_001

_LEDGER_DDL = f"""
create table if not exists {LEDGER} (
    filename   text primary key,
    applied_at timestamptz not null default now()
)
"""


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def run_migrations(engine: Engine) -> list[str]:
    """Apply pending migrations in filename order; return the filenames applied.

    Each file runs in its own transaction together with its ledger row, so a
    failing migration leaves nothing behind and is retried on the next run.
    """
    applied: list[str] = []
    for path in _migration_files():
        with engine.begin() as conn:
            conn.execute(text("select pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
            conn.exec_driver_sql(_LEDGER_DDL)
            done = conn.execute(
                text(f"select 1 from {LEDGER} where filename = :f"), {"f": path.name}
            ).first()
            if done:
                continue
            conn.execution_options(no_parameters=True).exec_driver_sql(
                path.read_text(encoding="utf-8")
            )
            conn.execute(
                text(f"insert into {LEDGER} (filename) values (:f)"), {"f": path.name}
            )
            applied.append(path.name)
    return applied


def current_version(engine: Engine) -> int:
    """Highest numeric filename prefix recorded in the ledger; 0 if none."""
    if not inspect(engine).has_table(LEDGER):
        return 0
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(text(f"select filename from {LEDGER}"))]
    versions = [int(m.group(1)) for n in names if (m := re.match(r"(\d+)_", n))]
    return max(versions, default=0)


def main() -> None:
    engine = get_engine()
    applied = run_migrations(engine)
    print(f"applied: {', '.join(applied) if applied else 'none'}")
    print(f"schema version: {current_version(engine)}")


if __name__ == "__main__":
    main()
