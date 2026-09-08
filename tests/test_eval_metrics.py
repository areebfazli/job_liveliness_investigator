"""`rli.eval.metrics`: spec.md §6 evaluation metrics (PLAN.md M6).

Every fixture in this file is built by inserting synthetic `companies` /
`postings` / `runs` / `run_steps` / `evidence` / `replay_datasets` /
`replay_cases` rows directly into the tmp SQLite database from
`tests/conftest.py`'s `conn` fixture. The arithmetic each test asserts is
hand-computed in a comment above it, so a failing assertion says which
definition moved rather than only that a number changed.

Two invariants get the most attention here because they are the ones a
future refactor is most likely to break silently:

* macro agreement is NOT overall agreement (spec.md §6 requires both,
  precisely so a default-heavy policy cannot pass on the majority class);
* probe COST POINTS and model DOLLARS are never summed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rli.config import Config
from rli.eval.baseline import HoldoutSplitRequestedError
from rli.eval.metrics import (
    ALL_SPLITS,
    DEFAULT_SPLITS,
    SYSTEM_A_CAVEAT,
    agent_efficiency,
    collect_system_runs,
    data_quality,
    read_match_precision,
    resolve_allowed_splits,
    split_map_for_dataset,
)
from rli.models.time import to_utc_z

NOW = datetime(2026, 9, 7, tzinfo=UTC)
DATASET = "ds-m6"
COMPANY = "acme.com"


# ---------------------------------------------------------------------------
# Row builders
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
    ats: str = "greenhouse",
    first_observed: datetime | None = NOW - timedelta(days=100),
) -> None:
    _add_company(conn, company_id)
    conn.execute(
        """
        INSERT INTO postings
            (posting_id, company_id, ats, canonical_url, created_at, first_observed)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            posting_id,
            company_id,
            ats,
            f"https://boards.greenhouse.io/{company_id}/jobs/{posting_id}",
            to_utc_z(NOW),
            None if first_observed is None else to_utc_z(first_observed),
        ),
    )


def _decision(
    action: str | None,
    *,
    posting_state: str = "open",
    evidence_quality: str = "mixed",
    reasons: list[dict[str, object]] | None = None,
    evidence: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "posting_state": posting_state,
            "recommended_action": action,
            "recheck_after_days": None,
            "evidence_quality": evidence_quality,
            "hypotheses": [],
            "reason": reasons or [],
            "evidence": evidence or [],
        }
    )


def _add_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    system: str,
    posting_id: str,
    action: str | None,
    input_url: str | None = None,
    replay_at: datetime = NOW,
    dataset: str = DATASET,
    status: str = "completed",
    started_at: datetime | None = None,
    total_latency_ms: int = 0,
    final_decision: str | None = "",
    mode: str = "replay",
    config_hash: str | None = None,
) -> str:
    """Insert one replay run. Returns the run id for convenience.

    `final_decision=""` (the default sentinel) means "build one from
    `action`"; pass `None` explicitly for a run whose decision is NULL, or a
    literal string for a corrupt blob.
    """
    url = input_url or f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"
    decision = _decision(action) if final_decision == "" else final_decision
    conn.execute(
        """
        INSERT INTO runs (id, posting_id, input_url, system, mode, replay_at, config_hash,
                          started_at, status, final_decision, total_cost_usd, total_latency_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            posting_id,
            url,
            system,
            mode,
            to_utc_z(replay_at) if mode == "replay" else None,
            config_hash if config_hash is not None else f"cfg:x|dataset:{dataset}",
            to_utc_z(started_at or NOW),
            status,
            decision,
            None,
            total_latency_ms,
        ),
    )
    return run_id


_STEP_COUNTER = {"n": 0}


def _add_step(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    component: str = "probe",
    decision_type: str = "probe_run",
    probe_name: str | None = None,
    args_hash: str | None = None,
    cost_usd: float = 0.0,
    error: str | None = None,
    step_index: int | None = None,
) -> None:
    _STEP_COUNTER["n"] += 1
    conn.execute(
        """
        INSERT INTO run_steps (run_id, step_index, component, decision_type, probe_name,
                               args_hash, cost_usd, latency_s, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            _STEP_COUNTER["n"] if step_index is None else step_index,
            component,
            decision_type,
            probe_name,
            args_hash,
            cost_usd,
            0.0,
            error,
            to_utc_z(NOW),
        ),
    )


