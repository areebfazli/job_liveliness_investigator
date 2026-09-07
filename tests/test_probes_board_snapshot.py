"""`board_snapshot` probe: current open jobs per ATS (spec.md §4)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import respx

from rli.probes.base import ProbeContext
from rli.probes.board_snapshot import (
    BoardSnapshotArgs,
    BoardSnapshotProbe,
    board_snapshot,
)

GH_BOARD = {
    "jobs": [
        {
            "id": 1,
            "title": "Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "departments": [{"name": "Engineering"}],
            "offices": [{"name": "Remote"}],
            "content": "desc",
        }
    ]
}

ASHBY_BOARD = {
    "jobs": [
        {
            "id": "b6a6d1c0-1234-4abc-8def-0123456789ab",
            "title": "Designer",
            "team": "Design",
            "location": "Remote",
            "publishedAt": "2026-08-01T00:00:00Z",
            "jobUrl": "https://jobs.ashbyhq.com/acme/b6a6d1c0-1234-4abc-8def-0123456789ab",
            "descriptionHtml": "<p>desc</p>",
        }
    ]
}

LEVER_BOARD = [
    {
        "id": "c7b7e2d1-4321-4cba-9fed-fedcba987654",
        "text": "Sales Rep",
        "categories": {"team": "Sales", "location": "SF"},
        "hostedUrl": "https://jobs.lever.co/acme/c7b7e2d1-4321-4cba-9fed-fedcba987654",
        "createdAt": 1755000000000,
        "descriptionPlain": "desc",
    }
]


def _ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    return ctx_factory()


@respx.mock
def test_greenhouse_board_snapshot(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD)
    )
    result = board_snapshot("greenhouse", "acme", _ctx(ctx_factory))

    assert result.ok is True
    jobs = result.data["jobs"]
    assert len(jobs) == 1
    assert jobs[0].job_id == "1"
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].team == "Engineering"
    assert jobs[0].location == "Remote"
    assert jobs[0].description_hash is not None


@respx.mock
def test_ashby_board_snapshot(ctx_factory) -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(200, json=ASHBY_BOARD)
    )
    result = board_snapshot("ashby", "acme", _ctx(ctx_factory))

    assert result.ok is True
    jobs = result.data["jobs"]
    assert len(jobs) == 1
    assert jobs[0].title == "Designer"
    assert jobs[0].team == "Design"


@respx.mock
def test_lever_board_snapshot(ctx_factory) -> None:
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(200, json=LEVER_BOARD)
    )
    result = board_snapshot("lever", "acme", _ctx(ctx_factory))

    assert result.ok is True
    jobs = result.data["jobs"]
    assert len(jobs) == 1
    assert jobs[0].title == "Sales Rep"
    assert jobs[0].team == "Sales"


@respx.mock
def test_board_snapshot_failure_returns_ok_false(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        side_effect=httpx.ConnectError("boom")
    )
    result = board_snapshot("greenhouse", "acme", _ctx(ctx_factory))

    assert result.ok is False
    assert result.retryable is True
    assert result.data["jobs"] == []


@respx.mock
def test_board_snapshot_404_is_a_failure_not_an_empty_board(ctx_factory) -> None:
    """A 404 on the board endpoint means the tenant/board is wrong or gone —
    it must not be silently reported as "zero open jobs"."""
    respx.get("https://boards-api.greenhouse.io/v1/boards/ghost/jobs").mock(
        return_value=httpx.Response(404, text="not found")
    )
    result = board_snapshot("greenhouse", "ghost", _ctx(ctx_factory))

    assert result.ok is False
    assert result.data["jobs"] == []


@respx.mock
def test_probe_class_run_delegates_to_function(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD)
    )
    probe = BoardSnapshotProbe()
    args = BoardSnapshotArgs(ats="greenhouse", tenant="acme")

    result = probe.run(args, _ctx(ctx_factory))

    assert result.ok is True
    assert len(result.data["jobs"]) == 1


def test_probe_metadata() -> None:
    assert BoardSnapshotProbe.name == "board_snapshot"
    assert BoardSnapshotProbe.cost_tier == "low"
    assert BoardSnapshotProbe.history_required is False
