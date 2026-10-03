"""The URL guard (worker/discovery/guard.py): what a found site's URLs may be, where they may
resolve, and how much of a reply is read."""

import gzip

import httpx
import pytest

from worker.collectors.http import FetchStatus
from worker.discovery.guard import (
    MAX_REDIRECTS,
    Refused,
    check_host,
    check_url,
    fetch_guarded,
    get,
)

PUBLIC = "93.184.215.14"


def resolver(table: dict[str, list[str]], default: list[str] | None = None):
    """A resolver that answers from `table`, and records what it was asked."""
    asked: list[str] = []

    async def resolve(host: str) -> list[str]:
        asked.append(host)
        if host in table:
            return table[host]
        if default is not None:
            return default
        raise OSError("no such host")

    resolve.asked = asked
    return resolve


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- check_url -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://Example.ORG/feed", "https://example.org/feed"),
        ("https://example.org", "https://example.org/"),
        ("https://example.org:443/a?format=rss", "https://example.org/a?format=rss"),
        ("https://example.org/a#top", "https://example.org/a"),
        ("https://example.org./a", "https://example.org/a"),
        ("https://bücher.example.org/", "https://xn--bcher-kva.example.org/"),
    ],
)
def test_a_public_https_url_is_kept_in_one_form(url, expected):
    assert check_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        None,
        42,
        "",
        "https://example.org/" + "a" * 500,
        "http://example.org/feed",
        "ftp://example.org/feed",
        "javascript:alert(1)",
        "https://user:pw@example.org/",
        "https://user@example.org/",
        "https://example.org:8443/",
        "https://example.org:80/",
        "https://10.0.0.1/",
        "https://127.1/",
        "https://0x7f000001/",
        "https://[::1]/",
        "https://localhost/",
        "https://printer.local/",
        "https://db.internal/",
        "https://nas.lan/",
        "https://router.home/",
        "https://site.test/",
        "https://example.org/a b",
        "https://example.org/\x00",
        "https:///path",
        "https://exa_mple.org/",
        "https://-bad.example.org/",
    ],
)
def test_anything_else_is_refused(url):
    with pytest.raises(Refused):
        check_url(url)


# --- check_host ----------------------------------------------------------------------------------


async def test_a_host_with_only_public_addresses_passes():
    await check_host("example.org", resolver({"example.org": [PUBLIC, "2606:2800:21f:cb07::1"]}))


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.5"],
        ["192.168.128.39"],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["0.0.0.0"],
        ["::1"],
        ["fe80::1%eth0"],
        ["fd00::1"],
        ["::ffff:127.0.0.1"],
        ["224.0.0.1"],
        [PUBLIC, "10.0.0.5"],  # any one private address is enough
        [],
        ["not-an-address"],
    ],
)
async def test_a_host_with_any_private_address_is_refused(addresses):
    with pytest.raises(Refused):
        await check_host("example.org", resolver({"example.org": addresses}))


async def test_a_host_that_does_not_resolve_is_refused():
    with pytest.raises(Refused, match="does not resolve"):
        await check_host("nowhere.example.org", resolver({}))


# --- get -----------------------------------------------------------------------------------------


async def test_get_reads_a_public_page():
    async def handler(request):
        return httpx.Response(200, content=b"<rss/>", headers={"ETag": '"v1"'})

    async with client_for(handler) as client:
        got = await get(client, "https://example.org/feed", resolve=resolver({}, [PUBLIC]))
    assert (got.url, got.status_code, got.body) == ("https://example.org/feed", 200, b"<rss/>")
    assert got.headers["etag"] == '"v1"'


async def test_each_redirect_is_checked_before_it_is_followed():
    requested = []

    async def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://inside.example.org/admin"})

    resolve = resolver({"example.org": [PUBLIC], "inside.example.org": ["10.0.0.5"]})
    async with client_for(handler) as client:
        with pytest.raises(Refused, match="private address"):
            await get(client, "https://example.org/feed", resolve=resolve)
    assert requested == ["https://example.org/feed"]  # the private host was never asked
    assert resolve.asked == ["example.org", "inside.example.org"]


@pytest.mark.parametrize(
    "location",
    ["http://example.org/feed", "https://example.org:8080/", "https://10.0.0.1/", "file:///etc"],
)
async def test_a_redirect_to_a_url_the_guard_would_refuse_is_refused(location):
    async def handler(request):
        return httpx.Response(301, headers={"Location": location})

    async with client_for(handler) as client:
        with pytest.raises(Refused):
            await get(client, "https://example.org/", resolve=resolver({}, [PUBLIC]))


