"""The evaluation report's honesty sections (rli.eval.diagnostics, gates, evaluate).

One small synthetic replay dataset with REAL-shaped evidence (so the
no-dynamic-probe counterfactual can be recomputed by the frozen policy) and
runs for all four systems A, B, C and R:

| case | company | T          | split | era (per company)       | A action | no-probe action |
|------|---------|------------|-------|-------------------------|----------|-----------------|
| p1   | c1      | 2026-09-20 | dev   | live (c1 own 09-01)     | wait     | apply_now       |
| p2   | c2      | 2026-09-13 | dev   | ARCHIVE (c2 own 09-15)  | apply_now| apply_now       |
| p3   | c3      | 2026-09-13 | dev   | archive (c3: no own)    | skip     | skip            |
| p4   | c1      | 2026-09-20 | test  | live                    | apply_now| apply_now       |
| p5   | c3      | 2026-09-13 | dev   | identity UNRESOLVED     | apply_now| apply_now       |

p2 is the per-company-era case: the GLOBAL first own capture (c1, 09-01) is
before its T, so a single global boundary would call it live-era.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rli.cli import app
from rli.config import Config
from rli.db import connect, connect_read_only, init_db
from rli.eval import evaluate as evaluate_module
from rli.eval.diagnostics import (
    agent_trace_stats,
    counterfactual_actions,
    era_boundaries,
)
from rli.eval.evaluate import HOLDOUT_TEST_STEP, READ_ONLY_SKIP, evaluate, write_evaluation_report
from rli.eval.gates import SPEC_PREFER_SIMPLER, llm_value_comparison
from rli.eval.metrics import (
    CaseRun,
    MetricsCase,
    MetricsCaseSet,
    collect_system_runs,
    split_map_for_dataset,
)
from rli.models.time import to_utc_z
from rli.policy.splits import SPLIT_METHOD_COMPANY_HASH

DATASET = "ds-honest"
BUILT = datetime(2026, 9, 21, tzinfo=UTC)
#: The dataset's `created_at`: cases at or after it are BUILD-TIME cases.
BUILD_START = datetime(2026, 9, 19, 23, tzinfo=UTC)
T_LATE = datetime(2026, 9, 20, tzinfo=UTC)
T_EARLY = datetime(2026, 9, 13, tzinfo=UTC)

CASES = (
    # posting, company, T, split, A action, posting_state
    ("p1", "c1.com", T_LATE, "dev", "wait", "open"),
    ("p2", "c2.com", T_EARLY, "dev", "apply_now", "open"),
    ("p3", "c3.com", T_EARLY, "dev", "skip", "closed"),
    ("p4", "c1.com", T_LATE, "test", "apply_now", "open"),
    ("p5", "c3.com", T_EARLY, "dev", "apply_now", "open"),
)

runner = CliRunner()


def _url(posting_id: str) -> str:
    return f"https://boards.greenhouse.io/acme/jobs/{posting_id}"


def _decision(action: str, state: str, quality: str, *, cite: str = "e1") -> str:
    return json.dumps(
        {
            "posting_state": state,
            "recommended_action": action,
            "recheck_after_days": 14 if action == "wait" else None,
            "evidence_quality": quality,
            "hypotheses": [],
            "reason": [{"text": "Still listed on the job board.", "evidence_ids": [cite]}],
            "evidence": [],
        }
    )


def _add_run(
    conn: sqlite3.Connection,
    *,
    system: str,
    posting_id: str,
    replay_at: datetime,
    action: str,
    state: str,
    dynamic: tuple[str, ...] = (),
    extra_steps: tuple[tuple[str, str, float], ...] = (),
    unresolved: bool = False,
    cite: str = "e1",
    null_posting: bool = False,
) -> str:
    run_id = f"{system}-{posting_id}"
    stamp = to_utc_z(replay_at)
    quality = "strong" if state == "open" else "weak"
    conn.execute(
        """
        INSERT INTO runs (id, posting_id, input_url, system, mode, replay_at, policy_version,
                          config_hash, started_at, finished_at, status, final_decision,
                          total_cost_usd, total_latency_ms)
        VALUES (?, ?, ?, ?, 'replay', ?, 'pv', ?, ?, ?, 'completed', ?, 0, 100)
        """,
        (
            run_id,
            None if null_posting else posting_id,
            _url(posting_id),
            system,
            stamp,
            f"cfg:x|dataset:{DATASET}",
            stamp,
            stamp,
            _decision(action, state, quality, cite=cite),
        ),
    )
    steps: list[tuple[str, str, str | None, float]] = [
        ("probe", "probe_run", "resolve_posting", 1.0),
        ("probe", "probe_run", "board_snapshot", 1.0),
    ]
    if unresolved:
        steps.append(("controller", "probe_skipped:identity_unresolved", None, 0.0))
    for name in dynamic:
        steps.append(("probe", "probe_run", name, 3.0))
    for component, decision_type, cost in extra_steps:
        steps.append((component, decision_type, None, cost))
    for index, (component, decision_type, probe, cost) in enumerate(steps, start=1):
        conn.execute(
            """
            INSERT INTO run_steps (run_id, step_index, component, decision_type, probe_name,
                                   args_hash, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, 'h', ?, ?)
            """,
            (run_id, index, component, decision_type, probe, cost, stamp),
        )
    published = to_utc_z(replay_at - timedelta(days=5))
    evidence = [
        ("e1", "resolve_posting", "posting_state", state, None),
        ("e2", "resolve_posting", "first_published", published, published),
    ]
    if "company_events" in dynamic:
        evidence.append(("e3", "company_events", "hiring_freeze", "true", stamp))
    for evidence_id, probe, claim_type, value, event_at in evidence:
        conn.execute(
            """
            INSERT INTO evidence (id, run_id, posting_id, probe, claim_type, value, source_url,
                                  source_quality, source_event_at, available_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, 'https://example.test', 'ats_native', ?, ?, ?)
            """,
            (
                evidence_id,
                run_id,
                None if null_posting else posting_id,
                probe,
                claim_type,
                value,
                event_at,
                stamp,
                stamp,
            ),
        )
    return run_id


C_MODEL_STEPS = (
    ("model", "investigator:tokens=1000/100", 0.001),
    ("model", "explanation:tokens=500/50", 0.0005),
)


def _populate(conn: sqlite3.Connection, *, r_differs: bool = False) -> None:
    for company in ("c1.com", "c2.com", "c3.com"):
        conn.execute(
            "INSERT INTO companies (company_id, name, website_domain, created_at) "
            "VALUES (?, ?, ?, ?)",
            (company, company, company, to_utc_z(BUILT)),
        )
    for company, captured in (("c1.com", "2026-09-01"), ("c2.com", "2026-09-15")):
        conn.execute(
            "INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status) "
            "VALUES (?, ?, 'own', 'complete')",
            (company, f"{captured}T00:00:00.000000Z"),
        )
    # c3 only ever has an ARCHIVE capture, which must not make it live-era.
    conn.execute(
        "INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status) "
        "VALUES ('c3.com', '2026-08-01T00:00:00.000000Z', 'archive', 'complete')"
    )
    conn.execute(
        """
        INSERT INTO replay_datasets (dataset_id, created_at, split_kind, split_name,
                                     grid_step_days, postings, companies, cases, notes,
                                     split_method, split_seed)
        VALUES (?, ?, 'company', 'dev', 7, 6, 3, 6, 'synthetic', ?, 1)
        """,
        (DATASET, to_utc_z(BUILD_START), SPLIT_METHOD_COMPANY_HASH),
    )
    for posting_id, company, replay_at, split, action, state in CASES:
        conn.execute(
            """
            INSERT INTO postings (posting_id, company_id, ats, canonical_url, title, created_at,
                                  first_observed)
            VALUES (?, ?, 'greenhouse', ?, 'Engineer', ?, ?)
            """,
            (posting_id, company, _url(posting_id), to_utc_z(BUILT), "2026-08-01T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO replay_cases (dataset_id, posting_id, replay_at, company_id,
                                      canonical_url, built_at, split)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                DATASET,
                posting_id,
                to_utc_z(replay_at),
                company,
                _url(posting_id),
                to_utc_z(BUILT),
                split,
            ),
        )
        unresolved = posting_id == "p5"
        # A runs company_events everywhere it can; only on p1 does it change the action.
        a_dynamic = () if unresolved else ("company_events",)
        _add_run(
            conn,
            system="A",
            posting_id=posting_id,
            replay_at=replay_at,
            action=action,
            state=state,
            dynamic=a_dynamic,
            unresolved=unresolved,
            extra_steps=(("controller", "policy_decision:P4_repeated_repost:strong", 0.0),)
            if posting_id == "p3"
            else (),
        )
        # B never runs a dynamic probe: its action IS the no-probe action.
        b_action = "apply_now" if posting_id == "p1" else action
        _add_run(
            conn,
            system="B",
            posting_id=posting_id,
            replay_at=replay_at,
            action=b_action,
            state=state,
            unresolved=unresolved,
        )
        c_steps = C_MODEL_STEPS
        if posting_id == "p1":
            c_steps = c_steps + (
                ("controller", "citation_unsupported:1", 0.0),
                ("controller", "explanation_fallback:latency_cap", 0.0),
            )
        if posting_id == "p2":
            c_steps = c_steps + (("controller", "run_flag:investigator_error", 0.0),)
        _add_run(
            conn,
            system="C",
            posting_id=posting_id,
            replay_at=replay_at,
            action=action,
            state=state,
            dynamic=a_dynamic,
            extra_steps=c_steps,
            unresolved=unresolved,
            cite="e9" if posting_id == "p1" else "e1",
        )
        r_action = action
        r_dynamic = a_dynamic
        if r_differs and posting_id == "p1":
            # R skips the probe that mattered and lands on the no-probe action.
            r_action, r_dynamic = "apply_now", ()
        _add_run(
            conn,
            system="R",
            posting_id=posting_id,
            replay_at=replay_at,
            action=r_action,
            state=state,
            dynamic=r_dynamic,
            unresolved=unresolved,
        )
    # p6: no run resolved the posting at all (runs.posting_id IS NULL); the
    # case is still a `replay_cases` row with a frozen split.
    conn.execute(
        """
        INSERT INTO postings (posting_id, company_id, ats, canonical_url, title, created_at,
                              first_observed)
        VALUES ('p6', 'c3.com', 'greenhouse', ?, 'Engineer', ?, '2026-08-01T00:00:00Z')
        """,
        (_url("p6"), to_utc_z(BUILT)),
    )
    conn.execute(
        """
        INSERT INTO replay_cases (dataset_id, posting_id, replay_at, company_id,
                                  canonical_url, built_at, split)
        VALUES (?, 'p6', ?, 'c3.com', ?, ?, 'dev')
        """,
        (DATASET, to_utc_z(T_EARLY), _url("p6"), to_utc_z(BUILT)),
    )
    for system in ("A", "B"):
        _add_run(
            conn,
            system=system,
            posting_id="p6",
            replay_at=T_EARLY,
            action="wait",
            state="unknown",
            unresolved=True,
            null_posting=True,
        )
    conn.commit()


@pytest.fixture(autouse=True)
def _no_llm_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evaluate_module, "endpoint_unavailable_reason", lambda _cfg: "not reachable"
    )


@pytest.fixture
def populated(conn: sqlite3.Connection) -> sqlite3.Connection:
    _populate(conn)
    return conn


def _case_set(conn: sqlite3.Connection) -> MetricsCaseSet:
    splits, _ = split_map_for_dataset(conn, dataset_id=DATASET)
    return collect_system_runs(
        conn,
        dataset_id=DATASET,
        splits=splits,
        systems=("A", "B", "C", "R"),
        allowed_splits=("dev", "validation", "test"),
    )


# ---------------------------------------------------------------------------
# Probe dependence
# ---------------------------------------------------------------------------


def test_counterfactual_drops_dynamic_evidence_and_reapplies_the_policy(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    actions = counterfactual_actions(
        populated,
        cfg,
        {"A-p1": to_utc_z(T_LATE), "A-p3": to_utc_z(T_EARLY), "B-p1": to_utc_z(T_LATE)},
    )
    # A's freeze evidence is dynamic; without it p1 is open + recent + strong.
    assert actions == {"A-p1": "apply_now", "A-p3": "skip", "B-p1": "apply_now"}


def test_probe_dependent_subset_and_its_agreement(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    pd = report.probe_dependence

    assert pd.probe_dependent == 1
    assert pd.transitions == {"apply_now -> wait": 1}
    # Every B run ran no dynamic probe; the no-probe action reproduces them all.
    assert pd.selfcheck_runs >= 4
    assert pd.selfcheck_matches == pd.selfcheck_runs
    # B agrees with A everywhere except the one case a probe decided.
    dependent_b = report.probe_dependent_efficiency["B"]
    assert dependent_b.paired_cases == 1
    assert dependent_b.overall_agreement == 0.0
    assert report.efficiency["B"].overall_agreement == pytest.approx(4 / 5)
    assert report.probe_dependent_gate is not None
    assert report.probe_dependent_gate.scope == "probe-dependent"
    assert report.probe_dependent_gate.candidate_runs == 1


# ---------------------------------------------------------------------------
# C vs R
# ---------------------------------------------------------------------------


def test_c_vs_r_on_a_tiny_sample_is_inconclusive_not_prefer_r(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """5 identical paired cases cannot show that R is as good: inconclusive, everywhere."""
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    cmp = report.llm_value

    assert cmp.status == "inconclusive"
    assert cmp.paired_cases == 5
    assert cmp.same_sequence_cases == 5
    assert cmp.same_action_cases == 5
    assert cmp.candidate_model_steps == 10
    assert cmp.deterministic_model_steps == 0
    assert cmp.candidate_model_cost_usd == pytest.approx(0.0075)
    assert "INCONCLUSIVE" in cmp.verdict
    assert "should be preferred" not in cmp.verdict
    # Gate notes and Limitations follow the status.
    assert any("C vs R (inconclusive" in n for n in report.agent_gate.notes)
    assert any("INCONCLUSIVE" in i for i in report.limitations)
    assert not any(
        "attributable to the deterministic controller" in n for n in report.agent_gate.notes
    )

    text = write_evaluation_report(tmp_path / "r.md", report).read_text(encoding="utf-8")
    section = text.split("## Does the LLM add anything? (System C vs System R)", 1)[1]
    assert "**INCONCLUSIVE**" in section
    assert "No paired case differs" in section


def test_c_vs_r_equal_on_enough_cases_says_the_gate_pass_belongs_to_the_controller(
    populated: sqlite3.Connection,
    cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rli.eval import gates

    monkeypatch.setattr(gates, "LLM_MIN_COMPARED_CASES", 4)
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    cmp = report.llm_value
    # 5 paired, 1 investigator error -> 4 compared, all identical.
    assert cmp.compared_cases == 4
    assert cmp.status == "llm_adds_nothing"
    assert "attributable to the deterministic controller" in cmp.verdict
    assert SPEC_PREFER_SIMPLER in cmp.verdict
    assert cmp.deterministic_gate is not None
    assert any("attributable to the deterministic controller" in n for n in report.agent_gate.notes)
    assert any("attributable to the deterministic controller" in i for i in report.limitations)
    text = write_evaluation_report(tmp_path / "r.md", report).read_text(encoding="utf-8")
    assert "THE LLM ADDS NOTHING MEASURABLE" in text


def test_c_vs_r_one_differing_case_is_not_material(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """R skips the probe that mattered on ONE case: listed, but not credited to the LLM."""
    _populate(conn, r_differs=True)
    report = evaluate(conn, cfg, dataset_id=DATASET, include_survival=False)
    cmp = report.llm_value

    assert cmp.differing_cases_total == 1
    assert cmp.differences[0].candidate_probes == ("company_events",)
    assert cmp.differences[0].deterministic_probes == ()
    assert cmp.differences[0].reference_action == "wait"
    # C's p2 run had an investigator error: a C failure, excluded from the rates.
    assert cmp.paired_cases == 5
    assert cmp.candidate_investigator_error_cases == 1
    assert cmp.compared_cases == 4
    assert cmp.candidate_only_agrees_cases == 1
    # One case is below LLM_MIN_DIFFERING_CASES: nothing is material.
    assert cmp.won == () and cmp.lost == ()
    # C's one extra agreement beats R's RATE by 25 points but is one case.
    assert "overall agreement with A (favours C)" in cmp.underpowered
    assert cmp.status == "inconclusive"

    text = write_evaluation_report(tmp_path / "r.md", report).read_text(encoding="utf-8")
    assert "### Per-case differences" in text
    assert "C FAILURES" in text


def _case(index: int, *, c_action: str, r_action: str, a_action: str) -> MetricsCase:
    def run(system: str, action: str) -> CaseRun:
        return CaseRun(
            system=system,
            run_id=f"{system}-{index}",
            status="completed",
            action=action,
            probes_run=("resolve_posting",),
        )

    return MetricsCase(
        input_url=f"u{index}",
        replay_at="t",
        posting_id=f"p{index}",
        split="dev",
        runs={
            "C": run("C", c_action),
            "R": run("R", r_action),
            "A": run("A", a_action),
        },
    )


def _set(cases: list[MetricsCase]) -> MetricsCaseSet:
    return MetricsCaseSet(
        dataset_id="d",
        allowed_splits=("dev",),
        systems=("A", "C", "R"),
        cases=tuple(cases),
        run_ids={
            name: tuple(case.runs[name].run_id for case in cases if name in case.runs)
            for name in ("A", "C", "R")
        },
    )


def _costs(n: int, c_mh: dict[int, int], r_mh: dict[int, int]) -> dict[str, tuple[int, float]]:
    costs: dict[str, tuple[int, float]] = {}
    for index in range(n):
        costs[f"C-{index}"] = (c_mh.get(index, 1), 2 + 3.0 * c_mh.get(index, 1))
        costs[f"R-{index}"] = (r_mh.get(index, 1), 2 + 3.0 * r_mh.get(index, 1))
    return costs


def _same(n: int, start: int = 0) -> list[MetricsCase]:
    return [
        _case(i, c_action="quick_apply", r_action="quick_apply", a_action="quick_apply")
        for i in range(start, start + n)
    ]


def test_an_investigator_error_saving_is_a_failure_not_a_win() -> None:
    """The dev-7d case: C 'saved' a medium probe only because its investigator failed."""
    cases = _same(52)
    costs = _costs(52, {i: 0 for i in range(12)}, {})
    result = llm_value_comparison(
        _set(cases),
        run_costs=costs,
        investigator_error_run_ids={f"C-{i}" for i in range(12)},
    )
    assert result.candidate_investigator_error_cases == 12
    assert result.compared_cases == 40
    assert result.relative_probe_saving == 0.0
    assert result.lost == ("investigator reliability",)
    assert result.status == "llm_adds_nothing"
    assert "counted as C FAILURES" in result.verdict
    assert "That is the LLM's measured contribution" not in result.verdict

    # The same saving WITHOUT the errors is material: credited.
    credited = llm_value_comparison(_set(cases), run_costs=costs, candidate_gate_status="pass")
    assert credited.status == "llm_better"
    assert credited.won == ("probe use",)
    assert "That is the LLM's measured contribution" in credited.verdict


def test_many_investigator_errors_turn_a_cost_win_into_mixed() -> None:
    """40/100 C runs failed, C cheaper on the other 60: not `llm_better`."""
    cases = _same(100)
    costs = _costs(100, {i: 0 for i in range(40, 60)}, {})
    result = llm_value_comparison(
        _set(cases), run_costs=costs, investigator_error_run_ids={f"C-{i}" for i in range(40)}
    )
    assert result.won == ("probe use",)
    assert result.lost == ("investigator reliability",)
    assert result.status == "mixed"
    assert "loses on investigator reliability" in result.verdict


def test_a_small_sample_with_a_big_c_advantage_is_inconclusive() -> None:
    """9 paired cases, C 100% vs R 0% and 67% fewer probes: never 'prefer R'."""
    cases = [
        _case(i, c_action="quick_apply", r_action="wait", a_action="quick_apply") for i in range(9)
    ]
    costs = _costs(9, {i: 1 for i in range(9)}, {i: 3 for i in range(9)})
    result = llm_value_comparison(_set(cases), run_costs=costs)
    assert result.candidate_overall_agreement == 1.0
    assert result.deterministic_overall_agreement == 0.0
    assert result.relative_probe_saving == pytest.approx(2 / 3)
    assert result.status == "inconclusive"
    assert "should be preferred" not in result.verdict
    assert "INCONCLUSIVE" in result.verdict


def test_an_underpowered_c_advantage_on_a_big_sample_is_inconclusive() -> None:
    cases = _same(60)
    # 9 cheaper cases (< LLM_MIN_DIFFERING_CASES) despite a large relative saving.
    result = llm_value_comparison(_set(cases), run_costs=_costs(60, {i: 0 for i in range(9)}, {}))
    assert result.relative_probe_saving == pytest.approx(9 / 60)
    assert result.underpowered == ("probe use (favours C)",)
    assert result.status == "inconclusive"


def test_a_rare_class_cannot_carry_a_macro_win() -> None:
    """2 extra correct cases in a rare class: +macro rate, but not a win."""
    cases = _same(38)
    cases += [_case(i, c_action="skip", r_action="quick_apply", a_action="skip") for i in (38, 39)]
    cases += [_case(i, c_action="skip", r_action="skip", a_action="skip") for i in range(40, 44)]
    result = llm_value_comparison(_set(cases), run_costs=_costs(44, {}, {}))
    assert result.candidate_macro_agreement - result.deterministic_macro_agreement > 0.15
    assert "macro agreement with A" not in result.won
    assert result.status == "inconclusive"


def test_mixed_names_each_dimension_once() -> None:
    """Overall agreement up, macro agreement down: never 'wins on X but loses on X'."""
    cases = []
    # 30 quick_apply cases where only C agrees with A ...
    cases += [
        _case(i, c_action="quick_apply", r_action="wait", a_action="quick_apply") for i in range(30)
    ]
    # ... and 10 skip + 10 wait cases where only R agrees, plus agreement elsewhere.
    cases += [
        _case(i, c_action="quick_apply", r_action="skip", a_action="skip") for i in range(30, 45)
    ]
    cases += [
        _case(i, c_action="quick_apply", r_action="wait", a_action="wait") for i in range(45, 60)
    ]
    result = llm_value_comparison(_set(cases), run_costs=_costs(60, {}, {}))
    assert result.candidate_overall_agreement == pytest.approx(30 / 60)
    assert result.deterministic_overall_agreement == pytest.approx(30 / 60)
    assert result.won == ()
    assert result.lost == ("macro agreement with A",)
    assert result.status == "llm_adds_nothing"

    # Shift the balance: 40 C-only hits vs 20 R-only hits.
    cases = [
        _case(i, c_action="quick_apply", r_action="wait", a_action="quick_apply") for i in range(40)
    ]
    cases += [
        _case(i, c_action="quick_apply", r_action="skip", a_action="skip") for i in range(40, 50)
    ]
    cases += [
        _case(i, c_action="quick_apply", r_action="wait", a_action="wait") for i in range(50, 60)
    ]
    result = llm_value_comparison(_set(cases), run_costs=_costs(60, {}, {}))
    assert result.won == ("overall agreement with A",)
    assert result.lost == ("macro agreement with A",)
    assert result.status == "mixed"
    assert "wins on overall agreement with A but loses on macro agreement with A" in (
        result.verdict
    )


def test_llm_value_comparison_not_run_without_r() -> None:
    cases = [_case(0, c_action="quick_apply", r_action="quick_apply", a_action="quick_apply")]
    for case in cases:
        case.runs.pop("R")
    result = llm_value_comparison(_set(cases), run_costs={})
    assert result.status == "not_run"
    assert "rli replay run --system R" in result.verdict


# ---------------------------------------------------------------------------
# Citations and trace facts per system
# ---------------------------------------------------------------------------


def test_citation_support_and_trace_stats_are_per_system(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)

    assert set(report.citation_by_system) == {"A", "B", "C", "R"}
    assert report.citation_by_system["A"].reasons_with_missing_ids == 0
    # C's p1 reason cites an id that run never recorded.
    assert report.citation_by_system["C"].reasons_with_missing_ids == 1

    c = report.trace_stats["C"]
    assert c.investigator_calls == 5 and c.explanation_calls == 5
    assert c.citation_unsupported_runs == 1 and c.citation_unsupported_reasons == 1
    assert c.fallbacks_by_reason == {"latency_cap": 1}
    assert c.investigator_error_runs == 1
    assert report.trace_stats["R"].model_steps == 0
    assert report.llm_value.candidate_investigator_error_cases == 1


def test_agent_trace_stats_reads_the_legacy_investigator_error_marker(
    populated: sqlite3.Connection,
) -> None:
    populated.execute(
        "INSERT INTO run_steps (run_id, step_index, component, decision_type, created_at) "
        "VALUES ('C-p3', 99, 'controller', 'controller_decision:stop:investigator_error', 'x')"
    )
    stats = agent_trace_stats(populated, "C", ["C-p2", "C-p3"])
    assert stats.investigator_error_runs == 2


# ---------------------------------------------------------------------------
# Sample sizes, eras, grid, holdout
# ---------------------------------------------------------------------------


def test_headline_gate_is_judged_on_scored_not_built(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    sizes = report.sample_sizes
    assert (sizes.dataset_postings, sizes.dataset_cases) == (6, 6)
    # p5 (posting id, unresolved company) AND p6 (no posting id at all).
    assert sizes.identity_unresolved_cases == 2
    assert sizes.unassigned_cases == 0
    assert (sizes.scored_postings, sizes.scored_companies, sizes.scored_cases) == (4, 3, 4)
    assert sizes.meets_postings is False
    # p6 is classified by the split of its replay_cases row, not as a split-map gap.
    assert report.case_set.identity_unresolved == 1
    assert report.case_set.identity_unresolved_by_split == {"dev": 1}
    assert report.case_set.unassigned == 0


def test_era_is_per_company_not_global(populated: sqlite3.Connection, cfg: Config) -> None:
    eras = era_boundaries(populated, dataset_id=DATASET, case_set=_case_set(populated))
    by_posting = {case.posting_id: eras.era_for_case(case) for case in _case_set(populated).cases}
    assert by_posting == {
        "p1": "live-era",
        "p2": "archive-era",  # a GLOBAL boundary (09-01) would say live-era
        "p3": "archive-era",
        "p4": "live-era",
        "p5": "archive-era",
    }
    assert eras.earliest == "2026-09-01T00:00:00.000000Z"

    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    assert report.sample_sizes.live_era_cases == 2
    assert report.sample_sizes.era_companies_with_own == 2
    assert report.efficiency_by_era["live-era"]["A"].runs == 2


def test_grid_distribution_shows_strong_evidence_at_build_time(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    grid = report.grid
    assert grid.build_started_at == to_utc_z(BUILD_START)
    assert grid.grid_points == 2
    assert grid.build_time_cases == 2  # p1 and p4, at T_LATE >= the build start
    # Strong = A's open cases: p1, p2, p4, p5; two are build-time cases.
    assert (grid.strong_at_build_time, grid.strong_total) == (2, 4)
    assert grid.apply_now_at_build_time["A"] == 1
    text = write_evaluation_report(tmp_path / "g.md", report).read_text(encoding="utf-8")
    assert "### Strong evidence and apply_now by grid point" in text


def test_holdout_counts_per_split_and_explicit_test_statement(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    counts = report.split_counts
    assert counts.frozen is True
    assert counts.cases_by_split == {"dev": 4, "test": 1}
    assert counts.runs_by_split["test"] == {"A": 1, "B": 1, "C": 1, "R": 1}
    assert report.holdout_markers_written == 4
    text = write_evaluation_report(tmp_path / "h.md", report).read_text(encoding="utf-8")
    section = text.split("## Holdout and splits", 1)[1].split("## Era split", 1)[0]
    assert "FROZEN per case" in section
    assert "The `test` holdout WAS read: 1 test-split case(s)" in section

    dev_only = evaluate(
        populated, cfg, dataset_id=DATASET, allow_test=False, include_survival=False
    )
    text = write_evaluation_report(tmp_path / "d.md", dev_only).read_text(encoding="utf-8")
    assert "The `test` holdout was NOT read" in text
    assert dev_only.split_counts.excluded_holdout == 1


# ---------------------------------------------------------------------------
# Derived limitations
# ---------------------------------------------------------------------------


def test_limitations_are_derived_from_config_and_data(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    text = "\n".join(report.limitations)
    assert "`team_signal` is ENABLED" in text
    assert "P4 fired A=1, B=0, C=0, R=0" in text
    assert "0/3 companies in this database carry any `company_events` row" in text
    assert "all 78 targets" not in text
    assert "unlicensed" not in text

    disabled = cfg.model_copy(
        update={"team_signal": cfg.team_signal.model_copy(update={"enabled": False})}
    )
    text = "\n".join(
        evaluate(populated, disabled, dataset_id=DATASET, include_survival=False).limitations
    )
    assert "`team_signal` is DISABLED" in text
    assert "unreachable" in text


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


def test_read_only_writes_nothing(tmp_path: Path, cfg: Config) -> None:
    db_path = tmp_path / "ro.db"
    init_db(db_path)
    writer = connect(db_path)
    try:
        _populate(writer)
        writer.execute("DELETE FROM run_steps WHERE run_id LIKE 'R-%'")
        writer.execute("DELETE FROM evidence WHERE run_id LIKE 'R-%'")
        writer.execute("DELETE FROM runs WHERE system = 'R'")
        writer.commit()
        before = writer.execute("SELECT COUNT(*) FROM run_steps").fetchone()[0]
    finally:
        writer.close()

    reader = connect_read_only(db_path)
    try:
        report = evaluate(
            reader, cfg, dataset_id=DATASET, include_survival=False, read_only=True, with_r=True
        )
    finally:
        reader.close()
    assert report.read_only is True
    assert report.systems_run["R"] == READ_ONLY_SKIP
    assert report.holdout_markers_written == 0
    assert any("READ-ONLY" in item for item in report.limitations)

    check = connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM run_steps").fetchone()[0] == before
        assert (
            check.execute(
                "SELECT COUNT(*) FROM run_steps WHERE decision_type = ?", (HOLDOUT_TEST_STEP,)
            ).fetchone()[0]
            == 0
        )
    finally:
        check.close()

    with pytest.raises(ValueError):
        evaluate(connect_read_only(db_path), cfg, dataset_id=DATASET, read_only=True, rerun=True)


def test_cli_eval_run_read_only_and_with_r(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"
    init_db(db_path)
    writer = connect(db_path)
    try:
        _populate(writer)
    finally:
        writer.close()
    out = tmp_path / "out.md"
    result = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--dataset",
            DATASET,
            "--db",
            str(db_path),
            "--out",
            str(out),
            "--no-survival",
            "--read-only",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "## Does the LLM add anything? (System C vs System R)" in out.read_text("utf-8")

    missing = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--dataset",
            DATASET,
            "--db",
            str(tmp_path / "nope.db"),
            "--out",
            str(out),
            "--read-only",
        ],
    )
    assert missing.exit_code != 0
    assert not (tmp_path / "nope.db").exists()


def test_build_time_cases_carry_one_t_per_posting() -> None:
    """After step 1 every open posting's last T is its own observation time."""
    from rli.eval.diagnostics import grid_distribution

    def case(index: int, replay_at: str, quality: str, action: str) -> MetricsCase:
        run = CaseRun(
            system="A",
            run_id=f"A-{index}",
            status="completed",
            action=action,
            evidence_quality=quality,
        )
        return MetricsCase(
            input_url=f"u{index}",
            replay_at=replay_at,
            posting_id=f"p{index}",
            split="dev",
            runs={"A": run},
        )

    cases = (
        case(0, "2026-09-01T00:00:00.000000Z", "weak", "quick_apply"),
        case(1, "2026-09-29T00:10:00.000000Z", "strong", "apply_now"),
        case(2, "2026-09-29T01:40:00.000000Z", "strong", "apply_now"),
        case(3, "2026-09-29T02:05:00.000000Z", "strong", "quick_apply"),
    )
    case_set = MetricsCaseSet(
        dataset_id="d",
        allowed_splits=("dev",),
        systems=("A",),
        cases=cases,
        run_ids={"A": tuple(c.runs["A"].run_id for c in cases)},
    )
    grid = grid_distribution(
        case_set, systems=("A",), build_started_at="2026-09-29T00:00:00.000000Z"
    )
    assert grid.build_time_cases == 3
    assert grid.build_time_grid_points == 3
    assert (grid.strong_at_build_time, grid.strong_total) == (3, 3)
    assert grid.apply_now_at_build_time == {"A": 2}
    # The old "latest T only" reading would have seen one of the three.
    assert grid.last_replay_at == "2026-09-29T02:05:00.000000Z"


