"""HTTP layer with conditional requests, retries, and raw response caching."""

import hashlib
import time
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

import httpx

from worker.models import SourceConfig
from worker.settings import get_settings


class FetchStatus(StrEnum):
    """Status of a fetch operation."""

    OK = "ok"
    NOT_MODIFIED = "not_modified"
    ERROR = "error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class FetchResult:
    """Result of a fetch operation."""

    def __init__(
        self,
        status: FetchStatus,
        body: bytes | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        duration_ms: int = 0,
        error: str | None = None,
        status_code: int | None = None,
    ):
        """Initialize a FetchResult.

        Parameters
        ----------
        status : FetchStatus
            The status of the fetch operation.
        body : bytes | None
            The response body, or None if not fetched/modified.
        etag : str | None
            The ETag header from the response.
        last_modified : str | None
            The Last-Modified header from the response.
        duration_ms : int
            Time taken for the fetch in milliseconds.
        error : str | None
            Error message if status is ERROR.
        status_code : int | None
            The HTTP status actually returned, or None when no response arrived (a timeout, a DNS
            failure, a connection reset). Kept alongside `error` because callers need to tell one
            4xx from another and parsing it back out of the message would be guesswork: a 404 from
            a per-record register means that record does not exist, which is an answer, while a 503
            means the register is unreachable, which is not. `error` stays the human-readable form.
        """
        self.status = status
        self.body = body
        self.etag = etag
        self.last_modified = last_modified
        self.duration_ms = duration_ms
        self.error = error
        self.status_code = status_code


