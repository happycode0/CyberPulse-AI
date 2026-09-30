"""Schema gate and secret scan for the publisher (Review Focus #5)."""

import copy
import json
import os
from datetime import UTC, datetime

import pytest

import worker.publish.validate as validate_module
from worker.models import Event
from worker.settings import Settings
from worker.publish.validate import ValidationFailure, scan_for_secrets, validate_payload

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _event(**overrides) -> dict:
    fields = {
        "event_id": "evt-2026-000001",
        "title": "Example vulnerability is being actively exploited",
        "summary": "CVE-2026-88772 exploited; patch available.",
        "first_seen": NOW,
        "last_seen": NOW,
    }
    fields.update(overrides)
    return Event(**fields).model_dump_public()


def _live(events=None) -> dict:
    events = [_event()] if events is None else events
    return {
        "generated_at": NOW.isoformat(),
        "last_completed_collection": NOW.isoformat(),
        "pipeline_version": "1.0.0",
        "counts": {"events": len(events)},
        "events": events,
    }


def payload_with(**event_overrides) -> dict:
    payload = _live()
    payload["events"][0].update(event_overrides)
    return payload


def test_valid_payload_passes():
    validate_payload(_live(), "live")


def test_missing_required_field_raises():
    payload = {k: v for k, v in _live().items() if k != "last_completed_collection"}
    with pytest.raises(ValidationFailure, match="last_completed_collection"):
        validate_payload(payload, "live")


def test_bad_enum_value_raises():
    with pytest.raises(ValidationFailure):
        validate_payload(payload_with(severity="spicy"), "live")


def test_unknown_event_field_raises():
    """A raw article body (or anything else unspecified) cannot ride along on an event."""
    with pytest.raises(ValidationFailure, match="raw_body"):
        validate_payload(payload_with(raw_body="<html>full article</html>"), "live")


def test_error_names_the_offending_path():
    with pytest.raises(ValidationFailure, match=r"events\.0\.severity|events/0/severity|\[0\]"):
        validate_payload(payload_with(severity="spicy"), "live")


def test_unknown_schema_name_raises():
    with pytest.raises(ValidationFailure, match="nope"):
        validate_payload(_live(), "nope")


def test_validation_error_does_not_echo_a_secret_value():
    secret = "sk-or-v1-0123456789abcdef0123456789abcdef"
    with pytest.raises(ValidationFailure) as exc:
        validate_payload(payload_with(severity=secret), "live")
    assert secret not in str(exc.value)


SECRETS = [
    "sk-or-v1-0123456789abcdef0123456789abcdef",  # OpenRouter
    "tvly-abcdef0123456789abcdef0123",  # Tavily
    "ghp_0123456789abcdefghij0123456789abcdef",  # GitHub classic
    "github_pat_11ABCDEFG0123456789_abcdefghij",  # GitHub fine-grained
    "postgresql://user:hunter2@db:5432/cyber_intel",
    "123456789:AAEhBOweik6ad9r_QXMENQjcrTu-oGXwLNo",  # Telegram
    "AKIAIOSFODNN7EXAMPLE",  # AWS access key id
    "-----BEGIN RSA PRIVATE KEY-----",
    "Zm9vYmFyQmF6MDEyMzQ1Njc4OWFiY2RlZkdISUpLTE1O",  # high-entropy mixed-case token
]


@pytest.mark.parametrize("secret", SECRETS)
def test_scan_detects_credential_shapes(secret):
    assert scan_for_secrets({"events": [{"summary": f"leaked {secret}"}]})


@pytest.mark.parametrize("secret", SECRETS)
def test_scan_finds_a_secret_anywhere_in_the_payload(secret):
    """Values at any depth, list items, bare top-level strings and dict keys all count."""
    for payload in (
        {"a": {"b": [{"c": [secret]}]}},
        {"events": [{"sources": [{"url": f"https://x.test/?k={secret}"}]}]},
        {"secret-in-key": 1, secret: "harmless"},
        {"note": {"deeply": {"nested": {"list": ["fine", ["fine", secret]]}}}},
    ):
        assert scan_for_secrets(payload), payload


