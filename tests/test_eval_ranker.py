"""`rli.eval.ranker`: the optional C2 learned probe ranker (spec.md §4; PLAN.md M6).

Two kinds of tests live here:

* Pure `TrainingRow` -> `RankerResult` tests (`train_ranker`, `should_keep`,
  the temporal split, `RankerConfig.load`) that never touch a database —
  they exercise the statistical/config machinery directly on hand-built
  rows, the same way `test_eval_baseline.py` hand-builds `PairedCase`s
  where it can.
* `build_training_rows` / `evaluate_ranker` tests that build a small
  synthetic replay database directly (`companies` / `postings` / `runs` /
  `run_steps` / `evidence`), following exactly the pattern
  `tests/test_eval_baseline.py` uses, since `rli.replay`'s own tables are
  not needed to exercise this module (see the `rli.eval.ranker` module
  docstring: it reads `runs` / `run_steps` / `evidence` via
  `rli.eval.metrics.collect_system_runs`).

Uses the `conn` / `cfg` fixtures from `tests/conftest.py`.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rli.config import Config
from rli.eval.ranker import (
    DEFAULT_RANKER_CONFIG_SECTION,
    RankerConfig,
    RankerResult,
    TrainingRow,
    _temporal_split,  # private: tests the split in isolation
    build_training_rows,
    evaluate_ranker,
    should_keep,
    train_ranker,
)
from rli.eval.report import cost_tier_for
from rli.models.time import to_utc_z

NOW = datetime(2026, 9, 7, tzinfo=UTC)
DATASET = "ds-ranker"
COMPANY = "acme.com"

# ---------------------------------------------------------------------------
# Synthetic-DB helpers (mirrors tests/test_eval_baseline.py's local helpers)
# ---------------------------------------------------------------------------


def _add_company(conn: sqlite3.Connection, company_id: str = COMPANY) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO companies (company_id, name, website_domain, created_at) "
        "VALUES (?, ?, ?, ?)",
        (company_id, company_id, company_id, to_utc_z(NOW)),
    )


def _add_posting(conn: sqlite3.Connection, posting_id: str, *, company_id: str = COMPANY) -> None:
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
    replay_at: datetime,
    action: str | None,
    dataset: str = DATASET,
) -> None:
    url = f"https://boards.greenhouse.io/{COMPANY}/jobs/{posting_id}"
    conn.execute(
        """
        INSERT INTO runs (id, posting_id, input_url, system, mode, replay_at, config_hash,
                           started_at, status, final_decision, total_cost_usd, total_latency_ms)
        VALUES (?, ?, ?, ?, 'replay', ?, ?, ?, 'completed', ?, 0.0, 0)
        """,
        (
            run_id,
            posting_id,
            url,
            system,
            to_utc_z(replay_at),
            f"cfg:x|dataset:{dataset}",
            to_utc_z(NOW),
            None if action is None else _decision(action),
        ),
    )


def _add_probe_run_step(
    conn: sqlite3.Connection, run_id: str, *, step_index: int, probe_name: str
) -> None:
    conn.execute(
        """
        INSERT INTO run_steps (run_id, step_index, component, decision_type, probe_name,
                                cost_usd, latency_s, error, created_at)
        VALUES (?, ?, 'probe', 'probe_run', ?, 1.0, 0.1, NULL, ?)
        """,
        (run_id, step_index, probe_name, to_utc_z(NOW)),
    )


def _add_evidence(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    evidence_id: str,
    posting_id: str,
    probe: str,
    claim_type: str,
    source_quality: str,
) -> None:
    conn.execute(
        """
        INSERT INTO evidence (id, run_id, posting_id, probe, claim_type, value, source_url,
                               raw_excerpt, source_quality, source_event_at, available_at,
                               fetched_at)
        VALUES (?, ?, ?, ?, ?, 'v', 'https://example.com', NULL, ?, NULL, ?, ?)
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


def _row(
    *,
    replay_at: str,
    label: int,
    features: dict[str, float],
    probe: str = "repost_history",
    posting_id: str = "p",
) -> TrainingRow:
    return TrainingRow(
        posting_id=posting_id, replay_at=replay_at, probe=probe, features=features, label=label
    )


# ---------------------------------------------------------------------------
# RankerConfig.load
# ---------------------------------------------------------------------------


def test_ranker_config_load_defaults_when_no_ranker_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[other]\nx = 1\n", encoding="utf-8")
    assert RankerConfig.load(path) == RankerConfig()


def test_ranker_config_load_defaults_when_file_missing(tmp_path: Path) -> None:
    assert RankerConfig.load(tmp_path / "does-not-exist.toml") == RankerConfig()


