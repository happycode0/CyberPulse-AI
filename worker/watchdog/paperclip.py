"""Paperclip, from the watchdog's side: the Incident routine's webhook, and whether the server
answers at all.

The routine is the owner's (docs/wiki/stage-6-self-healing.md). Its webhook trigger signs with
a bearer secret that Paperclip shows once, and the owner puts that and the trigger's URL in the
worker's environment. A fire opens TELETRAAN's issue, or joins the one already open
(`coalesce_if_active`). What it carries is an incident's id, kind, subject, severity and our
own title: TELETRAAN reads the rest from GET /ops/incidents, so nothing of the evidence goes
through Paperclip.

The secret goes in the Authorization header only, and the trigger's public id is redacted from
httpx's request lines, as the Telegram bot token is. `raise_for_status()` is never called: its
exception carries the URL.

The health probe sends no credentials. Any HTTP answer below 500 means the server is up: a
403 for a hostname it does not allow is still Paperclip answering.
"""

import logging
import re
from typing import Any

import httpx
from pydantic import SecretStr

from worker.notify.telegram import SendResult
from worker.publish.validate import scan_text_for_secrets
from worker.settings import Settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
PROBE_TIMEOUT_SECONDS = 10.0

_PUBLIC_ID = re.compile(r"(/routine-triggers/public/)[^/\s\"'<>?]+")


def redact(text: str) -> str:
    return _PUBLIC_ID.sub(r"\1<redacted>", text)


class _RedactTriggerIds(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def install_log_redaction() -> None:
    """Redact trigger ids from httpx's request lines. Safe to call more than once."""
    target = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactTriggerIds) for f in target.filters):
        target.addFilter(_RedactTriggerIds())


class IncidentRoutine:
    """Fires the Incident routine's webhook trigger."""

    def __init__(
        self,
        url: str,
        secret: SecretStr,
        *,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._secret = secret
        self._user_agent = user_agent
        self._transport = transport
        install_log_redaction()

    def __repr__(self) -> str:
        return "IncidentRoutine()"

    @classmethod
    def from_settings(cls, settings: Settings) -> "IncidentRoutine | None":
        """The routine, or None when its URL or secret is not set."""
        url = (settings.paperclip_incident_webhook_url or "").strip()
        secret = settings.paperclip_incident_webhook_secret
        if not url or secret is None or not secret.get_secret_value().strip():
            return None
        return cls(
            url, SecretStr(secret.get_secret_value().strip()), user_agent=settings.user_agent
        )

    async def fire(self, payload: dict[str, Any]) -> SendResult:
        findings = scan_text_for_secrets(repr(payload))
        if findings:
            logger.error("incident fire withheld by the secret scan; %s", "; ".join(findings))
            return SendResult(False, withheld=True, error="withheld by the secret scan")
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS,
                transport=self._transport,
                headers={"User-Agent": self._user_agent},
            ) as http:
                response = await http.post(
                    self._url,
                    json={"payload": payload},
                    headers={"Authorization": f"Bearer {self._secret.get_secret_value()}"},
                )
        except Exception as exc:  # noqa: BLE001 - its message may carry the URL; its type cannot
            return SendResult(False, error=type(exc).__name__)
        if 200 <= response.status_code < 300:
            return SendResult(True)
        return SendResult(False, error=f"HTTP {response.status_code}")


async def probe(
    url: str, *, user_agent: str, transport: httpx.AsyncBaseTransport | None = None
) -> str | None:
    """None when Paperclip answers; otherwise what went wrong, as a status or a type name."""
    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_SECONDS,
            transport=transport,
            headers={"User-Agent": user_agent},
        ) as http:
            response = await http.get(url)
    except Exception as exc:  # noqa: BLE001 - a probe that cannot connect is the answer
        return type(exc).__name__
    return None if response.status_code < 500 else f"HTTP {response.status_code}"
