"""`rli.eval.case`: the always-run pair, identity resolution, and the
evidence-claim contract it owes (spec.md §2/§4; module docstring of
`rli.eval.case`).

Every test builds its own `Run` + `ProbeRunner` (via `open_probe_runner`)
rather than going through `run_system_a`/`run_system_b`, so `build_case_state`
and `extend_case_state` are exercised directly and in isolation from the
routing/policy layers those two systems add on top.
"""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from test_eval_helpers import COMPANY, add_capture, add_posting, evidence_rows, job

from rli.config import Config
from rli.eval.case import (
    BOARD_SNAPSHOT_URL_PLACEHOLDER,
    CLAIM_BOARD_PRESENT,
    REFRESH_MATCH_PROBE,
    build_case_state,
    extend_case_state,
)
from rli.eval.runner import STEP_PROBE_SKIPPED, ProbeRunner, Run
from rli.events.store import (
    CollectionStatus,
    CompanyEvent,
    upsert_event,
    write_collection_status_csv,
)
from rli.models.policy_inputs import UNKNOWN
from rli.models.time import to_utc_z
from rli.policy.action import decide
from rli.policy.inputs import (
    CLAIM_BOARD_ABSENT,
    CLAIM_POSTING_STATE,
    CLAIM_REFRESHED_AT,
    could_change_action,
    last_publish_or_refresh,
)
from rli.policy.quality import evidence_quality_detail
from rli.probes.company_events import CompanyEventsProbe

NOW = datetime(2026, 9, 7, tzinfo=UTC)
REFRESH_MATCH_DAYS = 3  # cfg.thresholds.refresh_match_days default (config.toml)

GH_JOB = {
    "id": 5001,
    "title": "Backend Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/5001",
    "first_published": "2026-08-25T00:00:00Z",
    "content": "<p>desc</p>",
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "Remote"}],
}

NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"

GH_BOARD_WITH_JOB = {"jobs": [GH_JOB]}
GH_BOARD_EMPTY = {"jobs": []}


@contextmanager
def _run_and_probes(
    conn: sqlite3.Connection, cfg: Config, *, system: str = "A", now: datetime = NOW
) -> Iterator[tuple[Run, ProbeRunner]]:
    """Build a `Run` + `ProbeRunner` pair the way `run_system_a`/`b` do.

    `use_tool_cache=False` so a respx-mocked fetch can never be answered
    from a stored `tool_cache` row (see the eval runner's docstring); `sleep`
    is a no-op so a retry path never actually waits. `now` defaults to the
    module `NOW` but is overridable so a refresh-match test can put the run
    clock BEFORE a detecting capture (see the "available_at is a max" tests
    below) without disturbing every other test in this file.
    """
    from rli.eval.runner import open_probe_runner

    with Run(
        conn,
        cfg,
        input_url="test://case",
        system=system,  # type: ignore[arg-type]
        config_hash="cfg:test",
        started_at=now,
    ) as run:
        with open_probe_runner(
            conn, cfg, run, now, sleep=lambda _s: None, use_tool_cache=False
        ) as probes:
            yield run, probes


def _gh_job(job_id: str, **overrides: object) -> dict:
    """A Greenhouse single-job payload, overridable per test.

    Separate from the module-level `GH_JOB` fixture (which every existing
    test above pins by value) so a refresh-match test can freely set
    `updated_at` / `first_published` / `id` without perturbing them.
    """
    payload: dict[str, object] = {
        "id": job_id,
        "title": "Backend Engineer",
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "content": "<p>desc</p>",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
    }
    payload.update(overrides)
    return payload


def _mock_greenhouse(job_id: str, gh_job: dict) -> None:
    """Mock the three always-run fetches for one Greenhouse job (respx)."""
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/acme/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json=gh_job)
    )
    respx.get(f"https://boards.greenhouse.io/acme/jobs/{job_id}").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [gh_job]})
    )


