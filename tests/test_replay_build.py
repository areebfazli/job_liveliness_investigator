"""`rli.replay.build`: the point-in-time dataset builder (spec.md §6; PLAN.md M4).

Mocks the HTTP layer only (respx). The builder, every probe, the registry,
the policy, the runner and `rli.replay.pit` all run for real against the
synthetic corpus in `tests/test_replay_helpers.py`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import respx
from test_eval_helpers import COMPANY, OTHER_COMPANY, add_capture, add_posting, job
from test_replay_helpers import (
    CLOSED_JOB,
    NOW,
    OPEN_JOB,
    OPEN_URL,
    TENANT,
    dev_everything_cutoff,
    mock_ats,
    seed_corpus,
)

from rli.config import Config
from rli.eval.baseline import HoldoutSplitRequestedError
from rli.models.time import parse_utc
from rli.net import hash_args
from rli.policy.inputs import CLAIM_BOARD_ABSENT, CLAIM_POSTING_STATE
from rli.probes.company_events import CompanyEventsArgs, CompanyEventsProbe
from rli.replay.build import (
    archive_state_args_hash,
    archive_state_claims,
    build_dataset,
    grid_times,
    plan_cases,
)
from rli.replay.mode import ARCHIVE_BOARD_STATE_PROBE

DATASET = "ds-build"
OPEN_POSTING = f"greenhouse:{TENANT}:{OPEN_JOB}"
CLOSED_POSTING = f"greenhouse:{TENANT}:{CLOSED_JOB}"


def _build(conn: sqlite3.Connection, cfg: Config, **kwargs: object):
    defaults: dict[str, object] = {
        "dataset_id": DATASET,
        "split": "dev",
        "split_kind": "temporal",
        "grid_step_days": 30,
        "now": NOW,
        "cutoff": dev_everything_cutoff(),
        "use_tool_cache": False,
        "collection_status_csv": "/nonexistent/collection_status.csv",
    }
    defaults.update(kwargs)
    return build_dataset(conn, cfg, **defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# grid_times
# ---------------------------------------------------------------------------


def test_grid_walks_the_step_and_always_includes_the_endpoint() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    times = grid_times(start, start + timedelta(days=70), 30)
    assert times == [
        start,
        start + timedelta(days=30),
        start + timedelta(days=60),
        start + timedelta(days=70),
    ]


def test_grid_landing_exactly_on_the_endpoint_does_not_duplicate_it() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    times = grid_times(start, start + timedelta(days=60), 30)
    assert times == [start, start + timedelta(days=30), start + timedelta(days=60)]


def test_grid_of_a_single_instant_is_that_instant() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert grid_times(start, start, 30) == [start]


def test_grid_rejects_an_end_before_the_start_rather_than_guessing() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert grid_times(start, start - timedelta(days=1), 30) == []


def test_grid_rejects_a_non_positive_step() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="positive"):
        grid_times(start, start, 0)


def test_grid_rejects_a_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        grid_times(datetime(2026, 1, 1), datetime(2026, 2, 1), 30)  # noqa: DTZ001


# ---------------------------------------------------------------------------
# plan_cases
# ---------------------------------------------------------------------------


def test_plan_bounds_a_closed_posting_at_its_first_observed_absence(
    conn: sqlite3.Connection,
) -> None:
    seed_corpus(conn)
    splits = dict.fromkeys([OPEN_POSTING, CLOSED_POSTING], "dev")
    plans = {p.posting_id: p for p in plan_cases(conn, splits=splits, split="dev", now=NOW)}

    assert plans[OPEN_POSTING].end == NOW
    assert plans[CLOSED_POSTING].end == NOW - timedelta(days=30)
    # ... and therefore never asks "should I apply?" about a posting that was
    # already gone.
    assert max(plans[CLOSED_POSTING].times) == NOW - timedelta(days=30)


def test_plan_spreads_a_limit_round_robin_across_companies(
    conn: sqlite3.Connection,
) -> None:
    seed_corpus(conn)
    # Give acme a third posting so a naive posting_id-ordered scan with
    # limit=2 would return two acme rows and never reach globex.
    add_posting(conn, job_id="6003", tenant=TENANT, first_observed=NOW - timedelta(days=60))
    add_capture(conn, NOW - timedelta(days=5), [job("6003")], company_id=COMPANY)

    splits = {row["posting_id"]: "dev" for row in conn.execute("SELECT posting_id FROM postings")}
    plans = plan_cases(conn, splits=splits, split="dev", now=NOW, limit_postings=2)
    assert {plan.company_id for plan in plans} == {COMPANY, OTHER_COMPANY}


def test_plan_skips_a_posting_with_no_fetchable_url(conn: sqlite3.Connection) -> None:
    seed_corpus(conn)
    add_posting(
        conn,
        job_id="9999",
        tenant=None,
        posting_id="archive:acme.com:9999",
        canonical_url="archive-only:acme.com/9999",
        first_observed=NOW - timedelta(days=60),
    )
    splits = {row["posting_id"]: "dev" for row in conn.execute("SELECT posting_id FROM postings")}
    plans = plan_cases(conn, splits=splits, split="dev", now=NOW)
    assert "archive:acme.com:9999" not in {plan.posting_id for plan in plans}


# ---------------------------------------------------------------------------
# archive_state_claims
# ---------------------------------------------------------------------------


def test_archive_state_reports_open_from_the_capture_that_listed_the_job(
    conn: sqlite3.Connection,
) -> None:
    seed_corpus(conn)
    claims = archive_state_claims(conn, OPEN_POSTING, NOW - timedelta(days=40))

    state = next(c for c in claims if c.claim_type == CLAIM_POSTING_STATE)
    assert state.value == "open"
    # The claim is dated by the CAPTURE, not by T — that is what lets it pass
    # the `available_at <= T` gate on its own merits (spec.md §3).
    assert state.available_at == NOW - timedelta(days=45)
    assert state.available_at <= NOW - timedelta(days=40)
    assert state.source_quality == "ats_native"  # an own capture IS the ATS's answer
    assert {c.claim_type for c in claims} == {CLAIM_POSTING_STATE, "board_present"}


def test_archive_state_reports_closed_once_a_complete_capture_lacks_the_job(
    conn: sqlite3.Connection,
) -> None:
    seed_corpus(conn)
    claims = archive_state_claims(conn, CLOSED_POSTING, NOW - timedelta(days=30))

    state = next(c for c in claims if c.claim_type == CLAIM_POSTING_STATE)
    assert state.value == "closed"
    assert state.available_at == NOW - timedelta(days=30)
    assert {c.claim_type for c in claims} == {CLAIM_POSTING_STATE, CLAIM_BOARD_ABSENT}


def test_archive_state_still_reports_open_before_the_disappearance(
    conn: sqlite3.Connection,
) -> None:
    seed_corpus(conn)
    claims = archive_state_claims(conn, CLOSED_POSTING, NOW - timedelta(days=45))
    state = next(c for c in claims if c.claim_type == CLAIM_POSTING_STATE)
    assert state.value == "open"


def test_archive_state_says_nothing_when_no_capture_had_seen_the_job(
    conn: sqlite3.Connection,
) -> None:
    seed_corpus(conn)
    # Before the first capture: absence of evidence, which must reach the
    # policy as UNKNOWN through the ordinary path, not as an observation.
    assert archive_state_claims(conn, OPEN_POSTING, NOW - timedelta(days=90)) == []


def test_archive_state_ignores_a_partial_capture_as_evidence_of_absence(
    conn: sqlite3.Connection,
) -> None:
    """spec.md §4: a throttled/failed capture is a coverage gap, never an absence."""
    seed_corpus(conn)
    add_capture(
        conn,
        NOW - timedelta(days=5),
        [],
        company_id=COMPANY,
        coverage_status="partial",
    )
    claims = archive_state_claims(conn, OPEN_POSTING, NOW)
    state = next(c for c in claims if c.claim_type == CLAIM_POSTING_STATE)
    assert state.value == "open"


def test_archive_state_args_hash_is_stable_and_per_case() -> None:
    first = archive_state_args_hash(OPEN_POSTING, NOW)
    assert first == archive_state_args_hash(OPEN_POSTING, NOW)
    assert first != archive_state_args_hash(OPEN_POSTING, NOW - timedelta(days=1))
    assert first != archive_state_args_hash(CLOSED_POSTING, NOW)


# ---------------------------------------------------------------------------
# build_dataset
# ---------------------------------------------------------------------------


@respx.mock
def test_build_writes_dataset_cases_and_the_full_probe_record(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    mock_ats()

    summary = _build(conn, cfg)

    assert summary.postings == 3
    assert summary.companies == 2
    assert summary.postings_failed == 0, summary.failures
    assert summary.cases > 0

    dataset = conn.execute(
        "SELECT * FROM replay_datasets WHERE dataset_id = ?", (DATASET,)
    ).fetchone()
    assert dataset["split_kind"] == "temporal"
    assert dataset["split_name"] == "dev"
    assert dataset["grid_step_days"] == 30
    assert dataset["cases"] == summary.cases

    cases = conn.execute(
        "SELECT posting_id, replay_at FROM replay_cases WHERE dataset_id = ?", (DATASET,)
    ).fetchall()
    assert len(cases) == summary.cases

    # The open posting's grid reaches the build instant; the closed one's
    # stops at its first observed absence.
    open_times = sorted(
        parse_utc(row["replay_at"]) for row in cases if row["posting_id"] == OPEN_POSTING
    )
    closed_times = sorted(
        parse_utc(row["replay_at"]) for row in cases if row["posting_id"] == CLOSED_POSTING
    )
    assert open_times[-1] == NOW
    assert closed_times[-1] == NOW - timedelta(days=30)

    # Every case carries the always-run pair plus the archive board state.
    for row in cases:
        names = {
            r[0]
            for r in conn.execute(
                """
                SELECT probe_name FROM replay_probe_results
                WHERE dataset_id = ? AND posting_id = ? AND replay_at = ?
                """,
                (DATASET, row["posting_id"], row["replay_at"]),
            )
        }
        assert {"resolve_posting", "board_snapshot", ARCHIVE_BOARD_STATE_PROBE} <= names


@respx.mock
def test_build_records_one_live_observation_per_posting_not_per_case(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The world is observed once; the record is what gets stored per T."""
    seed_corpus(conn)
    mock_ats()

    summary = _build(conn, cfg)

    # `resolve_posting` executed once per posting, no matter how many grid
    # points that posting has.
    resolver_steps = conn.execute(
        """
        SELECT COUNT(*) FROM run_steps s JOIN runs r ON r.id = s.run_id
        WHERE r.mode = 'live' AND s.probe_name = 'resolve_posting'
              AND s.decision_type = 'probe_run'
        """
    ).fetchone()[0]
    assert resolver_steps == summary.postings

    # ... yet a record exists for every case.
    stored = conn.execute(
        """
        SELECT COUNT(DISTINCT posting_id || '|' || replay_at) FROM replay_probe_results
        WHERE dataset_id = ? AND probe_name = 'resolve_posting'
        """,
        (DATASET,),
    ).fetchone()[0]
    assert stored == summary.cases


