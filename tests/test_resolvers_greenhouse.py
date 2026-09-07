"""`rli.resolvers.greenhouse` adapter tests, against a compact realistic fixture.

The fixture shape (GUESSED — not verified against a live call; see the
module docstring in `rli/resolvers/greenhouse.py`) follows Greenhouse's
publicly documented job-board fields: id, title, absolute_url,
first_published, updated_at, departments[], offices[], location, content.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import respx

from rli.net import NetClient
from rli.probes.base import ProbeContext
from rli.resolvers import greenhouse

GH_JOB = {
    "id": 5762900002,
    "title": "Senior Backend Engineer",
    "updated_at": "2026-08-25T09:12:00-05:00",
    "location": {"name": "Remote - US"},
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/5762900002",
    "internal_job_id": 123456,
    "requisition_id": "REQ-2026-014",
    "first_published": "2026-08-20T10:00:00-05:00",
    "content": "<p>We are looking for a senior backend engineer.</p>",
    "departments": [{"id": 1, "name": "Engineering"}],
    "offices": [{"id": 1, "name": "Remote - US"}],
}

GH_JOB_NO_LOCATION_FIELD = {
    "id": 999,
    "title": "Support Engineer",
    "updated_at": "2026-08-01T00:00:00Z",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/999",
    "first_published": "2026-07-15T00:00:00Z",
    "departments": [{"id": 2, "name": "Support"}],
    "offices": [{"id": 3, "name": "Remote - EMEA"}],
}


def _net(ctx_factory: Callable[..., ProbeContext]) -> NetClient:
    return ctx_factory().net_client("resolve_posting")


@respx.mock
def test_fetch_job_parses_ats_native_dates_and_content_hash(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5762900002").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    net = _net(ctx_factory)
    result = greenhouse.fetch_job(net, "acme", "5762900002")

    assert result.ok
    assert result.status == 200
    job = result.data
    assert job is not None
    assert job.id == "5762900002"
    assert job.title == "Senior Backend Engineer"
    assert job.absolute_url == "https://boards.greenhouse.io/acme/jobs/5762900002"
    assert job.departments == ["Engineering"]
    assert job.location == "Remote - US"
    assert job.first_published_at is not None
    assert job.first_published_at.isoformat() == "2026-08-20T15:00:00+00:00"
    assert job.updated_at_dt is not None
    assert job.content_hash is not None


@respx.mock
def test_fetch_job_falls_back_to_offices_when_location_field_absent(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/999").mock(
        return_value=httpx.Response(200, json=GH_JOB_NO_LOCATION_FIELD)
    )
    net = _net(ctx_factory)
    result = greenhouse.fetch_job(net, "acme", "999")

    assert result.ok
    assert result.data is not None
    assert result.data.location == "Remote - EMEA"
    assert result.data.content_hash is None  # no `content` field in this fixture


@respx.mock
def test_fetch_job_404_is_structured_failure_not_exception(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/000").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    net = _net(ctx_factory)
    result = greenhouse.fetch_job(net, "acme", "000")

    assert result.ok is False
    assert result.status == 404
    assert result.data is None


@respx.mock
def test_fetch_job_malformed_json_body_is_ok_with_no_data(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/1").mock(
        return_value=httpx.Response(200, text="not json")
    )
    net = _net(ctx_factory)
    result = greenhouse.fetch_job(net, "acme", "1")

    assert result.ok is True
    assert result.data is None


@respx.mock
def test_fetch_board_lists_jobs(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [GH_JOB, GH_JOB_NO_LOCATION_FIELD]})
    )
    net = _net(ctx_factory)
    result = greenhouse.fetch_board(net, "acme")

    assert result.ok
    assert result.data is not None
    assert [job.id for job in result.data] == ["5762900002", "999"]
    assert greenhouse.find_job(result.data, "999") is not None
    assert greenhouse.find_job(result.data, "no-such-id") is None


@respx.mock
def test_fetch_board_transport_failure_is_retryable(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        side_effect=httpx.ConnectError("boom")
    )
    net = _net(ctx_factory)
    result = greenhouse.fetch_board(net, "acme")

    assert result.ok is False
    assert result.retryable is True
    assert result.data is None