def _add_probes(conn: sqlite3.Connection, run_id: str, *names: str) -> None:
    """The always-run pair plus `names`, each as one successful `probe_run` step."""
    for name in ("resolve_posting", "board_snapshot", *names):
        _add_step(conn, run_id, probe_name=name, args_hash=f"{name}-args", cost_usd=1.0)


def _add_evidence(
    conn: sqlite3.Connection,
    run_id: str,
    evidence_id: str,
    *,
    probe: str,
    claim_type: str,
    source_quality: str = "ats_native",
    posting_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO evidence (id, run_id, posting_id, probe, claim_type, value, source_url,
                              source_quality, available_at, fetched_at)
        VALUES (?, ?, ?, ?, ?, 'v', 'https://example.com/x', ?, ?, ?)
        """,
        (
            evidence_id,
            run_id,
            posting_id,
            probe,
            claim_type,
            source_quality,
            to_utc_z(NOW),
            to_utc_z(NOW),
        ),
    )


def _pair(
    conn: sqlite3.Connection,
    posting_id: str,
    *,
    a_action: str | None,
    b_action: str | None,
    a_probes: tuple[str, ...] = (),
    b_probes: tuple[str, ...] = (),
    split_map: dict[str, str] | None = None,
    split: str = "dev",
) -> tuple[str, str]:
    """One replay case with an A run and a B run, plus their probe steps."""
    _add_posting(conn, posting_id)
    if split_map is not None:
        split_map[posting_id] = split
    a_id = _add_run(conn, f"a-{posting_id}", system="A", posting_id=posting_id, action=a_action)
    b_id = _add_run(conn, f"b-{posting_id}", system="B", posting_id=posting_id, action=b_action)
    _add_probes(conn, a_id, *a_probes)
    _add_probes(conn, b_id, *b_probes)
    return a_id, b_id


# ---------------------------------------------------------------------------
# Split permission
# ---------------------------------------------------------------------------


def test_resolve_allowed_splits_refuses_test_without_permission() -> None:
    assert resolve_allowed_splits() == DEFAULT_SPLITS
    assert resolve_allowed_splits(("dev",)) == ("dev",)
    # Order is canonicalised to ALL_SPLITS order, and duplicates collapse.
    assert resolve_allowed_splits(("validation", "dev", "dev")) == ("dev", "validation")

    with pytest.raises(HoldoutSplitRequestedError):
        resolve_allowed_splits(("test",))
    with pytest.raises(HoldoutSplitRequestedError):
        resolve_allowed_splits(("dev", "test"))

    assert resolve_allowed_splits(("test",), allow_test=True) == ("test",)
    assert resolve_allowed_splits(None, allow_test=True) == ALL_SPLITS

    with pytest.raises(ValueError):
        resolve_allowed_splits(("holdout",))


def test_split_map_defaults_to_the_datasets_own_split_kind(conn: sqlite3.Connection) -> None:
    _add_posting(conn, "p1")
    conn.execute(
        """
        INSERT INTO replay_datasets (dataset_id, created_at, split_kind, split_name,
                                     grid_step_days, postings, companies, cases)
        VALUES (?, ?, 'temporal', 'dev', 7, 1, 1, 1)
        """,
        (DATASET, to_utc_z(NOW)),
    )
    conn.execute(
        """
        INSERT INTO replay_cases (dataset_id, posting_id, replay_at, company_id, canonical_url,
                                  built_at)
        VALUES (?, 'p1', ?, ?, 'https://example.com/p1', ?)
        """,
        (DATASET, to_utc_z(NOW), COMPANY, to_utc_z(NOW)),
    )

    splits, kind = split_map_for_dataset(conn, dataset_id=DATASET)
    assert kind == "temporal"
    assert splits["p1"] in ALL_SPLITS

    # An unknown dataset falls back to the stricter company split rather than
    # raising — the caller gets a usable map and a truthful `split_kind`.
    _, fallback_kind = split_map_for_dataset(conn, dataset_id="does-not-exist")
    assert fallback_kind == "company"


# ---------------------------------------------------------------------------
# Agreement: overall vs macro, and where the classes come from
# ---------------------------------------------------------------------------


def test_macro_agreement_is_not_overall_agreement_on_imbalanced_classes(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # 10 cases. Reference A: quick_apply x8, apply_now x2.
    # Candidate B agrees on all 8 quick_apply and on none of the 2 apply_now
    # (it answers `wait` there, an action A never produces).
    #   overall = 8/10                       = 0.80
    #   macro   = mean(quick_apply=8/8, apply_now=0/2) = 0.50
    splits: dict[str, str] = {}
    for i in range(8):
        _pair(conn, f"qa{i}", a_action="quick_apply", b_action="quick_apply", split_map=splits)
    for i in range(2):
        _pair(conn, f"an{i}", a_action="apply_now", b_action="wait", split_map=splits)

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )

    assert metrics.paired_cases == 10
    assert metrics.overall_agreement == pytest.approx(0.8)
    assert metrics.macro_agreement == pytest.approx(0.5)
    assert metrics.overall_agreement != metrics.macro_agreement

    # The per-class classes are the REFERENCE's actions. `wait` is something
    # only the candidate produced, so it is not a class.
    assert set(metrics.per_class_agreement) == {"quick_apply", "apply_now"}
    assert metrics.per_class_agreement["quick_apply"] == pytest.approx(1.0)
    assert metrics.per_class_agreement["apply_now"] == pytest.approx(0.0)
    assert metrics.per_class_counts == {"quick_apply": 8, "apply_now": 2}
    assert "wait" not in metrics.per_class_agreement

    # ...and it shows up in the confusion matrix instead, keyed by A's action.
    assert metrics.confusion_matrix["apply_now"] == {"wait": 2}

    assert metrics.structural_caveat == SYSTEM_A_CAVEAT
    assert "macro" in metrics.describe()
    assert str(metrics) == metrics.describe()


def test_action_distributions_are_reported_alongside_agreement(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # spec.md §6: agreement must be "reported with the action distribution so a
    # default-heavy policy cannot pass trivially". A candidate answering
    # quick_apply to everything scores 8/10 overall here; the distributions
    # are what make that visible.
    splits: dict[str, str] = {}
    for i in range(8):
        _pair(conn, f"qa{i}", a_action="quick_apply", b_action="quick_apply", split_map=splits)
    for i in range(2):
        _pair(conn, f"sk{i}", a_action="skip", b_action="quick_apply", split_map=splits)

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    assert metrics.action_distribution == {"quick_apply": 10}
    assert metrics.reference_action_distribution == {"quick_apply": 8, "skip": 2}
    assert metrics.overall_agreement == pytest.approx(0.8)
    assert metrics.macro_agreement == pytest.approx(0.5)


def test_self_comparison_is_trivially_perfect(conn: sqlite3.Connection, cfg: Config) -> None:
    # A vs A is how the report gets A's own cost/probe block; it must not be
    # special-cased away.
    splits: dict[str, str] = {}
    _pair(conn, "p1", a_action="wait", b_action="skip", split_map=splits)
    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="A", reference="A"
    )
    assert metrics.overall_agreement == pytest.approx(1.0)
    assert metrics.macro_agreement == pytest.approx(1.0)


def test_missing_decisions_are_counted_not_raised(conn: sqlite3.Connection, cfg: Config) -> None:
    splits: dict[str, str] = {}
    _add_posting(conn, "null-decision")
    splits["null-decision"] = "dev"
    _add_run(conn, "a-null", system="A", posting_id="null-decision", action=None)
    _add_run(
        conn, "b-null", system="B", posting_id="null-decision", action=None, final_decision=None
    )
    _add_posting(conn, "corrupt")
    splits["corrupt"] = "dev"
    _add_run(conn, "a-corrupt", system="A", posting_id="corrupt", action="wait")
    _add_run(
        conn,
        "b-corrupt",
        system="B",
        posting_id="corrupt",
        action=None,
        final_decision="{not json",
    )

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    assert metrics.decisions_missing == 2
    assert metrics.action_distribution["(missing)"] == 2
    # A reference case with no action can never be a match, but stays in the
    # denominator: 0/2.
    assert metrics.overall_agreement == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Early-stop regret and its counterpart
# ---------------------------------------------------------------------------


def test_early_stop_regret_needs_an_opportunity_and_a_disagreement(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # Three cases:
    #   regret     A ran company_events, B did not, and the actions DIFFER
    #   no-regret  A ran company_events, B did not, and the actions AGREE
    #   no-chance  both ran the same probes -> not an opportunity at all
    # opportunities = 2, regret = 1, rate = 1/2.
    # `reference_extra_probes_no_action_change` counts the probes A spent on
    # the agreeing case (1) plus any on `no-chance` (0).
    splits: dict[str, str] = {}
    _pair(
        conn,
        "regret",
        a_action="wait",
        b_action="quick_apply",
        a_probes=("company_events",),
        split_map=splits,
    )
    _pair(
        conn,
        "agree",
        a_action="quick_apply",
        b_action="quick_apply",
        a_probes=("company_events",),
        split_map=splits,
    )
    _pair(
        conn,
        "same",
        a_action="skip",
        b_action="skip",
        a_probes=("repost_history",),
        b_probes=("repost_history",),
        split_map=splits,
    )

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    assert metrics.early_stop_opportunities == 2
    assert metrics.early_stop_regret_cases == 1
    assert metrics.early_stop_regret_rate == pytest.approx(0.5)
    assert metrics.reference_extra_probes_no_action_change == 1


def test_early_stop_regret_rate_is_none_without_opportunities(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    splits: dict[str, str] = {}
    _pair(
        conn,
        "same",
        a_action="wait",
        b_action="skip",
        a_probes=("company_events",),
        b_probes=("company_events",),
        split_map=splits,
    )
    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    # The systems disagree, but B never stopped early — there is nothing to
    # regret, and the rate must be None rather than a misleading 0.0.
    assert metrics.early_stop_opportunities == 0
    assert metrics.early_stop_regret_rate is None


# ---------------------------------------------------------------------------
# Unnecessary probes / repeated calls / invalid arguments
# ---------------------------------------------------------------------------


def test_unnecessary_probe_steps_are_dynamic_executions_with_no_evidence(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # B runs two dynamic probes: `company_events` records nothing, while
    # `requirements_drift` records one evidence row. 1 of 2 dynamic
    # executions is "unnecessary" under the literal reading.
    splits: dict[str, str] = {}
    _add_posting(conn, "p1")
    splits["p1"] = "dev"
    _add_run(conn, "a-p1", system="A", posting_id="p1", action="wait")
    _add_probes(conn, "a-p1")
    _add_run(conn, "b-p1", system="B", posting_id="p1", action="wait")
    _add_probes(conn, "b-p1", "company_events", "requirements_drift")
    _add_evidence(
        conn, "b-p1", "e1", probe="requirements_drift", claim_type="requirements_unchanged"
    )
    # An evidence row from a DIFFERENT probe must not rescue company_events.
    _add_evidence(conn, "b-p1", "e2", probe="board_snapshot", claim_type="board_present")

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    assert metrics.unnecessary_probe_steps == 1
    assert metrics.unnecessary_probe_rate == pytest.approx(0.5)


def test_repeated_calls_need_the_same_probe_and_the_same_args_hash(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    splits: dict[str, str] = {}
    _add_posting(conn, "dup")
    _add_posting(conn, "distinct")
    splits["dup"] = splits["distinct"] = "dev"
    for posting_id in ("dup", "distinct"):
        _add_run(conn, f"a-{posting_id}", system="A", posting_id=posting_id, action="wait")
        _add_probes(conn, f"a-{posting_id}")
        _add_run(conn, f"b-{posting_id}", system="B", posting_id=posting_id, action="wait")
        _add_probes(conn, f"b-{posting_id}")

    # Same (probe_name, args_hash) twice in one run: exactly one repeat.
    _add_step(conn, "b-dup", probe_name="company_events", args_hash="h1", cost_usd=3.0)
    _add_step(conn, "b-dup", probe_name="company_events", args_hash="h1", cost_usd=3.0)
    # Same probe, DIFFERENT args: not a repeat.
    _add_step(conn, "b-distinct", probe_name="company_events", args_hash="h1", cost_usd=3.0)
    _add_step(conn, "b-distinct", probe_name="company_events", args_hash="h2", cost_usd=3.0)

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    assert metrics.repeated_calls == 1


def test_invalid_arguments_come_from_controller_rejection_rows(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    splits: dict[str, str] = {}
    _pair(conn, "p1", a_action="wait", b_action="wait", split_map=splits)
    for _ in range(2):
        _add_step(
            conn,
            "b-p1",
            component="controller",
            decision_type="candidate_rejected:invalid_args",
            probe_name="company_events",
        )
    # Other rejection reasons are real controller behaviour, not a bug, and
    # must not inflate the invalid-argument count.
    _add_step(
        conn,
        "b-p1",
        component="controller",
        decision_type="candidate_rejected:duplicate",
        probe_name="company_events",
    )
    _add_step(
        conn,
        "b-p1",
        component="controller",
        decision_type="candidate_rejected:ineligible",
        probe_name="team_signal",
    )

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    assert metrics.invalid_arguments == 2


def test_recovery_after_a_failed_probe(conn: sqlite3.Connection, cfg: Config) -> None:
    splits: dict[str, str] = {}
    _add_posting(conn, "recovered")
    _add_posting(conn, "lost")
    splits["recovered"] = splits["lost"] = "dev"
    for posting_id, status in (("recovered", "completed"), ("lost", "failed")):
        _add_run(conn, f"a-{posting_id}", system="A", posting_id=posting_id, action="wait")
        _add_probes(conn, f"a-{posting_id}")
        _add_run(
            conn,
            f"b-{posting_id}",
            system="B",
            posting_id=posting_id,
            action="wait",
            status=status,
        )
        _add_probes(conn, f"b-{posting_id}")
        _add_step(
            conn,
            f"b-{posting_id}",
            probe_name="company_events",
            args_hash="h",
            error="HTTP 500 from the news source",
        )

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    assert metrics.runs_with_failed_probe == 2
    assert metrics.recovered_runs == 1
    assert metrics.recovery_rate == pytest.approx(0.5)


def test_recovery_rate_is_none_when_nothing_failed(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    splits: dict[str, str] = {}
    _pair(conn, "p1", a_action="wait", b_action="wait", split_map=splits)
    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    assert metrics.runs_with_failed_probe == 0
    assert metrics.recovery_rate is None


# ---------------------------------------------------------------------------
# Cost: points and dollars, never summed
# ---------------------------------------------------------------------------


def test_probe_points_and_model_dollars_are_reported_separately(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # One System C run with:
    #   probe steps  -> 1 + 1 + 3   = 5 unitless COST POINTS
    #   model steps  -> 0.02 + 0.01 = $0.03 of REAL money
    # `run_steps.cost_usd` holds both. Adding them would produce "5.03",
    # a number in no unit at all.
    splits: dict[str, str] = {}
    _add_posting(conn, "p1")
    splits["p1"] = "dev"
    _add_run(conn, "a-p1", system="A", posting_id="p1", action="wait")
    _add_probes(conn, "a-p1")
    _add_run(conn, "c-p1", system="C", posting_id="p1", action="wait", total_latency_ms=1500)
    _add_probes(conn, "c-p1", "company_events")
    conn.execute(
        "UPDATE run_steps SET cost_usd = 3.0 WHERE run_id = 'c-p1' AND probe_name = ?",
        ("company_events",),
    )
    _add_step(
        conn,
        "c-p1",
        component="model",
        decision_type="investigator:tokens=1200/340",
        cost_usd=0.02,
    )
    _add_step(
        conn,
        "c-p1",
        component="model",
        decision_type="explanation:tokens=800/120",
        cost_usd=0.01,
    )
    # A failed model call carries no `:tokens=` suffix at all.
    _add_step(conn, "c-p1", component="model", decision_type="investigator", cost_usd=0.0)

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="C", reference="A"
    )
    cost = metrics.cost
    assert cost.probe_cost_points == pytest.approx(5.0)
    assert cost.model_cost_usd == pytest.approx(0.03)
    assert cost.probe_steps == 3
    assert cost.model_steps == 3
    assert cost.input_tokens == 2000
    assert cost.output_tokens == 460
    assert cost.mean_probe_cost_points == pytest.approx(5.0)
    assert cost.mean_model_cost_usd == pytest.approx(0.03)

    # There is deliberately no combined figure anywhere on the model.
    assert not hasattr(cost, "total")
    assert not hasattr(cost, "total_cost_usd")
    assert cost.probe_cost_points != cost.model_cost_usd
    rendered = cost.describe()
    assert "POINTS" in rendered and "USD" in rendered

    assert metrics.total_latency_ms == pytest.approx(1500.0)
    assert metrics.mean_latency_ms == pytest.approx(1500.0)


def test_medium_high_probe_steps_use_the_shared_cost_tier_table(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # resolve_posting/board_snapshot/repost_history are low; requirements_drift
    # and company_events are medium. 2 medium steps over 1 run.
    splits: dict[str, str] = {}
    _add_posting(conn, "p1")
    splits["p1"] = "dev"
    _add_run(conn, "a-p1", system="A", posting_id="p1", action="wait")
    _add_probes(conn, "a-p1")
    _add_run(conn, "b-p1", system="B", posting_id="p1", action="wait")
    _add_probes(conn, "b-p1", "repost_history", "requirements_drift", "company_events")

    metrics = agent_efficiency(
        conn, cfg, dataset_id=DATASET, splits=splits, system="B", reference="A"
    )
    assert metrics.probe_counts_by_tier == {"low": 3, "medium": 2}
    assert metrics.medium_high_probe_steps == 2
    assert metrics.mean_medium_high_probes_per_run == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Case-set accounting
# ---------------------------------------------------------------------------


def test_case_set_excludes_unassigned_and_holdout_and_collapses_duplicates(
    conn: sqlite3.Connection,
) -> None:
    splits: dict[str, str] = {}
    _pair(conn, "in-dev", a_action="wait", b_action="wait", split_map=splits)
    _pair(conn, "in-test", a_action="wait", b_action="wait", split_map=splits, split="test")
    # No split assignment at all -> `unassigned`, never silently included.
    _pair(conn, "unmapped", a_action="wait", b_action="wait")
    # A second A run for the dev case: collapsed, and the LATEST wins.
    _add_run(
        conn,
        "a-in-dev-2",
        system="A",
        posting_id="in-dev",
        action="skip",
        started_at=NOW + timedelta(hours=1),
    )
    # A run of a different dataset must not leak in.
    _add_run(conn, "a-other", system="A", posting_id="in-dev", action="wait", dataset="other")
    # `rli.replay.build.case_state_at`'s inspection runs end in `|case_state`.
    _add_run(
        conn,
        "a-inspect",
        system="A",
        posting_id="in-dev",
        action="wait",
        config_hash="cfg:x|case_state",
    )

    case_set = collect_system_runs(conn, dataset_id=DATASET, splits=splits, systems=("A", "B"))
    assert len(case_set.cases) == 1
    assert case_set.cases[0].posting_id == "in-dev"
    assert case_set.cases[0].runs["A"].action == "skip"
    assert case_set.excluded_holdout == 1
    assert case_set.unassigned == 1
    assert case_set.duplicates_collapsed == 1
    assert case_set.counts_by_system == {"A": 1, "B": 1}
    assert case_set.cases_for("A", "B") == case_set.cases
    assert "duplicates_collapsed=1" in case_set.describe()
    assert str(case_set) == case_set.describe()


def test_case_set_can_read_the_holdout_when_the_caller_permits_it(
    conn: sqlite3.Connection,
) -> None:
    splits: dict[str, str] = {}
    _pair(conn, "in-test", a_action="wait", b_action="wait", split_map=splits, split="test")
    allowed = resolve_allowed_splits(("test",), allow_test=True)
    case_set = collect_system_runs(
        conn, dataset_id=DATASET, splits=splits, systems=("A", "B"), allowed_splits=allowed
    )
    assert len(case_set.cases) == 1
    assert case_set.excluded_holdout == 0
    assert "'test' split is IN SCOPE" in case_set.describe()


def test_a_system_with_no_runs_is_reported_as_zero_not_dropped(
    conn: sqlite3.Connection,
) -> None:
    splits: dict[str, str] = {}
    _pair(conn, "p1", a_action="wait", b_action="wait", split_map=splits)
    case_set = collect_system_runs(
        conn, dataset_id=DATASET, splits=splits, systems=("A", "B", "C")
    )
    assert case_set.counts_by_system == {"A": 1, "B": 1, "C": 0}
    assert case_set.cases_for("A", "B", "C") == ()


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------


def test_ats_resolution_rate_and_distribution(conn: sqlite3.Connection, cfg: Config) -> None:
    splits: dict[str, str] = {}
    _add_posting(conn, "ok", ats="greenhouse")
    _add_posting(conn, "broken", ats="lever")
    splits["ok"] = splits["broken"] = "dev"

    _add_run(conn, "a-ok", system="A", posting_id="ok", action="wait")
    _add_probes(conn, "a-ok")
    _add_run(conn, "a-broken", system="A", posting_id="broken", action=None)
    _add_step(
        conn,
        "a-broken",
        probe_name="resolve_posting",
        args_hash="h",
        error="404 from the ATS",
    )

    quality = data_quality(conn, cfg, dataset_id=DATASET, splits=splits, systems=("A",))
    assert quality.runs_checked == 2
    assert quality.postings_checked == 2
    assert quality.ats_resolved_runs == 1
    assert quality.ats_resolution_rate == pytest.approx(0.5)
    assert quality.ats_distribution == {"greenhouse": 1, "lever": 1}
    assert str(quality) == quality.describe()


def test_publish_date_coverage_is_bucketed_by_source_quality(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # Two runs. The first carries publish evidence from two different source
    # qualities (so it appears under BOTH buckets, once each); the second
    # carries only a posting-state claim, which is not a publish date.
    splits: dict[str, str] = {}
    for posting_id in ("p1", "p2"):
        _add_posting(conn, posting_id)
        splits[posting_id] = "dev"
        _add_run(conn, f"a-{posting_id}", system="A", posting_id=posting_id, action="wait")
        _add_probes(conn, f"a-{posting_id}")
    _add_evidence(
        conn, "a-p1", "e1", probe="resolve_posting", claim_type="first_published",
        source_quality="ats_native",
    )
    _add_evidence(
        conn, "a-p1", "e2", probe="board_snapshot", claim_type="updated_at",
        source_quality="archive",
    )
    _add_evidence(
        conn, "a-p2", "e1", probe="board_snapshot", claim_type="board_present",
        source_quality="archive",
    )

    quality = data_quality(conn, cfg, dataset_id=DATASET, splits=splits, systems=("A",))
    assert quality.publish_date_runs == 1
    assert quality.publish_date_coverage == pytest.approx(0.5)
    assert quality.publish_date_by_source_quality == {"ats_native": 1, "archive": 1}


def test_citation_support_separates_missing_ids_from_unsupported_from_unclassified(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # Five reasons on one run, with e1 in the `evidence` table (claim_type
    # posting_state) and e2 present only in the decision blob (first_published):
    #   A "still listed ... job board", cites e1  -> ids ok, posting_state, SUPPORTED
    #   B "still listed ...",           cites e9  -> id MISSING, classified, unsupported
    #   C "Nothing conclusive ...",     cites e2  -> ids ok, UNCLASSIFIED
    #   D "was removed from the board", cites []  -> NO ids, classified, unsupported
    #   E "requirements changed ... posted", cites e1 -> ids ok, but posting_state
    #                                        is in neither the requirements nor
    #                                        the publish family -> unsupported
    # id_existence_rate = 3/5 ; support_rate = 1/4 (of the four classified).
    splits: dict[str, str] = {}
    _add_posting(conn, "p1")
    splits["p1"] = "dev"
    reasons: list[dict[str, object]] = [
        {
            "text": "The posting was still listed on the company's job board.",
            "evidence_ids": ["e1"],
        },
        {"text": "It is still listed according to the board snapshot.", "evidence_ids": ["e9"]},
        {"text": "Nothing conclusive was determinable.", "evidence_ids": ["e2"]},
        {"text": "The role was removed from the board.", "evidence_ids": []},
        {"text": "The requirements changed since it was posted.", "evidence_ids": ["e1"]},
    ]
    decision_evidence: list[dict[str, object]] = [
        {"id": "e2", "probe": "resolve_posting", "claim_type": "first_published"}
    ]
    conn.execute(
        """
        INSERT INTO runs (id, posting_id, input_url, system, mode, replay_at, config_hash,
                          started_at, status, final_decision, total_latency_ms)
        VALUES ('a-p1', 'p1', 'https://example.com/p1', 'A', 'replay', ?, ?, ?, 'completed', ?, 0)
        """,
        (
            to_utc_z(NOW),
            f"cfg:x|dataset:{DATASET}",
            to_utc_z(NOW),
            _decision("wait", reasons=reasons, evidence=decision_evidence),
        ),
    )
    _add_probes(conn, "a-p1")
    _add_evidence(conn, "a-p1", "e1", probe="board_snapshot", claim_type="posting_state")

    citation = data_quality(
        conn, cfg, dataset_id=DATASET, splits=splits, systems=("A",)
    ).citation
    assert citation.runs_checked == 1
    assert citation.runs_with_reasons == 1
    assert citation.reasons_total == 5
    assert citation.reasons_all_ids_exist == 3
    assert citation.reasons_with_missing_ids == 1
    assert citation.reasons_with_no_ids == 1
    assert citation.id_existence_rate == pytest.approx(3 / 5)
    assert citation.reasons_classified == 4
    assert citation.reasons_unclassified == 1
    assert citation.reasons_supported == 1
    assert citation.reasons_unsupported == 3
    assert citation.support_rate == pytest.approx(1 / 4)
    # An unclassified reason is neither supported nor unsupported.
    assert citation.reasons_supported + citation.reasons_unsupported == (
        citation.reasons_classified
    )
    assert citation.families_seen["posting_state"] == 3
    assert str(citation) == citation.describe()


def test_read_match_precision_is_pending_until_the_memo_exists(tmp_path: Path) -> None:
    absent = tmp_path / "match_precision.md"
    value, note = read_match_precision(absent)
    assert value is None
    assert note.startswith("pending")
    assert str(absent) in note

    absent.write_text(
        "# Repost match precision\n\nHand-labeled 40 pairs; precision was 87% on that sample.\n",
        encoding="utf-8",
    )
    value, note = read_match_precision(absent)
    assert value == pytest.approx(0.87)
    assert "87.0%" in note

    fraction = tmp_path / "frac.md"
    fraction.write_text("precision: 0.925\n", encoding="utf-8")
    assert read_match_precision(fraction)[0] == pytest.approx(0.925)

    prose = tmp_path / "prose.md"
    prose.write_text("We have not labeled anything yet.\n", encoding="utf-8")
    value, note = read_match_precision(prose)
    assert value is None
    assert note.startswith("pending")


def test_data_quality_records_the_leakage_audit(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    splits: dict[str, str] = {}
    _pair(conn, "p1", a_action="wait", b_action="wait", split_map=splits)
    quality = data_quality(
        conn,
        cfg,
        dataset_id=DATASET,
        splits=splits,
        systems=("A",),
        match_precision_path=tmp_path / "absent.md",
    )
    assert quality.leakage_violations == 0
    assert quality.leakage_clean is True
    assert quality.leakage_counts == {}
    assert quality.repost_match_precision_note.startswith("pending")


# ---------------------------------------------------------------------------
# Degradation on an empty database
# ---------------------------------------------------------------------------


def test_every_metric_degrades_cleanly_on_an_empty_database(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    case_set = collect_system_runs(
        conn, dataset_id="nothing-here", splits={}, systems=("A", "B", "C")
    )
    assert case_set.cases == ()
    assert case_set.counts_by_system == {"A": 0, "B": 0, "C": 0}
    assert case_set.describe()

    metrics = agent_efficiency(
        conn, cfg, dataset_id="nothing-here", splits={}, system="C", reference="A"
    )
    assert metrics.runs == 0
    assert metrics.paired_cases == 0
    # Every undefined ratio is None, never 0.0.
    for field in (
        metrics.overall_agreement,
        metrics.macro_agreement,
        metrics.mean_medium_high_probes_per_run,
        metrics.mean_latency_ms,
        metrics.recovery_rate,
        metrics.early_stop_regret_rate,
        metrics.unnecessary_probe_rate,
        metrics.cost.mean_probe_cost_points,
        metrics.cost.mean_model_cost_usd,
    ):
        assert field is None
    assert metrics.cost.probe_cost_points == 0.0
    assert metrics.cost.model_cost_usd == 0.0
    assert metrics.describe()

    quality = data_quality(conn, cfg, dataset_id="nothing-here", splits={}, systems=("A",))
    assert quality.runs_checked == 0
    assert quality.ats_resolution_rate is None
    assert quality.publish_date_coverage is None
    assert quality.citation.id_existence_rate is None
    assert quality.citation.support_rate is None
    assert quality.repost_match_precision is None
    assert quality.leakage_clean is True
    assert quality.describe()
