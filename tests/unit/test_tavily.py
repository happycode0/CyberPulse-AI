"""Tavily's search API (worker/discovery/tavily.py): the request is pinned to one credit, only
links, titles and dates are kept, and an error never carries the key."""

import json

import httpx
import pytest
from pydantic import SecretStr

from worker.discovery import tavily

KEY = SecretStr("fake-tavily-TESTONLY")


async def search_with(handler, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await tavily.search(
            client, KEY, "ransomware Australia", max_results=10, time_range="week", **kwargs
        )


async def test_the_request_is_the_one_credit_kind():
    seen = {}

    async def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [], "usage": {"credits": 1}})

    await search_with(handler)
    assert seen["url"] == tavily.SEARCH_URL
    assert seen["auth"] == "Bearer fake-tavily-TESTONLY"
    body = seen["body"]
    assert body["search_depth"] == "basic" and body["auto_parameters"] is False
    assert body["include_answer"] is False and body["include_raw_content"] is False
    assert body["include_images"] is False and body["include_usage"] is True
    assert (body["query"], body["max_results"], body["time_range"]) == (
        "ransomware Australia", 10, "week"
    )


async def test_only_links_titles_and_dates_are_kept():
    async def handler(request):
        return httpx.Response(200, json={
            "results": [
                {"url": "https://example.org/a", "title": "  A\n title ", "content": "page text",
                 "published_date": "2026-10-01", "score": 0.9},
                {"url": "https://example.org/b", "title": "x" * 300},
                {"title": "no link"},
                "not a result",
            ],
            "usage": {"credits": 1},
        })

    found = await search_with(handler)
    assert [h.url for h in found.hits] == ["https://example.org/a", "https://example.org/b"]
    assert found.hits[0].title == "A title" and found.hits[0].published == "2026-10-01"
    assert len(found.hits[1].title) == tavily.TITLE_MAX_CHARS and found.hits[1].published is None
    assert not hasattr(found.hits[0], "content")
    assert found.credits == 1


@pytest.mark.parametrize("usage", [None, {}, {"credits": -1}, {"credits": True}, {"credits": "1"}])
async def test_credits_not_said_plainly_are_unknown(usage):
    async def handler(request):
        return httpx.Response(200, json={"results": [], "usage": usage})

    assert (await search_with(handler)).credits is None


@pytest.mark.parametrize(
    "status, stop", [(401, True), (403, True), (429, True), (432, True), (433, True),
                     (400, False), (500, False), (502, False)],
)
async def test_a_refusal_says_whether_to_stop_and_never_echoes_the_body(status, stop):
    async def handler(request):
        return httpx.Response(status, json={"detail": {"error": "echo fake-tavily-TESTONLY"}})

    with pytest.raises(tavily.SearchFailed) as caught:
        await search_with(handler)
    assert caught.value.stop is stop
    assert str(status) in str(caught.value)
    assert "fake-tavily" not in str(caught.value)


@pytest.mark.parametrize(
    "reply", [httpx.Response(200, content=b"not json"), httpx.Response(200, json={"x": 1}),
              httpx.Response(200, json=[1, 2])],
)
async def test_an_answer_without_results_is_a_failure(reply):
    async def handler(request):
        return reply

    with pytest.raises(tavily.SearchFailed) as caught:
        await search_with(handler)
    assert caught.value.stop is False


async def test_no_answer_is_a_failure_that_does_not_stop_the_night():
    async def handler(request):
        raise httpx.ConnectError("unreachable")

    with pytest.raises(tavily.SearchFailed) as caught:
        await search_with(handler)
    assert caught.value.stop is False and "ConnectError" in str(caught.value)
