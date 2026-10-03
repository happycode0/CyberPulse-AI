"""Tavily's search API, for the nightly discovery searches (worker/discovery/run.py).

Each search is pinned to the cheapest form, one credit: `search_depth` basic, `auto_parameters`
off (it may otherwise choose advanced, which costs two), and no answer, raw content or images.
`include_usage` has Tavily say what the search cost, which `discovery_searches` records.

Only each result's link, title and date are kept. Its `content` is page text from the open web
and is never stored: the worker wants to know which sites write about its beat, not what they
said.
"""

from dataclasses import dataclass

import httpx
from pydantic import SecretStr

SEARCH_URL = "https://api.tavily.com/search"
TIMEOUT_SECONDS = 30.0
TITLE_MAX_CHARS = 200


class SearchFailed(Exception):
    """Tavily refused or did not answer. The message never carries the key."""

    def __init__(self, message: str, *, stop: bool) -> None:
        super().__init__(message)
        # True when no later search tonight can work: the key, the plan or the rate limit.
        self.stop = stop


@dataclass(frozen=True)
class Hit:
    url: str
    title: str
    published: str | None  # as Tavily gave it, when it did


@dataclass(frozen=True)
class Searched:
    hits: list[Hit]
    credits: int | None  # None when Tavily did not say


def _text(value: object, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def search(
    client: httpx.AsyncClient,
    key: SecretStr,
    query: str,
    *,
    max_results: int,
    time_range: str,
) -> Searched:
    try:
        response = await client.post(
            SEARCH_URL,
            headers={"Authorization": f"Bearer {key.get_secret_value()}"},
            json={
                "query": query,
                "search_depth": "basic",
                "auto_parameters": False,
                "topic": "general",
                "max_results": max_results,
                "time_range": time_range,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
                "include_usage": True,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise SearchFailed(f"no answer from Tavily ({type(exc).__name__})", stop=False) from None
    if response.status_code != 200:
        # 401 the key, 429 the rate limit, 432 and 433 the plan's limits: nothing later tonight
        # would get a different answer. Tavily's error body is not echoed.
        stop = response.status_code in (401, 403, 429, 432, 433)
        raise SearchFailed(f"Tavily answered HTTP {response.status_code}", stop=stop)
    try:
        body = response.json()
    except ValueError:
        raise SearchFailed("Tavily's answer is not JSON", stop=False) from None
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise SearchFailed("Tavily's answer has no results list", stop=False)

    hits = []
    for result in body["results"]:
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            continue
        published = result.get("published_date")
        hits.append(
            Hit(
                url=result["url"],
                title=_text(result.get("title"), TITLE_MAX_CHARS),
                published=_text(published, 40) or None,
            )
        )
    usage = body.get("usage")
    credits = usage.get("credits") if isinstance(usage, dict) else None
    valid = isinstance(credits, int) and not isinstance(credits, bool) and credits >= 0
    return Searched(hits, credits if valid else None)
