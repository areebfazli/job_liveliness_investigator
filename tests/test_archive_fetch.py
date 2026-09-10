"""`rli.archive.fetch.fetch_capture` — raw capture fetch, retries, structured failure."""

from __future__ import annotations

import httpx
import respx

from rli.archive.fetch import capture_url, fetch_capture


def test_capture_url_shape() -> None:
    url = capture_url("20250115120000", "https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    assert url == (
        "https://web.archive.org/web/20250115120000id_/"
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    )


@respx.mock
def test_fetch_capture_success(net_client_factory) -> None:
    net = net_client_factory("archive_backfill")
    url = capture_url("20250115120000", "https://example.com/jobs")
    respx.get(url).mock(return_value=httpx.Response(200, text='{"jobs": []}'))

    result = fetch_capture(net, "20250115120000", "https://example.com/jobs")

    assert result.ok is True
    assert result.status == 200
    assert result.body == '{"jobs": []}'
    assert result.error is None


@respx.mock
def test_fetch_capture_429_then_success(net_client_factory) -> None:
    net = net_client_factory("archive_backfill")
    url = capture_url("20250115120000", "https://example.com/jobs")
    route = respx.get(url).mock(side_effect=[httpx.Response(429), httpx.Response(200, text="body")])

    result = fetch_capture(net, "20250115120000", "https://example.com/jobs")

    assert route.call_count == 2
    assert result.ok is True
    assert result.body == "body"


@respx.mock
def test_fetch_capture_retry_exhaustion_is_structured_failure(net_client_factory) -> None:
    net = net_client_factory("archive_backfill")
    url = capture_url("20250115120000", "https://example.com/jobs")
    respx.get(url).mock(return_value=httpx.Response(503, text="down"))

    result = fetch_capture(net, "20250115120000", "https://example.com/jobs")

    assert result.ok is False
    assert result.retryable is True
    assert result.error is not None


@respx.mock
def test_fetch_capture_404_is_non_retryable_failure(net_client_factory) -> None:
    net = net_client_factory("archive_backfill")
    url = capture_url("20250115120000", "https://example.com/jobs")
    respx.get(url).mock(return_value=httpx.Response(404, text="gone"))

    result = fetch_capture(net, "20250115120000", "https://example.com/jobs")

    assert result.ok is False
    assert result.retryable is False
