"""`rli.eval.gates`: the spec.md §6 agent and product gates (PLAN.md M6).

Two kinds of test live here, deliberately:

* **Boundary tests** construct `EfficiencyMetrics` values directly and hand
  them to `agent_gate`. The gate's whole job is one line of float arithmetic
  against two thresholds, and the values that matter — an EXACTLY 70% probe
  ratio, an EXACTLY 2-point agreement drop — cannot be produced reliably by
  laying out synthetic rows and hoping the division lands on them. The
  numbers below are chosen so the naive comparison (without `GATE_TOLERANCE`)
  gets them WRONG: `0.7 * 3.0 == 2.0999999999999996 < 2.1`, and
  `0.2 - 0.02 == 0.18000000000000002 > 0.18`. Delete the tolerance and these
  tests fail, which is exactly what they are for.

* **Integration tests** build synthetic `runs` / `run_steps` / `postings` /
  `outcomes` rows in the tmp database from `tests/conftest.py` and call the
  gates end to end, so the boundary arithmetic is not being verified against
  a shape the real query path never produces.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from rli.config import Config
from rli.eval.gates import (
    AGENT_GATE_AGREEMENT_MARGIN,
    AGENT_GATE_PROBE_RATIO,
    GATE_TOLERANCE,
    agent_gate,
    product_gate,
)
from rli.eval.metrics import SYSTEM_A_CAVEAT, CostSplit, EfficiencyMetrics
from rli.models.time import to_utc_z

NOW = datetime(2026, 9, 7, tzinfo=UTC)
DATASET = "ds-gate"
COMPANY = "acme.com"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _metrics(
    system: str,
    *,
    runs: int,
    medium_high_steps: int,
    overall: float | None,
    macro: float | None,
    actions: dict[str, int] | None = None,
    reference: str = "A",
) -> EfficiencyMetrics:
    """An `EfficiencyMetrics` carrying exactly the fields the gate reads."""
    return EfficiencyMetrics(
        system=system,
        reference=reference,
        runs=runs,
        paired_cases=runs,
        action_distribution=actions or {},
        reference_action_distribution={"quick_apply": runs},
        overall_agreement=overall,
        macro_agreement=macro,
        medium_high_probe_steps=medium_high_steps,
        mean_medium_high_probes_per_run=(medium_high_steps / runs) if runs else None,
        cost=CostSplit(),
    )


def _gate(
    conn: sqlite3.Connection,
    cfg: Config,
    candidate: EfficiencyMetrics,
    baseline: EfficiencyMetrics,
    **kwargs: object,
):
    return agent_gate(
        conn,
        cfg,
        dataset_id=DATASET,
        splits={},
        candidate_metrics=candidate,
        baseline_metrics=baseline,
        **kwargs,  # type: ignore[arg-type]
    )


def _add_company(conn: sqlite3.Connection, company_id: str = COMPANY) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO companies (company_id, name, website_domain, created_at) "
        "VALUES (?, ?, ?, ?)",
        (company_id, company_id, company_id, to_utc_z(NOW)),
    )


def _add_posting(conn: sqlite3.Connection, posting_id: str) -> None:
    _add_company(conn)
    conn.execute(
        """
        INSERT INTO postings
            (posting_id, company_id, ats, canonical_url, created_at, first_observed)
        VALUES (?, ?, 'greenhouse', ?, ?, ?)
        """,
        (
            posting_id,
            COMPANY,
            f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}",
            to_utc_z(NOW),
            to_utc_z(NOW - timedelta(days=100)),
        ),
    )


def _decision(action: str) -> str:
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
    action: str,
    mode: str = "replay",
    status: str = "completed",
    started_at: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (id, posting_id, input_url, system, mode, replay_at, config_hash,
                          started_at, status, final_decision, total_latency_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            run_id,
            posting_id,
            f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}",
            system,
            mode,
            to_utc_z(NOW) if mode == "replay" else None,
            f"cfg:x|dataset:{DATASET}",
            to_utc_z(started_at or NOW),
            status,
            _decision(action),
        ),
    )


_STEP = {"n": 0}


def _add_probe_step(conn: sqlite3.Connection, run_id: str, probe_name: str) -> None:
    _STEP["n"] += 1
    conn.execute(
        """
        INSERT INTO run_steps (run_id, step_index, component, decision_type, probe_name,
                               args_hash, cost_usd, latency_s, created_at)
        VALUES (?, ?, 'probe', 'probe_run', ?, ?, 1.0, 0.0, ?)
        """,
        (run_id, _STEP["n"], probe_name, f"{probe_name}-args", to_utc_z(NOW)),
    )


def _add_outcome(
    conn: sqlite3.Connection, posting_id: str, outcome_type: str, *, count: int = 1
) -> None:
    for _ in range(count):
        conn.execute(
            "INSERT INTO outcomes (posting_id, outcome_type, occurred_at) VALUES (?, ?, ?)",
            (posting_id, outcome_type, to_utc_z(NOW)),
        )


# ---------------------------------------------------------------------------
# Probe-use boundary
# ---------------------------------------------------------------------------


def test_probe_use_passes_at_exactly_seventy_percent(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # Baseline B: 30 medium/high steps over 10 runs -> 3.0 per run.
    # Candidate C: 21 steps over 10 runs            -> 2.1 per run = exactly 70%.
    # In IEEE-754, 0.7 * 3.0 == 2.0999999999999996, so `2.1 <= 0.7 * 3.0` is
    # False without GATE_TOLERANCE. spec.md §6 says "at most 70%", so this
    # must PASS.
    assert 2.1 > AGENT_GATE_PROBE_RATIO * 3.0  # the float hazard, made explicit
    candidate = _metrics("C", runs=10, medium_high_steps=21, overall=0.9, macro=0.9)
    baseline = _metrics("B", runs=10, medium_high_steps=30, overall=0.9, macro=0.9)

    result = _gate(conn, cfg, candidate, baseline)
    assert result.probe_use_pass is True
    assert result.probe_ratio == pytest.approx(0.7)
    assert result.status == "pass"
    assert result.passed is True


def test_probe_use_fails_just_above_seventy_percent(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # 22/10 = 2.2 per run against a 3.0 baseline = 73.3%.
    candidate = _metrics("C", runs=10, medium_high_steps=22, overall=0.9, macro=0.9)
    baseline = _metrics("B", runs=10, medium_high_steps=30, overall=0.9, macro=0.9)

    result = _gate(conn, cfg, candidate, baseline)
    assert result.probe_use_pass is False
    assert result.probe_ratio == pytest.approx(2.2 / 3.0)
    assert result.status == "fail"
    assert result.passed is False


def test_probe_use_compares_per_run_rates_not_raw_totals(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # The candidate ran twice as many cases and used MORE total steps (40 vs
    # 30) while using LESS per run (2.0 vs 3.0). Raw totals would fail it;
    # the gate compares rates, so it passes.
    candidate = _metrics("C", runs=20, medium_high_steps=40, overall=0.9, macro=0.9)
    baseline = _metrics("B", runs=10, medium_high_steps=30, overall=0.9, macro=0.9)

    result = _gate(conn, cfg, candidate, baseline)
    assert result.candidate_medium_high_steps > result.baseline_medium_high_steps
    assert result.probe_ratio == pytest.approx(2.0 / 3.0)
    assert result.probe_use_pass is True


# ---------------------------------------------------------------------------
# Agreement boundary
# ---------------------------------------------------------------------------


def test_agreement_passes_at_exactly_two_points_below_on_both_measures(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # Baseline agreement 0.2, candidate 0.18: exactly -2pp on BOTH measures.
    # In IEEE-754, 0.2 - 0.02 == 0.18000000000000002, so `0.18 >= 0.2 - 0.02`
    # is False without GATE_TOLERANCE. spec.md §6 says "within 2 points", so
    # this must PASS.
    assert 0.18 < 0.2 - AGENT_GATE_AGREEMENT_MARGIN  # the float hazard, made explicit
    candidate = _metrics("C", runs=10, medium_high_steps=0, overall=0.18, macro=0.18)
    baseline = _metrics("B", runs=10, medium_high_steps=0, overall=0.2, macro=0.2)

    result = _gate(conn, cfg, candidate, baseline)
    assert result.overall_pass is True
    assert result.macro_pass is True
    assert result.status == "pass"
    assert result.overall_required == pytest.approx(0.18)
    assert result.macro_required == pytest.approx(0.18)


def test_agreement_fails_just_below_the_margin(conn: sqlite3.Connection, cfg: Config) -> None:
    candidate = _metrics("C", runs=10, medium_high_steps=0, overall=0.17, macro=0.9)
    baseline = _metrics("B", runs=10, medium_high_steps=0, overall=0.2, macro=0.9)

    result = _gate(conn, cfg, candidate, baseline)
    assert result.overall_pass is False
    assert result.macro_pass is True
    assert result.status == "fail"


def test_overall_alone_is_not_enough_macro_must_pass_too(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # The exact failure spec.md §6's macro requirement exists to catch: a
    # candidate that collapses onto the majority class keeps overall
    # agreement (0.90 vs 0.91) and loses the minority classes (0.40 vs 0.80).
    candidate = _metrics(
        "C", runs=10, medium_high_steps=0, overall=0.90, macro=0.40,
        actions={"quick_apply": 10},
    )
    baseline = _metrics(
        "B", runs=10, medium_high_steps=0, overall=0.91, macro=0.80,
        actions={"quick_apply": 7, "wait": 3},
    )

    result = _gate(conn, cfg, candidate, baseline)
    assert result.overall_pass is True
    assert result.macro_pass is False
    assert result.passed is False
    assert result.status == "fail"


# ---------------------------------------------------------------------------
# Zero-baseline probe use
# ---------------------------------------------------------------------------


def test_zero_baseline_probe_use_passes_only_when_the_candidate_is_also_zero(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    baseline = _metrics("B", runs=10, medium_high_steps=0, overall=0.9, macro=0.9)

    lean = _metrics("C", runs=10, medium_high_steps=0, overall=0.9, macro=0.9)
    passing = _gate(conn, cfg, lean, baseline)
    assert passing.baseline_medium_high_per_run == pytest.approx(0.0)
    assert passing.probe_ratio is None  # undefined, never `inf` or a fake 0.0
    assert passing.probe_use_pass is True
    assert passing.status == "pass"

    spendy = _metrics("C", runs=10, medium_high_steps=1, overall=0.9, macro=0.9)
    failing = _gate(conn, cfg, spendy, baseline)
    assert failing.probe_ratio is None
    assert failing.probe_use_pass is False
    assert failing.status == "fail"


# ---------------------------------------------------------------------------
# Not run / notes
# ---------------------------------------------------------------------------


def test_a_candidate_with_no_runs_is_not_run_not_failed(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # The state on the real database today: no reachable LLM endpoint, so
    # System C has no runs. "not measured" must never be reported as "failed".
    candidate = _metrics("C", runs=0, medium_high_steps=0, overall=None, macro=None)
    baseline = _metrics("B", runs=10, medium_high_steps=30, overall=0.9, macro=0.9)

    result = _gate(conn, cfg, candidate, baseline)
    assert result.status == "not_run"
    assert result.passed is None
    assert result.probe_use_pass is None
    assert result.overall_pass is None
    assert result.macro_pass is None
    assert any("no runs in scope" in note for note in result.notes)
    assert "NOT RUN" in result.describe()
    assert str(result) == result.describe()


def test_notes_carry_the_system_a_structural_caveat_and_the_exact_numbers(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    candidate = _metrics("C", runs=10, medium_high_steps=21, overall=0.9, macro=0.9)
    baseline = _metrics("B", runs=10, medium_high_steps=30, overall=0.9, macro=0.9)

    result = _gate(conn, cfg, candidate, baseline)
    assert SYSTEM_A_CAVEAT in result.notes
    assert any("unresolved-question gate" in note for note in result.notes)
    # A reader must be able to recompute the verdict from the notes alone.
    joined = "\n".join(result.notes)
    assert "21 steps / 10 runs" in joined
    assert "30 steps / 10 runs" in joined
    assert f"{AGENT_GATE_PROBE_RATIO:.2f}" in joined
    assert "overall agreement" in joined and "macro agreement" in joined
    assert SYSTEM_A_CAVEAT in result.describe()


def test_reading_the_holdout_is_flagged_in_the_notes(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    candidate = _metrics("C", runs=10, medium_high_steps=0, overall=0.9, macro=0.9)
    baseline = _metrics("B", runs=10, medium_high_steps=0, overall=0.9, macro=0.9)

    result = _gate(conn, cfg, candidate, baseline, allowed_splits=("dev", "validation", "test"))
    assert any("'test' split is IN SCOPE" in note for note in result.notes)


def test_an_unevidenced_agreement_comparison_does_not_pass(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # The candidate ran, but the baseline produced no paired cases, so its
    # agreement is undefined. A gate that cannot be evidenced must not pass.
    candidate = _metrics("C", runs=10, medium_high_steps=0, overall=0.9, macro=0.9)
    baseline = _metrics("B", runs=0, medium_high_steps=0, overall=None, macro=None)

    result = _gate(conn, cfg, candidate, baseline)
    assert result.overall_pass is False
    assert result.macro_pass is False
    assert result.overall_required is None
    assert result.status == "fail"
    assert any("could not be compared" in note for note in result.notes)


def test_gate_tolerance_is_small_enough_to_be_meaningless_on_real_corpora() -> None:
    # The slack exists for float noise, not to move the threshold. On a
    # 1000-case corpus one whole case is 1e-3, six orders of magnitude above.
    assert 0 < GATE_TOLERANCE < 1e-6


# ---------------------------------------------------------------------------
# End-to-end over real rows
# ---------------------------------------------------------------------------


def test_agent_gate_end_to_end_over_synthetic_runs(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # 10 cases. A and B and C all agree on the action. B runs
    # `company_events` (medium) on every case; C runs it on 7 of them, so
    # C's rate is exactly 70% of B's.
    splits: dict[str, str] = {}
    for i in range(10):
        posting_id = f"p{i}"
        _add_posting(conn, posting_id)
        splits[posting_id] = "dev"
        for system in ("A", "B", "C"):
            run_id = f"{system.lower()}-{posting_id}"
            _add_run(conn, run_id, system=system, posting_id=posting_id, action="quick_apply")
            _add_probe_step(conn, run_id, "resolve_posting")
            _add_probe_step(conn, run_id, "board_snapshot")
        _add_probe_step(conn, f"a-{posting_id}", "company_events")
        _add_probe_step(conn, f"b-{posting_id}", "company_events")
        if i < 7:
            _add_probe_step(conn, f"c-{posting_id}", "company_events")

    result = agent_gate(conn, cfg, dataset_id=DATASET, splits=splits)
    assert result.candidate_runs == 10
    assert result.baseline_runs == 10
    assert result.candidate_medium_high_steps == 7
    assert result.baseline_medium_high_steps == 10
    assert result.probe_ratio == pytest.approx(0.7)
    assert result.probe_use_pass is True
    assert result.candidate_overall_agreement == pytest.approx(1.0)
    assert result.candidate_macro_agreement == pytest.approx(1.0)
    assert result.status == "pass"
    assert result.passed is True


def test_agent_gate_end_to_end_reports_not_run_without_a_candidate(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    splits: dict[str, str] = {}
    for i in range(3):
        posting_id = f"p{i}"
        _add_posting(conn, posting_id)
        splits[posting_id] = "dev"
        for system in ("A", "B"):
            run_id = f"{system.lower()}-{posting_id}"
            _add_run(conn, run_id, system=system, posting_id=posting_id, action="quick_apply")
            _add_probe_step(conn, run_id, "board_snapshot")

    result = agent_gate(conn, cfg, dataset_id=DATASET, splits=splits)
    assert result.baseline_runs == 3
    assert result.candidate_runs == 0
    assert result.status == "not_run"
    assert result.passed is None


# ---------------------------------------------------------------------------
# Product gate
# ---------------------------------------------------------------------------


def test_product_gate_is_unproven_on_an_empty_outcomes_table(
    conn: sqlite3.Connection,
) -> None:
    result = product_gate(conn, splits={}, allowed_splits=("test",))
    assert result.status == "unproven"
    assert result.outcomes_total == 0
    assert result.postings_with_outcomes == 0
    assert result.held_out_postings == 0
    # The reason must name what is missing, not merely say "unproven".
    assert "outcomes" in result.reason
    assert "empty" in result.reason
    assert result.recommended_effort_per_screen is None
    assert result.comparison_effort_per_interview is None
    assert str(result) == result.describe()


def test_product_gate_is_unproven_below_the_minimum_outcome_count(
    conn: sqlite3.Connection,
) -> None:
    # Held-out postings with a completed run and outcomes that would, taken
    # at face value, favour the recommended group — but there are only 7
    # outcome rows, far below `min_outcomes`. The answer must stay
    # "unproven", never "pass".
    splits: dict[str, str] = {}
    _add_posting(conn, "rec")
    _add_posting(conn, "other")
    splits["rec"] = splits["other"] = "test"
    _add_run(conn, "a-rec", system="A", posting_id="rec", action="quick_apply", mode="live")
    _add_run(conn, "a-other", system="A", posting_id="other", action="skip", mode="live")
    _add_outcome(conn, "rec", "applied", count=1)
    _add_outcome(conn, "rec", "screen", count=1)
    _add_outcome(conn, "rec", "interview", count=1)
    _add_outcome(conn, "other", "applied", count=2)
    _add_outcome(conn, "other", "screen", count=1)
    _add_outcome(conn, "other", "interview", count=1)

    result = product_gate(conn, splits=splits, allowed_splits=("test",), min_outcomes=30)
    assert result.outcomes_total == 7
    assert result.held_out_postings == 2
    assert result.postings_matched_to_a_run == 2
    # The ratios ARE computable and DO favour the recommendation...
    assert result.recommended_effort_per_screen == pytest.approx(1.0)
    assert result.comparison_effort_per_screen == pytest.approx(2.0)
    # ...and the verdict is still "unproven", because 7 < 30.
    assert result.status == "unproven"
    assert "min_outcomes=30" in result.reason
    assert result.by_action["quick_apply"].applied == 1
    assert result.by_action["skip"].applied == 2
    assert result.by_action["quick_apply"].effort_per_interview == pytest.approx(1.0)
    assert result.by_action["quick_apply"].describe()


def test_product_gate_without_a_split_map_cannot_claim_held_out(
    conn: sqlite3.Connection,
) -> None:
    _add_posting(conn, "rec")
    _add_run(conn, "a-rec", system="A", posting_id="rec", action="quick_apply", mode="live")
    _add_outcome(conn, "rec", "applied", count=40)

    result = product_gate(conn, splits=None)
    assert result.outcomes_total == 40
    assert result.held_out_postings == 0
    assert result.status == "unproven"
    assert "no split map" in result.reason


def test_product_gate_passes_only_with_enough_held_out_evidence(
    conn: sqlite3.Connection,
) -> None:
    # 40 outcomes over two held-out postings. The recommended group needs
    # 4 applications per screen and 8 per interview; the comparison group
    # needs 10 and 20. Every requirement is met and the recommendation is
    # strictly better, so this is the one shape that may return "pass".
    splits = {"rec": "test", "other": "test"}
    _add_posting(conn, "rec")
    _add_posting(conn, "other")
    _add_run(conn, "a-rec", system="A", posting_id="rec", action="apply_now", mode="live")
    _add_run(conn, "a-other", system="A", posting_id="other", action="wait", mode="live")
    _add_outcome(conn, "rec", "applied", count=8)
    _add_outcome(conn, "rec", "screen", count=2)
    _add_outcome(conn, "rec", "interview", count=1)
    _add_outcome(conn, "other", "applied", count=20)
    _add_outcome(conn, "other", "screen", count=2)
    _add_outcome(conn, "other", "interview", count=1)

    result = product_gate(conn, splits=splits, allowed_splits=("test",), min_outcomes=30)
    assert result.outcomes_total == 34
    assert result.recommended_effort_per_screen == pytest.approx(4.0)
    assert result.comparison_effort_per_screen == pytest.approx(10.0)
    assert result.recommended_effort_per_interview == pytest.approx(8.0)
    assert result.comparison_effort_per_interview == pytest.approx(20.0)
    assert result.status == "pass"
    assert "pass:" in result.reason

    # Same data, recommendation reversed: enough evidence, worse result.
    conn.execute(
        "UPDATE runs SET final_decision = ? WHERE id = 'a-rec'", (_decision("wait"),)
    )
    conn.execute(
        "UPDATE runs SET final_decision = ? WHERE id = 'a-other'", (_decision("apply_now"),)
    )
    flipped = product_gate(conn, splits=splits, allowed_splits=("test",), min_outcomes=30)
    assert flipped.status == "fail"


def test_product_gate_defaults_to_the_held_out_split(conn: sqlite3.Connection) -> None:
    # `allowed_splits=None` means ("test",) here — the opposite default from
    # every other entry point, and deliberately so.
    splits = {"dev-posting": "dev"}
    _add_posting(conn, "dev-posting")
    _add_run(
        conn, "a-dev", system="A", posting_id="dev-posting", action="quick_apply", mode="live"
    )
    _add_outcome(conn, "dev-posting", "applied", count=40)

    result = product_gate(conn, splits=splits)
    assert result.outcomes_total == 40
    assert result.held_out_postings == 0
    assert result.status == "unproven"
    assert "('test',)" in result.reason
