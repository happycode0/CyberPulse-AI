"""Telegram, the first notification channel (PLAN.md section 2, decision 6).

The bot token is part of every request's URL (`https://api.telegram.org/bot<token>/sendMessage`),
so that URL must never reach a log, an exception message or the database:

- httpx logs every request's URL at INFO. `install_log_redaction` puts a filter on its logger
  that rewrites the token before the line is written.
- `raise_for_status()` is never called, because its exception carries the URL. A failed send is
  described by its status code and Telegram's own description (redacted the same way), or by an
  exception's type name alone.

Every message passes the publisher's secret scan before it is sent, and is withheld if anything
is found, as a publish would be.
"""

import json
import logging
import re
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

from worker.publish.validate import scan_text_for_secrets
from worker.settings import Settings

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
# Telegram refuses a message over 4096 characters; the margin is for characters it counts twice.
MAX_CHARS = 4000
TIMEOUT_SECONDS = 20.0
DESCRIPTION_MAX_CHARS = 120
_CUT = "\n… (cut to fit Telegram; the site has the rest)"

_TOKEN_PATTERNS = (
    re.compile(r"(api\.telegram\.org/bot)[^/\s\"'<>]+"),
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_\-]{30,}"),
)


def redact(text: str) -> str:
    """`text` with anything shaped like a bot token, or in a bot URL's token place, replaced."""
    text = _TOKEN_PATTERNS[0].sub(r"\1<redacted>", text)
    return _TOKEN_PATTERNS[1].sub("<redacted>", text)


class _RedactBotTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def install_log_redaction() -> None:
    """Redact bot tokens from httpx's request lines. Safe to call more than once."""
    target = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactBotTokens) for f in target.filters):
        target.addFilter(_RedactBotTokens())


@dataclass(frozen=True)
class SendResult:
    sent: bool
    withheld: bool = False
    # A status code and Telegram's description, or an exception's type name. Never a URL.
    error: str | None = None


def fit(message: str) -> str:
    """`message`, cut at a line break to fit in one Telegram message if it is too long."""
    if len(message) <= MAX_CHARS:
        return message
    room = MAX_CHARS - len(_CUT)
    cut = message.rfind("\n", 0, room)
    return message[: cut if cut > 0 else room].rstrip() + _CUT


class Telegram:
    """Sends plain-text messages to one chat. With no parse mode, nothing in an event's title
    can be read as formatting."""

    def __init__(
        self,
        token: SecretStr,
        chat_id: str,
        *,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._user_agent = user_agent
        self._transport = transport
        install_log_redaction()

    def __repr__(self) -> str:
        return "Telegram()"

    @classmethod
    def from_settings(cls, settings: Settings) -> "Telegram | None":
        """The channel, or None when the bot token or the chat id is not set."""
        token = settings.telegram_bot_token
        chat_id = (settings.telegram_chat_id or "").strip()
        if token is None or not token.get_secret_value().strip() or not chat_id:
            return None
        return cls(SecretStr(token.get_secret_value().strip()), chat_id,
                   user_agent=settings.user_agent)

    async def send(self, message: str) -> SendResult:
        message = fit(message)
        findings = scan_text_for_secrets(message)
        if findings:
            # Findings name what matched, never the value, so they are safe to log.
            logger.error("telegram message withheld: it failed the secret scan; %s",
                         "; ".join(findings))
            return SendResult(False, withheld=True, error="withheld by the secret scan")
        url = f"{API_URL}/bot{self._token.get_secret_value()}/sendMessage"
        body = {"chat_id": self._chat_id, "text": message, "link_preview_options":
                {"is_disabled": True}}
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS,
                transport=self._transport,
                headers={"User-Agent": self._user_agent},
            ) as http:
                response = await http.post(url, json=body)
        except Exception as exc:  # noqa: BLE001 - its message may carry the URL; its type cannot
            return SendResult(False, error=type(exc).__name__)
        if response.status_code == 200 and _ok(response):
            return SendResult(True)
        return SendResult(False, error=_describe(response))


def _ok(response: httpx.Response) -> bool:
    try:
        return json.loads(response.content).get("ok") is True
    except (ValueError, AttributeError):
        return False


def _describe(response: httpx.Response) -> str:
    error = f"HTTP {response.status_code}"
    try:
        description = json.loads(response.content).get("description")
    except (ValueError, AttributeError):
        description = None
    if isinstance(description, str) and description:
        error += f": {redact(description)[:DESCRIPTION_MAX_CHARS]}"
    return error
