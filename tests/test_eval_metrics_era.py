"""`rli.eval.metrics`'s archive-era / live-era split (dev-300-v3, spec.md §1).

dev-300-v3 pools two eras of replay case: "archive-era" cases predate this
project's own board-snapshot collection and rest on Wayback captures alone
(spec.md §1 calls that weak evidence), while "live-era" cases also have an
own `board_snapshots` row (`source='own'`) available. `test_eval_metrics.py`
is already 1000+ lines, so the three era primitives — `era_boundary`,
`era_for`, `split_case_set_by_era` — get their own file here.

The one hazard worth naming up front: `split_case_set_by_era` must hand back
a `MetricsCaseSet` whose `systems` tuple is IDENTICAL to the pooled input's,
never narrowed to the systems that happen to have a run in that era.
`_case_set_for` reuses a caller-supplied case set only when `all(system in
case_set.systems for system in systems)`; a narrowed `systems` would make
that check fail and silently rebuild a POOLED case set behind an era label.
The math test at the bottom of this file exists specifically to catch that
regression: it drives `agent_efficiency` with a live-era case set and checks
the numbers against a hand-computed live-era-only expectation that is
deliberately different from the pooled figure.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from rli.config import Config
from rli.eval.metrics import (
    CaseRun,
    MetricsCase,
    MetricsCaseSet,
    agent_efficiency,
    collect_system_runs,
    era_boundary,
    era_for,
    split_case_set_by_era,
)
from rli.models.time import to_utc_z

NOW = datetime(2026, 9, 7, tzinfo=UTC)
DATASET = "ds-m6-era"
COMPANY = "acme.com"
BOUNDARY = "2021-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Row builders (same shapes as tests/test_eval_metrics.py, kept local so this
# file has no dependency on that module's private helpers)
# ---------------------------------------------------------------------------


def _add_company(conn: sqlite3.Connection, company_id: str = COMPANY) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO companies (company_id, name, website_domain, created_at) "
        "VALUES (?, ?, ?, ?)",
        (company_id, company_id, company_id, to_utc_z(NOW)),
    )


def _add_posting(
    conn: sqlite3.Connection,
    posting_id: str,
    *,
    company_id: str = COMPANY,
) -> None:
    _add_company(conn, company_id)
    conn.execute(
        """
        INSERT INTO postings (posting_id, company_id, ats, canonical_url, created_at)
        VALUES (?, ?, 'greenhouse', ?, ?)
        """,
        (
            posting_id,
            company_id,
            f"https://boards.greenhouse.io/{company_id}/jobs/{posting_id}",
            to_utc_z(NOW),
        ),
    )


def _decision(action: str | None) -> str:
    return json.dumps(
        {
            "posting_state": "open",
            "recommended_action": action,
            "recheck_after_days": None,
            "evidence_quality": "mixed",
            "hypotheses": [],
            "reason": [],
            "evidence": [],
        }
    )


def _add_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    system: str,
    posting_id: str,
    action: str | None,
    replay_at: str,
) -> str:
    conn.execute(
        """
        INSERT INTO runs (id, posting_id, input_url, system, mode, replay_at, config_hash,
                          started_at, status, final_decision, total_cost_usd, total_latency_ms)
        VALUES (?, ?, ?, ?, 'replay', ?, ?, ?, 'completed', ?, NULL, 0)
        """,
        (
            run_id,
            posting_id,
            f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}",
            system,
            replay_at,
            f"cfg:x|dataset:{DATASET}",
            to_utc_z(NOW),
            _decision(action),
        ),
    )
    return run_id


def _pair_at(
    conn: sqlite3.Connection,
    posting_id: str,
    *,
    replay_at: str,
    a_action: str | None,
    b_action: str | None,
    split_map: dict[str, str],
) -> None:
    """One replay case at `replay_at`, with an A run and a B run."""
    _add_posting(conn, posting_id)
    split_map[posting_id] = "dev"
    _add_run(
        conn,
        f"a-{posting_id}",
        system="A",
        posting_id=posting_id,
        action=a_action,
        replay_at=replay_at,
    )
    _add_run(
        conn,
        f"b-{posting_id}",
        system="B",
        posting_id=posting_id,
        action=b_action,
        replay_at=replay_at,
    )


def _add_board_snapshot(
    conn: sqlite3.Connection,
    *,
    captured_at: str,
    source: str,
    company_id: str = COMPANY,
    coverage_status: str = "complete",
) -> None:
    _add_company(conn, company_id)
    conn.execute(
        """
        INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status)
        VALUES (?, ?, ?, ?)
        """,
        (company_id, captured_at, source, coverage_status),
    )


# ---------------------------------------------------------------------------
# era_for
# ---------------------------------------------------------------------------


def test_era_for_with_no_boundary_is_always_archive_era() -> None:
    # No own capture ever recorded (`boundary=None`): nothing can be
    # live-era, no matter how recent `replay_at` is.
    assert era_for("2015-01-01T00:00:00Z", None) == "archive-era"
    assert era_for("2030-01-01T00:00:00Z", None) == "archive-era"


def test_era_for_uses_lexical_comparison_against_the_boundary() -> None:
    before = "2020-12-31T23:59:59Z"
    after = "2021-01-02T00:00:00Z"
    assert era_for(before, BOUNDARY) == "archive-era"
    # Equal to the boundary counts as live-era: the own capture already
    # exists as of that instant.
    assert era_for(BOUNDARY, BOUNDARY) == "live-era"
    assert era_for(after, BOUNDARY) == "live-era"


# ---------------------------------------------------------------------------
# era_boundary
# ---------------------------------------------------------------------------


def test_era_boundary_is_min_captured_at_over_own_rows_only(conn: sqlite3.Connection) -> None:
    # An archive-source row with an EARLIER captured_at than every own row
    # must not win — era_boundary only ever looks at source='own'.
    _add_board_snapshot(conn, captured_at="2010-01-01T00:00:00Z", source="archive")
    _add_board_snapshot(conn, captured_at="2022-06-01T00:00:00Z", source="own")
    _add_board_snapshot(conn, captured_at="2021-03-15T00:00:00Z", source="own")

    assert era_boundary(conn) == "2021-03-15T00:00:00Z"


def test_era_boundary_is_none_without_any_own_row(conn: sqlite3.Connection) -> None:
    # Archive-only rows present: still None, an own capture must exist.
    _add_board_snapshot(conn, captured_at="2010-01-01T00:00:00Z", source="archive")
    assert era_boundary(conn) is None


def test_era_boundary_is_none_on_an_empty_table(conn: sqlite3.Connection) -> None:
    assert era_boundary(conn) is None


# ---------------------------------------------------------------------------
# split_case_set_by_era: pure partitioning (hand-constructed case set)
# ---------------------------------------------------------------------------


def _case(input_url: str, replay_at: str, runs: dict[str, str]) -> MetricsCase:
    """A `MetricsCase` whose systems map to `CaseRun`s named by `runs` (system -> run_id)."""
    return MetricsCase(
        input_url=input_url,
        replay_at=replay_at,
        posting_id=input_url,
        split="dev",
        runs={
            system: CaseRun(system=system, run_id=run_id, status="completed")
            for system, run_id in runs.items()
        },
    )


def test_split_case_set_by_era_partitions_cases_run_ids_and_counts() -> None:
    # Systems A and B run every case; C only ever ran in the live era, which
    # exercises "a system with no runs in one era" without it being absent
    # from `systems` altogether.
    cases = (
        _case("u1", "2020-01-01T00:00:00Z", {"A": "a1", "B": "b1"}),
        _case("u2", "2020-06-01T00:00:00Z", {"A": "a2", "B": "b2"}),
        _case("u3", "2022-01-01T00:00:00Z", {"A": "a3", "B": "b3", "C": "c3"}),
        _case("u4", "2022-06-01T00:00:00Z", {"A": "a4", "C": "c4"}),
    )
    pooled = MetricsCaseSet(
        dataset_id=DATASET,
        allowed_splits=("dev", "validation"),
        systems=("A", "B", "C"),
        cases=cases,
        run_ids={
            "A": ("a1", "a2", "a3", "a4"),
            "B": ("b1", "b2", "b3"),
            "C": ("c3", "c4"),
        },
        counts_by_system={"A": 4, "B": 3, "C": 2},
        unassigned=7,
        excluded_holdout=3,
        duplicates_collapsed=1,
    )

    split = split_case_set_by_era(pooled, BOUNDARY)

    assert set(split) == {"live-era", "archive-era"}
    archive, live = split["archive-era"], split["live-era"]

    # cases partition, in original order, and reconstruct the pooled set.
    assert [c.input_url for c in archive.cases] == ["u1", "u2"]
    assert [c.input_url for c in live.cases] == ["u3", "u4"]
    archive_urls = {c.input_url for c in archive.cases}
    live_urls = {c.input_url for c in live.cases}
    assert archive_urls | live_urls == {c.input_url for c in pooled.cases}
    assert not archive_urls & live_urls

    # run_ids partition per system, preserving relative order, and the two
    # halves reconstruct the pooled run_ids for every system.
    assert archive.run_ids == {"A": ("a1", "a2"), "B": ("b1", "b2"), "C": ()}
    assert live.run_ids == {"A": ("a3", "a4"), "B": ("b3",), "C": ("c3", "c4")}
    for system in pooled.systems:
        assert archive.run_ids[system] + live.run_ids[system] == pooled.run_ids[system]

    assert archive.counts_by_system == {"A": 2, "B": 2, "C": 0}
    assert live.counts_by_system == {"A": 2, "B": 1, "C": 2}

    # systems, dataset_id and allowed_splits are copied verbatim, not
    # narrowed to whichever systems have runs in that era (C has zero
    # archive-era runs but stays in archive.systems).
    for half in (archive, live):
        assert half.systems == pooled.systems
        assert half.dataset_id == pooled.dataset_id
        assert half.allowed_splits == pooled.allowed_splits

    # Bookkeeping fields are pre-split-gate / global concepts: zeroed on both
    # halves, not divided from the pooled totals.
    for half in (archive, live):
        assert half.unassigned == 0
        assert half.excluded_holdout == 0
        assert half.duplicates_collapsed == 0


def test_split_case_set_by_era_both_keys_present_when_one_era_is_empty() -> None:
    cases = (_case("u1", "2020-01-01T00:00:00Z", {"A": "a1"}),)
    pooled = MetricsCaseSet(
        dataset_id=DATASET,
        allowed_splits=("dev",),
        systems=("A",),
        cases=cases,
        run_ids={"A": ("a1",)},
        counts_by_system={"A": 1},
    )

    # boundary=None: every case is archive-era, live-era is empty but present.
    split = split_case_set_by_era(pooled, None)
    assert set(split) == {"live-era", "archive-era"}
    assert split["live-era"].cases == ()
    assert split["live-era"].run_ids == {"A": ()}
    assert split["live-era"].counts_by_system == {"A": 0}
    assert split["live-era"].systems == ("A",)
    assert [c.input_url for c in split["archive-era"].cases] == ["u1"]


# ---------------------------------------------------------------------------
# The math test: agent_efficiency over a live-era-only case set must NOT
# silently fall back to pooled numbers.
# ---------------------------------------------------------------------------


def test_agent_efficiency_on_live_era_case_set_matches_live_era_only_math(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    splits: dict[str, str] = {}

    # Archive-era: 3 agreements (quick_apply/quick_apply), 1 disagreement
    # (apply_now vs wait). Archive-only overall agreement would be 3/4.
    for i in range(3):
        _pair_at(
            conn,
            f"arch-agree-{i}",
            replay_at="2019-01-01T00:00:00Z",
            a_action="quick_apply",
            b_action="quick_apply",
            split_map=splits,
        )
    _pair_at(
        conn,
        "arch-disagree",
        replay_at="2019-06-01T00:00:00Z",
        a_action="apply_now",
        b_action="wait",
        split_map=splits,
    )

    # Live-era: 1 agreement (quick_apply/quick_apply), 2 disagreements
    # (quick_apply vs skip). Live-only overall agreement = 1/3.
    _pair_at(
        conn,
        "live-agree",
        replay_at="2022-01-01T00:00:00Z",
        a_action="quick_apply",
        b_action="quick_apply",
        split_map=splits,
    )
    for i in range(2):
        _pair_at(
            conn,
            f"live-disagree-{i}",
            replay_at="2022-06-01T00:00:00Z",
            a_action="quick_apply",
            b_action="skip",
            split_map=splits,
        )

    pooled = collect_system_runs(conn, dataset_id=DATASET, splits=splits, systems=("A", "B"))
    assert len(pooled.cases) == 7  # sanity: all 7 cases collected

    split = split_case_set_by_era(pooled, BOUNDARY)
    live = split["live-era"]
    assert len(live.cases) == 3  # sanity: only the 3 live-era cases

    pooled_metrics = agent_efficiency(
        conn,
        cfg,
        dataset_id=DATASET,
        splits=splits,
        system="B",
        reference="A",
        case_set=pooled,
    )
    live_metrics = agent_efficiency(
        conn,
        cfg,
        dataset_id=DATASET,
        splits=splits,
        system="B",
        reference="A",
        case_set=live,
    )

    # Pooled: 4/7 agree overall; candidate (B) actions: quick_apply x4, wait
    # x1, skip x2.
    assert pooled_metrics.paired_cases == 7
    assert pooled_metrics.overall_agreement == pytest.approx(4 / 7)
    assert pooled_metrics.action_distribution == {"quick_apply": 4, "wait": 1, "skip": 2}

    # Live-era only: 1/3 agree overall; candidate (B) actions restricted to
    # the live-era cases: quick_apply x1, skip x2. Deliberately different
    # from the pooled figures above — this is the check that would fail if
    # `split_case_set_by_era` dropped a system and `_case_set_for` silently
    # rebuilt a pooled set instead of honouring the live-era one.
    assert live_metrics.paired_cases == 3
    assert live_metrics.overall_agreement == pytest.approx(1 / 3)
    assert live_metrics.action_distribution == {"quick_apply": 1, "skip": 2}

    assert live_metrics.paired_cases != pooled_metrics.paired_cases
    assert live_metrics.overall_agreement != pooled_metrics.overall_agreement
    assert live_metrics.action_distribution != pooled_metrics.action_distribution
