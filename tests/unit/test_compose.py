"""docker-compose.yml since Paperclip was retired (2026-10-31): the server is kept, for the
rollback, under a profile `docker compose up -d` leaves off."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_the_server_is_under_the_paperclip_profile_and_the_rest_is_not():
    services = COMPOSE["services"]
    assert services["server"]["profiles"] == ["paperclip"]
    assert "profiles" not in services["worker"] and "profiles" not in services["db"]


def test_the_worker_does_not_need_the_server():
    assert set(COMPOSE["services"]["worker"].get("depends_on", {})) == {"db"}


def test_the_backup_and_restore_scripts_still_reach_the_stopped_server():
    """Compose ignores a service whose profile is off, and both scripts copy Paperclip's secrets
    to or from its container."""
    for script in ("backup.sh", "restore.sh"):
        assert "export COMPOSE_PROFILES=paperclip" in (ROOT / "ops" / script).read_text(), script
    assert "COMPOSE_PROFILES" not in (ROOT / "ops" / "rollback.sh").read_text()
