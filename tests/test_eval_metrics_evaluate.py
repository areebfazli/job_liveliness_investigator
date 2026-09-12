"""`rli.eval.evaluate`: the spec.md §6 evaluation orchestrator (PLAN.md M6).

Builds a TINY synthetic replay database — `companies`, `postings` (some
closed), `replay_datasets` / `replay_cases`, and completed `runs` +
`run_steps` + `evidence` for systems A and B over the same cases with
dataset-suffixed `config_hash` values — using the `conn` / `cfg` fixtures
from `tests/conftest.py`, then drives `evaluate` / `write_evaluation_report`
and the two new CLI commands over it.

The split assignment is pinned deliberately: the dataset's `split_kind` is
`temporal` and its `created_at` is the cutoff, so
`rli.policy.splits.temporal_split` puts every posting whose `first_observed`
is before `CUTOFF` in `dev` and the one after it in `test`. That gives the
holdout-marker path a real case to fire on without depending on a hash.

Most tests pass `include_survival=False` so lifelines is never imported;
exactly one test exercises the survival path and only asserts that it
degrades gracefully.
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
from rli.db import connect, init_db
from rli.eval import evaluate as evaluate_module
from rli.eval.evaluate import (
    C_NOT_RUN_REASON,
    DEFAULT_REPORT_PATH,
    HOLDOUT_TEST_STEP,
    SPEC_TARGET_POSTINGS,
    EvaluationReport,
    SampleSizes,
    evaluate,
    write_evaluation_report,
)
from rli.eval.gates import AgentGateResult, ProductGateResult
from rli.eval.metrics import CitationSupport, DataQuality, MetricsCaseSet
from rli.eval.ranker import RankerResult
from rli.models.time import to_utc_z

runner = CliRunner()

CUTOFF = datetime(2026, 6, 1, tzinfo=UTC)
DATASET = "ds-m6"
COMPANY = "acme.com"
CONFIG_HASH = f"policy:v1|probes:v1|dataset:{DATASET}"

#: Every heading `write_evaluation_report` must emit, in order.
REQUIRED_SECTIONS = (
    "# Evaluation report (spec.md §6 / PLAN.md M6)",
    "## Headline gate (sample sizes)",
    "## Systems run",
    "## Case-set accounting",
    "## Era split: live vs. archive",
    "## Action distributions",
    "## Agreement with System A",
    "### Per-class agreement",
    "### Confusion matrices",
    "## Cost and latency (probe cost points and model dollars reported SEPARATELY)",
    "## Failure and efficiency metrics",
    "## Data quality",
    "## Future leakage",
    "## Posting behaviour (survival summary)",
    "## Agent gate",
    "## Product gate",
    "## C2 — learned probe ranking",
    "## Limitations",
)


# ---------------------------------------------------------------------------
# Synthetic corpus
# ---------------------------------------------------------------------------


def _decision(action: str, *, state: str = "open") -> str:
    return json.dumps(
        {
            "posting_state": state,
            "recommended_action": action,
            "recheck_after_days": 14,
            "evidence_quality": "high",
            "hypotheses": [],
            "reason": [
                {
                    "text": "Still listed on the job board as of the latest snapshot.",
                    "evidence_ids": ["e1"],
                },
                {
                    "text": "First published 3 days ago.",
                    "evidence_ids": ["e2"],
                },
            ],
            "evidence": [
                {
                    "id": "e1",
                    "probe": "board_snapshot",
                    "claim_type": "board_present",
                    "source_quality": "ats_native",
                    "available_at": to_utc_z(CUTOFF - timedelta(days=2)),
                },
                {
                    "id": "e2",
                    "probe": "resolve_posting",
                    "claim_type": "first_published",
                    "source_quality": "ats_native",
                    "available_at": to_utc_z(CUTOFF - timedelta(days=2)),
                },
            ],
        }
    )


def _add_posting(
    conn: sqlite3.Connection,
    posting_id: str,
    *,
    first_observed: datetime,
    first_seen_absent: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO postings
            (posting_id, company_id, ats, canonical_url, title, created_at,
             first_observed, first_seen_absent)
        VALUES (?, ?, 'greenhouse', ?, ?, ?, ?, ?)
        """,
        (
            posting_id,
            COMPANY,
            f"https://boards.greenhouse.io/acme/jobs/{posting_id}",
            f"Engineer {posting_id}",
            to_utc_z(CUTOFF),
            to_utc_z(first_observed),
            None if first_seen_absent is None else to_utc_z(first_seen_absent),
        ),
    )