def test_ranker_config_load_reads_overrides(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"[{DEFAULT_RANKER_CONFIG_SECTION}]\n"
        "min_rows = 50\n"
        "test_fraction = 0.1\n"
        "min_auc_gain = 0.02\n"
        "min_accuracy_gain = 0.01\n"
        "seed = 42\n"
        "max_iter = 500\n"
        "c = 0.5\n",
        encoding="utf-8",
    )
    loaded = RankerConfig.load(path)
    assert loaded == RankerConfig(
        min_rows=50,
        test_fraction=0.1,
        min_auc_gain=0.02,
        min_accuracy_gain=0.01,
        seed=42,
        max_iter=500,
        c=0.5,
    )


# ---------------------------------------------------------------------------
# train_ranker: insufficient data, never touches sklearn
# ---------------------------------------------------------------------------


def test_train_ranker_below_min_rows_is_insufficient_and_skips_sklearn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Poison sklearn's import path: if train_ranker tries `import sklearn`
    # (or a submodule) below the min_rows gate, this raises ImportError and
    # fails the test loudly instead of silently importing the real thing.
    monkeypatch.setitem(sys.modules, "sklearn", None)
    monkeypatch.setitem(sys.modules, "sklearn.linear_model", None)
    monkeypatch.setitem(sys.modules, "sklearn.metrics", None)

    rows = [
        _row(replay_at=to_utc_z(NOW + timedelta(hours=i)), label=i % 2, features={"x": float(i)})
        for i in range(10)
    ]
    config = RankerConfig(min_rows=200)
    result = train_ranker(rows, config)

    assert result.status == "insufficient_data"
    assert result.keep is False
    assert result.rows == 10
    assert "200" in result.note


def test_should_keep_false_for_non_trained_status() -> None:
    result = RankerResult(status="insufficient_data", rows=5)
    assert should_keep(result) is False
    result = RankerResult(status="degenerate", rows=500)
    assert should_keep(result) is False


# ---------------------------------------------------------------------------
# train_ranker: synthetic learnable signal vs. pure noise
# ---------------------------------------------------------------------------


def _synthetic_rows(*, n: int, signal_correlated: bool, seed: int) -> list[TrainingRow]:
    rng = random.Random(seed)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    for i in range(n):
        label = 1 if rng.random() < 0.5 else 0
        if signal_correlated:
            signal = float(label) + rng.gauss(0.0, 0.05)
        else:
            signal = rng.random()
        noise = rng.random()
        rows.append(
            _row(
                replay_at=to_utc_z(base + timedelta(hours=i)),
                label=label,
                probe="repost_history",
                posting_id=f"p{i}",
                # probe_cost_points constant across all rows -> the
                # deterministic baseline (-probe_cost_points) is a constant
                # score, i.e. chance-level (AUC 0.5), so a genuinely
                # learnable "signal" feature has real room to beat it.
                features={"signal": signal, "noise": noise, "probe_cost_points": 3.0},
            )
        )
    return rows


def test_train_ranker_learnable_signal_beats_deterministic_and_is_kept() -> None:
    rows = _synthetic_rows(n=300, signal_correlated=True, seed=0)
    result = train_ranker(rows, RankerConfig())

    assert result.status == "trained"
    assert result.learned_auc is not None
    assert result.learned_auc == pytest.approx(result.learned_auc)  # finite, no NaN
    assert 0.0 <= result.learned_auc <= 1.0
    assert result.deterministic_auc is not None
    assert result.auc_gain is not None
    # The signal near-perfectly separates the classes; the deterministic
    # baseline is chance (constant cost feature) -> a large, unambiguous gain.
    assert result.learned_auc > 0.9
    assert result.deterministic_auc == pytest.approx(0.5, abs=0.05)
    assert result.auc_gain >= RankerConfig().min_auc_gain
    assert result.keep is True


def test_train_ranker_pure_noise_is_trained_but_not_kept() -> None:
    rows = _synthetic_rows(n=300, signal_correlated=False, seed=1)
    result = train_ranker(rows, RankerConfig())

    assert result.status == "trained"
    assert result.learned_auc is not None
    assert result.auc_gain is not None
    # Neither the learned model nor the deterministic baseline has any real
    # signal to work with; the gain must not clear the keep threshold.
    assert result.auc_gain < RankerConfig().min_auc_gain
    assert result.keep is False


# ---------------------------------------------------------------------------
# train_ranker: degenerate holdout (single label class)
# ---------------------------------------------------------------------------


