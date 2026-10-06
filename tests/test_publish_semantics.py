"""What a publish date may be cited as (spec.md §3 source policy, §5 recency).

* Ashby `publishedAt` is documented as "when the job was LAST published"
  (https://developers.ashbyhq.com/docs/public-job-posting-api): a re-publish
  moves it. It must never become a `first_published` claim — not from the
  board capture, not from the replay builder — and it reaches the policy only
  as a refresh CANDIDATE, held to spec.md §5's refresh rule (it counts toward
  recency only when an observed content-hash change coincides with it).
* A `first_published` date LATER than our own `first_observed` cannot be the
  first publication. `rli.eval.case.guard_first_published` re-labels it, on
  the live path (every system's always-run evidence) and the replay path
  (`rli.replay.build.capture_date_claims`), for ATS and JSON-LD dates alike.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import respx
from test_eval_helpers import COMPANY, add_capture, add_posting, job

from rli.config import Config
from rli.eval.case import _refresh_claim, guard_first_published
from rli.eval.system_a import run_system_a
from rli.models.evidence import EvidenceItem
from rli.models.time import to_utc_z
from rli.policy.inputs import (
    CLAIM_FIRST_PUBLISHED,
    CLAIM_LAST_PUBLISHED,
    CLAIM_PUBLISH_AFTER_FIRST_SEEN,
    CLAIM_REFRESHED_AT,
)
from rli.probes.base import ProbeClaim
from rli.probes.board_snapshot import BoardJob
from rli.probes.resolve_posting import resolve_posting
from rli.replay.build import capture_date_claims

NOW = datetime(2026, 9, 7, tzinfo=UTC)
NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"


def _claim(
    value: str, event_at: datetime, *, claim_type: str = CLAIM_FIRST_PUBLISHED
) -> ProbeClaim:
    return ProbeClaim(
        claim_type=claim_type,
        value=value,
        source_url="https://boards.greenhouse.io/acme/jobs/1",
        source_quality="ats_native",
        source_event_at=event_at,
        available_at=NOW,
        fetched_at=NOW,
    )


# ---------------------------------------------------------------------------
# guard_first_published
# ---------------------------------------------------------------------------


def test_a_first_publish_after_our_first_sighting_is_relabelled_not_dropped() -> None:
    first_seen = datetime(2026, 8, 1, 12, tzinfo=UTC)
    later = _claim("2026-08-03T09:00:00Z", datetime(2026, 8, 3, 9, tzinfo=UTC))
    earlier = _claim("2026-07-20T09:00:00Z", datetime(2026, 7, 20, 9, tzinfo=UTC))
    same = _claim("2026-08-01T12:00:00Z", first_seen)
    other = _claim("x", datetime(2026, 9, 1, tzinfo=UTC), claim_type="updated_at")

    guarded = guard_first_published([later, earlier, same, other], first_seen)

    assert [c.claim_type for c in guarded] == [
        CLAIM_PUBLISH_AFTER_FIRST_SEEN,
        CLAIM_FIRST_PUBLISHED,
        CLAIM_FIRST_PUBLISHED,  # equal to the first sighting is consistent
        "updated_at",  # only first_published claims are guarded
    ]
    # Kept as evidence, with every other field untouched.
    assert guarded[0].model_dump(exclude={"claim_type"}) == later.model_dump(exclude={"claim_type"})


def test_without_a_first_sighting_nothing_is_guarded() -> None:
    claim = _claim("2026-08-03T09:00:00Z", datetime(2026, 8, 3, 9, tzinfo=UTC))
    assert guard_first_published([claim], None) == [claim]


def test_a_bare_date_is_held_to_day_precision() -> None:
    """`datePosted: "2026-10-02"` is a local calendar day, parsed as UTC midnight."""
    first_seen = datetime(2026, 10, 1, 13, 37, tzinfo=UTC)
    # 2026-10-02 can begin as early as 2026-10-01T10:00Z (UTC+14): consistent.
    next_day = _claim("2026-10-02", datetime(2026, 10, 2, tzinfo=UTC))
    # 2026-10-03 cannot begin before 2026-10-02T10:00Z: after the sighting.
    two_days = _claim("2026-10-03", datetime(2026, 10, 3, tzinfo=UTC))
    guarded = guard_first_published([next_day, two_days], first_seen)
    assert [c.claim_type for c in guarded] == [
        CLAIM_FIRST_PUBLISHED,
        CLAIM_PUBLISH_AFTER_FIRST_SEEN,
    ]


# ---------------------------------------------------------------------------
# Replay path: capture_date_claims
# ---------------------------------------------------------------------------


def _dated_job(job_id: str, **dates: str | None) -> BoardJob:
    return BoardJob(
        job_id=job_id,
        title="Backend Engineer",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        **dates,
    )


def test_an_ashby_capture_yields_last_published_never_first_published(
    conn: sqlite3.Connection,
) -> None:
    posting = add_posting(
        conn,
        job_id="ab-1",
        ats="ashby",
        tenant="acme",
        canonical_url="https://jobs.ashbyhq.com/acme/ab-1",
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=1),
    )
    add_capture(
        conn,
        NOW - timedelta(days=10),
        [_dated_job("ab-1", last_published="2026-08-25T00:00:00Z")],
    )

    claims = capture_date_claims(conn, posting, NOW)

    assert [c.claim_type for c in claims] == [CLAIM_LAST_PUBLISHED]
    (claim,) = claims
    assert claim.source_quality == "ats_native"
    assert claim.source_event_at == datetime(2026, 8, 25, tzinfo=UTC)
    assert claim.available_at == NOW - timedelta(days=10)


def test_replay_relabels_a_capture_first_publish_after_the_first_sighting(
    conn: sqlite3.Connection,
) -> None:
    posting = add_posting(
        conn,
        job_id="11",
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=1),
    )
    add_capture(conn, NOW - timedelta(days=60), [job("11")])
    add_capture(
        conn,
        NOW - timedelta(days=5),
        [_dated_job("11", first_published="2026-08-25T00:00:00Z", updated_at=None)],
    )
    claims = {c.claim_type: c for c in capture_date_claims(conn, posting, NOW)}
    assert CLAIM_FIRST_PUBLISHED not in claims
    assert claims[CLAIM_PUBLISH_AFTER_FIRST_SEEN].source_event_at == datetime(
        2026, 8, 25, tzinfo=UTC
    )


def test_replay_relabels_a_lever_page_date_after_the_first_sighting(
    conn: sqlite3.Connection,
) -> None:
    posting = add_posting(
        conn,
        job_id="lv-1",
        ats="lever",
        tenant="lev",
        canonical_url="https://jobs.lever.co/lev/lv-1",
        first_observed=NOW - timedelta(days=30),
        last_seen_open=NOW - timedelta(days=1),
    )
    fetched = NOW - timedelta(days=2)
    conn.execute(
        """
        INSERT INTO posting_page_dates
            (posting_id, page_url, status, date_posted_raw, date_posted, fetched_at,
             attempts, last_attempt_at)
        VALUES (?, 'https://jobs.lever.co/lev/lv-1', 'ok', '2026-09-01',
                '2026-09-01T00:00:00.000000Z', ?, 1, ?)
        """,
        (posting, to_utc_z(fetched), to_utc_z(fetched)),
    )
    conn.commit()
    (claim,) = capture_date_claims(conn, posting, NOW)
    assert claim.claim_type == CLAIM_PUBLISH_AFTER_FIRST_SEEN
    assert claim.source_quality == "page_structured"


# ---------------------------------------------------------------------------
# Refresh semantics for Ashby's "last published" date
# ---------------------------------------------------------------------------


def _last_published_evidence(stamp: datetime) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            id="e1",
            run_id="r",
            probe="resolve_posting",
            claim_type=CLAIM_LAST_PUBLISHED,
            value=to_utc_z(stamp),
            source_url="https://jobs.ashbyhq.com/acme/ab-1",
            source_quality="ats_native",
            source_event_at=stamp,
            available_at=NOW,
            fetched_at=NOW,
        )
    ]


def _ashby_history(conn: sqlite3.Connection, *, content_changes: bool) -> str:
    posting = add_posting(
        conn,
        job_id="ab-1",
        ats="ashby",
        tenant="acme",
        canonical_url="https://jobs.ashbyhq.com/acme/ab-1",
        first_observed=NOW - timedelta(days=90),
        last_seen_open=NOW - timedelta(days=1),
    )
    second_hash = "h2" if content_changes else "h1"
    add_capture(conn, NOW - timedelta(days=90), [job("ab-1", description_hash="h1")])
    add_capture(conn, NOW - timedelta(days=20), [job("ab-1", description_hash="h1")])
    add_capture(conn, NOW - timedelta(days=10), [job("ab-1", description_hash=second_hash)])
    return posting


def test_a_republish_with_an_observed_content_change_is_a_refresh(
    conn: sqlite3.Connection,
) -> None:
    posting = _ashby_history(conn, content_changes=True)
    refresh = _refresh_claim(
        conn,
        evidence=_last_published_evidence(NOW - timedelta(days=15)),
        posting_id=posting,
        company_id=COMPANY,
        ats_job_id="ab-1",
        refresh_match_days=3,
        now=NOW,
    )
    assert refresh is not None
    assert refresh.claim_type == CLAIM_REFRESHED_AT
    assert refresh.source_event_at == NOW - timedelta(days=15)
    # Not knowable before the capture that revealed the content change.
    assert refresh.available_at == NOW
    assert "last_published" in (refresh.raw_excerpt or "")


def test_a_bare_republish_never_makes_an_old_posting_recent(conn: sqlite3.Connection) -> None:
    posting = _ashby_history(conn, content_changes=False)
    assert (
        _refresh_claim(
            conn,
            evidence=_last_published_evidence(NOW - timedelta(days=15)),
            posting_id=posting,
            company_id=COMPANY,
            ats_job_id="ab-1",
            refresh_match_days=3,
            now=NOW,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Live path: every system's always-run evidence goes through the guard
# ---------------------------------------------------------------------------


def _mock_greenhouse(job_id: str, first_published: datetime) -> None:
    payload = {
        "id": int(job_id),
        "title": "Backend Engineer",
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "first_published": first_published.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "content": "<p>Build things.</p>",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
    }
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/acme/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    respx.get(f"https://boards.greenhouse.io/acme/jobs/{job_id}").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [payload]})
    )


def _seed(conn: sqlite3.Connection, job_id: str) -> None:
    add_posting(
        conn,
        job_id=job_id,
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=10),
    )
    for offset in (60, 45, 30, 10):
        add_capture(conn, NOW - timedelta(days=offset), [job(job_id)], company_id=COMPANY)


def _claim_types(conn: sqlite3.Connection, run_id: str) -> set[str]:
    return {
        row["claim_type"]
        for row in conn.execute("SELECT claim_type FROM evidence WHERE run_id = ?", (run_id,))
    }


@respx.mock
def test_live_run_does_not_cite_a_publish_date_after_the_first_sighting(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _seed(conn, "8101")
    _mock_greenhouse("8101", NOW - timedelta(days=5))  # first seen 60 days ago
    result = run_system_a(
        conn,
        cfg,
        "https://boards.greenhouse.io/acme/jobs/8101",
        now=NOW,
        sleep=lambda _s: None,
        use_tool_cache=False,
    )
    types = _claim_types(conn, result.run_id)
    assert CLAIM_FIRST_PUBLISHED not in types
    assert CLAIM_PUBLISH_AFTER_FIRST_SEEN in types
    # No primary publish evidence is left, so the date cannot make it "recent".
    assert result.decision.evidence_quality == "weak"
    assert result.decision.recommended_action != "apply_now"


@respx.mock
def test_live_run_keeps_a_publish_date_consistent_with_the_first_sighting(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _seed(conn, "8102")
    _mock_greenhouse("8102", NOW - timedelta(days=61))
    result = run_system_a(
        conn,
        cfg,
        "https://boards.greenhouse.io/acme/jobs/8102",
        now=NOW,
        sleep=lambda _s: None,
        use_tool_cache=False,
    )
    types = _claim_types(conn, result.run_id)
    assert CLAIM_FIRST_PUBLISHED in types
    assert CLAIM_PUBLISH_AFTER_FIRST_SEEN not in types


@respx.mock
def test_an_ashby_page_json_ld_date_is_last_published_too(ctx_factory) -> None:
    """Ashby renders `publishedAt` as the page's JSON-LD `datePosted`.

    Same calendar day on all 1,160 resolver runs that recorded both, so the
    page date is the same "last published" fact and must not slip back in as
    a `first_published` claim through the JSON-LD route.
    """
    job_id = "b6a6d1c0-1234-4abc-8def-0123456789ab"
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": job_id,
                        "title": "Designer",
                        "publishedAt": "2026-09-01T10:00:00.000Z",
                        "jobUrl": f"https://jobs.ashbyhq.com/acme/{job_id}",
                    }
                ]
            },
        )
    )
    respx.get(f"https://jobs.ashbyhq.com/acme/{job_id}").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<html><head><script type="application/ld+json">'
                '{"@context": "https://schema.org", "@type": "JobPosting", '
                '"title": "Designer", "datePosted": "2026-09-01"}'
                "</script></head><body></body></html>"
            ),
        )
    )
    result = resolve_posting(f"https://jobs.ashbyhq.com/acme/{job_id}", ctx_factory())
    claims = {(c.claim_type, c.source_quality) for c in result.data["evidence"]}
    assert claims == {
        (CLAIM_LAST_PUBLISHED, "ats_native"),
        (CLAIM_LAST_PUBLISHED, "page_structured"),
    }
