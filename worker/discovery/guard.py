"""The guard on every request to a URL the worker did not write itself: a found site's home page,
the feeds it advertises, and each collection of a source the gate activated.

Such a URL is the open web's choice, and the worker sits on a private network beside Paperclip,
its database and the Proxmox host. So it is fetched only if it is https on port 443 and names a
host, not an address, and only once that host has resolved to public addresses alone. Redirects
are followed by hand, each hop checked the same way. TLS is always verified, which also closes the
gap between the check and the connection: a name re-pointed at a private address after the check
reaches a server that cannot present that name's certificate. A body is capped as it arrives,
after decompression, so a small compressed reply cannot unpack into a large one.
"""

import asyncio
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from worker.collectors.http import FetchResult, FetchStatus
from worker.settings import get_settings

URL_MAX_CHARS = 500
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0
# For one URL, redirects and all: a server that drips its reply is cut off here.
DEADLINE_SECONDS = 45.0
DNS_TIMEOUT_SECONDS = 10.0

# Names that only mean something on a private network.
_PRIVATE_SUFFIXES = (
    ".local", ".localhost", ".localdomain", ".internal", ".intranet", ".lan", ".home", ".corp",
    ".arpa", ".onion", ".test", ".invalid", ".example",
)
# A host name, not an address: the last label starts with a letter, so 10.0.0.1 is not one.
_HOST = re.compile(
    r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?"
)

Resolver = Callable[[str], Awaitable[list[str]]]


class Refused(ValueError):
    """A URL the guard will not fetch, and why."""


def check_url(url: object) -> str:
    """`url` with its host in lower case and no default port or fragment, or Refused.

    The query is kept: some feeds are a query on a page (?format=rss).
    """
    if not isinstance(url, str) or not url or len(url) > URL_MAX_CHARS:
        raise Refused(f"a URL is a string of at most {URL_MAX_CHARS} characters")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise Refused("a URL has no spaces or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise Refused("not a URL") from None
    if parts.scheme.lower() != "https":
        raise Refused("only https URLs are fetched")
    if parts.username is not None or parts.password is not None:
        raise Refused("a URL may not carry a user name or password")
    if port not in (None, 443):
        raise Refused("only port 443 is fetched")
    host = (parts.hostname or "").rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise Refused("the host is not a valid name") from None
    if not _HOST.fullmatch(host):
        raise Refused("a URL names a host, not an address")
    if host.endswith(_PRIVATE_SUFFIXES):
        raise Refused("the host is a private name")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def check_host(host: str, resolve: Resolver = _resolve) -> None:
    """Refused unless every address `host` resolves to is a public one."""
    try:
        addresses = await asyncio.wait_for(resolve(host), DNS_TIMEOUT_SECONDS)
    except (OSError, TimeoutError):
        raise Refused(f"{host} does not resolve") from None
    if not addresses:
        raise Refused(f"{host} does not resolve")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            raise Refused(f"{host} resolves to something that is not an address") from None
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise Refused(f"{host} resolves to a private address")


@dataclass(frozen=True)
class Fetched:
    url: str  # where it ended, after any redirects
    status_code: int
    headers: httpx.Headers
    body: bytes  # empty unless the status is 2xx


async def get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_BODY_BYTES,
    resolve: Resolver = _resolve,
) -> Fetched:
    """GET `url` through the guard. Refused for a URL, host, redirect or body it will not take;
    httpx's own errors for a request that failed."""
    async with asyncio.timeout(DEADLINE_SECONDS):
        current = check_url(url)
        for _ in range(MAX_REDIRECTS + 1):
            await check_host(urlsplit(current).hostname or "", resolve)
            async with client.stream(
                "GET",
                current,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as response:
                # Not `is_redirect`, which is any 3xx, 304 included.
                if response.has_redirect_location:
                    current = check_url(urljoin(current, response.headers["location"]))
                    continue
                body = b""
                if response.is_success:
                    body = await _read_capped(response, max_bytes)
                return Fetched(current, response.status_code, response.headers, body)
        raise Refused(f"more than {MAX_REDIRECTS} redirects")


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    too_big = Refused(f"the body is over {max_bytes // (1024 * 1024)} MiB")
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise too_big
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise too_big
        chunks.append(chunk)
    return b"".join(chunks)


def request_headers(etag: str | None = None, last_modified: str | None = None) -> dict[str, str]:
    """The collector's headers (worker/collectors/http.py), conditional when it can be."""
    headers = {"User-Agent": get_settings().user_agent, "Accept-Encoding": "gzip"}
    if etag is not None:
        headers["If-None-Match"] = etag
    if last_modified is not None:
        headers["If-Modified-Since"] = last_modified
    return headers


async def fetch_guarded(
    client: httpx.AsyncClient,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    resolve: Resolver = _resolve,
) -> FetchResult:
    """`worker.collectors.http.fetch` for a discovered source: one attempt, through the guard.

    Never raises. A refusal is the source's error for the run, so its health records it.
    """
    started = time.monotonic()

    def took() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        got = await get(
            client, url, headers=request_headers(etag, last_modified), resolve=resolve
        )
    except Refused as exc:
        return FetchResult(FetchStatus.ERROR, error=f"refused: {exc}", duration_ms=took())
    except (TimeoutError, httpx.TimeoutException):
        return FetchResult(FetchStatus.TIMEOUT, error="no response in time", duration_ms=took())
    except httpx.HTTPError as exc:
        return FetchResult(FetchStatus.ERROR, error=type(exc).__name__, duration_ms=took())
    if got.status_code == 304:
        return FetchResult(FetchStatus.NOT_MODIFIED, duration_ms=took(), status_code=304)
    if 200 <= got.status_code < 300:
        return FetchResult(
            FetchStatus.OK,
            body=got.body,
            etag=got.headers.get("etag"),
            last_modified=got.headers.get("last-modified"),
            duration_ms=took(),
            status_code=got.status_code,
        )
    return FetchResult(
        FetchStatus.ERROR,
        error=f"HTTP {got.status_code}",
        duration_ms=took(),
        status_code=got.status_code,
    )
