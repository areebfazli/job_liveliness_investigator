"""`rli.eval.case`: the always-run pair, identity resolution, and the
evidence-claim contract it owes (spec.md §2/§4; module docstring of
`rli.eval.case`).

Every test builds its own `Run` + `ProbeRunner` (via `open_probe_runner`)
rather than going through `run_system_a`/`run_system_b`, so `build_case_state`
and `extend_case_state` are exercised directly and in isolation from the
routing/policy layers those two systems add on top.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx
from pydantic import BaseModel
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
from rli.events.policy_signals import CLAIM_EVENTS_SEARCHED
from rli.events.store import (
    CollectionStatus,
    CompanyEvent,
    upsert_event,
    write_collection_status_csv,
)
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN
from rli.models.probe import ProbeResult
from rli.models.time import to_utc_z
from rli.policy.action import decide
from rli.policy.inputs import (
    CLAIM_BOARD_ABSENT,
    CLAIM_POSTING_STATE,
    CLAIM_REFRESHED_AT,
    CLAIM_TEAM_SIGNAL,
    could_change_action,
    last_publish_or_refresh,
)
from rli.policy.quality import evidence_quality_detail
from rli.probes.base import Probe, ProbeClaim
from rli.probes.company_events import CompanyEventsProbe
from rli.probes.team_signal import TeamSignalArgs, TeamSignalProbe

NOW = datetime(2026, 9, 7, tzinfo=UTC)
REFRESH_MATCH_DAYS = 3  # cfg.thresholds.refresh_match_days default (config.toml)

#: A collection-status path that deliberately does not exist. A missing file
#: degrades to an empty status map — "no company has been searched yet" — so
#: pinning it keeps every test in this file independent of the real
#: `data/events/collection_status.csv` in the working checkout.
NO_COLLECTION_STATUS = "/nonexistent/collection_status.csv"

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
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    system: str = "A",
    now: datetime = NOW,
    collection_status_csv: str | Path | None = NO_COLLECTION_STATUS,
) -> Iterator[tuple[Run, ProbeRunner]]:
    """Build a `Run` + `ProbeRunner` pair the way `run_system_a`/`b` do.

    `use_tool_cache=False` so a respx-mocked fetch can never be answered
    from a stored `tool_cache` row (see the eval runner's docstring); `sleep`
    is a no-op so a retry path never actually waits. `now` defaults to the
    module `NOW` but is overridable so a refresh-match test can put the run
    clock BEFORE a detecting capture (see the "available_at is a max" tests
    below) without disturbing every other test in this file.

    `collection_status_csv` pins the `company_events` collection state on the
    `ProbeContext` — the run-level handle the probe reads (see
    `rli.probes.base.ProbeContext`). It defaults to a path that does not
    exist, i.e. "no company has been searched yet", so no test in this file
    accidentally depends on whatever `data/events/collection_status.csv`
    happens to contain in the working checkout.
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
            conn,
            cfg,
            run,
            now,
            sleep=lambda _s: None,
            use_tool_cache=False,
            collection_status_csv=collection_status_csv,
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
def test_extend_case_state_appends_company_events_evidence(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`company_events` evidence is appended; an UNSEARCHED company stays UNKNOWN.

    The run is pinned at `NO_COLLECTION_STATUS`, so the probe emits no
    `company_events_searched` claim — which is exactly what must keep all
    three event inputs UNKNOWN even though a (minor, non-negative) event
    claim WAS appended. "We hold an event for this company" is not the same
    statement as "we searched this company", and only the latter can turn a
    signal into a known `False`.
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
        # The always-run pair reads no event store at all any more, so the
        # three event inputs are UNKNOWN here by construction.
        assert case.inputs.material_negative_event is UNKNOWN
        assert case.inputs.freeze_or_pause is UNKNOWN
        assert case.inputs.last_material_event_at is UNKNOWN
        before = len(case.evidence)

        extend_case_state(case, [CompanyEventsProbe], probes=probes)

    assert len(case.evidence) == before + 1
    added = case.evidence[-1]
    assert added.probe == "company_events"
    assert added.claim_type == "funding"
    assert case.inputs.material_negative_event is UNKNOWN
    assert case.inputs.freeze_or_pause is UNKNOWN
    assert case.inputs.last_material_event_at is UNKNOWN


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
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """A 17-month-old open posting + a material layoff after the last refresh -> `wait`/P3c.

    Exercises the amendment end to end through the real
    `build_case_state`/`extend_case_state` pipeline, on the temp sqlite
    `conn`, with the Greenhouse endpoints respx-mocked — no fallback to a
    lower-level `derive_policy_inputs`/`decide` call was needed.

    ONE `collection_status.csv` is pinned, on the `ProbeContext`, and it
    records this company as searched. That is all the plumbing this needs
    now: `build_case_state` reads no event store at all, so assertion (a) —
    "the question is still open before `company_events` has run" — holds by
    construction rather than by pointing the case builder at a file that
    happens to omit the company. The probe reads the pinned file through
    `ctx.collection_status_csv` (`rli.probes.company_events`), which is also
    why nothing here has to monkeypatch that module's default constant any
    more.
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

    gh_job = _gh_job(job_id, first_published=to_utc_z(old_publish))
    _mock_greenhouse(job_id, gh_job)

    with _run_and_probes(conn, cfg, collection_status_csv=str(populated_csv)) as (run, probes):
        case = build_case_state(
            conn,
            cfg,
            url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            now=NOW,
            probes=probes,
        )

        # -- (a) BEFORE company_events has run ----------------------------
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
        extend_case_state(case, [CompanyEventsProbe], probes=probes)

    assert case.inputs.material_negative_event is True
    assert isinstance(case.inputs.last_material_event_at, datetime)

    # And the input is EVIDENCE-BACKED: the layoff claim the policy read is
    # in the case's evidence, carrying its materiality prefix. This is the
    # regression guard for the bug this pipeline was rebuilt around — a
    # `wait` on P3c with no layoff evidence behind it.
    layoffs = [c for c in case.evidence if c.probe == "company_events" and c.claim_type == "layoff"]
    assert len(layoffs) == 1
    assert layoffs[0].value.startswith("material: ")
    assert any(
        c.claim_type == CLAIM_EVENTS_SEARCHED and c.probe == "company_events" for c in case.evidence
    )

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


# ---------------------------------------------------------------------------
# The point-in-time invariant: every policy input comes from CLAIMS (which
# replay gates by `available_at <= T`) plus history features, and never from
# a `ProbeResult.data` blob. See `extend_case_state`'s docstring — "NOTHING
# is threaded into `derive_policy_inputs` from a `ProbeResult.data` blob" —
# and `rli.replay.build`, which writes the same contract down.
# ---------------------------------------------------------------------------

#: When the replay dataset's `team_signal` record was built. The record below
#: is collected with `as_of=BUILD_TIME`, so every claim on it is stamped from
#: a capture at or before BUILD_TIME and AFTER the archive-era `NOW` the
#: first test replays it at — which is the shape the gate has to reject,
#: however the record came to exist. (`rli.replay.build` no longer produces
#: one such record per posting: `TeamSignalArgs` now carries an `as_of` and
#: the probe is re-run per `T`. These two tests are about `rli.eval.case`'s
#: own contract — inputs come from gated claims, never from a blob — which
#: has to hold for ANY record served to it, so they keep building the
#: hostile one deliberately.)
BUILD_TIME = NOW + timedelta(days=90)


@dataclass
class _RecordedProbeRunner(ProbeRunner):
    """`rli.replay.mode.ReplayProbeRunner` in miniature: two overridden methods.

    `execute` serves the probes named in `record` from that pre-built record
    instead of running them, which is what replay does for every dynamic
    probe; probes absent from `record` still run live, so the always-run pair
    goes through respx exactly as in every other test in this file.
    `save_evidence` applies spec.md §3's gate — "expose only evidence with
    `available_at <= T`" — at the one choke point every system's evidence
    passes through, with the same inclusive comparison as the real thing.

    Written here rather than imported from `rli.replay.mode` on purpose. This
    is a test of `rli.eval.case`'s own contract, and the dependency runs one
    way only: replay knows about the systems, the systems know only
    `rli.eval.runner.ReplayHook` (see its docstring). Importing the replay
    runner here would also make the test pass or fail for reasons that live
    in `rli/replay/`, which is not what it is asserting.
    """

    record: dict[str, ProbeResult]
    replay_at: datetime

    def execute(self, probe_cls: type[Probe], args: BaseModel) -> ProbeResult:
        recorded = self.record.get(probe_cls.name)
        return recorded if recorded is not None else super().execute(probe_cls, args)

    def save_evidence(
        self, *, probe: str, claims: Sequence[ProbeClaim], posting_id: str | None = None
    ) -> list[EvidenceItem]:
        kept = [claim for claim in claims if claim.available_at <= self.replay_at]
        return self.run.save_evidence(probe=probe, claims=kept, posting_id=posting_id)


def _recorded(
    probes: ProbeRunner, record: dict[str, ProbeResult], replay_at: datetime
) -> _RecordedProbeRunner:
    """The same run, context and client pool, wrapped to serve `record` at `replay_at`."""
    return _RecordedProbeRunner(
        run=probes.run,
        ctx=probes.ctx,
        cfg=probes.cfg,
        now=probes.now,
        pool=probes.pool,
        record=record,
        replay_at=replay_at,
    )


def _team_history_with_a_post_t_new_role(conn: sqlite3.Connection, job_id: str) -> None:
    """Board history deep and dense at `NOW`, plus a new role opened AFTER it.

    The sibling posting on the same team is first observed ten days before
    `BUILD_TIME`, i.e. a fortnight of hiring activity that had not happened
    yet at `NOW`. That is what makes the leak measurable rather than merely
    structural: the build-time probe answers `True` on evidence that did not
    exist at the `T` the answer is about.
    """
    for offset in range(60):
        add_capture(conn, NOW - timedelta(days=59 - offset), [])
    add_posting(conn, job_id=job_id, first_observed=NOW - timedelta(days=200), last_seen_open=NOW)
    add_posting(conn, job_id="5002", first_observed=BUILD_TIME - timedelta(days=10))


def _build_time_team_signal_record(probes: ProbeRunner, posting_id: str) -> ProbeResult:
    """Run the REAL `team_signal` probe on a build-time clock, as the builder does.

    The asserts are not the test — they are what stops the test from passing
    vacuously if the fixture ever stops producing the shape the bug needed: a
    genuine `bool` in `data` and claims stamped after `NOW`.
    """
    ctx = replace(probes.ctx, now=lambda: BUILD_TIME)
    result = TeamSignalProbe().run(
        TeamSignalArgs(posting_id=posting_id, company_id=COMPANY, as_of=BUILD_TIME), ctx
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["corroborating_hiring_signal"] is True
    assert result.data["evidence"]
    # Stamped from the capture that supports each claim (`rli.probes.
    # team_signal`'s availability rule), which for this fixture is the
    # post-`NOW` sibling posting's `first_observed`. What the two tests below
    # need is only that every stamp is after `NOW` and at or before
    # `BUILD_TIME`; asserting the exact instant would pin the test to the
    # attribution rule rather than to the gate it is about.
    assert all(NOW < claim.available_at <= BUILD_TIME for claim in result.data["evidence"])
    return result


@respx.mock
def test_team_signal_blob_does_not_populate_the_input_when_its_claims_are_after_t(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """An archive-era case must not learn `corroborating_hiring_signal` from a blob.

    The replay shape exactly: one build-time `team_signal` record whose
    `data["corroborating_hiring_signal"]` is a real `True` and whose claims
    are all `available_at = BUILD_TIME`. At `NOW` the point-in-time gate drops
    every one of those claims — correctly, because nothing the probe said was
    knowable then — so the input must stay UNKNOWN and stay an OPEN QUESTION
    for the controller.

    This is the regression guard for the leak. `extend_case_state` used to
    read `team.data["corroborating_hiring_signal"]` and thread it into
    `derive_policy_inputs`, so the case came out of replay answered `True`
    with zero team_signal evidence rows behind it and the key absent from
    `unpopulated`: a post-T value deciding a pre-T case, and one `rli replay
    check` cannot see, because it audits `evidence` and `run_steps` rather
    than policy inputs.
    """
    _team_history_with_a_post_t_new_role(conn, "5001")
    _mock_greenhouse("5001", _gh_job("5001"))

    with _run_and_probes(conn, cfg) as (_run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )
        assert case.posting_id is not None
        record = {TeamSignalProbe.name: _build_time_team_signal_record(probes, case.posting_id)}
        extend_case_state(case, [TeamSignalProbe], probes=_recorded(probes, record, NOW))

    # The probe ran and its blob says True...
    served = case.probe_results[TeamSignalProbe.name]
    assert served.data is not None
    assert served.data["corroborating_hiring_signal"] is True
    # ...and not one of its claims was knowable at T.
    assert [c for c in case.evidence if c.probe == TeamSignalProbe.name] == []
    # ...so the question is still open, and says so.
    assert case.inputs.corroborating_hiring_signal is UNKNOWN
    assert "corroborating_hiring_signal" in case.unpopulated


@respx.mock
def test_team_signal_claim_available_at_t_does_populate_the_input(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The complementary half: a claim the gate admits still answers the input.

    Same corpus, same record, same runner — only the gate's `T` moves, to the
    moment the record was built. Every claim is now `available_at <= T`, the
    `corroborating_hiring_signal` claim reaches the evidence table, and the
    input is answered FROM THAT CLAIM. Deleting the blob fallback deleted a
    leak, not the feature.

    The decision clock stays at `NOW` so that exactly one thing differs
    between this test and the one above; `corroborating_hiring_signal` is not
    a windowed input, so it reads the same either way.
    """
    _team_history_with_a_post_t_new_role(conn, "5001")
    _mock_greenhouse("5001", _gh_job("5001"))

    with _run_and_probes(conn, cfg) as (_run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )
        assert case.posting_id is not None
        record = {TeamSignalProbe.name: _build_time_team_signal_record(probes, case.posting_id)}
        extend_case_state(case, [TeamSignalProbe], probes=_recorded(probes, record, BUILD_TIME))

    claims = [c for c in case.evidence if c.claim_type == CLAIM_TEAM_SIGNAL]
    assert len(claims) == 1
    assert claims[0].probe == TeamSignalProbe.name
    assert claims[0].value == "true"
    assert case.inputs.corroborating_hiring_signal is True
    assert "corroborating_hiring_signal" not in case.unpopulated