def _posting_snapshot(
    conn: sqlite3.Connection,
    posting_id: str,
    captured_at: datetime,
    *,
    content_hash: str | None,
    source: str = "own",
    status: str = "open",
) -> None:
    """Raw-SQL `posting_snapshots` row — the per-posting capture table.

    No helper for this exists in `test_eval_helpers` (only the board-capture
    route, via `add_capture`, is covered there); the pattern mirrors
    `tests/test_probes_requirements_drift.py`'s `_archive_snapshot`, which
    inserts into the same table the same way.
    """
    conn.execute(
        "INSERT INTO posting_snapshots (posting_id, captured_at, source, status, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (posting_id, to_utc_z(captured_at), source, status, content_hash),
    )
    conn.commit()


@respx.mock
def test_build_case_state_emits_posting_state_and_board_present_claims(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="5001", first_observed=NOW, last_seen_open=NOW)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5001").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD_WITH_JOB)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )

    assert case.posting_id == "greenhouse:acme:5001"
    assert case.posting_row_exists is True
    assert case.company_id == COMPANY

    # The contract in rli.eval.case's module docstring: these two claim
    # types are synthesized by THIS module, not by any probe.
    state_claims = [c for c in case.evidence if c.claim_type == CLAIM_POSTING_STATE]
    assert len(state_claims) == 1
    assert state_claims[0].probe == "resolve_posting"
    assert state_claims[0].value == "open"
    assert state_claims[0].source_quality == "ats_native"

    present_claims = [c for c in case.evidence if c.claim_type == CLAIM_BOARD_PRESENT]
    assert len(present_claims) == 1
    assert present_claims[0].probe == "board_snapshot"
    assert present_claims[0].source_quality == "ats_native"


@respx.mock
def test_build_case_state_emits_board_absent_when_job_missing_from_listing(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="5001", first_observed=NOW, last_seen_open=NOW)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5001").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    # The board listing does NOT include job 5001.
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD_EMPTY)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )

    absent_claims = [c for c in case.evidence if c.claim_type == CLAIM_BOARD_ABSENT]
    assert len(absent_claims) == 1
    absent = absent_claims[0]
    assert absent.probe == "board_snapshot"
    assert absent.source_quality == "ats_native"
    # An absence has no fetchable per-job URL: the self-describing placeholder.
    assert absent.source_url == BOARD_SNAPSHOT_URL_PLACEHOLDER.format(
        ats="greenhouse", tenant="acme"
    )


