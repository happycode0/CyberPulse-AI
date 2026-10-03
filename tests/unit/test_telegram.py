"""worker/notify/telegram.py: what is sent, what is refused, and that the bot token is never in a
log line or an error."""

import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from worker.notify import telegram
from worker.notify.telegram import MAX_CHARS, Telegram, fit, redact
from worker.settings import Settings

# Built at run time, so no file in the repository holds anything shaped like a bot token.
TOKEN = "1000001" + ":" + "fake-bot-token-TESTONLY-0123456789"
CHAT = "-1001234"


def channel(handler) -> Telegram:
    return Telegram(SecretStr(TOKEN), CHAT, user_agent="CyberPulse-AI/1.0",
                    transport=httpx.MockTransport(handler))


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


async def test_a_message_is_sent_as_plain_text_to_the_chat():
    seen = []

    def handler(request):
        seen.append(request)
        return ok(request)

    result = await channel(handler).send("hello")
    assert result.sent and result.error is None
    [request] = seen
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(request.content)
    assert body == {"chat_id": CHAT, "text": "hello", "link_preview_options": {"is_disabled": True}}
    assert "parse_mode" not in body
    assert request.headers["User-Agent"] == "CyberPulse-AI/1.0"


async def test_the_token_never_reaches_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    await channel(ok).send("hello")
    assert "HTTP Request: POST https://api.telegram.org/bot<redacted>/sendMessage" in caplog.text
    assert TOKEN not in caplog.text and TOKEN.split(":")[1] not in caplog.text


async def test_a_refusal_is_described_by_its_status_and_telegram_s_words():
    def handler(request):
        return httpx.Response(
            400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        )

    result = await channel(handler).send("hello")
    assert not result.sent and not result.withheld
    assert result.error == "HTTP 400: Bad Request: chat not found"


async def test_a_200_that_is_not_ok_is_a_failure():
    result = await channel(lambda r: httpx.Response(200, text="<html>")).send("hello")
    assert (result.sent, result.error) == (False, "HTTP 200")


async def test_a_network_failure_is_named_by_its_type_alone(caplog):
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    result = await channel(handler).send("hello")
    assert (result.sent, result.error) == (False, "ConnectError")
    assert TOKEN not in caplog.text


async def test_a_description_echoing_a_token_is_redacted():
    def handler(request):
        return httpx.Response(401, json={"ok": False, "description": f"bad token {TOKEN}"})

    result = await channel(handler).send("hello")
    assert TOKEN not in result.error and "<redacted>" in result.error


async def test_a_message_carrying_a_secret_is_withheld_and_never_sent(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    seen = []
    result = await channel(lambda r: seen.append(r) or ok(r)).send(f"oops {TOKEN}")
    assert result.withheld and not result.sent and seen == []
    assert "withheld" in caplog.text and TOKEN not in caplog.text


def test_redact_handles_the_url_and_the_bare_token():
    assert redact(f"https://api.telegram.org/bot{TOKEN}/getMe") == (
        "https://api.telegram.org/bot<redacted>/getMe"
    )
    assert redact(f"token={TOKEN}") == "token=<redacted>"
    assert redact("CVE-2026-1234 at 10:30") == "CVE-2026-1234 at 10:30"


def test_the_redaction_filter_is_installed_once():
    telegram.install_log_redaction()
    telegram.install_log_redaction()
    filters = logging.getLogger("httpx").filters
    assert sum(isinstance(f, telegram._RedactBotTokens) for f in filters) == 1


def test_a_long_message_is_cut_at_a_line_to_fit():
    message = "\n".join(f"line {i:04d} " + "x" * 50 for i in range(200))
    cut = fit(message)
    assert len(cut) <= MAX_CHARS and cut.endswith("the site has the rest)")
    assert cut.split("\n")[-2].startswith("line ")  # whole lines only
    assert fit("short") == "short"


@pytest.mark.parametrize(
    "token, chat, configured",
    [
        (None, None, False),
        (TOKEN, None, False),
        (TOKEN, "  ", False),
        ("", CHAT, False),
        (TOKEN, CHAT, True),
    ],
)
def test_the_channel_exists_only_with_both_token_and_chat(token, chat, configured):
    settings = Settings(
        _env_file=None, database_url="postgresql://x", telegram_bot_token=token,
        telegram_chat_id=chat,
    )
    assert (Telegram.from_settings(settings) is not None) is configured


def test_the_channel_s_repr_holds_no_token():
    assert TOKEN not in repr(channel(ok)) and CHAT not in repr(channel(ok))
