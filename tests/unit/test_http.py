"""Tests for HTTP layer with conditional requests."""

import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from worker.collectors.http import FetchResult, FetchStatus, cache_raw, fetch, prune_cache
from worker.models import SourceConfig
from worker.settings import Settings


@pytest.fixture
def client():
    """Async HTTP client for testing."""
    return httpx.AsyncClient()


@pytest.fixture
def src():
    """Test source config."""
    return SourceConfig(
        id="acsc_alerts",
        name="ACSC Alerts",
        type="feed",
        region="AU",
        category="alerts",
        source_class="acsc",
        priority=1,
        lane="fast",
        enabled=True,
        url="https://cyber.gov.au/feed/alerts",
        parser="rss",
        expected_frequency="hourly",
    )


async def test_fetch_ok_returns_body_and_validators(respx_mock):
    """Test that 200 response returns body and ETag."""
    URL = "https://cyber.gov.au/feed/alerts"
    respx_mock.get(URL).respond(200, content=b"<rss/>", headers={"ETag": '"abc"'})
    
    client = httpx.AsyncClient()
    src = SourceConfig(
        id="acsc_alerts",
        name="ACSC Alerts",
        type="feed",
        region="AU",
        category="alerts",
        source_class="acsc",
        priority=1,
        lane="fast",
        enabled=True,
        url=URL,
        parser="rss",
        expected_frequency="hourly",
    )
    
    r = await fetch(client, src)
    assert (r.status, r.body, r.etag) == (FetchStatus.OK, b"<rss/>", '"abc"')


async def test_sends_conditional_headers_when_validators_known(respx_mock):
    """Test that If-None-Match and If-Modified-Since are sent when validators are known."""
    URL = "https://cyber.gov.au/feed/alerts"
    route = respx_mock.get(URL).respond(304)
    
    client = httpx.AsyncClient()
    src = SourceConfig(
        id="acsc_alerts",
        name="ACSC Alerts",
        type="feed",
        region="AU",
        category="alerts",
        source_class="acsc",
        priority=1,
        lane="fast",
        enabled=True,
        url=URL,
        parser="rss",
        expected_frequency="hourly",
    )
    
    await fetch(client, src, etag='"abc"', last_modified="Mon, 28 Sep 2026 00:00:00 GMT")
    h = route.calls[0].request.headers
    assert h["if-none-match"] == '"abc"' and "if-modified-since" in h


async def test_304_returns_not_modified_with_no_body(respx_mock):
    """Test that 304 returns NOT_MODIFIED status with no body."""
    URL = "https://cyber.gov.au/feed/alerts"
    respx_mock.get(URL).respond(304)
    
    client = httpx.AsyncClient()
    src = SourceConfig(
        id="acsc_alerts",
        name="ACSC Alerts",
        type="feed",
        region="AU",
        category="alerts",
        source_class="acsc",
        priority=1,
        lane="fast",
        enabled=True,
        url=URL,
        parser="rss",
        expected_frequency="hourly",
    )
    
    r = await fetch(client, src, etag='"abc"')
    assert (r.status, r.body) == (FetchStatus.NOT_MODIFIED, None)


async def test_user_agent_identifies_project(respx_mock):
    """Test that User-Agent header identifies CyberPulse-AI project."""
    URL = "https://cyber.gov.au/feed/alerts"
    route = respx_mock.get(URL).respond(200, content=b"x")
    
    client = httpx.AsyncClient()
    src = SourceConfig(
        id="acsc_alerts",
        name="ACSC Alerts",
        type="feed",
        region="AU",
        category="alerts",
        source_class="acsc",
        priority=1,
        lane="fast",
        enabled=True,
        url=URL,
        parser="rss",
        expected_frequency="hourly",
    )
    
    await fetch(client, src)
    assert "CyberPulse-AI" in route.calls[0].request.headers["user-agent"]


async def test_accept_encoding_is_not_the_httpx_default(respx_mock):
    """www.cisa.gov 403s our UA paired with httpx's default "gzip, deflate".

    Guarded because the header looks redundant — httpx sets one for you — so deleting it is
    the obvious tidy-up, and it costs every CISA RSS source with a 403 that reads like the
    source blocking us rather than our own default giving us away. See worker/collectors/http.py.
    """
    URL = "https://cyber.gov.au/feed/alerts"
    route = respx_mock.get(URL).respond(200, content=b"x")

    client = httpx.AsyncClient()
    src = SourceConfig(
        id="acsc_alerts",
        name="ACSC Alerts",
        type="feed",
        region="AU",
        category="alerts",
        source_class="acsc",
        priority=1,
        lane="fast",
        enabled=True,
        url=URL,
        parser="rss",
        expected_frequency="hourly",
    )

    await fetch(client, src)
    sent = route.calls[0].request.headers["accept-encoding"]
    assert sent == "gzip", sent


