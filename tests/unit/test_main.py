import pytest

from worker import main as main_mod
from worker.ai.ladder import (
    Breach,
    CatalogueUnavailable,
    Ladder,
    LadderRejected,
    Tier,
    VerifiedLadder,
)
from worker.models import Lane


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


# ─── _check_models itself: a breach fails the command but never the scheduler ──────────────────────


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
