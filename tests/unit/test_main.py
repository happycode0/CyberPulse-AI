import httpx
import pytest
from pydantic import SecretStr

from worker import main as main_mod
from worker.ai.budget import KEY_URL
from worker.ai.ladder import (
    Breach,
    CatalogueUnavailable,
    Ladder,
    LadderRejected,
    Tier,
    VerifiedLadder,
)
from worker.models import Lane
from worker.settings import Settings


@pytest.fixture
def calls(monkeypatch):
    log = []

    class Summary:
        run_id = "r1"

        def model_dump_json(self):
            return "{}"

    async def fake_run_lane(lane, *, once=False):
        log.append(("run", lane, once))
        return Summary()

    async def fake_serve(lanes=None):
        log.append(("serve", lanes))

    monkeypatch.setattr(main_mod, "run_lane", fake_run_lane)
    monkeypatch.setattr(main_mod, "serve", fake_serve)
    monkeypatch.setattr(main_mod, "get_engine", lambda: "engine")
    monkeypatch.setattr(main_mod, "run_migrations", lambda e: log.append(("migrate", e)) or [])
    monkeypatch.setattr(main_mod, "_publish", lambda: log.append(("publish",)) or 0)
    monkeypatch.setattr(
        main_mod, "_check_models", lambda *, required: log.append(("check", required)) or 0
    )
    monkeypatch.setattr(main_mod, "_check_budget", lambda: log.append(("budget",)) or 0)
    return log


def test_no_arguments_checks_the_models_then_runs_the_scheduler(calls):
    assert main_mod.main([]) == 0
    assert calls == [("check", False), ("serve", None)]


def test_lane_once_runs_that_lane_a_single_time_and_exits(calls):
    assert main_mod.main(["--lane", "fast", "--once"]) == 0
    assert calls == [("run", Lane.FAST, True)]


def test_lane_without_once_schedules_only_that_lane(calls):
    main_mod.main(["--lane", "normal"])
    assert calls == [("check", False), ("serve", (Lane.NORMAL,))]


def test_deep_lane_needs_once(calls):
    with pytest.raises(SystemExit) as e:
        main_mod.main(["--lane", "deep"])
    assert e.value.code == 2 and calls == []
    main_mod.main(["--lane", "deep", "--once"])
    assert calls == [("run", Lane.DEEP, True)]


def test_once_without_lane_is_a_usage_error(calls):
    with pytest.raises(SystemExit) as e:
        main_mod.main(["--once"])
    assert e.value.code == 2


def test_migrate_then_run_then_publish_in_that_order_and_no_scheduler(calls):
    assert main_mod.main(["--publish", "--lane", "fast", "--once", "--migrate"]) == 0
    assert [c[0] for c in calls] == ["migrate", "run", "publish"]


def test_migrate_alone_exits_without_starting_the_scheduler(calls):
    main_mod.main(["--migrate"])
    assert calls == [("migrate", "engine")]


def test_enrich_runs_after_ground_truth_and_before_publish(calls, monkeypatch):
    # A severity judgment is asked for only where the registers still have no official score,
    # and the publish that follows carries what both wrote.
    monkeypatch.setattr(main_mod, "_groundtruth", lambda n: calls.append(("groundtruth", n)) or 0)
    monkeypatch.setattr(main_mod, "_enrich", lambda n: calls.append(("enrich", n)) or 0)
    argv = ["--publish", "--enrich", "--enrich-batch", "4", "--groundtruth", "--cvss-batch", "7"]
    assert main_mod.main(argv) == 0
    assert calls == [("groundtruth", 7), ("enrich", 4), ("publish",)]


def test_enrich_alone_exits_without_starting_the_scheduler(calls, monkeypatch):
    monkeypatch.setattr(main_mod, "_enrich", lambda n: calls.append(("enrich", n)) or 0)
    assert main_mod.main(["--enrich"]) == 0
    assert calls == [("enrich", main_mod.DEFAULT_BATCH)]


def test_failed_publish_returns_a_failure_code(calls, monkeypatch):
    monkeypatch.setattr(main_mod, "_publish", lambda: 1)
    assert main_mod.main(["--publish"]) == 1


def test_unknown_lane_is_rejected(calls):
    with pytest.raises(SystemExit):
        main_mod.main(["--lane", "slow"])


