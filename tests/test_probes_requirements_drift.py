"""`requirements_drift` probe: live-vs-stored comparison + coarse diff (spec.md §4)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import respx

from rli.probes.base import ProbeContext
from rli.probes.requirements_drift import (
    RequirementsDriftArgs,
    RequirementsDriftProbe,
    requirements_drift,
)
from rli.resolvers.common import content_hash

COMPANY = "acme.com"
POSTING = "greenhouse:acme:J1"
JOB = "J1"
NOW = datetime(2026, 9, 7, tzinfo=UTC)
START = datetime(2026, 1, 1, tzinfo=UTC)

GH_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/J1"
LEVER_URL = "https://api.lever.co/v0/postings/acme"
ARCHIVE_URL = "https://web.archive.org/web/20260101/https://boards.greenhouse.io/acme/jobs/J1"

DESCRIPTION = "<p>We need 5 years of Python.</p>"
DESCRIPTION_HASH = content_hash(DESCRIPTION)


def _z(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _day(offset: int) -> datetime:
    return START + timedelta(days=offset)


def _gh_job(**overrides) -> dict:
    job = {
        "id": JOB,
        "title": "Backend Engineer",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/J1",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
        "content": DESCRIPTION,
    }
    job.update(overrides)
    return job


def _company(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?, ?, ?, ?)",
        (COMPANY, "Acme", COMPANY, _z(START)),
    )


def _posting(
    conn: sqlite3.Connection,
    *,
    ats: str = "greenhouse",
    tenant: str | None = "acme",
    title: str | None = "Backend Engineer",
    team: str | None = "Engineering",
    location: str | None = "Remote",
) -> None:
    conn.execute(
        """
        INSERT INTO postings (posting_id, company_id, ats, ats_tenant_id, ats_job_id,
                              canonical_url, title, team, location, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            POSTING,
            COMPANY,
            ats,
            tenant,
            JOB,
            "https://boards.greenhouse.io/acme/jobs/J1",
            title,
            team,
            location,
            _z(START),
            _z(START),
        ),
    )


def _capture(
    conn: sqlite3.Connection,
    captured_at: datetime,
    *,
    description_hash: str | None = DESCRIPTION_HASH,
    listed: bool = True,
) -> None:
    cursor = conn.execute(
        "INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status) "
        "VALUES (?, ?, 'own', 'complete')",
        (COMPANY, _z(captured_at)),
    )
    if listed:
        conn.execute(
            "INSERT INTO board_snapshot_jobs "
            "(board_snapshot_id, job_id, title, team, location, description_hash, url) "
            "VALUES (?, ?, 'Backend Engineer', 'Engineering', 'Remote', ?, NULL)",
            (int(cursor.lastrowid), JOB, description_hash),
        )


def _history(conn: sqlite3.Connection, *, days: int = 40, description_hash=DESCRIPTION_HASH):
    """Board history spanning `days` (>= min_history_days when days >= 30)."""
    _capture(conn, _day(0), description_hash=description_hash)
    _capture(conn, _day(days), description_hash=description_hash)


def _archive_snapshot(conn: sqlite3.Connection, capture_url: str | None = ARCHIVE_URL) -> None:
    conn.execute(
        "INSERT INTO posting_snapshots "
        "(posting_id, captured_at, source, status, content_hash, capture_url) "
        "VALUES (?, ?, 'archive', 'open', NULL, ?)",
        (POSTING, _z(_day(5)), capture_url),
    )


def _ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    return ctx_factory(now=lambda: NOW)


# ---------------------------------------------------------------------------
# history gating
# ---------------------------------------------------------------------------