def _add_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    posting_id: str,
    system: str,
    replay_at: datetime,
    action: str,
    probes: tuple[tuple[str, float, str | None], ...],
    config_hash: str = CONFIG_HASH,
) -> None:
    """One completed replay run plus its probe steps and its evidence rows."""
    stamp = to_utc_z(replay_at)
    conn.execute(
        """
        INSERT INTO runs
            (id, posting_id, input_url, system, mode, replay_at, policy_version,
             config_hash, started_at, finished_at, status, final_decision,
             total_cost_usd, total_latency_ms)
        VALUES (?, ?, ?, ?, 'replay', ?, 'policy-test', ?, ?, ?, 'completed', ?, ?, ?)
        """,
        (
            run_id,
            posting_id,
            f"https://boards.greenhouse.io/acme/jobs/{posting_id}",
            system,
            stamp,
            config_hash,
            stamp,
            stamp,
            _decision(action),
            sum(cost for _, cost, _ in probes),
            120 * len(probes),
        ),
    )

    index = 0
    conn.execute(
        """
        INSERT INTO run_steps
            (run_id, step_index, component, decision_type, created_at)
        VALUES (?, ?, 'controller', 'system_version', ?)
        """,
        (run_id, index, stamp),
    )
    for probe_name, cost, error in probes:
        index += 1
        conn.execute(
            """
            INSERT INTO run_steps
                (run_id, step_index, component, decision_type, probe_name, args_hash,
                 cost_usd, latency_s, error, created_at)
            VALUES (?, ?, 'probe', 'probe_run', ?, ?, ?, 0.12, ?, ?)
            """,
            (run_id, index, probe_name, f"{probe_name}-args", cost, error, stamp),
        )
    index += 1
    conn.execute(
        """
        INSERT INTO run_steps
            (run_id, step_index, component, decision_type, created_at)
        VALUES (?, ?, 'controller', 'policy_decision:P1:high', ?)
        """,
        (run_id, index, stamp),
    )

    evidence_rows = [
        ("e1", "board_snapshot", "board_present", "ats_native"),
        ("e2", "resolve_posting", "first_published", "ats_native"),
    ]
    if any(name == "company_events" for name, _, _ in probes):
        evidence_rows.append(("e3", "company_events", "layoff", "news"))
    for evidence_id, probe, claim_type, quality in evidence_rows:
        conn.execute(
            """
            INSERT INTO evidence
                (id, run_id, posting_id, probe, claim_type, value, source_url,
                 source_quality, available_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, 'yes', 'https://example.test/e', ?, ?, ?)
            """,
            (
                evidence_id,
                run_id,
                posting_id,
                probe,
                claim_type,
                quality,
                stamp,
                stamp,
            ),
        )


#: System A runs every probe; System B runs only the always-run pair. Costs
#: are the `[probe_costs]` tiers (low=1, medium=3, high=10) as COST POINTS.
A_PROBES = (
    ("resolve_posting", 1.0, None),
    ("board_snapshot", 1.0, None),
    ("company_events", 3.0, None),
    ("requirements_drift", 3.0, None),
)
B_PROBES = (
    ("resolve_posting", 1.0, None),
    ("board_snapshot", 1.0, None),
)