def test_first_publish_coverage_is_reported_apart_from_refresh_dates(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    # p3's A run: no first publish date, only an Ashby-style last_published.
    populated.execute("DELETE FROM evidence WHERE run_id = 'A-p3' AND id = 'e2'")
    populated.execute(
        """
        INSERT INTO evidence (id, run_id, posting_id, probe, claim_type, value, source_url,
                              source_quality, source_event_at, available_at, fetched_at)
        VALUES ('e7', 'A-p3', 'p3', 'resolve_posting', 'last_published', 'x', 'https://e',
                'ats_native', '2026-09-10T00:00:00Z', '2026-09-13T00:00:00Z',
                '2026-09-13T00:00:00Z')
        """
    )
    populated.commit()
    dq = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False).data_quality
    assert dq.runs_checked == 5
    assert dq.first_publish_runs == 4
    assert dq.refresh_runs == 1
    assert dq.refresh_by_claim_type == {"last_published": 1}
    assert dq.publish_date_runs == 5  # the old, blended figure


def test_disagreements_outside_the_probe_dependent_subset_are_counted(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """The subset follows A only; a B disagreement where A == no-probe is reported apart."""
    populated.execute(
        "UPDATE runs SET final_decision = ? WHERE id = 'B-p2'",
        (_decision("wait", "open", "strong"),),
    )
    populated.commit()
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    assert report.probe_dependence.probe_dependent == 1
    assert report.probe_dependence.disagreements_outside_subset == {"B": 1}
    text = write_evaluation_report(tmp_path / "pd.md", report).read_text(encoding="utf-8")
    assert "defined from System A's action ONLY" in text
    assert "Outside the subset some systems still disagree with A" in text
    assert "B=1" in text
    assert not any("agrees with A (no disagreement" in item for item in report.limitations)


def test_no_outside_disagreement_is_stated_as_such(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    assert report.probe_dependence.disagreements_outside_subset == {}
    assert any("no disagreement was found there" in item for item in report.limitations)