@respx.mock
def test_build_case_state_prefers_collected_row_team_and_location(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """Team/location come from the collected `postings` row, not the resolver."""
    add_posting(
        conn,
        job_id="5001",
        team="Platform",
        location="NYC",
        first_observed=NOW,
        last_seen_open=NOW,
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5001").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD_WITH_JOB)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )

    assert case.team == "Platform"
    assert case.location == "NYC"
    # Title prefers the freshly-fetched resolver value.
    assert case.title == "Backend Engineer"


@respx.mock
def test_generic_ats_unresolved_identity_yields_no_case_file(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A `generic` posting with no JSON-LD company domain resolves no identity."""
    respx.get("https://careers.acme.com/jobs/1").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://careers.acme.com/jobs/1", now=NOW, probes=probes
        )

    assert case.posting_id is None
    assert case.company_id is None
    assert case.case_file() is None

    # board_snapshot has no endpoint for a generic ATS: recorded, not run.
    skipped = [
        step
        for step in conn.execute(
            "SELECT * FROM run_steps WHERE run_id = ? AND component = 'controller'", (run.id,)
        ).fetchall()
        if step["decision_type"].startswith(STEP_PROBE_SKIPPED)
    ]
    assert any(s["probe_name"] == "board_snapshot" for s in skipped)


@respx.mock
def test_extend_case_state_appends_company_events_evidence_and_preserves_signals(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`company_events` evidence is appended; pre-populated UNKNOWN signals hold.

    Neither `acme.com` nor `globex.com` appears in the real
    `data/events/collection_status.csv`, so `derive_policy_signals` reports
    both booleans UNKNOWN both before and after the probe runs — the case
    state's docstring says the two must "agree by construction".
    """
    add_posting(conn, job_id="5001", first_observed=NOW, last_seen_open=NOW)
    # A real event so the probe has at least one claim to append.
    upsert_event(
        conn,
        CompanyEvent(
            company_id=COMPANY,
            event_type="funding",
            event_date=NOW.date(),
            available_at=NOW,
            source_url="https://news.example.com/acme-funding",
            headline="Acme raises a round",
            materiality="minor",
            collected_at=NOW,
        ),
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5001").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD_WITH_JOB)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )
        assert case.event_signals == (UNKNOWN, UNKNOWN, UNKNOWN)
        before = len(case.evidence)

        extend_case_state(case, [CompanyEventsProbe], probes=probes)

    assert len(case.evidence) == before + 1
    added = case.evidence[-1]
    assert added.probe == "company_events"
    assert added.claim_type == "funding"
    # The probe's own answer replaces the pre-populated one; they agree.
    assert case.event_signals == (UNKNOWN, UNKNOWN, UNKNOWN)
    assert case.inputs.material_negative_event == UNKNOWN
    assert case.inputs.freeze_or_pause == UNKNOWN


@respx.mock
def test_extend_case_state_is_a_noop_without_a_case_file(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """No posting_id/company_id -> nothing runs, `case` is returned unchanged."""
    respx.get("https://careers.acme.com/jobs/1").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://careers.acme.com/jobs/1", now=NOW, probes=probes
        )
        before = len(case.evidence)
        result = extend_case_state(case, [CompanyEventsProbe], probes=probes)

    assert result is case
    assert len(case.evidence) == before


# ---------------------------------------------------------------------------
# The refresh match (spec.md §5, Amendment 2026-09-10) — rli.eval.case's
# "The refresh match" section, `REFRESH_MATCH_PROBE`, `_HashChange`,
# `_changes_from_rows`, `_observed_hash_changes`, `_refresh_claim`.
# ---------------------------------------------------------------------------


@respx.mock
def test_refresh_match_from_board_snapshot_jobs_hash_change(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`board_snapshot_jobs.description_hash` bracketing an `updated_at`.

    Also doubles as the "claim's own `available_at` wins" case (both
    captures predate the run clock `NOW`, so `max(claim.available_at,
    change.end) == NOW`) and as the persistence check: the claim must show
    up in `evidence_rows` attributed to `refresh_match`, like any other
    evidence row.
    """
    job_id = "6001"
    hash_old, hash_new = "sha256:old-board-hash", "sha256:new-board-hash"
    t1, t2 = NOW - timedelta(days=10), NOW - timedelta(days=5)
    updated_at = NOW - timedelta(days=7)  # inside [t1, t2], no tolerance needed

    add_posting(conn, job_id=job_id, first_observed=t1, last_seen_open=NOW)
    add_capture(conn, t1, jobs=[job(job_id, description_hash=hash_old)])
    add_capture(conn, t2, jobs=[job(job_id, description_hash=hash_new)])

    gh_job = _gh_job(
        job_id, first_published="2026-01-01T00:00:00Z", updated_at=to_utc_z(updated_at)
    )
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=NOW,
            probes=probes,
        )

    refreshed = [c for c in case.evidence if c.claim_type == CLAIM_REFRESHED_AT]
    assert len(refreshed) == 1
    claim = refreshed[0]
    assert claim.probe == REFRESH_MATCH_PROBE
    assert claim.source_quality == "ats_native"
    assert claim.source_event_at == updated_at
    assert claim.value == to_utc_z(updated_at)
    assert "board_snapshot_jobs" in claim.raw_excerpt
    assert to_utc_z(t1) in claim.raw_excerpt and to_utc_z(t2) in claim.raw_excerpt
    assert hash_old[:12] in claim.raw_excerpt and hash_new[:12] in claim.raw_excerpt
    # Both captures predate NOW, so the updated_at claim's own available_at
    # (NOW, the run clock) is the later of the two -> it wins the max.
    assert claim.available_at == NOW

    # Persisted like any other evidence row (module docstring's replay-seam
    # rule: every claim goes through probes.save_evidence).
    rows = evidence_rows(conn, run.id)
    assert any(
        r["probe"] == REFRESH_MATCH_PROBE and r["claim_type"] == CLAIM_REFRESHED_AT for r in rows
    )


@respx.mock
def test_refresh_match_from_posting_snapshots_hash_change(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The same match, driven instead from `posting_snapshots.content_hash`."""
    job_id = "6002"
    posting_id = add_posting(conn, job_id=job_id, first_observed=NOW - timedelta(days=30))
    hash_old, hash_new = "sha256:aaa111own", "sha256:bbb222own"
    t1, t2 = NOW - timedelta(days=12), NOW - timedelta(days=6)
    updated_at = NOW - timedelta(days=8)  # inside [t1, t2]

    _posting_snapshot(conn, posting_id, t1, content_hash=hash_old)
    _posting_snapshot(conn, posting_id, t2, content_hash=hash_new)

    gh_job = _gh_job(
        job_id, first_published="2026-01-01T00:00:00Z", updated_at=to_utc_z(updated_at)
    )
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=NOW,
            probes=probes,
        )

    refreshed = [c for c in case.evidence if c.claim_type == CLAIM_REFRESHED_AT]
    assert len(refreshed) == 1
    claim = refreshed[0]
    assert claim.probe == REFRESH_MATCH_PROBE
    assert claim.source_quality == "ats_native"
    assert claim.source_event_at == updated_at
    assert "posting_snapshots" in claim.raw_excerpt
    assert hash_old[:12] in claim.raw_excerpt and hash_new[:12] in claim.raw_excerpt


@respx.mock
def test_refresh_match_available_at_is_the_detecting_capture_when_it_is_later(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`available_at = max(claim.available_at, change.end)`: the CAPTURE wins.

    The updated_at claim's own `available_at` is the run clock (`ctx.now()`
    inside `resolve_posting`); here the detecting capture is dated AFTER that
    run clock, so the max must pick the capture, not the claim. Captures
    normally precede a live run, but nothing in `_refresh_claim`'s arithmetic
    assumes that — this pins the case where it does not hold and would, if
    the max were computed backwards, silently backdate the refresh onto the
    run clock instead of the moment the change actually became knowable.
    """
    run_now = datetime(2026, 8, 1, tzinfo=UTC)
    job_id = "6003"
    hash_old, hash_new = "sha256:old-later", "sha256:new-later"
    t1, t2 = run_now - timedelta(days=5), run_now + timedelta(days=5)
    updated_at = run_now - timedelta(days=2)  # inside [t1 - 3, t2 + 3]

    add_posting(conn, job_id=job_id, first_observed=t1)
    add_capture(conn, t1, jobs=[job(job_id, description_hash=hash_old)])
    add_capture(conn, t2, jobs=[job(job_id, description_hash=hash_new)])

    gh_job = _gh_job(
        job_id, first_published="2026-01-01T00:00:00Z", updated_at=to_utc_z(updated_at)
    )
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg, now=run_now) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=run_now,
            probes=probes,
        )

    refreshed = [c for c in case.evidence if c.claim_type == CLAIM_REFRESHED_AT]
    assert len(refreshed) == 1
    claim = refreshed[0]
    assert claim.available_at == t2  # the detecting capture, not run_now
    assert claim.fetched_at == run_now


@respx.mock
def test_refresh_match_earliest_matching_change_wins_when_several_match(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """One `updated_at` matching changes from BOTH tables: the EARLIEST wins.

    `_refresh_claim` picks the earliest detecting capture among every
    matching change (module docstring: "the earliest moment we could
    verifiably have known"). The board-route change here detects earlier
    than the posting_snapshots-route change; both widened intervals overlap
    the chosen `updated_at`, so both are candidates, and the raw_excerpt must
    name the EARLIER (board) one.
    """
    job_id = "6008"
    posting_id = add_posting(conn, job_id=job_id, first_observed=NOW - timedelta(days=40))

    board_old, board_new = "sha256:board-old", "sha256:board-new"
    add_capture(conn, NOW - timedelta(days=30), jobs=[job(job_id, description_hash=board_old)])
    add_capture(conn, NOW - timedelta(days=25), jobs=[job(job_id, description_hash=board_new)])
    # This change detects LATER (day -15) than the board one (day -25).

    own_old, own_new = "sha256:own-old", "sha256:own-new"
    _posting_snapshot(conn, posting_id, NOW - timedelta(days=20), content_hash=own_old)
    _posting_snapshot(conn, posting_id, NOW - timedelta(days=15), content_hash=own_new)

    # Overlap of the two widened intervals ([-33,-22] and [-23,-12]) is
    # [-23,-22]; -22.5 sits inside both, so both changes match.
    updated_at = NOW - timedelta(days=22, hours=12)
    gh_job = _gh_job(
        job_id, first_published="2026-01-01T00:00:00Z", updated_at=to_utc_z(updated_at)
    )
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=NOW,
            probes=probes,
        )

    refreshed = [c for c in case.evidence if c.claim_type == CLAIM_REFRESHED_AT]
    assert len(refreshed) == 1
    claim = refreshed[0]
    # The earlier (board) change's raw_excerpt is cited, not the later (own) one.
    assert "board_snapshot_jobs" in claim.raw_excerpt
    assert board_old[:12] in claim.raw_excerpt and board_new[:12] in claim.raw_excerpt
    assert own_old[:12] not in claim.raw_excerpt and own_new[:12] not in claim.raw_excerpt


@respx.mock
def test_no_hash_change_yields_no_refresh_claim_and_publish_recency_from_publish_alone(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """An `updated_at` with no corroborating hash change is silence, never a claim.

    Two captures with the SAME hash: nothing observed to have moved. The
    `updated_at` (dated 1 day ago, which WOULD read as "recent" if it were
    wrongly allowed through) must not affect `publish_recency`, which must
    fall back to the (stale) `first_published` date alone.
    """
    job_id = "6004"
    same_hash = "sha256:unchanged"
    t1, t2 = NOW - timedelta(days=10), NOW - timedelta(days=5)
    first_published = NOW - timedelta(days=100)  # well outside recent_publish_days
    updated_at = NOW - timedelta(days=1)  # would be "recent" if it counted

    add_posting(conn, job_id=job_id, first_observed=t1)
    add_capture(conn, t1, jobs=[job(job_id, description_hash=same_hash)])
    add_capture(conn, t2, jobs=[job(job_id, description_hash=same_hash)])

    gh_job = _gh_job(
        job_id, first_published=to_utc_z(first_published), updated_at=to_utc_z(updated_at)
    )
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=NOW,
            probes=probes,
        )

    assert [c for c in case.evidence if c.claim_type == CLAIM_REFRESHED_AT] == []
    assert case.inputs.publish_recency == "not_recent"


@respx.mock
def test_refresh_match_boundary_exact_limit_matches(conn: sqlite3.Connection, cfg: Config) -> None:
    """`updated_at` exactly `refresh_match_days` past the interval end still matches (inclusive)."""
    job_id = "6005"
    hash_old, hash_new = "sha256:boundary-old", "sha256:boundary-new"
    t1, t2 = NOW - timedelta(days=20), NOW - timedelta(days=10)
    updated_at = t2 + timedelta(days=cfg.thresholds.refresh_match_days)

    add_posting(conn, job_id=job_id, first_observed=t1)
    add_capture(conn, t1, jobs=[job(job_id, description_hash=hash_old)])
    add_capture(conn, t2, jobs=[job(job_id, description_hash=hash_new)])

    gh_job = _gh_job(
        job_id, first_published="2026-01-01T00:00:00Z", updated_at=to_utc_z(updated_at)
    )
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=NOW,
            probes=probes,
        )

    refreshed = [c for c in case.evidence if c.claim_type == CLAIM_REFRESHED_AT]
    assert len(refreshed) == 1
    assert refreshed[0].source_event_at == updated_at


@respx.mock
def test_refresh_match_boundary_one_day_beyond_does_not_match(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """One day past `refresh_match_days` no longer matches — the tolerance is not open-ended."""
    job_id = "6006"
    hash_old, hash_new = "sha256:beyond-old", "sha256:beyond-new"
    t1, t2 = NOW - timedelta(days=20), NOW - timedelta(days=10)
    updated_at = t2 + timedelta(days=cfg.thresholds.refresh_match_days + 1)

    add_posting(conn, job_id=job_id, first_observed=t1)
    add_capture(conn, t1, jobs=[job(job_id, description_hash=hash_old)])
    add_capture(conn, t2, jobs=[job(job_id, description_hash=hash_new)])

    gh_job = _gh_job(
        job_id, first_published="2026-01-01T00:00:00Z", updated_at=to_utc_z(updated_at)
    )
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=NOW,
            probes=probes,
        )

    assert [c for c in case.evidence if c.claim_type == CLAIM_REFRESHED_AT] == []


@respx.mock
def test_hash_change_without_an_updated_at_claim_yields_no_refresh_claim(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A real observed content change with no `updated_at` claim: silence, never manufactured.

    `_refresh_claim` must never invent a claim from the capture side alone —
    the ATS timestamp is the thing being corroborated, not derived.
    """
    job_id = "6007"
    hash_old, hash_new = "sha256:silent-old", "sha256:silent-new"
    t1, t2 = NOW - timedelta(days=10), NOW - timedelta(days=5)

    add_posting(conn, job_id=job_id, first_observed=t1)
    add_capture(conn, t1, jobs=[job(job_id, description_hash=hash_old)])
    add_capture(conn, t2, jobs=[job(job_id, description_hash=hash_new)])

    # No "updated_at" key at all: GH_JOB's base shape has none.
    gh_job = _gh_job(job_id, first_published="2026-01-01T00:00:00Z")
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=NOW,
            probes=probes,
        )

    assert [c for c in case.evidence if c.claim_type == CLAIM_REFRESHED_AT] == []


# ---------------------------------------------------------------------------
# The headline verification: spec.md §5 Amendment 2026-09-10's new P3c row,
# built through the REAL build_case_state / extend_case_state path.
# ---------------------------------------------------------------------------


@respx.mock
def test_material_event_after_last_refresh_forces_wait_on_p3c(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 17-month-old open posting + a material layoff after the last refresh -> `wait`/P3c.

    Exercises the amendment end to end through the real
    `build_case_state`/`extend_case_state` pipeline, on the temp sqlite
    `conn`, with the Greenhouse endpoints respx-mocked — no fallback to a
    lower-level `derive_policy_inputs`/`decide` call was needed.

    Two `collection_status.csv` views are used deliberately:

    * `build_case_state` is pointed at a status file that does NOT mention
      this company (a nonexistent path degrades to "no company has been
      investigated yet" — see `rli.eval.case._event_signals`), so its
      pre-population leaves `material_negative_event` UNKNOWN. This is what
      makes assertion (a) meaningful: the question really is still open
      before `company_events` has run.
    * `extend_case_state` runs `CompanyEventsProbe` through the ordinary
      dynamic-probe path (`rli.probes.registry.build_args`), which always
      builds `CompanyEventsArgs` with `collection_status_csv=None` — i.e. it
      always reads `rli.probes.company_events.DEFAULT_COLLECTION_STATUS_CSV`
      (see that module's docstring; there is no override plumbed through
      `extend_case_state`/`build_args` the way `build_case_state` has one).
      To exercise the probe without touching the real repo file, that module
      constant is monkeypatched to a status file that HAS since recorded
      this company as searched — the same "collection ran between the case
      being opened and the probe being executed" story a real deployment
      would tell.
    """
    company_id = COMPANY
    old_publish = NOW - timedelta(days=520)  # ~17 months
    event_date = (NOW - timedelta(days=10)).date()  # after old_publish, within any window
    job_id = "9001"

    add_posting(
        conn,
        job_id=job_id,
        company_id=company_id,
        first_observed=old_publish,
        last_seen_open=NOW,
    )
    upsert_event(
        conn,
        CompanyEvent(
            company_id=company_id,
            event_type="layoff",
            event_date=event_date,
            available_at=NOW - timedelta(days=9),
            source_url="https://news.example.com/acme-layoffs",
            headline="Acme lays off 20% of staff",
            materiality="material",
            collected_at=NOW,
        ),
    )
    populated_csv = tmp_path / "collection_status_populated.csv"
    write_collection_status_csv(
        [
            CollectionStatus(
                company_id=company_id,
                searched_at=NOW - timedelta(days=1),
                queries_run=5,
                events_found=1,
            )
        ],
        populated_csv,
    )
    not_yet_collected_csv = tmp_path / "collection_status_not_yet_written.csv"

    gh_job = _gh_job(job_id, first_published=to_utc_z(old_publish))
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=NOW,
            probes=probes,
            collection_status_csv=str(not_yet_collected_csv),
        )

        # -- (a) BEFORE company_events has run ----------------------------
        assert case.event_signals == (UNKNOWN, UNKNOWN, UNKNOWN)
        assert case.inputs.material_negative_event is UNKNOWN
        last_refreshed = last_publish_or_refresh(case.evidence)
        quality_before = evidence_quality_detail(case.evidence, case.inputs, case.failures, cfg)
        outcome_before = decide(
            case.inputs,
            quality_before.quality,
            NOW,
            cfg,
            long_lived=case.features.long_lived if case.features is not None else UNKNOWN,
            last_refreshed_at=last_refreshed,
        )
        changeable_before = could_change_action(
            case.inputs,
            outcome_before.recommended_action,
            quality=quality_before.quality,
            long_lived=case.features.long_lived if case.features is not None else UNKNOWN,
            last_refreshed_at=last_refreshed,
            now=NOW,
            cfg=cfg,
        )
        assert changeable_before  # non-empty: the question is still open
        assert "material_negative_event" in changeable_before

        # -- (b) AFTER company_events has run, via extend_case_state ------
        # `rli.probes.__init__` re-exports the FUNCTION `company_events`
        # under the same name as this submodule (`from
        # rli.probes.company_events import ..., company_events`), which
        # shadows the submodule on `rli.probes.company_events` ATTRIBUTE
        # access — both `import rli.probes.company_events as x` and
        # `monkeypatch.setattr("rli.probes.company_events...", ...)` walk
        # that attribute chain and would silently patch the function instead.
        # `importlib.import_module` looks the submodule up in `sys.modules`
        # by its fully-qualified name instead, sidestepping the shadowing.
        monkeypatch.setattr(
            importlib.import_module("rli.probes.company_events"),
            "DEFAULT_COLLECTION_STATUS_CSV",
            populated_csv,
        )
        extend_case_state(case, [CompanyEventsProbe], probes=probes)

    assert case.inputs.material_negative_event is True
    assert isinstance(case.inputs.last_material_event_at, datetime)

    last_refreshed_after = last_publish_or_refresh(case.evidence)
    quality_after = evidence_quality_detail(case.evidence, case.inputs, case.failures, cfg)
    outcome_after = decide(
        case.inputs,
        quality_after.quality,
        NOW,
        cfg,
        long_lived=case.features.long_lived if case.features is not None else UNKNOWN,
        last_refreshed_at=last_refreshed_after,
    )
    assert outcome_after.recommended_action == "wait"
    assert outcome_after.branch == "P3c_material_event_unrefreshed"