def _populate(conn: sqlite3.Connection) -> None:
    """A three-posting, three-case dataset with completed A and B replay runs."""
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) "
        "VALUES (?, 'Acme', ?, ?)",
        (COMPANY, COMPANY, to_utc_z(CUTOFF)),
    )
    # p1/p2 land in `dev` (first_observed < cutoff); p3 lands in the `test`
    # holdout (first_observed >= cutoff), which is what makes the
    # holdout-marker path fire.
    _add_posting(conn, "p1", first_observed=CUTOFF - timedelta(days=100))
    _add_posting(
        conn,
        "p2",
        first_observed=CUTOFF - timedelta(days=90),
        first_seen_absent=CUTOFF - timedelta(days=10),
    )
    _add_posting(conn, "p3", first_observed=CUTOFF + timedelta(days=1))

    conn.execute(
        """
        INSERT INTO replay_datasets
            (dataset_id, created_at, split_kind, split_name, grid_step_days,
             postings, companies, cases, notes)
        VALUES (?, ?, 'temporal', 'dev', 30, 3, 1, 3, 'synthetic M6 fixture')
        """,
        (DATASET, to_utc_z(CUTOFF)),
    )

    cases = (
        ("p1", CUTOFF - timedelta(days=20)),
        ("p2", CUTOFF - timedelta(days=20)),
        ("p3", CUTOFF + timedelta(days=5)),
    )
    for posting_id, replay_at in cases:
        conn.execute(
            """
            INSERT INTO replay_cases
                (dataset_id, posting_id, replay_at, company_id, canonical_url, built_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                DATASET,
                posting_id,
                to_utc_z(replay_at),
                COMPANY,
                f"https://boards.greenhouse.io/acme/jobs/{posting_id}",
                to_utc_z(CUTOFF),
            ),
        )

    # A and B over the SAME cases. B disagrees on p2 so the per-class and
    # confusion tables are not degenerate.
    actions_b = {"p1": "apply_now", "p2": "wait", "p3": "apply_now"}
    for posting_id, replay_at in cases:
        _add_run(
            conn,
            run_id=f"run-a-{posting_id}",
            posting_id=posting_id,
            system="A",
            replay_at=replay_at,
            action="apply_now",
            probes=A_PROBES,
        )
        _add_run(
            conn,
            run_id=f"run-b-{posting_id}",
            posting_id=posting_id,
            system="B",
            replay_at=replay_at,
            action=actions_b[posting_id],
            probes=B_PROBES,
        )

    # A run from a DIFFERENT dataset: it must never be scoped in.
    _add_run(
        conn,
        run_id="run-a-other",
        posting_id="p1",
        system="A",
        replay_at=CUTOFF - timedelta(days=20),
        action="skip",
        probes=A_PROBES,
        config_hash="policy:v1|probes:v1|dataset:some-other-dataset",
    )
    conn.commit()


@pytest.fixture
def populated(conn: sqlite3.Connection) -> sqlite3.Connection:
    _populate(conn)
    return conn


def _add_board_snapshot(
    conn: sqlite3.Connection,
    *,
    captured_at: datetime,
    source: str = "own",
    coverage_status: str = "complete",
) -> None:
    conn.execute(
        """
        INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status)
        VALUES (?, ?, ?, ?)
        """,
        (COMPANY, to_utc_z(captured_at), source, coverage_status),
    )


@pytest.fixture
def populated_with_own_snapshots(conn: sqlite3.Connection) -> sqlite3.Connection:
    """`populated`, plus one 'archive' and one 'own' board snapshot.

    The 'archive' row is captured EARLIER than the 'own' row specifically to
    prove `rli.eval.metrics.era_boundary`'s own-only filter matters: the
    boundary must come from the 'own' row, never the earlier 'archive' one.
    The 'own' row sits strictly between p1/p2's `replay_at`
    (`CUTOFF - 20d`) and p3's (`CUTOFF + 5d`), so p1/p2 fall archive-era and
    p3 falls live-era.
    """
    _populate(conn)
    _add_board_snapshot(conn, captured_at=CUTOFF - timedelta(days=200), source="archive")
    _add_board_snapshot(conn, captured_at=CUTOFF, source="own")
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def _no_llm_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """System C must be unrunnable, which is the real-world state (contract §HARD).

    Substituted rather than left to the real probe: `[llm].base_url`
    defaults to a local Ollama, so on a machine that happens to be running
    one the real probe would report the endpoint as USABLE and this suite
    would start driving a live model. What these tests are about is the
    reporting of an unavailable C, so the availability answer is pinned.
    """
    monkeypatch.setattr(
        evaluate_module,
        "endpoint_unavailable_reason",
        lambda _cfg: "LLM endpoint is not reachable",
    )


def _cli_db(tmp_path: Path, name: str = "cli.db") -> Path:
    db_path = tmp_path / name
    init_db(db_path)
    connection = connect(db_path)
    try:
        _populate(connection)
    finally:
        connection.close()
    return db_path


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------


def test_evaluate_end_to_end_reuses_a_and_b(populated: sqlite3.Connection, cfg: Config) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)

    assert isinstance(report, EvaluationReport)
    assert report.dataset_id == DATASET
    assert report.split_kind == "temporal"
    assert report.split_name == "dev"
    assert report.allow_test is True
    assert report.allowed_splits == ("dev", "validation", "test")

    # Nothing was replayed: both systems already had scoped runs.
    assert report.systems_run["A"] == "reused"
    assert report.systems_run["B"] == "reused"
    assert C_NOT_RUN_REASON in report.systems_run["C"]

    assert set(report.efficiency) == {"A", "B"}
    assert report.efficiency["A"].runs == 3
    assert report.efficiency["B"].runs == 3
    assert report.efficiency["A"].overall_agreement == 1.0  # A vs itself
    assert report.efficiency["B"].paired_cases == 3
    assert report.efficiency["B"].overall_agreement == pytest.approx(2 / 3)

    # The `|dataset:some-other-dataset` run must not be scoped in.
    assert report.case_set.counts_by_system["A"] == 3

    assert report.data_quality.runs_checked == 3
    assert report.data_quality.ats_resolution_rate == 1.0
    assert report.data_quality.citation.reasons_total > 0

    assert report.agent_gate.status == "not_run"
    assert report.agent_gate.passed is None
    assert report.product_gate.status == "unproven"
    assert report.ranker.status == "insufficient_data"

    assert report.sample_sizes.dataset_postings == 3
    assert report.sample_sizes.corpus_postings == 3
    assert report.sample_sizes.corpus_closures == 1
    assert report.sample_sizes.headline_gate_met is False

    assert report.survival is None
    assert "include_survival=False" in report.survival_note
    assert report.describe()


def test_cost_split_keeps_probe_points_and_model_dollars_apart(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)

    cost_a = report.efficiency["A"].cost
    cost_b = report.efficiency["B"].cost
    # A runs four probes per case (1 + 1 + 3 + 3 points), B runs two (1 + 1).
    assert cost_a.probe_cost_points == pytest.approx(24.0)
    assert cost_b.probe_cost_points == pytest.approx(6.0)
    # Neither system makes a model call, so the dollar column is genuinely
    # zero rather than being folded into the points column.
    assert cost_a.model_cost_usd == 0.0
    assert cost_a.model_steps == 0
    assert report.efficiency["A"].medium_high_probe_steps == 6
    assert report.efficiency["B"].medium_high_probe_steps == 0


def test_evaluate_marks_the_test_holdout_in_the_trace_once(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)

    rows = populated.execute(
        "SELECT run_id, component, error FROM run_steps WHERE decision_type = ?",
        (HOLDOUT_TEST_STEP,),
    ).fetchall()
    # Exactly the two runs (A and B) whose case is the `test`-split posting p3.
    assert {row["run_id"] for row in rows} == {"run-a-p3", "run-b-p3"}
    assert all(row["component"] == "controller" for row in rows)
    assert all("spec.md §6 final evaluation" in row["error"] for row in rows)

    # The marker is appended after the run's existing steps, not at index 0.
    marker_index = populated.execute(
        "SELECT step_index FROM run_steps WHERE run_id = 'run-a-p3' AND decision_type = ?",
        (HOLDOUT_TEST_STEP,),
    ).fetchone()["step_index"]
    max_other = populated.execute(
        "SELECT MAX(step_index) AS m FROM run_steps "
        "WHERE run_id = 'run-a-p3' AND decision_type != ?",
        (HOLDOUT_TEST_STEP,),
    ).fetchone()["m"]
    assert marker_index == max_other + 1

    # Re-running must not spam the trace.
    evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    again = populated.execute(
        "SELECT COUNT(*) AS n FROM run_steps WHERE decision_type = ?",
        (HOLDOUT_TEST_STEP,),
    ).fetchone()["n"]
    assert again == 2


def test_holdout_surfaces_in_gate_notes_and_limitations(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)

    assert any("test" in note and "holdout" in note for note in report.agent_gate.notes)
    assert any(C_NOT_RUN_REASON in note for note in report.agent_gate.notes)
    assert any("`test` holdout WAS read" in item for item in report.limitations)


def test_allow_test_false_excludes_the_holdout_and_writes_no_marker(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, allow_test=False, include_survival=False)

    assert report.allowed_splits == ("dev", "validation")
    assert report.case_set.excluded_holdout >= 1
    assert report.efficiency["A"].runs == 2
    marker_count = populated.execute(
        "SELECT COUNT(*) AS n FROM run_steps WHERE decision_type = ?",
        (HOLDOUT_TEST_STEP,),
    ).fetchone()["n"]
    assert marker_count == 0


def test_with_c_without_a_usable_endpoint_is_skipped_and_never_attempted(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, with_c=True, include_survival=False)

    assert report.systems_run["C"] == f"skipped: {C_NOT_RUN_REASON}"
    assert "C" not in report.efficiency
    assert report.agent_gate.status == "not_run"
    # No System C run was created, so nothing tried to reach a model.
    assert (
        populated.execute("SELECT COUNT(*) AS n FROM runs WHERE system = 'C'").fetchone()["n"] == 0
    )


def test_c_status_does_not_blame_the_endpoint_when_it_is_usable(
    populated: sqlite3.Connection, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`with_c=False` plus a usable endpoint is 'not requested', not 'not run'."""
    monkeypatch.setattr(evaluate_module, "endpoint_unavailable_reason", lambda _cfg: None)

    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)

    assert report.systems_run["C"] == "skipped: not requested (pass with_c=True / --with-c)"
    assert C_NOT_RUN_REASON not in report.systems_run["C"]
    assert (
        populated.execute("SELECT COUNT(*) AS n FROM runs WHERE system = 'C'").fetchone()["n"] == 0
    )


