"""cost_ledger rows: one per billed call, written by worker/ai/client.py (migration 005)."""

from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from typing import Literal

from sqlalchemy import Connection, Engine, text

Outcome = Literal["ok", "invalid_output"]


@dataclass(frozen=True)
class LedgerEntry:
    provider: str
    stage: str
    outcome: Outcome
    agent: str | None = None
    event_id: str | None = None
    run_id: str | None = None
    tier: str | None = None
    requested_model: str | None = None
    model: str | None = None
    upstream: str | None = None
    generation_id: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    # None when the provider did not say what it charged. Never defaulted to zero (see 005).
    cost_usd: Decimal | None = None
    duration_ms: int | None = None


_COLUMNS = tuple(f.name for f in fields(LedgerEntry))


def record(conn: Connection, entry: LedgerEntry) -> None:
    conn.execute(
        text(
            f"insert into cost_ledger ({', '.join(_COLUMNS)}) "
            f"values ({', '.join(':' + c for c in _COLUMNS)})"
        ),
        asdict(entry),
    )


def record_to(engine: Engine, entry: LedgerEntry) -> None:
    """`record` in its own transaction: the sink the AI client is given in production."""
    with engine.begin() as conn:
        record(conn, entry)