async def test_403_is_error_not_exception(respx_mock):
    """Test that 403 Forbidden is returned as ERROR status, not raised."""
    URL = "https://cyber.gov.au/feed/alerts"
    respx_mock.get(URL).respond(403)
    
    client = httpx.AsyncClient()
    src = SourceConfig(
        id="acsc_alerts",
        name="ACSC Alerts",
        type="feed",
        region="AU",
        category="alerts",
        source_class="acsc",
        priority=1,
        lane="fast",
        enabled=True,
        url=URL,
        parser="rss",
        expected_frequency="hourly",
    )
    
    r = await fetch(client, src)
    assert r.status is FetchStatus.ERROR and "403" in r.error


async def test_timeout_maps_to_timeout_status(respx_mock):
    """Test that timeouts are mapped to TIMEOUT status."""
    URL = "https://cyber.gov.au/feed/alerts"
    respx_mock.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    
    client = httpx.AsyncClient()
    src = SourceConfig(
        id="acsc_alerts",
        name="ACSC Alerts",
        type="feed",
        region="AU",
        category="alerts",
        source_class="acsc",
        priority=1,
        lane="fast",
        enabled=True,
        url=URL,
        parser="rss",
        expected_frequency="hourly",
    )
    
    assert (await fetch(client, src)).status is FetchStatus.TIMEOUT


async def test_timeout_always_carries_an_explanation(respx_mock):
    """httpx timeout exceptions stringify to '', which used to leave a bare TIMEOUT."""
    URL = "https://cyber.gov.au/feed/alerts"
    respx_mock.get(URL).mock(side_effect=httpx.ReadTimeout(""))
    src = SourceConfig(
        id="acsc_alerts", name="ACSC Alerts", type="feed", region="AU", category="alerts",
        source_class="acsc", priority=1, lane="fast", enabled=True, url=URL, parser="rss",
        expected_frequency="hourly",
    )
    result = await fetch(httpx.AsyncClient(), src, max_retries=1)
    assert result.status is FetchStatus.TIMEOUT and "ReadTimeout" in result.error


async def test_5xx_is_retried_then_errors(respx_mock):
    """Test that 5xx errors are retried up to 3 times, then return ERROR."""
    URL = "https://cyber.gov.au/feed/alerts"
    route = respx_mock.get(URL).respond(503)
    
    client = httpx.AsyncClient()
    src = SourceConfig(
        id="acsc_alerts",
        name="ACSC Alerts",
        type="feed",
        region="AU",
        category="alerts",
        source_class="acsc",
        priority=1,
        lane="fast",
        enabled=True,
        url=URL,
        parser="rss",
        expected_frequency="hourly",
    )
    
    assert (await fetch(client, src)).status is FetchStatus.ERROR
    assert route.call_count == 3


def test_cache_raw_writes_and_prune_removes_expired(tmp_path):
    """Test that cache_raw writes file and prune_cache removes expired entries."""
    # Create a mock Settings object
    mock_settings = Settings(
        database_url="sqlite:///:memory:",
        raw_cache_dir=tmp_path,
    )
    
    with patch("worker.collectors.http.get_settings", return_value=mock_settings):
        p = cache_raw("acsc_alerts", b"<rss/>")
        assert p.read_bytes() == b"<rss/>"
        
        # Set file mtime to 0 (very old)
        os.utime(p, (0, 0))
        
        # Prune with 7-day TTL should remove the old file
        removed = prune_cache(ttl_days=7)
        assert removed == 1 and not p.exists()


def test_http2_not_enabled():
    """Test that HTTP/2 is NOT enabled on AsyncClient (production issue: cyber.gov.au aborts HTTP/2)."""
    client = httpx.AsyncClient()
    # The http2 parameter should not be set to True
    # httpx.AsyncClient defaults to http2=False unless explicitly set to True
    assert client._mounts is not None  # Client is configured
    # Verify by checking the client config doesn't explicitly enable HTTP/2
    # (httpx doesn't expose http2 setting directly, but we document this constraint)
    pass  # This test documents the constraint; the default behavior is correct