def test_evaluate_on_an_unbuilt_dataset_raises_a_clear_lookup_error(
    populated: sqlite3.Connection, cfg: Config
) -> None:
    with pytest.raises(LookupError) as excinfo:
        evaluate(populated, cfg, dataset_id="nope", include_survival=False)
    assert "nope" in str(excinfo.value)
    assert "no cases" in str(excinfo.value)


def test_survival_path_degrades_gracefully_on_a_tiny_corpus(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """The only test that imports lifelines. It must not lose the report."""
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=True)

    # Either a real BehaviorReport or a named degradation — never an exception
    # and never a silently empty note.
    assert report.survival_note
    if report.survival is None:
        assert "unavailable" in report.survival_note
    else:
        assert "corpus-wide" in report.survival_note
        assert hasattr(report.survival, "total_intervals")
    assert report.agent_gate.status == "not_run"

    text = write_evaluation_report(tmp_path / "survival.md", report).read_text(encoding="utf-8")
    survival_section = text.split("## Posting behaviour (survival summary)", 1)[1].split(
        "## Agent gate", 1
    )[0]
    assert "Corpus-wide" in survival_section
    assert "NOT dataset-scoped" in survival_section


# ---------------------------------------------------------------------------
# Era split: live vs. archive (spec.md §1)
# ---------------------------------------------------------------------------


