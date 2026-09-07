"""`repost_history` probe: history gating, interval claims, version claims (spec.md §4)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from rli.probes.base import ProbeContext
from rli.probes.repost_history import (
    BOARD_HISTORY_URL_PLACEHOLDER,
    POSTING_SNAPSHOT_URL_PLACEHOLDER,
    RepostHistoryArgs,
    RepostHistoryProbe,
    repost_history,
)

COMPANY = "acme.com"
POSTING = "greenhouse:acme:J1"
JOB = "J1"
NOW = datetime(2026, 9, 7, tzinfo=UTC)
START = datetime(2026, 1, 1, tzinfo=UTC)


def _z(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _day(offset: int) -> datetime:
    return START + timedelta(days=offset)


def _company(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?, ?, ?, ?)",
        (COMPANY, "Acme", COMPANY, _z(START)),
    )


def _posting(conn: sqlite3.Connection, posting_id: str = POSTING, job_id: str = JOB) -> None:
    conn.execute(
        """
        INSERT INTO postings (posting_id, company_id, ats, ats_tenant_id, ats_job_id,
                              canonical_url, title, team, location, created_at, updated_at)
        VALUES (?, ?, 'greenhouse', 'acme', ?, ?, 'Backend Engineer', 'Engineering',
                'Remote', ?, ?)
        """,
        (
            posting_id,
            COMPANY,
            job_id,
            f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            _z(START),
            _z(START),
        ),
    )


def _capture(
    conn: sqlite3.Connection,
    captured_at: datetime,
    job_ids: list[str],
    *,
    source: str = "own",
    coverage_status: str = "complete",
    url: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status) "
        "VALUES (?, ?, ?, ?)",
        (COMPANY, _z(captured_at), source, coverage_status),
    )
    snapshot_id = int(cursor.lastrowid)
    for job_id in job_ids:
        conn.execute(
            "INSERT INTO board_snapshot_jobs "
            "(board_snapshot_id, job_id, title, team, location, description_hash, url) "
            "VALUES (?, ?, 'Backend Engineer', 'Engineering', 'Remote', 'h1', ?)",
            (snapshot_id, job_id, url),
        )
    return snapshot_id


def _snapshot(
    conn: sqlite3.Connection,
    captured_at: datetime,
    content_hash: str | None,
    *,
    source: str = "own",
    capture_url: str | None = None,
    posting_id: str = POSTING,
) -> None:
    conn.execute(
        "INSERT INTO posting_snapshots "
        "(posting_id, captured_at, source, status, content_hash, capture_url) "
        "VALUES (?, ?, ?, 'open', ?, ?)",
        (posting_id, _z(captured_at), source, content_hash, capture_url),
    )


def _ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    return ctx_factory(now=lambda: NOW)


# ---------------------------------------------------------------------------
# (a) thin history
# ---------------------------------------------------------------------------


def test_insufficient_history_returns_ok_with_no_claims(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    # Only a 10-day span: below thresholds.min_history_days (30).
    _capture(conn, _day(0), [JOB])
    _capture(conn, _day(10), [JOB])
    conn.commit()

    result = repost_history(POSTING, _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["usable_history"] is False
    assert result.data["history_days"] == 10.0
    assert result.data["evidence"] == []
    # Missing history is never reported as "nothing happened".
    assert "first_seen_absent" not in result.data


# ---------------------------------------------------------------------------
# (b) / (c) interval-derived claims
# ---------------------------------------------------------------------------


def test_disappearance_emits_claim_with_per_event_source_quality(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _capture(conn, _day(0), [JOB], source="own")
    _capture(conn, _day(20), [JOB], source="own")
    # The ABSENCE capture is an archive capture: the claim must be graded
    # 'archive' even though own captures also listed this job.
    _capture(conn, _day(40), [], source="archive")
    conn.commit()

    result = repost_history(POSTING, _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["usable_history"] is True
    assert result.data["first_seen_absent"] == _z(_day(40))
    assert result.data["closure_absent_at"] == _z(_day(40))
    assert result.data["censoring"] == "interval"
    assert result.data["gap_days"] == 20.0

    claims = result.data["evidence"]
    assert [c.claim_type for c in claims] == ["disappeared_interval"]
    claim = claims[0]
    assert claim.source_quality == "archive"
    assert claim.value == (
        f"last_seen_open={_z(_day(20))} first_seen_absent={_z(_day(40))}"
    )
    assert claim.available_at == _day(40)
    assert claim.source_event_at == _day(40)
    assert claim.fetched_at == NOW
    # No per-job url stored -> the documented self-describing placeholder.
    assert claim.source_url == BOARD_HISTORY_URL_PLACEHOLDER.format(
        company_id=COMPANY, job_id=JOB
    )


def test_own_capture_absence_is_graded_ats_native(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _capture(conn, _day(0), [JOB], source="archive")
    _capture(conn, _day(40), [], source="own")
    conn.commit()

    result = repost_history(POSTING, _ctx(ctx_factory))

    assert result.data is not None
    claim = result.data["evidence"][0]
    assert claim.claim_type == "disappeared_interval"
    assert claim.source_quality == "ats_native"


def test_reappearance_emits_reappeared_claim(conn: sqlite3.Connection, ctx_factory) -> None:
    _company(conn)
    _posting(conn)
    board_url = "https://boards.greenhouse.io/acme/jobs/J1"
    _capture(conn, _day(0), [JOB], source="own", url=board_url)
    _capture(conn, _day(20), [], source="archive")
    _capture(conn, _day(40), [JOB], source="own", url=board_url)
    conn.commit()

    result = repost_history(POSTING, _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["reappeared_at"] == _z(_day(40))
    # Still open at the end of the window -> right-censored, no closure bracket.
    assert result.data["censoring"] == "right"
    assert result.data["closure_absent_at"] is None

    by_type = {c.claim_type: c for c in result.data["evidence"]}
    assert set(by_type) == {"disappeared_interval", "reappeared"}
    assert by_type["disappeared_interval"].source_quality == "archive"
    reappeared = by_type["reappeared"]
    assert reappeared.source_quality == "ats_native"
    assert reappeared.available_at == _day(40)
    # A stored per-job url is preferred over the placeholder.
    assert reappeared.source_url == board_url


def test_job_never_seen_in_any_capture_is_not_an_error(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn, posting_id="greenhouse:acme:J9", job_id="J9")
    _posting(conn)
    _capture(conn, _day(0), [JOB])
    _capture(conn, _day(40), [JOB])
    conn.commit()

    result = repost_history("greenhouse:acme:J9", _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["usable_history"] is True
    assert result.data["first_seen_absent"] is None
    assert result.data["censoring"] is None
    assert result.data["evidence"] == []


# ---------------------------------------------------------------------------
# (d) version changes
# ---------------------------------------------------------------------------


def test_content_hash_changes_emit_version_change_claims(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _capture(conn, _day(0), [JOB])
    _capture(conn, _day(40), [JOB])
    _snapshot(conn, _day(0), "hash_a", source="own")
    _snapshot(conn, _day(10), "hash_a", source="own")
    _snapshot(
        conn,
        _day(20),
        "hash_b",
        source="archive",
        capture_url="https://web.archive.org/web/2026/x",
    )
    _snapshot(conn, _day(30), "hash_c", source="own")
    conn.commit()

    result = repost_history(POSTING, _ctx(ctx_factory))

    assert result.data is not None
    versions = result.data["versions"]
    assert [v["content_hash"] for v in versions] == ["hash_a", "hash_a", "hash_b", "hash_c"]

    changes = [c for c in result.data["evidence"] if c.claim_type == "version_change"]
    assert [c.value for c in changes] == ["hash_a->hash_b", "hash_b->hash_c"]
    assert changes[0].source_quality == "archive"
    assert changes[0].source_url == "https://web.archive.org/web/2026/x"
    assert changes[1].source_quality == "ats_native"
    assert changes[1].source_url == POSTING_SNAPSHOT_URL_PLACEHOLDER.format(
        posting_id=POSTING, captured_at=_z(_day(30))
    )
    assert changes[0].available_at == _day(20)
    assert changes[0].fetched_at == NOW


def test_null_content_hash_never_manufactures_a_version_change(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _capture(conn, _day(0), [JOB])
    _capture(conn, _day(40), [JOB])
    _snapshot(conn, _day(0), None)
    _snapshot(conn, _day(10), "hash_a")
    _snapshot(conn, _day(20), None)
    _snapshot(conn, _day(30), "hash_a")
    conn.commit()

    result = repost_history(POSTING, _ctx(ctx_factory))

    assert result.data is not None
    assert [c for c in result.data["evidence"] if c.claim_type == "version_change"] == []
    assert len(result.data["versions"]) == 4


# ---------------------------------------------------------------------------
# repost_links is surfaced as a fact, read-only
# ---------------------------------------------------------------------------


def test_best_repost_link_is_reported_without_writing(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _posting(conn, posting_id="greenhouse:acme:J2", job_id="J2")
    _capture(conn, _day(0), [JOB])
    _capture(conn, _day(40), [])
    components = {"scores": {"title": 1.0, "description": 1.0}}
    for new_posting, score in (("greenhouse:acme:J2", 0.91), (POSTING, 0.40)):
        conn.execute(
            "INSERT INTO repost_links (company_id, old_posting_id, new_posting_id, "
            "combined_score, component_scores, matched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (COMPANY, POSTING, new_posting, score, json.dumps(components), _z(NOW)),
        )
    conn.commit()

    result = repost_history(POSTING, _ctx(ctx_factory))

    assert result.data is not None
    link = result.data["repost_link"]
    assert link["new_posting_id"] == "greenhouse:acme:J2"
    assert link["combined_score"] == 0.91
    assert link["component_scores"] == components
    # The probe reports the link but never classifies repost_pattern itself.
    assert "repost_pattern" not in result.data
    # And it never writes: replacement_job_id is untouched.
    row = conn.execute(
        "SELECT replacement_job_id FROM postings WHERE posting_id = ?", (POSTING,)
    ).fetchone()
    assert row["replacement_job_id"] is None


def test_no_repost_link_reports_none(conn: sqlite3.Connection, ctx_factory) -> None:
    _company(conn)
    _posting(conn)
    _capture(conn, _day(0), [JOB])
    _capture(conn, _day(40), [JOB])
    conn.commit()

    result = repost_history(POSTING, _ctx(ctx_factory))
    assert result.data is not None
    assert result.data["repost_link"] is None


# ---------------------------------------------------------------------------
# (e) eligibility / (f) missing posting
# ---------------------------------------------------------------------------


def test_eligible_is_false_below_threshold_and_true_above(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _capture(conn, _day(0), [JOB])
    _capture(conn, _day(10), [JOB])
    conn.commit()
    ctx = _ctx(ctx_factory)
    args = RepostHistoryArgs(posting_id=POSTING)

    assert RepostHistoryProbe.eligible(ctx, args) is False

    _capture(conn, _day(40), [JOB])
    conn.commit()
    assert RepostHistoryProbe.eligible(ctx, args) is True


def test_eligible_is_false_for_unknown_posting(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    assert (
        RepostHistoryProbe.eligible(
            _ctx(ctx_factory), RepostHistoryArgs(posting_id="nope")
        )
        is False
    )


def test_missing_posting_is_a_structured_non_retryable_failure(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    result = repost_history("greenhouse:acme:missing", _ctx(ctx_factory))

    assert result.ok is False
    assert result.retryable is False
    assert "greenhouse:acme:missing" in (result.error or "")
    assert result.data == {"posting_id": "greenhouse:acme:missing", "evidence": []}


def test_probe_run_delegates_to_the_pure_function(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _posting(conn)
    _capture(conn, _day(0), [JOB])
    _capture(conn, _day(40), [])
    conn.commit()

    ctx = _ctx(ctx_factory)
    from_probe = RepostHistoryProbe().run(RepostHistoryArgs(posting_id=POSTING), ctx)
    direct = repost_history(POSTING, ctx)
    assert from_probe.data is not None and direct.data is not None
    assert from_probe.data["evidence"][0].value == direct.data["evidence"][0].value
    assert RepostHistoryProbe.cost_tier == "low"
    assert RepostHistoryProbe.history_required is True
    assert RepostHistoryProbe.populates == frozenset({"repost_pattern"})