async def test_a_relative_redirect_is_followed_on_the_same_host():
    async def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        return httpx.Response(200, content=b"ok")

    async with client_for(handler) as client:
        got = await get(client, "https://example.org/old", resolve=resolver({}, [PUBLIC]))
    assert (got.url, got.body) == ("https://example.org/new", b"ok")


async def test_redirects_stop_after_the_limit():
    hops = []

    async def handler(request):
        hops.append(request.url.path)
        return httpx.Response(302, headers={"Location": f"/hop{len(hops)}"})

    async with client_for(handler) as client:
        with pytest.raises(Refused, match="redirects"):
            await get(client, "https://example.org/", resolve=resolver({}, [PUBLIC]))
    assert len(hops) == MAX_REDIRECTS + 1


async def test_a_body_over_the_cap_is_refused_as_it_arrives():
    async def handler(request):
        return httpx.Response(200, content=b"x" * 5000)

    async with client_for(handler) as client:
        with pytest.raises(Refused, match="body"):
            await get(client, "https://example.org/", max_bytes=1000,
                      resolve=resolver({}, [PUBLIC]))


async def test_the_cap_counts_the_body_after_decompression():
    packed = gzip.compress(b"\0" * 100_000)
    assert len(packed) < 1000

    async def handler(request):
        return httpx.Response(200, content=packed, headers={"Content-Encoding": "gzip"})

    async with client_for(handler) as client:
        with pytest.raises(Refused, match="body"):
            await get(client, "https://example.org/", max_bytes=10_000,
                      resolve=resolver({}, [PUBLIC]))


async def test_a_declared_length_over_the_cap_is_refused_unread():
    async def handler(request):
        return httpx.Response(200, content=b"x" * 2000, headers={"Content-Length": "2000"})

    async with client_for(handler) as client:
        with pytest.raises(Refused, match="body"):
            await get(client, "https://example.org/", max_bytes=1000,
                      resolve=resolver({}, [PUBLIC]))


async def test_a_failed_reply_has_no_body():
    async def handler(request):
        return httpx.Response(404, content=b"not here")

    async with client_for(handler) as client:
        got = await get(client, "https://example.org/", resolve=resolver({}, [PUBLIC]))
    assert (got.status_code, got.body) == (404, b"")


# --- fetch_guarded -------------------------------------------------------------------------------


async def fetch_with(handler, url="https://example.org/feed", **kwargs):
    async with client_for(handler) as client:
        return await fetch_guarded(client, url, resolve=resolver({}, [PUBLIC]), **kwargs)


async def test_fetch_guarded_sends_the_conditional_headers_and_reads_the_validators():
    seen = {}

    async def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, content=b"<rss/>",
                              headers={"ETag": '"v2"', "Last-Modified": "Fri, 02 Oct 2026"})

    result = await fetch_with(handler, etag='"v1"', last_modified="Thu, 01 Oct 2026")
    assert result.status is FetchStatus.OK and result.body == b"<rss/>"
    assert (result.etag, result.last_modified) == ('"v2"', "Fri, 02 Oct 2026")
    assert seen["if-none-match"] == '"v1"'
    assert seen["if-modified-since"] == "Thu, 01 Oct 2026"
    assert "user-agent" in seen


async def test_fetch_guarded_reads_not_modified():
    async def handler(request):
        return httpx.Response(304)

    result = await fetch_with(handler)
    assert (result.status, result.status_code) == (FetchStatus.NOT_MODIFIED, 304)


async def test_fetch_guarded_reports_a_server_error():
    async def handler(request):
        return httpx.Response(503)

    result = await fetch_with(handler)
    assert (result.status, result.error, result.status_code) == (FetchStatus.ERROR, "HTTP 503", 503)


async def test_fetch_guarded_reports_a_refusal_as_the_sources_error():
    async def handler(request):  # pragma: no cover - never reached
        raise AssertionError("a refused URL was fetched")

    result = await fetch_with(handler, url="http://example.org/feed")
    assert result.status is FetchStatus.ERROR and result.error.startswith("refused: ")


@pytest.mark.parametrize(
    "error, status",
    [(httpx.ConnectTimeout("slow"), FetchStatus.TIMEOUT),
     (httpx.ConnectError("refused"), FetchStatus.ERROR)],
)
async def test_fetch_guarded_never_raises(error, status):
    async def handler(request):
        raise error

    result = await fetch_with(handler)
    assert result.status is status and result.body is None
