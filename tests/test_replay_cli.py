"""CLI smoke tests for the PLAN.md M4 pipeline.

`rli replay build` -> `rli replay run` (A and B) -> `rli replay check` ->
`rli eval baseline` -> `rli eval behavior`, end to end through `CliRunner`
against a temp database. Only the HTTP layer is mocked, and only for the
build step — the replay steps run with respx OFF, so a live call escaping
replay mode would fail loudly instead of silently succeeding.
"""

from __future__ import annotations

from pathlib import Path

import respx
from test_eval_helpers import COMPANY
from test_replay_helpers import NOW, dev_everything_cutoff, mock_ats, seed_corpus
from typer.testing import CliRunner

from rli.cli import app
from rli.db import connect, init_db
from rli.models.time import to_utc_z

runner = CliRunner()

DATASET = "cli-ds"


def _prepared_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        seed_corpus(conn)
    finally:
        conn.close()
    return db_path


@respx.mock
def _build(db_path: Path, *args: str):
    mock_ats()
    return runner.invoke(
        app,
        [
            "replay",
            "build",
            "--dataset",
            DATASET,
            "--split",
            "dev",
            "--split-kind",
            "temporal",
            "--cutoff",
            to_utc_z(dev_everything_cutoff()),
            "--grid-days",
            "30",
            "--db",
            str(db_path),
            *args,
        ],
    )


def test_the_whole_m4_pipeline_runs_from_the_cli(tmp_path: Path) -> None:
    db_path = _prepared_db(tmp_path)

    build = _build(db_path)
    assert build.exit_code == 0, build.output
    assert "replay dataset" in build.output
    assert "postings that failed to build: 0" in build.output

    for system in ("A", "B"):
        replayed = runner.invoke(
            app,
            ["replay", "run", "--system", system, "--dataset", DATASET, "--db", str(db_path)],
        )
        assert replayed.exit_code == 0, replayed.output
        assert f"replay {system} over dataset" in replayed.output
        assert "violations=0" in replayed.output

    checked = runner.invoke(app, ["replay", "check", "--dataset", DATASET, "--db", str(db_path)])
    assert checked.exit_code == 0, checked.output
    assert "CLEAN (0 violations)" in checked.output

    baseline_path = tmp_path / "reports" / "baseline.md"
    baseline = runner.invoke(
        app,
        [
            "eval",
            "baseline",
            "--dataset",
            DATASET,
            "--out",
            str(baseline_path),
            "--db",
            str(db_path),
        ],
    )
    assert baseline.exit_code == 0, baseline.output
    assert baseline_path.exists()
    text = baseline_path.read_text(encoding="utf-8")
    assert "# A/B baseline report" in text
    assert "Limitations and interpretation" in text
    # Every paired case must have been counted; a report over zero pairs would
    # print an agreement of "n/a" and prove nothing.
    assert "paired=0" not in baseline.output

    behavior_path = tmp_path / "reports" / "behavior.md"
    behavior = runner.invoke(
        app, ["eval", "behavior", "--out", str(behavior_path), "--db", str(db_path)]
    )
    assert behavior.exit_code == 0, behavior.output
    assert behavior_path.exists()
    assert "Limitations" in behavior_path.read_text(encoding="utf-8")


def test_replay_build_refuses_the_test_holdout(tmp_path: Path) -> None:
    db_path = _prepared_db(tmp_path)
    result = runner.invoke(
        app,
        [
            "replay",
            "build",
            "--dataset",
            DATASET,
            "--split",
            "test",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code != 0
    assert "M6" in str(result.exception)


def test_replay_build_limit_postings_spreads_across_companies(tmp_path: Path) -> None:
    db_path = _prepared_db(tmp_path)
    result = _build(db_path, "--limit-postings", "2")
    assert result.exit_code == 0, result.output
    assert "postings=2 companies=2" in result.output


def test_replay_check_exits_nonzero_on_a_planted_violation(tmp_path: Path) -> None:
    db_path = _prepared_db(tmp_path)
    assert _build(db_path).exit_code == 0
    assert (
        runner.invoke(
            app, ["replay", "run", "--system", "A", "--dataset", DATASET, "--db", str(db_path)]
        ).exit_code
        == 0
    )

    conn = connect(db_path)
    try:
        run_id = conn.execute("SELECT id FROM runs WHERE mode = 'replay' LIMIT 1").fetchone()[0]
        leaked = to_utc_z(NOW.replace(year=NOW.year + 1))
        conn.execute(
            """
            INSERT INTO evidence
                (id, run_id, posting_id, probe, claim_type, value, source_url,
                 source_quality, available_at, fetched_at)
            VALUES ('e999', ?, NULL, 'resolve_posting', 'first_published', 'x',
                    'https://example.com/x', 'ats_native', ?, ?)
            """,
            (run_id, leaked, leaked),
        )
        conn.commit()
    finally:
        conn.close()

    checked = runner.invoke(app, ["replay", "check", "--dataset", DATASET, "--db", str(db_path)])
    assert checked.exit_code == 1
    assert "evidence_after_t" in checked.output


def test_replay_run_rejects_an_unknown_system(tmp_path: Path) -> None:
    db_path = _prepared_db(tmp_path)
    result = runner.invoke(
        app, ["replay", "run", "--system", "Z", "--dataset", DATASET, "--db", str(db_path)]
    )
    assert result.exit_code == 2


def test_replay_build_rejects_an_unparsable_cutoff(tmp_path: Path) -> None:
    db_path = _prepared_db(tmp_path)
    result = runner.invoke(
        app,
        [
            "replay",
            "build",
            "--dataset",
            DATASET,
            "--cutoff",
            "yesterday",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 2


def test_eval_behavior_scopes_to_one_company(tmp_path: Path) -> None:
    db_path = _prepared_db(tmp_path)
    out = tmp_path / "behavior.md"
    result = runner.invoke(
        app,
        ["eval", "behavior", "--out", str(out), "--company", COMPANY, "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