def test_train_ranker_degenerate_holdout_single_class() -> None:
    config = RankerConfig(min_rows=20, test_fraction=0.2)
    rows = []
    base = datetime(2025, 1, 1, tzinfo=UTC)
    # First 20 rows (train slice): alternating labels, both classes present.
    for i in range(20):
        rows.append(
            _row(
                replay_at=to_utc_z(base + timedelta(hours=i)),
                label=i % 2,
                posting_id=f"train-{i}",
                features={"x": float(i % 2), "probe_cost_points": 1.0},
            )
        )
    # Last 5 rows (holdout slice, per max(1, round(25*0.2)) == 5): all label 0.
    for i in range(20, 25):
        rows.append(
            _row(
                replay_at=to_utc_z(base + timedelta(hours=i)),
                label=0,
                posting_id=f"holdout-{i}",
                features={"x": 0.0, "probe_cost_points": 1.0},
            )
        )

    result = train_ranker(rows, config)

    assert result.rows == 25
    assert result.holdout_rows == 5
    assert result.train_rows == 20
    assert result.status == "degenerate"
    assert result.keep is False


# ---------------------------------------------------------------------------
# The temporal split itself
# ---------------------------------------------------------------------------


def test_temporal_split_boundary_and_chronology() -> None:
    base = datetime(2025, 1, 1, tzinfo=UTC)
    n = 40
    # Build out of chronological order to prove the split sorts first.
    indices = list(range(n))
    random.Random(7).shuffle(indices)
    rows = [
        _row(
            replay_at=to_utc_z(base + timedelta(hours=i)),
            label=i % 2,
            posting_id=f"p{i}",
            features={"x": float(i)},
        )
        for i in indices
    ]

    train_rows, holdout_rows = _temporal_split(rows, 0.25)

    assert len(train_rows) + len(holdout_rows) == n
    assert len(holdout_rows) == max(1, round(n * 0.25))

    train_times = [row.replay_at for row in train_rows]
    holdout_times = [row.replay_at for row in holdout_rows]
    assert train_times == sorted(train_times)
    assert holdout_times == sorted(holdout_times)
    # No holdout row predates any train row: the latest train timestamp is
    # <= the earliest holdout timestamp.
    assert max(train_times) <= min(holdout_times)


# ---------------------------------------------------------------------------
# build_training_rows against a small synthetic replay database
# ---------------------------------------------------------------------------


def _build_small_replay_db(conn: sqlite3.Connection) -> dict[str, str]:
    """Two cases; returns the `splits` map `build_training_rows` needs.

    Case "p1": A runs resolve_posting, board_snapshot, then the dynamic
    probe `repost_history` and reaches "wait". B runs the SAME case but
    only the always-run pair (no dynamic probes) and reaches "quick_apply"
    — a different action. Per the module docstring's label rule, this is
    exactly the counterfactual that makes `repost_history`'s row a positive
    label: another system (B) ran the case, did not run `repost_history`,
    and landed on a different action than the reference (A) — and every
    system that DID run `repost_history` (only A) agrees with A.

    Case "p2": A only (no B run), running `requirements_drift` then
    `company_events` in that order, reaching "quick_apply". With no other
    system present for this case, neither of its two rows can satisfy the
    counterfactual, so both are expected to label 0.
    """
    _add_posting(conn, "p1")
    _add_posting(conn, "p2")

    # --- case p1: A runs the always-run pair + repost_history -----------
    _add_run(conn, "a-p1", system="A", posting_id="p1", replay_at=NOW, action="wait")
    _add_probe_run_step(conn, "a-p1", step_index=1, probe_name="resolve_posting")
    _add_probe_run_step(conn, "a-p1", step_index=2, probe_name="board_snapshot")
    _add_probe_run_step(conn, "a-p1", step_index=3, probe_name="repost_history")
    _add_evidence(
        conn,
        "a-p1",
        evidence_id="e1",
        posting_id="p1",
        probe="resolve_posting",
        claim_type="first_published",
        source_quality="ats_native",
    )
    _add_evidence(
        conn,
        "a-p1",
        evidence_id="e2",
        posting_id="p1",
        probe="board_snapshot",
        claim_type="board_present",
        source_quality="page_structured",
    )
    # Evidence produced BY repost_history itself must NOT count toward its
    # own row's "before" features.
    _add_evidence(
        conn,
        "a-p1",
        evidence_id="e3",
        posting_id="p1",
        probe="repost_history",
        claim_type="disappeared_interval",
        source_quality="archive",
    )

    # B runs the same case, does NOT run repost_history, disagrees with A.
    _add_run(conn, "b-p1", system="B", posting_id="p1", replay_at=NOW, action="quick_apply")
    _add_probe_run_step(conn, "b-p1", step_index=1, probe_name="resolve_posting")
    _add_probe_run_step(conn, "b-p1", step_index=2, probe_name="board_snapshot")

    # --- case p2: A only, two dynamic probes in sequence -----------------
    replay_at_2 = NOW + timedelta(days=1)
    _add_run(conn, "a-p2", system="A", posting_id="p2", replay_at=replay_at_2, action="quick_apply")
    _add_probe_run_step(conn, "a-p2", step_index=1, probe_name="resolve_posting")
    _add_probe_run_step(conn, "a-p2", step_index=2, probe_name="board_snapshot")
    _add_probe_run_step(conn, "a-p2", step_index=3, probe_name="requirements_drift")
    _add_probe_run_step(conn, "a-p2", step_index=4, probe_name="company_events")
    _add_evidence(
        conn,
        "a-p2",
        evidence_id="e1",
        posting_id="p2",
        probe="resolve_posting",
        claim_type="updated_at",
        source_quality="ats_native",
    )
    _add_evidence(
        conn,
        "a-p2",
        evidence_id="e2",
        posting_id="p2",
        probe="requirements_drift",
        claim_type="requirements_changed",
        source_quality="page_structured",
    )

    conn.commit()
    return {"p1": "dev", "p2": "dev"}


