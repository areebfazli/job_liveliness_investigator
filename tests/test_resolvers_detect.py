"""`detect_ats` classification (spec.md §3; PLAN.md M1)."""

from __future__ import annotations

import pytest

from rli.resolvers.detect import AtsRef, detect_ats

GREENHOUSE_CASES = [
    "https://boards.greenhouse.io/acme/jobs/5762900002",
    "https://boards.greenhouse.io/acme/jobs/5762900002/",
    "https://job-boards.greenhouse.io/acme/jobs/5762900002",
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs/5762900002",
]

ASHBY_UUID = "b6a6d1c0-1234-4abc-8def-0123456789ab"
LEVER_UUID = "c7b7e2d1-4321-4cba-9fed-fedcba987654"


@pytest.mark.parametrize("url", GREENHOUSE_CASES)
def test_detect_greenhouse(url: str) -> None:
    ref = detect_ats(url)
    assert ref == AtsRef(
        ats="greenhouse",
        tenant="acme",
        job_id="5762900002",
        canonical_url="https://boards.greenhouse.io/acme/jobs/5762900002",
    )


def test_detect_ashby() -> None:
    ref = detect_ats(f"https://jobs.ashbyhq.com/acme/{ASHBY_UUID}")
    assert ref == AtsRef(
        ats="ashby",
        tenant="acme",
        job_id=ASHBY_UUID,
        canonical_url=f"https://jobs.ashbyhq.com/acme/{ASHBY_UUID}",
    )


def test_detect_lever() -> None:
    ref = detect_ats(f"https://jobs.lever.co/acme/{LEVER_UUID}")
    assert ref == AtsRef(
        ats="lever",
        tenant="acme",
        job_id=LEVER_UUID,
        canonical_url=f"https://jobs.lever.co/acme/{LEVER_UUID}",
    )


def test_detect_lever_tolerates_trailing_path_segment() -> None:
    """Lever's `hostedUrl` sometimes carries extra path segments after the id."""
    ref = detect_ats(f"https://jobs.lever.co/acme/{LEVER_UUID}/apply")
    assert ref is not None
    assert ref.ats == "lever"
    assert ref.job_id == LEVER_UUID


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.acme.com/jobs/123",
        "https://acme.com/careers/senior-engineer",
        "https://www.linkedin.com/jobs/view/12345",
    ],
)
def test_detect_generic_fallback(url: str) -> None:
    ref = detect_ats(url)
    assert ref == AtsRef(ats="generic", tenant=None, job_id=None, canonical_url=url)


def test_detect_ashby_host_with_non_matching_path_is_generic() -> None:
    """A recognized ATS host that doesn't match the known path shape still
    resolves rather than raising — it just isn't classified as that ATS."""
    url = "https://jobs.ashbyhq.com/acme"
    ref = detect_ats(url)
    assert ref == AtsRef(ats="generic", tenant=None, job_id=None, canonical_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "ftp://boards.greenhouse.io/acme/jobs/1",
        "https://",
        "javascript:alert(1)",
    ],
)
def test_detect_returns_none_for_unparseable_url(url: str) -> None:
    assert detect_ats(url) is None