def test_insufficient_history_returns_ok_with_no_claims(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _history(conn, days=5)
    conn.commit()

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["usable_history"] is False
    assert result.data["evidence"] == []
    assert result.data["description_changed"] is None


def test_missing_posting_is_a_structured_non_retryable_failure(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    result = requirements_drift("greenhouse:acme:missing", _ctx(ctx_factory))
    assert result.ok is False
    assert result.retryable is False


def test_eligible_mirrors_the_history_gate(conn: sqlite3.Connection, ctx_factory) -> None:
    _company(conn)
    _posting(conn)
    _history(conn, days=5)
    conn.commit()
    ctx = _ctx(ctx_factory)
    args = RequirementsDriftArgs(posting_id=POSTING)

    assert RequirementsDriftProbe.eligible(ctx, args) is False
    assert RequirementsDriftProbe.eligible(ctx, RequirementsDriftArgs(posting_id="x")) is False

    _capture(conn, _day(40))
    conn.commit()
    assert RequirementsDriftProbe.eligible(ctx, args) is True


# ---------------------------------------------------------------------------
# change detection
# ---------------------------------------------------------------------------


@respx.mock
def test_unchanged_posting_emits_requirements_unchanged(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _history(conn)
    conn.commit()
    respx.get(GH_URL).mock(return_value=httpx.Response(200, json=_gh_job()))

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["live_job_found"] is True
    assert result.data["title_changed"] is False
    assert result.data["team_changed"] is False
    assert result.data["location_changed"] is False
    assert result.data["description_changed"] is False

    claims = result.data["evidence"]
    assert len(claims) == 1
    assert claims[0].claim_type == "requirements_unchanged"
    assert claims[0].value == "no changes detected"
    assert claims[0].source_quality == "ats_native"
    assert claims[0].source_url == "https://boards.greenhouse.io/acme/jobs/J1"
    assert claims[0].available_at == NOW


@respx.mock
def test_title_team_location_and_description_changes_are_detected(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _history(conn)
    conn.commit()
    respx.get(GH_URL).mock(
        return_value=httpx.Response(
            200,
            json=_gh_job(
                title="Staff Backend Engineer",
                departments=[{"name": "Platform"}],
                offices=[{"name": "New York"}],
                content="<p>We need 8 years of Python.</p>",
            ),
        )
    )

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["title_changed"] is True
    assert result.data["team_changed"] is True
    assert result.data["location_changed"] is True
    assert result.data["description_changed"] is True

    claim = result.data["evidence"][0]
    assert claim.claim_type == "requirements_changed"
    assert json.loads(claim.value) == {
        "title_changed": True,
        "team_changed": True,
        "location_changed": True,
        "description_changed": True,
    }


@respx.mock
def test_formatting_only_title_difference_is_not_drift(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn, title="Backend Engineer (Remote)")
    _history(conn)
    conn.commit()
    respx.get(GH_URL).mock(
        return_value=httpx.Response(200, json=_gh_job(title="backend engineer, remote"))
    )

    result = requirements_drift(POSTING, _ctx(ctx_factory))
    assert result.data is not None
    assert result.data["title_changed"] is False


@respx.mock
def test_missing_prior_description_hash_is_unknown_not_changed(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _history(conn, description_hash=None)
    conn.commit()
    respx.get(GH_URL).mock(return_value=httpx.Response(200, json=_gh_job()))

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["prior_description_hash"] is None
    assert result.data["description_changed"] is None
    # The other, comparable fields still support a claim.
    assert result.data["evidence"][0].claim_type == "requirements_unchanged"
    assert json.loads(result.data["evidence"][0].raw_excerpt)["description_changed"] is None


@respx.mock
def test_missing_live_description_hash_is_unknown_not_changed(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _history(conn)
    conn.commit()
    respx.get(GH_URL).mock(return_value=httpx.Response(200, json=_gh_job(content=None)))

    result = requirements_drift(POSTING, _ctx(ctx_factory))
    assert result.data is not None
    assert result.data["live_description_hash"] is None
    assert result.data["description_changed"] is None


def test_prior_hash_skips_captures_that_carry_no_hash(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn, ats="other", tenant=None)
    _capture(conn, _day(0), description_hash=DESCRIPTION_HASH)
    _capture(conn, _day(40), description_hash=None)
    conn.commit()

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["prior_description_hash"] == DESCRIPTION_HASH
    assert result.data["prior_description_captured_at"] == _z(_day(0))


# ---------------------------------------------------------------------------
# degraded / non-comparable cases
# ---------------------------------------------------------------------------


def test_ats_without_an_adapter_is_not_a_failure_and_emits_no_claim(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn, ats="other", tenant=None)
    _history(conn)
    conn.commit()

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["live_fetch"] == "no_adapter"
    # Nothing was comparable -> no "unchanged" claim is manufactured.
    assert result.data["evidence"] == []


@respx.mock
def test_job_removed_from_ats_is_ok_with_no_live_job(conn: sqlite3.Connection, ctx_factory) -> None:
    _company(conn)
    _posting(conn)
    _history(conn)
    conn.commit()
    respx.get(GH_URL).mock(return_value=httpx.Response(404, text="not found"))

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["live_fetch"] == "not_found"
    assert result.data["live_job_found"] is False
    assert result.data["evidence"] == []


@respx.mock
def test_transport_failure_sets_ok_false_and_emits_no_claim(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _history(conn)
    conn.commit()
    respx.get(GH_URL).mock(return_value=httpx.Response(503, text="unavailable"))

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.ok is False
    assert result.retryable is True
    assert result.data is not None
    assert result.data["live_fetch"] == "fetch_failed"
    assert result.data["evidence"] == []


# ---------------------------------------------------------------------------
# lever (board listing, no single-job endpoint) + archive line diff
# ---------------------------------------------------------------------------


@respx.mock
def test_lever_board_listing_supplies_the_live_job(conn: sqlite3.Connection, ctx_factory) -> None:
    _company(conn)
    _posting(conn, ats="lever", title="Backend Engineer", team="Engineering", location="Remote")
    _history(conn, description_hash=content_hash("plain text description"))
    conn.commit()
    respx.get(LEVER_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": JOB,
                    "text": "Backend Engineer",
                    "categories": {"team": "Engineering", "location": "Remote"},
                    "hostedUrl": "https://jobs.lever.co/acme/J1",
                    "descriptionPlain": "plain text description",
                }
            ],
        )
    )

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["live_fetch"] == "found"
    assert result.data["description_changed"] is False
    assert result.data["evidence"][0].source_url == "https://jobs.lever.co/acme/J1"


@respx.mock
def test_lever_job_absent_from_the_board_is_not_found(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn, ats="lever")
    _history(conn)
    conn.commit()
    respx.get(LEVER_URL).mock(return_value=httpx.Response(200, json=[]))

    result = requirements_drift(POSTING, _ctx(ctx_factory))
    assert result.ok is True
    assert result.data is not None
    assert result.data["live_fetch"] == "not_found"


@respx.mock
def test_archive_body_produces_a_coarse_line_diff_that_does_not_decide_the_claim(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _history(conn)
    _archive_snapshot(conn)
    conn.commit()
    respx.get(GH_URL).mock(return_value=httpx.Response(200, json=_gh_job()))
    respx.get(ARCHIVE_URL).mock(
        return_value=httpx.Response(
            200,
            text="<html>\n<nav>archive banner</nav>\n<p>We need 5 years of Python.</p>\n</html>",
        )
    )

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["archive_fetch"] == "found"
    diff = result.data["line_diff"]
    assert diff is not None
    assert diff["comparable"] is False
    assert diff["removed_lines"] >= 1
    assert len(diff["added_sample"]) <= 10
    # Everything comparable matched, so the diff noise did NOT flip the claim.
    assert result.data["evidence"][0].claim_type == "requirements_unchanged"


@respx.mock
def test_archive_capture_url_outside_the_allowlist_never_raises(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _history(conn)
    _archive_snapshot(conn, capture_url="https://evil.example.com/capture")
    conn.commit()
    respx.get(GH_URL).mock(return_value=httpx.Response(200, json=_gh_job()))

    result = requirements_drift(POSTING, _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["archive_fetch"] == "disallowed_host"
    assert result.data["line_diff"] is None


@respx.mock
def test_probe_run_delegates_and_declares_its_spec_metadata(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _history(conn)
    conn.commit()
    respx.get(GH_URL).mock(return_value=httpx.Response(200, json=_gh_job()))

    result = RequirementsDriftProbe().run(
        RequirementsDriftArgs(posting_id=POSTING), _ctx(ctx_factory)
    )
    assert result.ok is True
    assert RequirementsDriftProbe.cost_tier == "medium"
    assert RequirementsDriftProbe.history_required is True
    assert RequirementsDriftProbe.populates == frozenset({"repost_pattern"})