def test_check_models_alone_exits_without_starting_the_scheduler(calls):
    assert main_mod.main(["--check-models"]) == 0
    assert calls == [("check", True)]


def test_check_models_runs_first_and_a_failure_stops_the_rest(calls, monkeypatch):
    monkeypatch.setattr(
        main_mod, "_check_models", lambda *, required: calls.append(("check", required)) or 1
    )
    assert main_mod.main(["--check-models", "--migrate", "--publish"]) == 1
    assert calls == [("check", True)]


# ─── _check_models itself: a breach fails the command but never the scheduler ─────────────────────


def _rejecting(exc):
    async def fake_verify():
        raise exc

    return fake_verify


def _rejection():
    return LadderRejected([Breach(Tier.CHEAP, "vendor/pricey", "over the ceiling")])


@pytest.mark.parametrize("required, expected", [(True, 1), (False, 0)])
def test_a_rejected_ladder_fails_only_when_required(monkeypatch, caplog, required, expected):
    monkeypatch.setattr(main_mod, "verify_ladder", _rejecting(_rejection()))
    assert main_mod._check_models(required=required) == expected
    assert "vendor/pricey" in caplog.text and "AI layer stays off" in caplog.text


@pytest.mark.parametrize("required, expected", [(True, 1), (False, 0)])
def test_an_unreadable_catalogue_is_treated_like_a_rejection(
    monkeypatch, caplog, required, expected
):
    monkeypatch.setattr(main_mod, "verify_ladder", _rejecting(CatalogueUnavailable("HTTP 503")))
    assert main_mod._check_models(required=required) == expected
    assert "HTTP 503" in caplog.text


def test_a_passing_ladder_is_logged(monkeypatch, caplog):
    async def fake_verify():
        return VerifiedLadder(ladder=Ladder.load(), checked_at=None)

    monkeypatch.setattr(main_mod, "verify_ladder", fake_verify)
    caplog.set_level("INFO")
    assert main_mod._check_models(required=True) == 0
    assert "model ladder passed" in caplog.text


def test_check_budget_runs_after_check_models_and_exits(calls):
    assert main_mod.main(["--check-budget", "--check-models"]) == 0
    assert calls == [("check", True), ("budget",)]


def test_a_failed_budget_check_stops_the_rest(calls, monkeypatch):
    monkeypatch.setattr(main_mod, "_check_budget", lambda: calls.append(("budget",)) or 1)
    assert main_mod.main(["--check-budget", "--migrate"]) == 1
    assert calls == [("budget",)]


# ─── _check_budget itself: a read of the key, never a spend ───────────────────────────────────────


def _with_key(monkeypatch, key="fake-key-TESTONLY"):
    settings = Settings(
        _env_file=None,
        database_url="sqlite://",
        openrouter_api_key=SecretStr(key) if key else None,
    )
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)


def test_check_budget_without_a_key_fails(monkeypatch, caplog):
    _with_key(monkeypatch, key=None)
    assert main_mod._check_budget() == 1
    assert "OPENROUTER_API_KEY is not set" in caplog.text


def test_check_budget_logs_the_mode_and_never_the_label(monkeypatch, caplog, respx_mock):
    _with_key(monkeypatch)
    body = {
        "data": {
            "label": "fake-label-TESTONLY...890",
            "limit": 20,
            "limit_remaining": 14,
            "limit_reset": "monthly",
            "usage_daily": 0.5,
            "usage_monthly": 6,
            "free_model_daily_requests": {"used": 0, "limit": 1000, "remaining": 1000},
        }
    }
    respx_mock.get(KEY_URL).mock(return_value=httpx.Response(200, json=body))
    caplog.set_level("INFO")
    assert main_mod._check_budget() == 0
    assert "mode full" in caplog.text and "$14.00 of $20.00" in caplog.text
    assert "fake-label" not in caplog.text


def test_check_budget_fails_when_the_key_cannot_be_read(monkeypatch, caplog, respx_mock):
    _with_key(monkeypatch)
    respx_mock.get(KEY_URL).mock(return_value=httpx.Response(401))
    assert main_mod._check_budget() == 1
    assert "did not accept the key" in caplog.text
