"""`rli.resolvers.ashby` adapter tests, against a compact realistic fixture.

The fixture shape (GUESSED — not verified against a live call; see the
module docstring in `rli/resolvers/ashby.py`) follows the publicly
documented Ashby job-board fields: id, title, department, team, location,
publishedAt, jobUrl, descriptionHtml.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import respx

from rli.net import NetClient
from rli.probes.base import ProbeContext
from rli.resolvers import ashby

ASHBY_BOARD = {
    "organizationName": "Acme",
    "jobs": [
        {
            "id": "b6a6d1c0-1234-4abc-8def-0123456789ab",
            "title": "Product Designer",
            "department": "Design",
            "team": "Product Design",
            "location": "Remote",
            "isListed": True,
            "isRemote": True,
            "publishedAt": "2026-08-01T00:00:00.000Z",
            "jobUrl": "https://jobs.ashbyhq.com/acme/b6a6d1c0-1234-4abc-8def-0123456789ab",
            "descriptionHtml": "<p>Join our design team.</p>",
        },
        {
            "id": "aaaaaaaa-1111-2222-3333-444444444444",
            "title": "Data Scientist",
            "department": "Data",
            "team": "Data",
            "location": "New York, NY",
            "publishedAt": "2026-08-10T00:00:00.000Z",
            "jobUrl": "https://jobs.ashbyhq.com/acme/aaaaaaaa-1111-2222-3333-444444444444",
        },
    ],
}


def _net(ctx_factory: Callable[..., ProbeContext]) -> NetClient:
    return ctx_factory().net_client("resolve_posting")


@respx.mock
def test_fetch_board_parses_jobs_with_published_at(ctx_factory) -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(200, json=ASHBY_BOARD)
    )
    net = _net(ctx_factory)
    result = ashby.fetch_board(net, "acme")

    assert result.ok
    assert result.data is not None
    assert len(result.data) == 2
    job = ashby.find_job(result.data, "b6a6d1c0-1234-4abc-8def-0123456789ab")
    assert job is not None
    assert job.title == "Product Designer"
    assert job.team == "Product Design"
    assert job.published_at_dt is not None
    assert job.published_at_dt.isoformat() == "2026-08-01T00:00:00+00:00"
    assert job.content_hash is not None

    second = ashby.find_job(result.data, "aaaaaaaa-1111-2222-3333-444444444444")
    assert second is not None
    assert second.content_hash is None  # no descriptionHtml in this fixture


@respx.mock
def test_find_job_returns_none_when_absent(ctx_factory) -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(200, json=ASHBY_BOARD)
    )
    net = _net(ctx_factory)
    result = ashby.fetch_board(net, "acme")
    assert result.data is not None
    assert ashby.find_job(result.data, "no-such-id") is None


@respx.mock
def test_fetch_board_404_is_structured_failure(ctx_factory) -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/ghost-co").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    net = _net(ctx_factory)
    result = ashby.fetch_board(net, "ghost-co")

    assert result.ok is False
    assert result.status == 404
    assert result.data is None


@respx.mock
def test_fetch_board_malformed_body_is_ok_with_no_data(ctx_factory) -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(200, json={"jobs": "not-a-list"})
    )
    net = _net(ctx_factory)
    result = ashby.fetch_board(net, "acme")

    assert result.ok is True
    assert result.data is None
