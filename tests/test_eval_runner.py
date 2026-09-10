"""`rli.eval.runner`: `Run` lifecycle, tracing, `config_hash` (spec.md §6/§7).

`decide_and_finish`'s citation invariant (spec.md §9) is exercised
end-to-end in `tests/test_eval_system_a.py` / `test_eval_system_b.py`, where
real evidence and probe failures exist to cite; here the runner's own
bookkeeping — the `runs`/`run_steps` writes, failure handling, and the
`config_hash` fingerprint — is tested directly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from rli.config import Config
from rli.eval.runner import Run, config_hash

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def test_config_hash_is_stable_and_changes_with_a_threshold(cfg: Config) -> None:
    assert config_hash(cfg) == config_hash(cfg)

    changed = cfg.model_copy(
        update={
            "thresholds": cfg.thresholds.model_copy(
                update={"max_dynamic_steps": cfg.thresholds.max_dynamic_steps + 1}
            )
        }
    )
    assert config_hash(cfg) != config_hash(changed)


def test_run_open_inserts_running_row(conn: sqlite3.Connection, cfg: Config) -> None:
    run = Run(
        conn,
        cfg,
        input_url="https://example.com/1",
        system="A",
        config_hash="cfg:x",
        started_at=NOW,
    )
    with run:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run.id,)).fetchone()
        assert row["status"] == "running"
        assert row["system"] == "A"
        assert row["input_url"] == "https://example.com/1"
        assert row["final_decision"] is None

    # No decision was ever recorded -> the block ended honestly as 'stopped'.
    row = conn.execute("SELECT status FROM runs WHERE id = ?", (run.id,)).fetchone()
    assert row["status"] == "stopped"


def test_run_step_indices_are_contiguous_and_one_based(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    run = Run(
        conn,
        cfg,
        input_url="https://example.com/1",
        system="A",
        config_hash="cfg:x",
        started_at=NOW,
    )
    with run:
        run.step(component="controller", decision_type="probe_skipped:x")
        run.step(component="controller", decision_type="probe_skipped:y")
        run.step(component="controller", decision_type="probe_skipped:z")

    rows = conn.execute(
        "SELECT step_index FROM run_steps WHERE run_id = ? ORDER BY step_index", (run.id,)
    ).fetchall()
    assert [r["step_index"] for r in rows] == [1, 2, 3]


def test_run_save_evidence_numbers_continuously_across_calls(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    from rli.probes.base import ProbeClaim

    def claim(claim_type: str) -> ProbeClaim:
        return ProbeClaim(
            claim_type=claim_type,
            value="v",
            source_url="https://example.com",
            source_quality="ats_native",
            available_at=NOW,
            fetched_at=NOW,
        )

    run = Run(
        conn,
        cfg,
        input_url="https://example.com/1",
        system="A",
        config_hash="cfg:x",
        started_at=NOW,
    )
    with run:
        first = run.save_evidence(probe="resolve_posting", claims=[claim("a"), claim("b")])
        second = run.save_evidence(probe="board_snapshot", claims=[claim("c")])

    assert [item.id for item in first] == ["e1", "e2"]
    assert [item.id for item in second] == ["e3"]
    assert run.next_evidence_index == 4

    rows = conn.execute(
        "SELECT id, probe FROM evidence WHERE run_id = ? ORDER BY id", (run.id,)
    ).fetchall()
    assert [(r["id"], r["probe"]) for r in rows] == [
        ("e1", "resolve_posting"),
        ("e2", "resolve_posting"),
        ("e3", "board_snapshot"),
    ]


def test_run_exit_with_exception_marks_failed_and_reraises(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    run = Run(
        conn,
        cfg,
        input_url="https://example.com/1",
        system="A",
        config_hash="cfg:x",
        started_at=NOW,
    )
    with pytest.raises(RuntimeError, match="boom"):
        with run:
            run.step(component="probe", decision_type="probe_run", probe_name="resolve_posting")
            raise RuntimeError("boom")

    row = conn.execute("SELECT status, final_decision FROM runs WHERE id = ?", (run.id,)).fetchone()
    assert row["status"] == "failed"
    assert row["final_decision"] is None

    # The failure itself is recorded as a controller step (module docstring:
    # "a crashed run explicable from run_steps alone"), after the probe step
    # that was already recorded.
    steps = conn.execute(
        "SELECT component, decision_type, error FROM run_steps WHERE run_id = ? "
        "ORDER BY step_index",
        (run.id,),
    ).fetchall()
    assert steps[-1]["component"] == "controller"
    assert steps[-1]["decision_type"] == "run_failed"
    assert "boom" in (steps[-1]["error"] or "")


def test_run_set_posting_id_requires_an_existing_postings_row(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    run = Run(
        conn,
        cfg,
        input_url="https://example.com/1",
        system="A",
        config_hash="cfg:x",
        started_at=NOW,
    )
    with run:
        # No `postings` row for this id: the FK would fail, so the runner
        # must refuse rather than attempt the UPDATE.
        assert run.set_posting_id("greenhouse:acme:999") is False
        assert run.posting_id is None
        assert run.set_posting_id(None) is False

    row = conn.execute("SELECT posting_id FROM runs WHERE id = ?", (run.id,)).fetchone()
    assert row["posting_id"] is None


def test_run_finish_stores_decision_json_and_totals(conn: sqlite3.Connection, cfg: Config) -> None:
    from rli.models.decision import Decision

    run = Run(
        conn,
        cfg,
        input_url="https://example.com/1",
        system="A",
        config_hash="cfg:x",
        started_at=NOW,
    )
    with run:
        run.step(
            component="probe",
            decision_type="probe_run",
            probe_name="resolve_posting",
            cost_usd=1.0,
            latency_s=0.5,
        )
        decision = Decision(
            posting_state="open",
            recommended_action="quick_apply",
            evidence_quality="weak",
        )
        run.finish(decision)

    row = conn.execute(
        "SELECT status, final_decision, total_cost_usd, total_latency_ms FROM runs WHERE id = ?",
        (run.id,),
    ).fetchone()
    assert row["status"] == "completed"
    assert json.loads(row["final_decision"])["recommended_action"] == "quick_apply"
    assert row["total_cost_usd"] == 1.0
    assert row["total_latency_ms"] == 500

    # Finishing twice is a no-op (already closed).
    run.finish(decision)
