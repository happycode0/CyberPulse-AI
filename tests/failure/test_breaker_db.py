"""§11 "One agent fails" and the circuit breaker, against a real Postgres: TELETRAAN's failed
verdicts, sent through the ops API, trip the breaker in the SQL, and the API then turns work away.

Uses tests/integration/test_watchdog_db.py's `db` fixture, so it skips when DATABASE_URL is
not set, as every integration test does.
"""

import json
from pathlib import Path

from pydantic import SecretStr

from tests.integration.test_watchdog_db import NOW, SOURCES, db, failing  # noqa: F401 - fixture
from worker.db.incidents import BREAKER_LIMIT, load_incident, record_pass
from worker.ops_api import OpsApi

from .conftest import OPS_TOKEN

AUTH = {"Authorization": f"Bearer {OPS_TOKEN}"}


def test_three_failed_verdicts_through_the_api_trip_the_breaker(db):  # noqa: F811 - the fixture
    [incident] = record_pass(db, [failing()], SOURCES, NOW).opened
    api = OpsApi(
        engine=db, token=SecretStr(OPS_TOKEN), data_dir=Path("."), key_status=lambda: None,
        monthly_budget=20, clock=lambda: NOW,
    )  # fmt: skip
    body = json.dumps({"verdict": "fail", "pr": 42, "reasons": "The new fixture fails."}).encode()
    path = f"/ops/incidents/{incident.id}/verdict"
    answers = [api.handle("POST", path, AUTH, body) for _ in range(BREAKER_LIMIT + 1)]
    assert [a.status for a in answers] == [200] * BREAKER_LIMIT + [409]
    assert answers[BREAKER_LIMIT - 1].json()["tripped"] is True
    assert answers[BREAKER_LIMIT].json()["result"] == "halted"
    with db.connect() as conn:
        stored = load_incident(conn, incident.id)
    assert stored.needs_human and stored.fix_failures == BREAKER_LIMIT
