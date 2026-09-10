"""`resolve_posting` probe: end-to-end with mocked ATS + JSON-LD (spec.md §4)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import respx

from rli.probes.base import ProbeContext
from rli.probes.resolve_posting import ResolvePostingArgs, ResolvePostingProbe, resolve_posting

NOW = datetime(2026, 9, 7, tzinfo=UTC)

GH_JOB = {
    "id": 5762900002,
    "title": "Senior Backend Engineer",
    "updated_at": "2026-08-25T09:12:00-05:00",
    "location": {"name": "Remote - US"},
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/5762900002",
    "first_published": "2026-08-20T10:00:00-05:00",
    "content": "<p>We are looking for a senior backend engineer.</p>",
    "departments": [{"id": 1, "name": "Engineering"}],
    "offices": [{"id": 1, "name": "Remote - US"}],
}

NO_JSONLD_PAGE = "<html><body>no structured data here</body></html>"

JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Senior Backend Engineer", "datePosted": "2026-08-20",
 "validThrough": "2026-09-20T00:00:00Z",
 "hiringOrganization": {"sameAs": "https://acme.com"}}
</script>
</head></html>
"""

ASHBY_BOARD = {
    "jobs": [
        {
            "id": "b6a6d1c0-1234-4abc-8def-0123456789ab",
            "title": "Product Designer",
            "team": "Design",
            "location": "Remote",
            "publishedAt": "2026-08-01T00:00:00.000Z",
            "jobUrl": "https://jobs.ashbyhq.com/acme/b6a6d1c0-1234-4abc-8def-0123456789ab",
        }
    ]
}

LEVER_BOARD = [
    {
        "id": "c7b7e2d1-4321-4cba-9fed-fedcba987654",
        "text": "Staff Software Engineer",
        "categories": {"team": "Platform", "location": "Remote"},
        "hostedUrl": "https://jobs.lever.co/acme/c7b7e2d1-4321-4cba-9fed-fedcba987654",
        "createdAt": 1755000000000,
    }
]


def _ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    return ctx_factory(now=lambda: NOW)


@respx.mock
def test_greenhouse_open_emits_ats_native_evidence(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5762900002").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5762900002").mock(
        return_value=httpx.Response(200, text=JSONLD_PAGE)
    )

    result = resolve_posting("https://boards.greenhouse.io/acme/jobs/5762900002", _ctx(ctx_factory))

    assert result.ok is True
    assert result.data["posting_state"] == "open"
    assert result.data["ats"] == "greenhouse"
    assert result.data["tenant"] == "acme"
    assert result.data["title"] == "Senior Backend Engineer"
    assert result.data["company_domain"] == "acme.com"

    claim_types = [c.claim_type for c in result.data["evidence"]]
    assert "first_published" in claim_types
    assert "updated_at" in claim_types
    assert "declared_expiry" in claim_types
    ats_native_claims = [c for c in result.data["evidence"] if c.source_quality == "ats_native"]
    assert any(c.claim_type == "first_published" for c in ats_native_claims)
    for claim in result.data["evidence"]:
        assert claim.available_at == NOW
        assert claim.fetched_at == NOW


@respx.mock
def test_greenhouse_404_is_closed_not_a_failure(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/000").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/000").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    result = resolve_posting("https://boards.greenhouse.io/acme/jobs/000", _ctx(ctx_factory))

    assert result.ok is True
    assert result.data["posting_state"] == "closed"


@respx.mock
def test_ats_transport_failure_is_unknown_and_ok_false_retryable(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/1").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/1").mock(
        side_effect=httpx.ConnectError("boom")
    )

    result = resolve_posting("https://boards.greenhouse.io/acme/jobs/1", _ctx(ctx_factory))

    assert result.ok is False
    assert result.retryable is True
    assert result.data["posting_state"] == "unknown"
    # a failed fetch must never be reported as closed (spec.md §4)
    assert result.data["posting_state"] != "closed"