async def fetch(
    client: httpx.AsyncClient,
    source: SourceConfig,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    max_size_bytes: int = 32 * 1024 * 1024,  # 32 MiB
) -> FetchResult:
    """Fetch from a source with conditional requests and retries.

    Parameters
    ----------
    client : httpx.AsyncClient
        The async HTTP client to use.
    source : SourceConfig
        The source configuration containing the URL.
    etag : str | None
        ETag to use for If-None-Match header.
    last_modified : str | None
        Last-Modified date to use for If-Modified-Since header.
    timeout : float
        Request timeout in seconds.
    max_retries : int
        Maximum number of retry attempts (total attempts = max_retries).
    max_size_bytes : int
        Maximum response size in bytes before truncating.

    Returns
    -------
    FetchResult
        The result of the fetch operation.
    """
    settings = get_settings()
    headers = {
        "User-Agent": settings.user_agent,
        # Set explicitly to override httpx's default of "gzip, deflate", which www.cisa.gov
        # treats as a bot signature: measured 2026-10-01, the pair (User-Agent
        # "CyberPulse-AI/1.0", Accept-Encoding "gzip, deflate") returns 403 on every CISA RSS
        # endpoint, 10 attempts out of 10, while changing either side alone returns 200 —
        # "gzip" and "identity" both pass, as does the same default under a different UA. The
        # value is still honest: gzip is what we can actually decode, so we do not advertise
        # br or zstd to get through. It also keeps the responses compressed, which "identity"
        # would not: cisa_kev alone is 1.7 MB per fetch.
        "Accept-Encoding": "gzip",
    }

    if etag is not None:
        headers["If-None-Match"] = etag
    if last_modified is not None:
        headers["If-Modified-Since"] = last_modified

    start_time = time.time()

    for attempt in range(max_retries):
        try:
            response = await client.get(
                source.url,
                headers=headers,
                timeout=timeout,
            )

            duration_ms = int((time.time() - start_time) * 1000)

            # Handle 304 Not Modified
            if response.status_code == 304:
                return FetchResult(
                    status=FetchStatus.NOT_MODIFIED,
                    duration_ms=duration_ms,
                    status_code=response.status_code,
                )

            # Handle 2xx success
            if 200 <= response.status_code < 300:
                body = response.content
                if len(body) > max_size_bytes:
                    body = body[:max_size_bytes]

                etag_header = response.headers.get("etag")
                last_modified_header = response.headers.get("last-modified")

                return FetchResult(
                    status=FetchStatus.OK,
                    body=body,
                    etag=etag_header,
                    last_modified=last_modified_header,
                    duration_ms=duration_ms,
                    status_code=response.status_code,
                )

            # Handle 4xx (don't retry)
            if 400 <= response.status_code < 500:
                return FetchResult(
                    status=FetchStatus.ERROR,
                    error=f"HTTP {response.status_code}",
                    duration_ms=duration_ms,
                    status_code=response.status_code,
                )

            # Handle 5xx (retry)
            if 500 <= response.status_code < 600:
                if attempt < max_retries - 1:
                    # Exponential backoff: 0.1s, 0.2s, 0.4s, etc.
                    wait_time = 0.1 * (2 ** attempt)
                    await _async_sleep(wait_time)
                    continue
                else:
                    return FetchResult(
                        status=FetchStatus.ERROR,
                        error=f"HTTP {response.status_code} after {max_retries} attempts",
                        duration_ms=duration_ms,
                        status_code=response.status_code,
                    )

            # Unexpected status code
            return FetchResult(
                status=FetchStatus.ERROR,
                error=f"Unexpected HTTP status {response.status_code}",
                duration_ms=duration_ms,
                status_code=response.status_code,
            )

        except httpx.TimeoutException as e:
            duration_ms = int((time.time() - start_time) * 1000)
            if attempt < max_retries - 1:
                wait_time = 0.1 * (2 ** attempt)
                await _async_sleep(wait_time)
                continue
            return FetchResult(
                status=FetchStatus.TIMEOUT,
                # httpx timeout exceptions stringify to "", which would leave the health
                # record with a TIMEOUT status and no explanation.
                error=str(e) or f"{type(e).__name__} after {timeout:g}s",
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return FetchResult(
                status=FetchStatus.ERROR,
                error=str(e),
                duration_ms=duration_ms,
            )

    # Should not reach here, but return error if we do
    return FetchResult(
        status=FetchStatus.ERROR,
        error="Max retries exceeded",
        duration_ms=int((time.time() - start_time) * 1000),
    )


def cache_raw(source_id: str, body: bytes) -> Path:
    """Cache raw response body to filesystem.

    Saves to: `{raw_cache_dir}/{source_id}/{utc-date}/{sha256[:16]}.raw`

    Parameters
    ----------
    source_id : str
        The source identifier.
    body : bytes
        The raw response body.

    Returns
    -------
    Path
        The path where the file was written.
    """
    settings = get_settings()
    cache_dir = settings.raw_cache_dir

    # Create date directory
    utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_dir = cache_dir / source_id / utc_date

    # Create all parent directories
    date_dir.mkdir(parents=True, exist_ok=True)

    # Generate SHA256 hash (first 16 chars)
    hash_digest = hashlib.sha256(body).hexdigest()[:16]

    # Write file
    file_path = date_dir / f"{hash_digest}.raw"
    file_path.write_bytes(body)

    return file_path


def prune_cache(ttl_days: int = 7) -> int:
    """Remove cached files older than ttl_days.

    Parameters
    ----------
    ttl_days : int
        Time-to-live in days. Files older than this will be removed.

    Returns
    -------
    int
        Number of files removed.
    """
    settings = get_settings()
    cache_dir = settings.raw_cache_dir

    if not cache_dir.exists():
        return 0

    cutoff_time = time.time() - (ttl_days * 24 * 3600)
    removed_count = 0

    for file_path in cache_dir.glob("**/*.raw"):
        try:
            # Check file modification time
            mtime = file_path.stat().st_mtime
            if mtime < cutoff_time:
                file_path.unlink()
                removed_count += 1

                # Clean up empty parent directories
                parent = file_path.parent
                while parent != cache_dir and parent.exists():
                    try:
                        if not any(parent.iterdir()):
                            parent.rmdir()
                            parent = parent.parent
                        else:
                            break
                    except OSError:
                        break
        except OSError:
            # Skip files we can't access
            pass

    return removed_count


async def _async_sleep(duration: float) -> None:
    """Sleep asynchronously using asyncio.sleep."""
    import asyncio
    await asyncio.sleep(duration)
