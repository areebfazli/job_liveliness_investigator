"""`rli.archive.cdx.list_captures` — CDX pagination, retries, structured failure."""

from __future__ import annotations

import json

import httpx
import respx

from rli.archive.cdx import CDX_URL, list_captures

HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]


def _page(rows: list[list[str]], resume_key: str | None = None) -> str:
    body = [HEADER, *rows]
    if resume_key:
        body.append([])
        body.append([resume_key])
    return json.dumps(body)


@respx.mock
def test_list_captures_single_page(net_client_factory) -> None:
    net = net_client_factory("archive_backfill")
    surt = "org,example)/jobs"
    url = "https://example.com/jobs"
    rows = [
        [surt, "20250101000000", url, "application/json", "200", "AAA", "100"],
        [surt, "20250102000000", url, "application/json", "200", "BBB", "100"],
    ]
    respx.get(CDX_URL).mock(return_value=httpx.Response(200, text=_page(rows)))

    result = list_captures(net, "https://example.com/jobs*", "20250101", "20250201")

    assert result.ok is True
    assert result.error is None
    assert [c.timestamp for c in result.captures] == ["20250101000000", "20250102000000"]
    assert result.captures[0].original == "https://example.com/jobs"
    assert result.captures[0].digest == "AAA"


@respx.mock
def test_list_captures_pagination_resume_key_handoff(net_client_factory) -> None:
    net = net_client_factory("archive_backfill")

    page1_rows = [
        ["k", "20250101000000", "https://example.com/jobs", "application/json", "200", "AAA", "1"],
        ["k", "20250102000000", "https://example.com/jobs", "application/json", "200", "BBB", "1"],
    ]
    page2_rows = [
        ["k", "20250103000000", "https://example.com/jobs", "application/json", "200", "CCC", "1"],
    ]

    call_count = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        params = dict(request.url.params)
        if "resumeKey" in params:
            assert params["resumeKey"] == "rk-123"
            return httpx.Response(200, text=_page(page2_rows))
        return httpx.Response(200, text=_page(page1_rows, resume_key="rk-123"))

    respx.get(CDX_URL).mock(side_effect=responder)

    result = list_captures(net, "https://example.com/jobs*", "20250101", "20250201")

    assert result.ok is True
    assert call_count["n"] == 2
    assert [c.timestamp for c in result.captures] == [
        "20250101000000",
        "20250102000000",
        "20250103000000",
    ]


@respx.mock
def test_list_captures_empty_result_no_pagination(net_client_factory) -> None:
    net = net_client_factory("archive_backfill")
    respx.get(CDX_URL).mock(return_value=httpx.Response(200, text=json.dumps([HEADER])))

    result = list_captures(net, "https://example.com/nothing*", "20250101", "20250201")

    assert result.ok is True
    assert result.captures == []


@respx.mock
def test_list_captures_429_then_success(net_client_factory) -> None:
    """Exercises rli.net.NetClient's own retry/backoff logic through cdx.py."""
    net = net_client_factory("archive_backfill")
    rows = [
        ["k", "20250101000000", "https://example.com/jobs", "application/json", "200", "AAA", "1"]
    ]

    route = respx.get(CDX_URL).mock(
        side_effect=[
            httpx.Response(429, text="slow down"),
            httpx.Response(200, text=_page(rows)),
        ]
    )

    result = list_captures(net, "https://example.com/jobs*", "20250101", "20250201")

    assert result.ok is True
    assert len(result.captures) == 1
    assert route.call_count == 2


@respx.mock
def test_list_captures_retry_exhaustion_is_structured_failure(net_client_factory) -> None:
    net = net_client_factory("archive_backfill")
    respx.get(CDX_URL).mock(return_value=httpx.Response(503, text="down"))

    result = list_captures(net, "https://example.com/jobs*", "20250101", "20250201")

    assert result.ok is False
    assert result.retryable is True
    assert result.captures == []
    assert result.error is not None


@respx.mock
def test_list_captures_malformed_json_is_structured_failure(net_client_factory) -> None:
    net = net_client_factory("archive_backfill")
    respx.get(CDX_URL).mock(return_value=httpx.Response(200, text="not json"))

    result = list_captures(net, "https://example.com/jobs*", "20250101", "20250201")

    assert result.ok is False
    assert result.retryable is False
    assert "JSON" in result.error