@respx.mock
def test_ashby_job_found_is_open_with_ats_native_evidence(ctx_factory) -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(200, json=ASHBY_BOARD)
    )
    respx.get("https://jobs.ashbyhq.com/acme/b6a6d1c0-1234-4abc-8def-0123456789ab").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )

    result = resolve_posting(
        "https://jobs.ashbyhq.com/acme/b6a6d1c0-1234-4abc-8def-0123456789ab", _ctx(ctx_factory)
    )

    assert result.ok is True
    assert result.data["posting_state"] == "open"
    assert result.data["title"] == "Product Designer"
    claims = result.data["evidence"]
    assert len(claims) == 1
    assert claims[0].claim_type == "first_published"
    assert claims[0].source_quality == "ats_native"


@respx.mock
def test_ashby_job_absent_from_board_is_closed(ctx_factory) -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(200, json=ASHBY_BOARD)
    )
    missing_id = "00000000-0000-0000-0000-000000000000"
    respx.get(f"https://jobs.ashbyhq.com/acme/{missing_id}").mock(
        return_value=httpx.Response(404, text="gone")
    )

    result = resolve_posting(f"https://jobs.ashbyhq.com/acme/{missing_id}", _ctx(ctx_factory))

    assert result.ok is True
    assert result.data["posting_state"] == "closed"
    assert result.data["evidence"] == []


@respx.mock
def test_lever_job_found_emits_board_listing_not_a_publish_date(ctx_factory) -> None:
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(200, json=LEVER_BOARD)
    )
    respx.get("https://jobs.lever.co/acme/c7b7e2d1-4321-4cba-9fed-fedcba987654").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )

    result = resolve_posting(
        "https://jobs.lever.co/acme/c7b7e2d1-4321-4cba-9fed-fedcba987654", _ctx(ctx_factory)
    )

    assert result.ok is True
    assert result.data["posting_state"] == "open"
    claims = result.data["evidence"]
    assert len(claims) == 1
    assert claims[0].claim_type == "board_listing"
    # Lever's date fields are undocumented/untrusted (spec.md §3): never a
    # first_published claim, and the raw createdAt is only in raw_excerpt.
    assert not any(c.claim_type == "first_published" for c in claims)
    assert "1755000000000" in (claims[0].raw_excerpt or "")


@respx.mock
def test_generic_ats_uses_jsonld_as_only_signal_open(ctx_factory) -> None:
    respx.get("https://careers.acme.com/jobs/1").mock(
        return_value=httpx.Response(200, text=JSONLD_PAGE)
    )

    result = resolve_posting("https://careers.acme.com/jobs/1", _ctx(ctx_factory))

    assert result.ok is True
    assert result.data["ats"] == "generic"
    assert result.data["posting_state"] == "open"
    assert result.data["company_domain"] == "acme.com"


@respx.mock
def test_generic_ats_no_jsonld_is_unknown_but_ok(ctx_factory) -> None:
    respx.get("https://careers.acme.com/jobs/1").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )

    result = resolve_posting("https://careers.acme.com/jobs/1", _ctx(ctx_factory))

    assert result.ok is True
    assert result.data["posting_state"] == "unknown"


@respx.mock
def test_generic_ats_fetch_failure_is_unknown_and_ok_false(ctx_factory) -> None:
    respx.get("https://careers.acme.com/jobs/1").mock(side_effect=httpx.ConnectError("boom"))

    result = resolve_posting("https://careers.acme.com/jobs/1", _ctx(ctx_factory))

    assert result.ok is False
    assert result.retryable is True
    assert result.data["posting_state"] == "unknown"


def test_invalid_url_is_structured_non_retryable_failure(ctx_factory) -> None:
    result = resolve_posting("not a url", _ctx(ctx_factory))

    assert result.ok is False
    assert result.retryable is False
    assert result.data["posting_state"] == "unknown"


@respx.mock
def test_probe_class_run_delegates_to_function(ctx_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5762900002").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5762900002").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    probe = ResolvePostingProbe()
    args = ResolvePostingArgs(url="https://boards.greenhouse.io/acme/jobs/5762900002")

    result = probe.run(args, _ctx(ctx_factory))

    assert result.ok is True
    assert result.data["posting_state"] == "open"


def test_probe_metadata() -> None:
    assert ResolvePostingProbe.name == "resolve_posting"
    assert ResolvePostingProbe.cost_tier == "low"
    assert ResolvePostingProbe.history_required is False
    assert ResolvePostingProbe.ArgsModel is ResolvePostingArgs
