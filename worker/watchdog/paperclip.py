"""Paperclip, from the worker's side: the crew's webhook routines, and whether the server answers
at all.

Three routines are webhooks the worker fires (docs/wiki/stage-4a-paperclip-setup.md, step 7):
Incident, from the watchdog, and Follow-up and Source discovery, from worker/notify/routines.py,
only when there is work for them. Each trigger signs with a bearer secret that Paperclip shows
when it is rotated, and the owner puts that and the trigger's URL in the worker's environment.
A fire opens the routine's issue, or skips or joins the one already open, as the routine's
concurrency policy says. What it carries is counts, ids and our own titles: the agent reads the
rest from the ops API, so nothing fetched from the open web goes through Paperclip.

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


class RoutineWebhook:
    """Fires one routine's webhook trigger."""

    name = "routine"

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
        return f"{type(self).__name__}()"

    @classmethod
    def from_parts(
        cls, url: str | None, secret: SecretStr | None, *, user_agent: str
    ) -> "RoutineWebhook | None":
        """The routine, or None when its URL or secret is not set."""
        url = (url or "").strip()
        if not url or secret is None or not secret.get_secret_value().strip():
            return None
        return cls(url, SecretStr(secret.get_secret_value().strip()), user_agent=user_agent)

    async def fire(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> SendResult:
        """Fire it. `idempotency_key`, when given, lets Paperclip refuse a second fire of the
        same wake, should one ever get past the worker's own claim."""
        findings = scan_text_for_secrets(repr(payload))
        if findings:
            logger.error("%s fire withheld by the secret scan; %s", self.name, "; ".join(findings))
            return SendResult(False, withheld=True, error="withheld by the secret scan")
        headers = {"Authorization": f"Bearer {self._secret.get_secret_value()}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS,
                transport=self._transport,
                headers={"User-Agent": self._user_agent},
            ) as http:
                response = await http.post(self._url, json={"payload": payload}, headers=headers)
        except Exception as exc:  # noqa: BLE001 - its message may carry the URL; its type cannot
            return SendResult(False, error=type(exc).__name__)
        if 200 <= response.status_code < 300:
            return SendResult(True)
        return SendResult(False, error=f"HTTP {response.status_code}")


class IncidentRoutine(RoutineWebhook):
    """The Incident routine: TELETRAAN's issue for an incident the watchdog opened."""

    name = "incident"

    @classmethod
    def from_settings(cls, settings: Settings) -> "IncidentRoutine | None":
        return cls.from_parts(
            settings.paperclip_incident_webhook_url,
            settings.paperclip_incident_webhook_secret,
            user_agent=settings.user_agent,
        )


class FollowupRoutine(RoutineWebhook):
    name = "follow-up"


class DiscoveryRoutine(RoutineWebhook):
    name = "source discovery"


def followup_routine(settings: Settings) -> RoutineWebhook | None:
    """DECKARD's Follow-up routine, or None when its URL or secret is not set."""
    return FollowupRoutine.from_parts(
        settings.paperclip_followup_webhook_url,
        settings.paperclip_followup_webhook_secret,
        user_agent=settings.user_agent,
    )


def discovery_routine(settings: Settings) -> RoutineWebhook | None:
    """TACHIKOMA's Source discovery routine, or None when its URL or secret is not set."""
    return DiscoveryRoutine.from_parts(
        settings.paperclip_discovery_webhook_url,
        settings.paperclip_discovery_webhook_secret,
        user_agent=settings.user_agent,
    )


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