def test_era_boundary_and_split_from_own_snapshots(
    populated_with_own_snapshots: sqlite3.Connection, cfg: Config
) -> None:
    report = evaluate(populated_with_own_snapshots, cfg, dataset_id=DATASET, include_survival=False)

    # The boundary is the OWN row's captured_at, never the earlier 'archive' row.
    assert report.sample_sizes.era_boundary == to_utc_z(CUTOFF)
    assert report.sample_sizes.live_era_cases == 1
    assert report.sample_sizes.archive_era_cases == 2
    assert report.sample_sizes.live_era_share == pytest.approx(1 / 3)

    assert set(report.efficiency_by_era["live-era"]) == set(report.efficiency)
    assert set(report.efficiency_by_era["archive-era"]) == set(report.efficiency)
    for system in report.efficiency:
        live = report.efficiency_by_era["live-era"][system]
        archive = report.efficiency_by_era["archive-era"][system]
        assert live.runs == 1
        assert live.paired_cases == 1
        assert archive.runs == 2
        assert archive.paired_cases == 2

    # B disagrees with A only on p2 (archive-era), so the per-era agreement
    # figures differ from each other AND from the pooled figure.
    pooled_b = report.efficiency["B"]
    live_b = report.efficiency_by_era["live-era"]["B"]
    archive_b = report.efficiency_by_era["archive-era"]["B"]
    assert pooled_b.overall_agreement == pytest.approx(2 / 3)
    assert live_b.overall_agreement == pytest.approx(1.0)
    assert archive_b.overall_agreement == pytest.approx(0.5)
    assert live_b.overall_agreement != pooled_b.overall_agreement
    assert archive_b.overall_agreement != pooled_b.overall_agreement

    # Informational only: with System C absent, it reads not_run, exactly
    # like the pooled gate.
    assert isinstance(report.live_era_gate, AgentGateResult)
    assert report.live_era_gate.status == "not_run"
    assert report.live_era_gate.passed is None


def test_era_boundary_is_none_without_any_own_snapshot(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    # An 'archive'-only row must not manufacture a boundary.
    _add_board_snapshot(populated, captured_at=CUTOFF - timedelta(days=5), source="archive")
    populated.commit()

    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)

    assert report.sample_sizes.era_boundary is None
    assert report.sample_sizes.live_era_cases == 0
    assert report.sample_sizes.archive_era_cases == len(report.case_set.cases)
    # A real 0.0, not `None`: the denominator (every case) is non-zero, so
    # the share is well defined and "0% live-era" is the true reading. The
    # explicit boundary line asserted below is what explains WHY.
    assert report.sample_sizes.live_era_share == 0.0

    text = write_evaluation_report(tmp_path / "no_own.md", report).read_text(encoding="utf-8")
    assert "no own board snapshots yet — every case is archive-era" in text