@pytest.mark.parametrize("secret", SECRETS)
def test_findings_never_contain_the_secret_itself(secret):
    findings = scan_for_secrets({secret: secret, "list": [secret]})
    assert findings
    assert all(secret not in f for f in findings)


def test_scan_detects_env_values_present_in_output(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "9999:VERYSECRETVALUE")
    assert scan_for_secrets({"note": "9999:VERYSECRETVALUE"})


def test_scan_detects_an_unanticipated_env_secret(monkeypatch):
    """No regex knows this shape; the literal-value check is what catches it."""
    monkeypatch.setenv("SOME_NEW_VENDOR_API_KEY", "plainwordsecret")
    findings = scan_for_secrets({"a": ["prefix plainwordsecret suffix"]})
    assert findings
    assert all("plainwordsecret" not in f for f in findings)


@pytest.mark.parametrize(
    "name",
    ["OPENROUTER_API_KEY", "TAVILY_API_KEY", "NVD_API_KEY", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN"],
)
def test_scan_detects_every_settings_secret(monkeypatch, name):
    monkeypatch.setenv(name, "opaque value 8842")
    assert scan_for_secrets({"x": "see opaque value 8842 here"})


def test_scan_detects_database_password_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://cyber:p4ssw0rdOnly@db:5432/cyber_intel")
    assert scan_for_secrets({"x": "the password is p4ssw0rdOnly"})


def test_scan_detects_url_encoded_secret(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "abc def/ghi+jkl==")
    assert scan_for_secrets({"x": "https://x.test/?k=abc%20def%2Fghi%2Bjkl%3D%3D"})


def test_scan_ignores_blank_and_trivial_env_values(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "x")
    assert scan_for_secrets({"summary": "x marks the spot"}) == []


def test_scan_is_clean_on_legitimate_content():
    assert (
        scan_for_secrets({"events": [{"summary": "CVE-2026-88772 exploited; patch available."}]})
        == []
    )


def test_scan_is_clean_on_a_full_valid_payload():
    assert scan_for_secrets(_live()) == []


def test_scan_does_not_flag_hashes_or_slugs():
    payload = {
        "iocs": [
            "d41d8cd98f00b204e9800998ecf8427e",  # md5
            "da39a3ee5e6b4b0d3255bfef95601890afd80709",  # sha1
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # sha256
        ],
        "url": "https://example.org/news/critical-vulnerability-in-acme-vpn-exploited-in-the-wild",
        "https": "https://user@example.org/path",
        "slug": "https://www.cisa.gov/news-events/alerts/2026/09/29/CISA-Adds-Two-Known-"
        "Exploited-Vulnerabilities-to-Catalog-2026",
        "snake": "Exploited_Vulnerabilities_Catalog_Update_Notice_2026_Advisory",
        "query": "https://example.org/a?utm_source=newsletter&utm_campaign=weekly-roundup",
    }
    assert scan_for_secrets(payload) == []


ADVISORY_SHAPED = [
    # Reviewer's four examples, verbatim in shape.
    "https://blog.example/Log4Shell-Exploitation-Detection-and-Mitigation-Guidance",
    "Windows11-Update-KB5031354-Cumulative-Security-Patch-Notes",
    ".../Microsoft-Exchange2019-Server-Zero-Day-Exploited-In-Attacks/",
    "PowerShellInvokeExpressionObfuscation2026Campaign",
    # Same family: mixed-alphanumeric words, acronyms, CamelCase, snake_case, prose.
    "https://x.test/2026/Citrix-NetScaler-ADC-CVE-2023-4966-Session-Token-Leakage-Advisory",
    "https://x.test/PSExec-and-IEXObfuscation-in-RedTeamToolkits2026-Report",
    "Apache_Struts2_Remote_Code_Execution_CVE_2017_5638_Post_Incident_Review",
    "MicrosoftOutlookElevationOfPrivilegeVulnerabilityCVE202323397Analysis",
    "Barracuda-ESG-0day-CVE-2023-2868-UNC4841-Espionage-Campaign-Analysis",
    "Ivanti-EPMM-CVE-2023-35078-Exploited-Against-Norwegian-Government-2023",
]


@pytest.mark.parametrize("text", ADVISORY_SHAPED)
def test_scan_is_clean_on_advisory_shaped_titles_and_urls(text):
    assert scan_for_secrets({"title": text, "url": f"https://x.test/{text}"}) == []
    assert scan_for_secrets({"events": [{"sources": [{"url": text}]}]}) == []


RANDOM_TOKENS = [
    "Xk9Lm2Qp7Rt4Vw8Yz1Bn5Cd3Fg6Hj0Ks",
    "q83JxK2mZp0RvT7yLc9WbN4sHd1GfA6eUo",
    "aB3xQ9-kL2mZ7-pR4vT8-yH1cW5-nD6sG0",
    "a1B2-c3D4-e5F6-g7H8-i9J0-k1L2-m3N4",
    "Zm9vYmFyQmF6MDEyMzQ1Njc4OWFiY2RlZkdISUpLTE1O",
    "8fA3kD9xQ2mLp7ZrT5vB1nYc6HjW0sGe4UoIw",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY".replace("/", "_"),
]


@pytest.mark.parametrize("token", RANDOM_TOKENS)
def test_scan_still_flags_random_tokens_with_no_word_structure(token):
    assert scan_for_secrets({"note": token})
    assert scan_for_secrets({"note": f"see https://x.test/{token}/details"})


def test_scan_does_not_hide_a_random_token_inside_advisory_text():
    token = "Xk9Lm2Qp7Rt4Vw8Yz1Bn5Cd3Fg6Hj0Ks"
    assert scan_for_secrets({"title": f"Log4Shell-Exploitation-Guidance {token}"})


def test_env_scan_survives_settings_failing_to_validate(tmp_path, monkeypatch):
    """`Settings()` needs variables the .env may not have; the scan must not depend on it."""
    (tmp_path / ".env").write_text(
        "TAVILY_API_KEY=tvlyLooksNothingLikeAKey\nGITHUB_REPOSITORY=happycode0/CyberPulse-AI\n"
        "# NVD_API_KEY=commentedOutValue123\n"
    )
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.upper() in {"DATABASE_URL", "TAVILY_API_KEY"}:
            monkeypatch.delenv(name)
    with pytest.raises(Exception):
        Settings()  # the precondition: full validation really does fail here
    findings = scan_for_secrets({"note": "x tvlyLooksNothingLikeAKey y"})
    assert findings and all("tvlyLooksNothingLikeAKey" not in f for f in findings)
    assert "TAVILY_API_KEY" in findings[0]
    assert scan_for_secrets({"note": "happycode0/CyberPulse-AI"}) == []
    assert scan_for_secrets({"note": "commentedOutValue123"}) == []


def test_env_scan_does_not_depend_on_constructing_settings(tmp_path, monkeypatch):
    class Boom:
        model_fields = Settings.model_fields
        model_config = Settings.model_config

        def __new__(cls, *a, **k):
            raise RuntimeError("Settings() must not be needed")

    monkeypatch.setattr(validate_module, "Settings", Boom)
    monkeypatch.setenv("OPENROUTER_API_KEY", "some-opaque-value-1")
    assert scan_for_secrets({"x": "some-opaque-value-1"})


def test_unreadable_env_file_fails_closed(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TAVILY_API_KEY=whatever-value-1\n")
    monkeypatch.chdir(tmp_path)

    def denied(*a, **k):
        raise PermissionError("nope")

    monkeypatch.setattr(validate_module, "dotenv_values", denied)
    with pytest.raises(ValidationFailure, match="\\.env"):
        scan_for_secrets({"x": 1})


def test_missing_env_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert scan_for_secrets({"x": "hello"}) == []


def test_scan_survives_non_string_values():
    assert scan_for_secrets({"a": 1, "b": 2.5, "c": None, "d": True, "e": [1, None]}) == []


def test_good_payload_is_json_serialisable():
    json.dumps(copy.deepcopy(_live()))
