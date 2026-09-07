"""`rli.eval.baseline`: A/B baseline metrics over a replay dataset (PLAN.md M4).

Builds synthetic `companies` / `postings` / `runs` / `run_steps` rows
directly (the `replay_cases` / `replay_datasets` tables `rli.replay` may add
are NOT depended on here — see the module docstring's contract), using the
`conn` / `cfg` fixtures from `tests/conftest.py`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from rli.config import Config
from rli.eval.baseline import (
    ALLOWED_SPLITS,
    HoldoutSplitRequestedError,
    baseline_report,
    collect_cases,
    load_split_map,
    write_baseline_report,
)
from rli.models.time import to_utc_z

NOW = datetime(2026, 9, 7, tzinfo=UTC)
DATASET = "ds1"
COMPANY = "acme.com"


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
    first_observed: datetime | None = NOW - timedelta(days=100),
) -> None:
    _add_company(conn, company_id)
    conn.execute(
        """
        INSERT INTO postings
            (posting_id, company_id, ats, canonical_url, created_at, first_observed)
        VALUES (?, ?, 'greenhouse', ?, ?, ?)
        """,
        (
            posting_id,
            company_id,
            f"https://boards.greenhouse.io/{company_id}/jobs/{posting_id}",
            to_utc_z(NOW),
            None if first_observed is None else to_utc_z(first_observed),
        ),
    )


def _decision(action: str, *, posting_state: str = "open", evidence_quality: str = "mixed") -> str:
    return json.dumps(
        {
            "posting_state": posting_state,
            "recommended_action": action,
            "recheck_after_days": None,
            "evidence_quality": evidence_quality,
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
    input_url: str,
    replay_at: datetime,
    posting_id: str | None,
    action: str | None,
    dataset: str = DATASET,
    started_at: datetime | None = None,
    status: str = "completed",
    posting_state: str = "open",
    evidence_quality: str = "mixed",
    total_cost_usd: float = 0.0,
    total_latency_ms: int = 0,
    config_hash: str | None = None,
) -> None:
    final_decision = (
        None
        if action is None
        else _decision(action, posting_state=posting_state, evidence_quality=evidence_quality)
    )
    conn.execute(
        """
        INSERT INTO runs (id, posting_id, input_url, system, mode, replay_at, config_hash,
                           started_at, status, final_decision, total_cost_usd, total_latency_ms)
        VALUES (?, ?, ?, ?, 'replay', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            posting_id,
            input_url,
            system,
            to_utc_z(replay_at),
            config_hash or f"cfg:x|dataset:{dataset}",
            to_utc_z(started_at or NOW),
            status,
            final_decision,
            total_cost_usd,
            total_latency_ms,
        ),
    )