def test_era_split_section_renders_live_before_archive_with_gate_verdict(
    populated_with_own_snapshots: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = evaluate(populated_with_own_snapshots, cfg, dataset_id=DATASET, include_survival=False)
    text = write_evaluation_report(tmp_path / "era.md", report).read_text(encoding="utf-8")

    assert "## Era split: live vs. archive" in text
    era_section = text.split("## Era split: live vs. archive", 1)[1].split(
        "## Action distributions", 1
    )[0]

    assert "`live-era`" in era_section
    assert "`archive-era`" in era_section
    assert era_section.index("`live-era`") < era_section.index("`archive-era`")
    assert "product-relevant view" in era_section

    assert "Live-era gate (informational)" in era_section
    assert "Live-era gate verdict (informational" in era_section
    assert "NOT RUN" in era_section
    # The pooled gate stays authoritative and is explicitly labelled as such;
    # this era-scoped one is explicitly labelled non-authoritative.
    assert "authoritative spec.md §6 verdict" in era_section
    assert "NON-authoritative" in era_section


# ---------------------------------------------------------------------------
# Regression: the pooled `## Agent gate` must never leak live-era numbers.
#
# `rli.eval.metrics.split_case_set_by_era`'s docstring explains the hazard
# directly: it hands back a `MetricsCaseSet` whose `systems` tuple is copied
# VERBATIM from the pooled input, never narrowed to the systems that happen
# to have a run in that era, because `_case_set_for` only reuses a supplied
# `case_set` when `all(system in case_set.systems for system in systems)` —
# a narrowed `systems` fails that check and silently rebuilds a POOLED case
# set behind an era label. `evaluate` leans on that same fragility from the
# other side: its pooled `agent_gate(...)` call passes `case_set=case_set`
# (the pooled set) plus precomputed pooled `candidate_metrics` /
# `baseline_metrics`, while its `live_era_gate` call passes
# `case_set=era_case_sets["live-era"]` and no precomputed metrics. Swap
# either call's `case_set=` (or drop the pooled call's precomputed metrics,
# or narrow `era_case_sets["live-era"].systems`) and the pooled section
# would start printing live-era-scoped medium/high-probe numbers with
# nothing today to catch it.
# ---------------------------------------------------------------------------

GATE_ERA_DATASET = "ds-m6-gate-era"
GATE_ERA_CONFIG_HASH = f"policy:v1|probes:v1|dataset:{GATE_ERA_DATASET}"
GATE_ERA_ARCHIVE_REPLAY_AT = CUTOFF - timedelta(days=20)
GATE_ERA_LIVE_REPLAY_AT = CUTOFF + timedelta(days=5)


def _populate_pooled_vs_live_era_gate(conn: sqlite3.Connection) -> None:
    """Two archive-era and two live-era cases with OPPOSITE probe profiles.

    Candidate C runs the expensive `A_PROBES` set (2 medium/high steps) in
    archive-era cases and the cheap `B_PROBES` set (0 medium/high steps) in
    live-era cases; baseline B runs the exact opposite. That makes the
    pooled (all 4 cases) and live-era-only (2 cases) medium/high-per-run
    figures different for BOTH the candidate and the baseline, so a
    pooled/live-era mixup on either side of the gate cannot land on a number
    that happens to match by coincidence.
    """
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) "
        "VALUES (?, 'Acme', ?, ?)",
        (COMPANY, COMPANY, to_utc_z(CUTOFF)),
    )

    postings = ("ga1", "ga2", "gl1", "gl2")
    for posting_id in postings:
        _add_posting(conn, posting_id, first_observed=CUTOFF - timedelta(days=100))

    conn.execute(
        """
        INSERT INTO replay_datasets
            (dataset_id, created_at, split_kind, split_name, grid_step_days,
             postings, companies, cases, notes)
        VALUES (?, ?, 'temporal', 'dev', 30, 4, 1, 4, 'pooled-vs-live-era gate fixture')
        """,
        (GATE_ERA_DATASET, to_utc_z(CUTOFF)),
    )

    cases = (
        ("ga1", GATE_ERA_ARCHIVE_REPLAY_AT),
        ("ga2", GATE_ERA_ARCHIVE_REPLAY_AT),
        ("gl1", GATE_ERA_LIVE_REPLAY_AT),
        ("gl2", GATE_ERA_LIVE_REPLAY_AT),
    )
    for posting_id, replay_at in cases:
        conn.execute(
            """
            INSERT INTO replay_cases
                (dataset_id, posting_id, replay_at, company_id, canonical_url, built_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                GATE_ERA_DATASET,
                posting_id,
                to_utc_z(replay_at),
                COMPANY,
                f"https://boards.greenhouse.io/acme/jobs/{posting_id}",
                to_utc_z(CUTOFF),
            ),
        )

    for posting_id, replay_at in cases:
        is_archive = posting_id.startswith("ga")
        _add_run(
            conn,
            run_id=f"run-a-{posting_id}",
            posting_id=posting_id,
            system="A",
            replay_at=replay_at,
            action="apply_now",
            probes=A_PROBES,
            config_hash=GATE_ERA_CONFIG_HASH,
        )
        _add_run(
            conn,
            run_id=f"run-b-{posting_id}",
            posting_id=posting_id,
            system="B",
            replay_at=replay_at,
            action="apply_now",
            probes=B_PROBES if is_archive else A_PROBES,
            config_hash=GATE_ERA_CONFIG_HASH,
        )
        _add_run(
            conn,
            run_id=f"run-c-{posting_id}",
            posting_id=posting_id,
            system="C",
            replay_at=replay_at,
            action="apply_now",
            probes=A_PROBES if is_archive else B_PROBES,
            config_hash=GATE_ERA_CONFIG_HASH,
        )

    # The own snapshot sits strictly between the archive-era and live-era
    # `replay_at` values, exactly like `populated_with_own_snapshots` above.
    _add_board_snapshot(conn, captured_at=CUTOFF, source="own")
    conn.commit()


@pytest.fixture
def gate_era_split_populated(conn: sqlite3.Connection) -> sqlite3.Connection:
    _populate_pooled_vs_live_era_gate(conn)
    return conn


def _table_row_cells(section: str, row_label: str) -> list[str]:
    """The cells of the Markdown table row starting with `| row_label |`.

    Anchored on the literal row prefix (not a loose substring search) so a
    match only fires on the actual table row, never on prose elsewhere in
    the section that happens to mention the same words.
    """
    prefix = f"| {row_label} |"
    for line in section.splitlines():
        if line.startswith(prefix):
            return [cell.strip() for cell in line.strip().strip("|").split("|")]
    raise AssertionError(f"no {prefix!r} row found in section:\n{section}")


def test_agent_gate_reports_pooled_numbers_not_live_era_numbers(
    gate_era_split_populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """Pins `evaluate`'s pooled `agent_gate` to POOLED medium/high-probe
    numbers and its `live_era_gate` to live-era-only numbers — see the block
    comment above `_populate_pooled_vs_live_era_gate` for the exact
    `split_case_set_by_era` / `_case_set_for` trap this guards against.
    """
    report = evaluate(
        gate_era_split_populated, cfg, dataset_id=GATE_ERA_DATASET, include_survival=False
    )

    # --- guard: both eras are actually populated ---------------------------
    assert report.sample_sizes.archive_era_cases > 0
    assert report.sample_sizes.live_era_cases > 0

    pooled_c = report.efficiency["C"]
    pooled_b = report.efficiency["B"]
    live_c = report.efficiency_by_era["live-era"]["C"]
    live_b = report.efficiency_by_era["live-era"]["B"]

    # --- guard: the fixture actually discriminates pooled from live-era ----
    assert pooled_c.mean_medium_high_probes_per_run != live_c.mean_medium_high_probes_per_run, (
        "fixture is degenerate: pooled and live-era candidate (C) medium/high-per-run "
        "rates are equal, so this test cannot distinguish a pooled/live-era mixup from "
        "correct behaviour"
    )
    assert pooled_b.mean_medium_high_probes_per_run != live_b.mean_medium_high_probes_per_run, (
        "fixture is degenerate: pooled and live-era baseline (B) medium/high-per-run "
        "rates are equal, so this test cannot distinguish a pooled/live-era mixup from "
        "correct behaviour"
    )

    # --- the pooled gate must be scoped to ALL cases, not a live-era slice -
    gate = report.agent_gate
    assert gate.candidate_medium_high_per_run == pooled_c.mean_medium_high_probes_per_run
    assert gate.baseline_medium_high_per_run == pooled_b.mean_medium_high_probes_per_run
    assert gate.candidate_runs == pooled_c.runs
    assert gate.baseline_runs == pooled_b.runs

    # --- the informational live-era gate must be scoped to live-era only ---
    live_gate = report.live_era_gate
    assert live_gate.candidate_medium_high_per_run == live_c.mean_medium_high_probes_per_run
    assert live_gate.baseline_medium_high_per_run == live_b.mean_medium_high_probes_per_run
    assert live_gate.candidate_runs == live_c.runs
    assert live_gate.baseline_runs == live_b.runs

    # --- and the rendered report shows each number in the right section ----
    text = write_evaluation_report(tmp_path / "gate_era.md", report).read_text(encoding="utf-8")

    pooled_section = text.split("## Agent gate", 1)[1].split("## Product gate", 1)[0]
    era_section = text.split("## Era split: live vs. archive", 1)[1].split(
        "## Action distributions", 1
    )[0]
    live_gate_section = era_section.split("### Live-era gate (informational)", 1)[1]

    pooled_row = _table_row_cells(pooled_section, "medium/high probes per run")
    live_row = _table_row_cells(live_gate_section, "medium/high probes per run")

    assert pooled_row[1] == f"{gate.candidate_medium_high_per_run:.2f}"
    assert pooled_row[2] == f"{gate.baseline_medium_high_per_run:.2f}"
    assert live_row[1] == f"{live_gate.candidate_medium_high_per_run:.2f}"
    assert live_row[2] == f"{live_gate.baseline_medium_high_per_run:.2f}"
    assert pooled_row[1] != live_row[1]
    assert pooled_row[2] != live_row[2]

    # --- and the console output must not echo BOTH under the spec banner --
    # `rli eval run` prints `report.describe()`; the live-era re-run sits
    # directly below the pooled verdict, so an identical "agent gate
    # (spec.md §6)" banner on both is how a reader ends up quoting the
    # informational numbers as the spec.md §6 result.
    described = report.describe()
    assert described.count("agent gate (spec.md §6)") == 1
    assert gate.scope == "pooled"
    assert live_gate.scope == "live-era"
    assert "agent gate (live-era slice, INFORMATIONAL" in described


# ---------------------------------------------------------------------------
# write_evaluation_report()
# ---------------------------------------------------------------------------


def test_write_evaluation_report_emits_every_section(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    destination = tmp_path / "nested" / "evaluation.md"

    path = write_evaluation_report(destination, report)

    assert path == destination
    text = path.read_text(encoding="utf-8")

    position = -1
    for heading in REQUIRED_SECTIONS:
        found = text.find(heading)
        assert found > position, f"missing or out-of-order section: {heading}"
        position = found

    # Real Markdown tables, not prose.
    assert "| system | status | scoped runs |" in text
    assert "| --- |" in text


def test_report_names_the_c_not_run_state_everywhere_it_matters(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    text = write_evaluation_report(tmp_path / "evaluation.md", report).read_text(encoding="utf-8")

    assert C_NOT_RUN_REASON in text
    # Next to the gate verdict, not only in a footnote.
    gate_section = text.split("## Agent gate", 1)[1].split("## Product gate", 1)[0]
    assert C_NOT_RUN_REASON in gate_section
    assert "NOT RUN" in gate_section
    # ... and the System A structural caveat rides alongside it.
    assert "System A structural caveat" in gate_section
    assert "unresolved-question" in gate_section or "could_change_action" in gate_section


def test_report_separates_probe_points_from_model_dollars(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    text = write_evaluation_report(tmp_path / "evaluation.md", report).read_text(encoding="utf-8")

    cost_section = text.split(
        "## Cost and latency (probe cost points and model dollars reported SEPARATELY)", 1
    )[1].split("## Failure and efficiency metrics", 1)[0]
    assert "probe cost POINTS (total)" in cost_section
    assert "model cost USD (total)" in cost_section
    assert "NEVER summed" in cost_section
    assert "total cost" in cost_section  # the explicit "no combined figure" note
    assert "$" in cost_section


def test_report_limitations_cover_the_required_caveats(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    text = write_evaluation_report(tmp_path / "evaluation.md", report).read_text(encoding="utf-8")

    limitations = text.split("## Limitations", 1)[1]
    assert "Archive-era" in limitations
    assert "team_signal" in limitations
    assert "P4" in limitations
    assert "Company-event coverage" in limitations
    assert "spec.md §6 targets" in limitations
    assert str(SPEC_TARGET_POSTINGS) in limitations
    assert "NOT MET" in limitations
    assert C_NOT_RUN_REASON in limitations


def test_report_headline_gate_shows_dataset_and_corpus_side_by_side(
    populated: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = evaluate(populated, cfg, dataset_id=DATASET, include_survival=False)
    text = write_evaluation_report(tmp_path / "evaluation.md", report).read_text(encoding="utf-8")

    headline = text.split("## Headline gate (sample sizes)", 1)[1].split("## Systems run", 1)[0]
    assert "evaluated dataset" in headline
    assert "collection corpus" in headline
    assert "NOT MET" in headline
    assert "corpus-wide" in headline


def _empty_report() -> EvaluationReport:
    return EvaluationReport(
        dataset_id="",
        split_kind="company",
        split_name="(unknown)",
        allowed_splits=(),
        allow_test=False,
        generated_at="",
        policy_version="",
        systems_run={},
        case_set=MetricsCaseSet(dataset_id="", allowed_splits=(), systems=()),
        efficiency={},
        data_quality=DataQuality(
            dataset_id="", allowed_splits=(), systems=(), citation=CitationSupport()
        ),
        agent_gate=AgentGateResult(dataset_id="", allowed_splits=()),
        live_era_gate=AgentGateResult(dataset_id="", allowed_splits=()),
        product_gate=ProductGateResult(),
        ranker=RankerResult(status="insufficient_data"),
        sample_sizes=SampleSizes(),
    )


def test_write_evaluation_report_never_raises_on_an_empty_report(tmp_path: Path) -> None:
    path = write_evaluation_report(tmp_path / "deep" / "empty.md", _empty_report())

    text = path.read_text(encoding="utf-8")
    for heading in REQUIRED_SECTIONS:
        assert heading in text
    assert "(none" in text  # empty tables/notes render a placeholder, not a crash


def test_default_report_path_is_the_tracked_artifact() -> None:
    assert DEFAULT_REPORT_PATH == "reports/evaluation.md"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_eval_run_writes_the_report(tmp_path: Path) -> None:
    db_path = _cli_db(tmp_path)
    out_path = tmp_path / "out" / "evaluation.md"

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
            str(out_path),
            "--no-survival",
        ],
    )

    assert result.exit_code == 0, result.output
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "# Evaluation report (spec.md §6 / PLAN.md M6)" in text
    assert C_NOT_RUN_REASON in text
    assert "evaluation report:" in result.stdout


def test_cli_eval_gates_prints_both_verdicts(tmp_path: Path) -> None:
    db_path = _cli_db(tmp_path, "gates.db")

    result = runner.invoke(app, ["eval", "gates", "--dataset", DATASET, "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "agent gate: NOT_RUN" in result.stdout
    assert "product gate: UNPROVEN" in result.stdout


def test_cli_eval_run_on_an_unbuilt_dataset_reports_a_clear_error(tmp_path: Path) -> None:
    db_path = _cli_db(tmp_path, "missing.db")

    result = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--dataset",
            "does-not-exist",
            "--db",
            str(db_path),
            "--out",
            str(tmp_path / "unused.md"),
            "--no-survival",
        ],
    )

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "does-not-exist" in result.output
    assert "no cases" in result.output


def test_cli_eval_run_rejects_an_unknown_split_kind(tmp_path: Path) -> None:
    db_path = _cli_db(tmp_path, "kind.db")

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
            str(tmp_path / "unused.md"),
            "--split-kind",
            "sideways",
            "--no-survival",
        ],
    )

    assert result.exit_code != 0
    assert "sideways" in result.output


def test_eval_help_lists_the_new_commands() -> None:
    result = runner.invoke(app, ["eval", "--help"])

    assert result.exit_code == 0
    assert "run" in result.stdout
    assert "gates" in result.stdout
    assert "baseline" in result.stdout  # the pre-existing command is untouched