def test_build_training_rows_shape_features_and_counterfactual_label(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    splits = _build_small_replay_db(conn)

    rows = build_training_rows(conn, cfg, dataset_id=DATASET, splits=splits)

    assert len(rows) == 3

    by_key = {(row.posting_id, row.probe): row for row in rows}
    assert set(by_key) == {
        ("p1", "repost_history"),
        ("p2", "requirements_drift"),
        ("p2", "company_events"),
    }

    expected_feature_names = {
        "n_evidence",
        "n_evidence_ats_native",
        "n_evidence_page_structured",
        "n_evidence_archive",
        "n_evidence_news",
        "n_evidence_enrichment",
        "has_publish_evidence",
        "has_board_present",
        "has_board_absent",
        "n_probes_before",
        "probe_cost_points",
        "probe_is_company_events",
        "probe_is_repost_history",
        "probe_is_requirements_drift",
        "probe_is_team_signal",
    }
    for row in rows:
        assert set(row.features) == expected_feature_names

    # The documented counterfactual: A ran repost_history, B did not, and B
    # disagreed with A -> label 1. Every other row has no counterfactual
    # evidence (single-system cases) -> label 0.
    assert by_key[("p1", "repost_history")].label == 1
    assert by_key[("p2", "requirements_drift")].label == 0
    assert by_key[("p2", "company_events")].label == 0

    # p1/repost_history: "before" evidence is resolve_posting + board_snapshot
    # only (repost_history's own evidence is excluded).
    row = by_key[("p1", "repost_history")]
    assert row.features["n_evidence"] == 2.0
    assert row.features["n_evidence_ats_native"] == 1.0
    assert row.features["n_evidence_page_structured"] == 1.0
    assert row.features["n_evidence_archive"] == 0.0
    assert row.features["has_publish_evidence"] == 1.0
    assert row.features["has_board_present"] == 1.0
    assert row.features["has_board_absent"] == 0.0
    assert row.features["n_probes_before"] == 2.0
    assert row.features["probe_is_repost_history"] == 1.0
    assert row.features["probe_is_company_events"] == 0.0
    assert row.features["probe_cost_points"] == float(
        cfg.probe_costs.value_for(cost_tier_for("repost_history"))
    )

    # p2/requirements_drift: "before" evidence is resolve_posting's only.
    row = by_key[("p2", "requirements_drift")]
    assert row.features["n_evidence"] == 1.0
    assert row.features["n_evidence_ats_native"] == 1.0
    assert row.features["has_publish_evidence"] == 1.0
    assert row.features["n_probes_before"] == 2.0

    # p2/company_events: "before" evidence adds requirements_drift's own
    # evidence, since requirements_drift ran earlier in probes_run order.
    row = by_key[("p2", "company_events")]
    assert row.features["n_evidence"] == 2.0
    assert row.features["n_evidence_page_structured"] == 1.0
    assert row.features["n_probes_before"] == 3.0
    assert row.features["probe_cost_points"] == float(
        cfg.probe_costs.value_for(cost_tier_for("company_events"))
    )


def test_evaluate_ranker_on_realistic_but_small_db_is_insufficient_data(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_small_replay_db(conn)

    result = evaluate_ranker(conn, cfg, dataset_id=DATASET, splits={"p1": "dev", "p2": "dev"})

    assert result.status == "insufficient_data"
    assert result.keep is False
    assert result.rows == 3


@pytest.mark.parametrize(
    ("claim_type", "expected"),
    [("first_published", 1.0), ("updated_at", 1.0), ("last_published", 1.0), ("other", 0.0)],
)
def test_has_publish_evidence_counts_ashby_last_published(
    cfg: Config, claim_type: str, expected: float
) -> None:
    """Ashby's `last_published` is a stated publish date, read like `updated_at`."""
    from rli.eval.ranker import _features_for

    rows = [{"source_quality": "ats_native", "claim_type": claim_type}]
    features = _features_for(
        rows,  # type: ignore[arg-type]
        probe="company_events",
        n_probes_before=2,
        cfg=cfg,
    )
    assert features["has_publish_evidence"] == expected