def _add_step(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    step_index: int,
    component: str = "probe",
    decision_type: str = "probe_run",
    probe_name: str | None,
    error: str | None = None,
    cost_usd: float = 0.0,
    latency_s: float = 0.0,
) -> None:
    conn.execute(
        """
        INSERT INTO run_steps (run_id, step_index, component, decision_type, probe_name,
                                cost_usd, latency_s, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            step_index,
            component,
            decision_type,
            probe_name,
            cost_usd,
            latency_s,
            error,
            to_utc_z(NOW),
        ),
    )


def _always_run_steps(conn: sqlite3.Connection, run_id: str) -> None:
    _add_step(conn, run_id, step_index=1, probe_name="resolve_posting", cost_usd=0.1, latency_s=0.1)
    _add_step(conn, run_id, step_index=2, probe_name="board_snapshot", cost_usd=0.1, latency_s=0.1)


# ---------------------------------------------------------------------------
# Overall vs macro divergence (hand-checked arithmetic)
# ---------------------------------------------------------------------------


def test_overall_vs_macro_agreement_diverges_on_imbalanced_classes(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # 10 cases: A says quick_apply x8, apply_now x2.
    # B agrees on all 8 quick_apply, but 0 of the 2 apply_now.
    # overall = 8/10 = 0.8 ; macro = mean(quick_apply=1.0, apply_now=0.0) = 0.5
    splits: dict[str, str] = {}
    for i in range(8):
        posting_id = f"qa-{i}"
        _add_posting(conn, posting_id)
        splits[posting_id] = "dev"
        url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"
        _add_run(
            conn,
            f"a-qa-{i}",
            system="A",
            input_url=url,
            replay_at=NOW,
            posting_id=posting_id,
            action="quick_apply",
        )
        _add_run(
            conn,
            f"b-qa-{i}",
            system="B",
            input_url=url,
            replay_at=NOW,
            posting_id=posting_id,
            action="quick_apply",
        )
    for i in range(2):
        posting_id = f"an-{i}"
        _add_posting(conn, posting_id)
        splits[posting_id] = "dev"
        url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"
        _add_run(
            conn,
            f"a-an-{i}",
            system="A",
            input_url=url,
            replay_at=NOW,
            posting_id=posting_id,
            action="apply_now",
        )
        _add_run(
            conn,
            f"b-an-{i}",
            system="B",
            input_url=url,
            replay_at=NOW,
            posting_id=posting_id,
            action="quick_apply",
        )
    conn.commit()

    case_set = collect_cases(conn, dataset_id=DATASET, splits=splits)
    assert case_set.paired == 10

    report = baseline_report(conn, cfg, dataset_id=DATASET, splits=splits)
    comparison = report.comparison
    assert comparison.overall_agreement == 0.8
    assert comparison.macro_agreement == 0.5
    assert comparison.per_class_agreement == {"apply_now": 0.0, "quick_apply": 1.0}
    assert comparison.per_class_counts == {"apply_now": 2, "quick_apply": 8}
    # confusion matrix
    assert comparison.confusion_matrix["quick_apply"] == {"quick_apply": 8}
    assert comparison.confusion_matrix["apply_now"] == {"quick_apply": 2}


# ---------------------------------------------------------------------------
# Split guard
# ---------------------------------------------------------------------------


def test_split_guard_rejects_test_split(conn: sqlite3.Connection, cfg: Config) -> None:
    try:
        collect_cases(conn, dataset_id=DATASET, splits={}, allowed_splits=("dev", "test"))
        raised = False
    except HoldoutSplitRequestedError:
        raised = True
    assert raised

    try:
        baseline_report(conn, cfg, dataset_id=DATASET, splits={}, allowed_splits=("test",))
        raised = False
    except HoldoutSplitRequestedError:
        raised = True
    assert raised


def test_posting_in_test_split_is_excluded_from_paired_set(conn: sqlite3.Connection) -> None:
    posting_id = "held-out"
    _add_posting(conn, posting_id)
    url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"
    _add_run(
        conn, "a-1", system="A", input_url=url, replay_at=NOW, posting_id=posting_id, action="skip"
    )
    _add_run(
        conn, "b-1", system="B", input_url=url, replay_at=NOW, posting_id=posting_id, action="skip"
    )
    conn.commit()

    splits = {posting_id: "test"}
    case_set = collect_cases(conn, dataset_id=DATASET, splits=splits)
    assert case_set.paired == 0
    assert case_set.excluded_holdout == 1
    assert case_set.unassigned == 0
    assert case_set.cases == ()


# ---------------------------------------------------------------------------
# Unassigned: no posting_id / posting absent from split map
# ---------------------------------------------------------------------------


def test_run_with_no_posting_id_is_unassigned(conn: sqlite3.Connection) -> None:
    url = "https://boards.greenhouse.io/acme.com/jobs/unresolved"
    _add_run(
        conn,
        "a-1",
        system="A",
        input_url=url,
        replay_at=NOW,
        posting_id=None,
        action=None,
        status="failed",
    )
    _add_run(
        conn,
        "b-1",
        system="B",
        input_url=url,
        replay_at=NOW,
        posting_id=None,
        action=None,
        status="failed",
    )
    conn.commit()

    case_set = collect_cases(conn, dataset_id=DATASET, splits={})
    assert case_set.unassigned == 1
    assert case_set.paired == 0
    assert case_set.a_only == 0
    assert case_set.b_only == 0


def test_posting_absent_from_split_map_is_unassigned(conn: sqlite3.Connection) -> None:
    posting_id = "no-split-info"
    _add_posting(conn, posting_id)
    url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"
    _add_run(
        conn, "a-1", system="A", input_url=url, replay_at=NOW, posting_id=posting_id, action="wait"
    )
    _add_run(
        conn, "b-1", system="B", input_url=url, replay_at=NOW, posting_id=posting_id, action="wait"
    )
    conn.commit()

    # splits map does not mention posting_id at all.
    case_set = collect_cases(conn, dataset_id=DATASET, splits={})
    assert case_set.unassigned == 1
    assert case_set.paired == 0


# ---------------------------------------------------------------------------
# A-only / B-only
# ---------------------------------------------------------------------------


def test_a_only_and_b_only_cases_counted_never_paired(conn: sqlite3.Connection) -> None:
    posting_a = "a-only-posting"
    posting_b = "b-only-posting"
    _add_posting(conn, posting_a)
    _add_posting(conn, posting_b)
    splits = {posting_a: "dev", posting_b: "validation"}

    url_a = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_a}"
    url_b = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_b}"
    _add_run(
        conn,
        "a-only-1",
        system="A",
        input_url=url_a,
        replay_at=NOW,
        posting_id=posting_a,
        action="apply_now",
    )
    _add_run(
        conn,
        "b-only-1",
        system="B",
        input_url=url_b,
        replay_at=NOW,
        posting_id=posting_b,
        action="skip",
    )
    conn.commit()

    case_set = collect_cases(conn, dataset_id=DATASET, splits=splits)
    assert case_set.a_only == 1
    assert case_set.b_only == 1
    assert case_set.paired == 0
    assert case_set.a_run_ids == ("a-only-1",)
    assert case_set.b_run_ids == ("b-only-1",)


# ---------------------------------------------------------------------------
# Duplicate re-runs collapse to the latest
# ---------------------------------------------------------------------------


def test_duplicate_reruns_collapse_to_latest(conn: sqlite3.Connection) -> None:
    posting_id = "dup-posting"
    _add_posting(conn, posting_id)
    splits = {posting_id: "dev"}
    url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"

    # Two A runs for the same case: an older failed attempt, then a fixed re-run.
    _add_run(
        conn,
        "a-old",
        system="A",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="wait",
        started_at=NOW - timedelta(hours=2),
    )
    _add_run(
        conn,
        "a-new",
        system="A",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
        started_at=NOW - timedelta(hours=1),
    )
    _add_run(
        conn,
        "b-1",
        system="B",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
    )
    conn.commit()

    case_set = collect_cases(conn, dataset_id=DATASET, splits=splits)
    assert case_set.duplicates_collapsed == 1
    assert case_set.paired == 1
    assert case_set.cases[0].a_run_id == "a-new"
    assert case_set.cases[0].a_action == "apply_now"


# ---------------------------------------------------------------------------
# Medium/high probe counting matches cost_tier_for; ratio None when A is 0
# ---------------------------------------------------------------------------


def test_medium_high_probe_counting_and_ratio(conn: sqlite3.Connection, cfg: Config) -> None:
    posting_id = "mh-posting"
    _add_posting(conn, posting_id)
    splits = {posting_id: "dev"}
    url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"

    _add_run(
        conn,
        "a-1",
        system="A",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
    )
    _always_run_steps(conn, "a-1")
    _add_step(conn, "a-1", step_index=3, probe_name="requirements_drift")  # medium
    _add_step(conn, "a-1", step_index=4, probe_name="company_events")  # medium
    _add_step(conn, "a-1", step_index=5, probe_name="team_signal")  # high

    _add_run(
        conn,
        "b-1",
        system="B",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
    )
    _always_run_steps(conn, "b-1")
    _add_step(conn, "b-1", step_index=3, probe_name="company_events")  # medium
    conn.commit()

    report = baseline_report(conn, cfg, dataset_id=DATASET, splits=splits)
    a_metrics = report.systems["A"]
    b_metrics = report.systems["B"]

    # always-run pair excluded from medium/high; low tier for the always-run pair.
    assert a_metrics.medium_high_probe_steps == 3
    assert b_metrics.medium_high_probe_steps == 1
    assert a_metrics.probe_counts_by_tier["low"] == 2
    assert a_metrics.probe_counts_by_tier["medium"] == 2
    assert a_metrics.probe_counts_by_tier["high"] == 1

    assert report.comparison.medium_high_ratio == 1 / 3


def test_medium_high_ratio_is_none_when_a_has_zero(conn: sqlite3.Connection, cfg: Config) -> None:
    posting_id = "mh-zero"
    _add_posting(conn, posting_id)
    splits = {posting_id: "dev"}
    url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"

    _add_run(
        conn,
        "a-1",
        system="A",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
    )
    _always_run_steps(conn, "a-1")
    _add_run(
        conn,
        "b-1",
        system="B",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
    )
    _always_run_steps(conn, "b-1")
    conn.commit()

    report = baseline_report(conn, cfg, dataset_id=DATASET, splits=splits)
    assert report.comparison.medium_high_ratio is None


# ---------------------------------------------------------------------------
# Cost / latency / failure aggregation
# ---------------------------------------------------------------------------


def test_cost_latency_failure_aggregation(conn: sqlite3.Connection, cfg: Config) -> None:
    posting_id = "cost-posting"
    _add_posting(conn, posting_id)
    splits = {posting_id: "dev"}
    url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"

    _add_run(
        conn,
        "a-1",
        system="A",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
        total_cost_usd=5.0,
        total_latency_ms=1000,
    )
    _add_step(conn, "a-1", step_index=1, probe_name="resolve_posting", cost_usd=2.0, latency_s=0.5)
    _add_step(
        conn,
        "a-1",
        step_index=2,
        probe_name="board_snapshot",
        cost_usd=1.0,
        latency_s=0.2,
        error="boom",
    )

    _add_run(
        conn,
        "b-1",
        system="B",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
        total_cost_usd=1.0,
        total_latency_ms=200,
    )
    conn.commit()

    report = baseline_report(conn, cfg, dataset_id=DATASET, splits=splits)
    a_metrics = report.systems["A"]
    assert a_metrics.total_cost_usd == 5.0
    assert a_metrics.mean_cost_usd == 5.0
    assert a_metrics.total_latency_ms == 1000
    assert a_metrics.mean_latency_ms == 1000
    assert a_metrics.failed_steps == 1
    assert a_metrics.failure_counts == {"board_snapshot": 1}


# ---------------------------------------------------------------------------
# write_baseline_report
# ---------------------------------------------------------------------------


def test_write_baseline_report_contains_limitations_and_key_numbers(
    conn: sqlite3.Connection, cfg: Config, tmp_path
) -> None:
    posting_id = "report-posting"
    _add_posting(conn, posting_id)
    splits = {posting_id: "dev"}
    url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"

    _add_run(
        conn,
        "a-1",
        system="A",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
    )
    _add_run(
        conn,
        "b-1",
        system="B",
        input_url=url,
        replay_at=NOW,
        posting_id=posting_id,
        action="apply_now",
    )
    conn.commit()

    report = baseline_report(conn, cfg, dataset_id=DATASET, splits=splits)
    out_path = tmp_path / "nested" / "baseline.md"
    result = write_baseline_report(out_path, report)

    assert result == out_path
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "cost points" in text
    assert "lower bound on wall clock" in text
    assert "Splits included" in text
    assert "test" in text.lower()  # mentions the excluded test holdout
    assert "action distribution" in text.lower()
    assert "100.0%" in text  # overall agreement for this fully-agreeing pair
    assert DATASET in text


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_empty_input_produces_valid_zero_report(conn: sqlite3.Connection, cfg: Config) -> None:
    report = baseline_report(conn, cfg, dataset_id=DATASET, splits={})
    assert report.case_set.paired == 0
    assert report.case_set.a_only == 0
    assert report.case_set.b_only == 0
    assert report.comparison.paired_cases == 0
    assert report.comparison.overall_agreement is None
    assert report.comparison.macro_agreement is None
    assert report.systems["A"].runs == 0
    assert report.systems["B"].runs == 0
    assert "describe" not in report.describe()  # sanity: describe() renders, doesn't crash
    assert ALLOWED_SPLITS == ("dev", "validation")


# ---------------------------------------------------------------------------
# load_split_map delegates to rli.policy.splits
# ---------------------------------------------------------------------------


def test_load_split_map_temporal(conn: sqlite3.Connection) -> None:
    old_posting = "old-posting"
    new_posting = "new-posting"
    _add_posting(conn, old_posting, first_observed=NOW - timedelta(days=200))
    _add_posting(conn, new_posting, first_observed=NOW - timedelta(days=1))
    conn.commit()

    split_map = load_split_map(conn, cutoff=NOW - timedelta(days=30), split_kind="temporal")
    assert split_map[old_posting] == "dev"
    assert split_map[new_posting] == "test"


def test_load_split_map_rejects_bad_kind(conn: sqlite3.Connection) -> None:
    try:
        load_split_map(conn, cutoff=NOW, split_kind="bogus")  # type: ignore[arg-type]
        raised = False
    except ValueError:
        raised = True
    assert raised
