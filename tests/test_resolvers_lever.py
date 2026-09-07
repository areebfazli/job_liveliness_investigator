"""`rli.resolvers.lever` adapter tests, against a compact realistic fixture.

The fixture shape (GUESSED — not verified against a live call; see the
module docstring in `rli/resolvers/lever.py`) follows the public schema at
https://github.com/lever/postings-api: a bare JSON array of postings with
id, text, categories{team,location}, hostedUrl, createdAt (epoch ms,
UNDOCUMENTED/untrusted per spec.md §3).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import respx

from rli.net import NetClient
from rli.probes.base import ProbeContext
from rli.resolvers import lever

LEVER_BOARD = [
    {
        "id": "c7b7e2d1-4321-4cba-9fed-fedcba987654",
        "text": "Staff Software Engineer",
        "categories": {"team": "Platform", "location": "Remote", "commitment": "Full-time"},
        "hostedUrl": "https://jobs.lever.co/acme/c7b7e2d1-4321-4cba-9fed-fedcba987654",
        "createdAt": 1755000000000,
        "descriptionPlain": "We are hiring a staff engineer.",
    },
    {
        "id": "deadbeef-0000-1111-2222-333344445555",
        "text": "Account Executive",
        "categories": {"team": "Sales", "location": "San Francisco, CA"},
        "hostedUrl": "https://jobs.lever.co/acme/deadbeef-0000-1111-2222-333344445555",
        "createdAt": 1750000000000,
    },
]


def _net(ctx_factory: Callable[..., ProbeContext]) -> NetClient:
    return ctx_factory().net_client("resolve_posting")


@respx.mock
def test_fetch_board_parses_jobs_without_treating_created_at_as_trusted(ctx_factory) -> None:
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(200, json=LEVER_BOARD)
    )
    net = _net(ctx_factory)
    result = lever.fetch_board(net, "acme")

    assert result.ok
    assert result.data is not None
    assert len(result.data) == 2
    job = lever.find_job(result.data, "c7b7e2d1-4321-4cba-9fed-fedcba987654")
    assert job is not None
    assert job.text == "Staff Software Engineer"
    assert job.team == "Platform"
    assert job.location == "Remote"
    # createdAt is parsed as a raw epoch-ms int, never surfaced as a
    # datetime/claim by this adapter (spec.md §3: untrusted).
    assert job.created_at_ms == 1755000000000
    assert not hasattr(job, "created_at")
    assert job.content_hash is not None

    second = lever.find_job(result.data, "deadbeef-0000-1111-2222-333344445555")
    assert second is not None
    assert second.content_hash is None


@respx.mock
def test_find_job_returns_none_when_absent(ctx_factory) -> None:
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(200, json=LEVER_BOARD)
    )
    net = _net(ctx_factory)
    result = lever.fetch_board(net, "acme")
    assert result.data is not None
    assert lever.find_job(result.data, "no-such-id") is None


@respx.mock
def test_fetch_board_transport_failure_is_retryable(ctx_factory) -> None:
    respx.get("https://api.lever.co/v0/postings/acme").mock(side_effect=httpx.ConnectError("boom"))
    net = _net(ctx_factory)
    result = lever.fetch_board(net, "acme")

    assert result.ok is False
    assert result.retryable is True
    assert result.data is None


@respx.mock
def test_fetch_board_malformed_body_is_ok_with_no_data(ctx_factory) -> None:
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(200, json={"not": "a list"})
    )
    net = _net(ctx_factory)
    result = lever.fetch_board(net, "acme")

    assert result.ok is True
    assert result.data is None