@respx.mock
def test_build_stores_company_events_under_the_args_hash_replay_will_ask_for(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`company_events`' args carry `as_of`, so its hash is a function of T."""
    seed_corpus(conn)
    mock_ats()
    _build(conn, cfg)

    rows = conn.execute(
        """
        SELECT replay_at, args_hash FROM replay_probe_results
        WHERE dataset_id = ? AND posting_id = ? AND probe_name = ?
        ORDER BY replay_at
        """,
        (DATASET, OPEN_POSTING, CompanyEventsProbe.name),
    ).fetchall()
    assert len(rows) >= 2
    # One distinct hash per T ...
    assert len({row["args_hash"] for row in rows}) == len(rows)
    # ... and each is exactly what `build_args` would produce at that T.
    for row in rows:
        args = CompanyEventsArgs(company_id=COMPANY, as_of=parse_utc(row["replay_at"]))
        expected = hash_args(CompanyEventsProbe.name, **args.model_dump(mode="json"))
        assert row["args_hash"] == expected


@respx.mock
def test_build_stores_the_resolver_observation_at_the_build_instant_not_at_t(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §3: a current discovery is never backdated onto the timeline."""
    seed_corpus(conn)
    mock_ats()
    _build(conn, cfg)

    rows = conn.execute(
        """
        SELECT replay_at, observed_at FROM replay_probe_results
        WHERE dataset_id = ? AND posting_id = ? AND probe_name = 'resolve_posting'
        """,
        (DATASET, OPEN_POSTING),
    ).fetchall()
    assert rows
    for row in rows:
        assert parse_utc(row["observed_at"]) == NOW


@respx.mock
def test_build_run_is_traced_but_can_never_be_read_as_a_replay_run(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    mock_ats()
    _build(conn, cfg)

    rows = conn.execute("SELECT mode, config_hash FROM runs").fetchall()
    assert rows
    for row in rows:
        assert row["mode"] == "live"
        # `rli.eval.baseline.collect_cases` matches a trailing `|dataset:<id>`.
        assert not str(row["config_hash"]).endswith(f"|dataset:{DATASET}")
        assert f"|replay_build:{DATASET}" in str(row["config_hash"])


@respx.mock
def test_rebuilding_the_same_dataset_replaces_it_in_place(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    mock_ats()
    first = _build(conn, cfg)
    second = _build(conn, cfg)

    assert second.cases == first.cases
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM replay_cases WHERE dataset_id = ?", (DATASET,)
        ).fetchone()[0]
        == first.cases
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM replay_datasets WHERE dataset_id = ?", (DATASET,)
        ).fetchone()[0]
        == 1
    )


def test_build_refuses_the_test_holdout(conn: sqlite3.Connection, cfg: Config) -> None:
    seed_corpus(conn)
    with pytest.raises(HoldoutSplitRequestedError, match="M6"):
        _build(conn, cfg, split="test")


def test_build_rejects_an_unknown_split(conn: sqlite3.Connection, cfg: Config) -> None:
    with pytest.raises(ValueError, match="unknown split"):
        _build(conn, cfg, split="train")


@respx.mock
def test_build_reports_a_posting_that_failed_instead_of_losing_the_dataset(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """One bad posting must not cost the whole build (it is named, not swallowed)."""
    seed_corpus(conn)
    mock_ats()

    import rli.replay.build as build_module

    original = build_module.build_case_state
    calls: list[str] = []

    def exploding(*args: object, **kwargs: object):
        url = str(kwargs.get("url"))
        calls.append(url)
        if url == OPEN_URL:
            raise RuntimeError("probe contract violation")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    build_module.build_case_state = exploding  # type: ignore[assignment]
    try:
        summary = _build(conn, cfg)
    finally:
        build_module.build_case_state = original  # type: ignore[assignment]

    assert summary.postings_failed == 1
    assert any(OPEN_POSTING in failure for failure in summary.failures)
    assert summary.postings == 2
    assert OPEN_URL in calls
