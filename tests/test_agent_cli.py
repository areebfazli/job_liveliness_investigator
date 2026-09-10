"""Tests for `rli.agent.cli` — System C's CLI sub-app (`run` and `trace`).

Mirrors the `typer.testing.CliRunner` pattern in `tests/test_cli.py`. Every
test here builds its own fixtures locally (a fresh `tmp_path` SQLite file per
test, seeded either through `rli.db.init_db`/`connect` or, for `trace`, with
hand-written SQL against the real `run_steps`/`runs` schema) so this file has
no dependency on any other test module or on `tests/conftest.py`.

No test ever constructs a real `rli.llm.client.OpenAICompatibleClient` or
makes a network call: `rli.agent.cli.OpenAICompatibleClient` is monkeypatched
to a tiny local stand-in wherever `run` is invoked, and
`rli.agent.cli.run_system_c` is
monkeypatched to a recording stub that returns a hand-built `RunResult`
(mirroring the shape `rli.agent.loop.run_system_c` returns) instead of
driving a live agent loop.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rli.agent import cli as agent_cli
from rli.agent.cli import app
from rli.agent.loop import with_tokens
from rli.config import Config, load_config
from rli.db import connect, init_db
from rli.eval.runner import RunResult
from rli.llm.client import LLMError
from rli.models.decision import Decision

runner = CliRunner()


# ---------------------------------------------------------------------------
# Local fixtures / helpers
# ---------------------------------------------------------------------------


class _StubLLMClient:
    """Stands in for `rli.llm.client.OpenAICompatibleClient`, without a socket.

    Mirrors the factory the CLI actually calls — `from_config(cfg,
    model_id=...)` — and the real class's `model_id` resolution rule
    (`model_id` if given, else `cfg.llm.model_id`), so a test that
    monkeypatches `rli.agent.cli.OpenAICompatibleClient` with this class
    still exercises cli.py's real argument-passing exactly as written, with
    no HTTP client constructed and no network access.
    """

    def __init__(self, cfg: Config, *, model_id: str | None = None) -> None:
        self.cfg = cfg
        self.model_id = model_id if model_id is not None else cfg.llm.model_id

    @classmethod
    def from_config(
        cls, cfg: Config, *, model_id: str | None = None, **_kwargs: Any
    ) -> _StubLLMClient:
        return cls(cfg, model_id=model_id)


def _make_decision() -> Decision:
    """A minimal, schema-valid `Decision` (spec.md §1)."""
    return Decision(
        posting_state="open",
        recommended_action="apply_now",
        evidence_quality="strong",
    )


def _make_run_result(run_id: str = "run-stub-0001") -> RunResult:
    """A hand-built `RunResult`, matching the shape `run_system_c` returns."""
    return RunResult(
        run_id=run_id,
        system="C",
        decision=_make_decision(),
        probes_run=(),
    )


def _recording_run_system_c(calls: list[dict[str, Any]], result: RunResult) -> Any:
    """A `run_system_c` stub matching the call shape `cli.run_command` uses.

    `run_command` calls it as `run_system_c(conn, cfg, url, llm,
    max_steps=max_steps)`; this records every argument it was given (by
    keyword, so a positional/keyword mismatch in a future cli.py edit would
    surface as a `TypeError` at call time rather than silently recording
    nothing) and returns the pre-built `result`.
    """

    def stub(
        conn: sqlite3.Connection,
        cfg: Config,
        url: str,
        llm: Any,
        *,
        max_steps: int | None = None,
    ) -> RunResult:
        calls.append(
            {
                "conn": conn,
                "cfg": cfg,
                "url": url,
                "llm": llm,
                "max_steps": max_steps,
            }
        )
        return result

    return stub


def _seed_run_and_steps(
    db_path: Path,
    *,
    run_id: str,
    input_url: str = "https://example.com/jobs/9",
    system: str = "C",
    status: str = "completed",
    config_hash: str = "cfg:abc123|c1:claude-x:v1",
    total_cost_usd: float = 0.0123,
    total_latency_ms: int = 1500,
    steps: list[dict[str, Any]] | None = None,
) -> None:
    """Seed one `runs` row and its `run_steps` rows via the real schema.

    Uses `rli.db.init_db`/`connect` (the same functions `cli.py` itself uses)
    rather than a hand-copied `CREATE TABLE`, so the columns this test relies
    on can never drift from `rli/db/schema.sql`. Rows are inserted in an
    order that deliberately does NOT match `step_index`, so a `trace` test
    reading them back in order actually exercises the command's own `ORDER
    BY step_index` rather than the insertion order.
    """
    init_db(db_path)
    conn = connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO runs
                (id, input_url, system, mode, config_hash, started_at,
                 status, final_decision, total_cost_usd, total_latency_ms)
            VALUES (?, ?, ?, 'live', ?, '2026-01-01T00:00:00Z', ?, ?, ?, ?)
            """,
            (
                run_id,
                input_url,
                system,
                config_hash,
                status,
                _make_decision().model_dump_json(),
                total_cost_usd,
                total_latency_ms,
            ),
        )
        for step in steps or []:
            conn.execute(
                """
                INSERT INTO run_steps
                    (run_id, step_index, component, decision_type, probe_name,
                     args_hash, prompt_hash, model_id, cache_status, cost_usd,
                     latency_s, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')
                """,
                (
                    run_id,
                    step["step_index"],
                    step["component"],
                    step["decision_type"],
                    step.get("probe_name"),
                    step.get("args_hash"),
                    step.get("prompt_hash"),
                    step.get("model_id"),
                    step.get("cache_status"),
                    step.get("cost_usd"),
                    step.get("latency_s"),
                    step.get("error"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _model_step_row(
    step_index: int, *, error: str | None = None, model_id: str = "claude-x"
) -> dict[str, Any]:
    """A minimal `component='model'` `run_steps` row for the warning tests."""
    return {
        "step_index": step_index,
        "component": "model",
        "decision_type": "investigator",
        "model_id": model_id,
        "error": error,
    }


def _run_system_c_seeding_model_steps(run_id: str, steps: list[dict[str, Any]]) -> Any:
    """A `run_system_c` stub that seeds `run_steps` rows into the CLI's own conn.

    Unlike `_recording_run_system_c`, this writes directly through the
    `conn` argument `run_command` passes in (the same connection `cli.py`
    itself opened via `connect(db)`), rather than opening a second
    connection to the same db path — matching how `cli.run_command` will
    query those very rows back out afterward. A `runs` row is inserted
    first since `run_steps.run_id` has a `FOREIGN KEY` constraint and
    `rli.db.connect` turns `PRAGMA foreign_keys` on.
    """

    def stub(
        conn: sqlite3.Connection,
        cfg: Config,
        url: str,
        llm: Any,
        *,
        max_steps: int | None = None,
    ) -> RunResult:
        conn.execute(
            """
            INSERT INTO runs
                (id, input_url, system, mode, config_hash, started_at,
                 status, final_decision, total_cost_usd, total_latency_ms)
            VALUES (?, ?, 'C', 'live', 'cfg:stub', '2026-01-01T00:00:00Z',
                    'completed', ?, 0.0, 0)
            """,
            (run_id, url, _make_decision().model_dump_json()),
        )
        for step in steps:
            conn.execute(
                """
                INSERT INTO run_steps
                    (run_id, step_index, component, decision_type, model_id,
                     error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')
                """,
                (
                    run_id,
                    step["step_index"],
                    step["component"],
                    step["decision_type"],
                    step.get("model_id"),
                    step.get("error"),
                ),
            )
        conn.commit()
        return _make_run_result(run_id=run_id)

    return stub


# ---------------------------------------------------------------------------
# 1. Help text
# ---------------------------------------------------------------------------


def test_app_help_exits_zero_and_names_both_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout
    assert "trace" in result.stdout


def test_run_help_exits_zero_and_names_its_options() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--url" in result.stdout
    assert "--db" in result.stdout
    assert "--model" in result.stdout
    assert "--max-steps" in result.stdout


def test_trace_help_exits_zero_and_names_its_options() -> None:
    result = runner.invoke(app, ["trace", "--help"])
    assert result.exit_code == 0
    assert "--run-id" in result.stdout
    assert "--db" in result.stdout
    assert "--json" in result.stdout


# ---------------------------------------------------------------------------
# 2. Missing API key
# ---------------------------------------------------------------------------


def test_run_command_reports_llm_error_cleanly_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`run` surfaces an `LLMError` (e.g. an unreachable endpoint) as a clean,
    human-readable failure: exit code 2, the pinned message, on stderr, and
    never a Python traceback in the output.

    The configured API-key env var is explicitly removed so this test is
    correct even on a machine that happens to have one set.
    `rli.agent.cli.OpenAICompatibleClient` is stubbed so no real client is
    ever constructed, and `rli.agent.cli.run_system_c` is stubbed to raise
    `LLMError` directly — the exact exception `cli.py`'s `except LLMError`
    clause is written to catch — so this test pins `cli.py`'s own
    error-handling code without needing a live call or a network-dependent
    failure path.
    """
    cfg = load_config()
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)
    monkeypatch.setattr(agent_cli, "OpenAICompatibleClient", _StubLLMClient)

    def _boom(*args: Any, **kwargs: Any) -> RunResult:
        raise LLMError("LLM endpoint is not reachable")

    monkeypatch.setattr(agent_cli, "run_system_c", _boom)

    db_path = tmp_path / "rli.db"
    result = runner.invoke(
        app,
        ["run", "--url", "https://example.com/jobs/1", "--db", str(db_path)],
    )

    assert result.exit_code == 2
    assert "LLM call failed: LLM endpoint is not reachable" in result.stderr
    # The remedy names the configured endpoint and the env var to export.
    assert cfg.llm.base_url in result.stderr
    assert cfg.llm.api_key_env in result.stderr
    assert "Traceback" not in result.output
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# 3. `run` succeeds with clean stdout, and forwards --model/--max-steps
# ---------------------------------------------------------------------------


def test_run_command_success_prints_only_decision_json_on_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful `run`:

    * writes ONLY the `Decision` JSON to stdout (parseable, and a valid
      `Decision` per `rli.models.decision.Decision.model_validate`);
    * writes the `run_id=...` line to stderr instead;
    * forwards `--model` / `--max-steps` through to `run_system_c`'s call
      (inspected via the recording stub), with `--model` reaching the LLM
      client's own `model_id` rather than the loaded `Config`;
    * never mutates the `Config` object `load_config()` produced — its
      `llm.model_id` still reads the configured default after the run, not
      the `--model` override, because `cli.py` never rewrites `cfg.llm.
      model_id` at all (only the client's own `model_id` changes). See this
      module's docstring / the final report for why that is worth pinning
      explicitly.
    """
    monkeypatch.setattr(agent_cli, "OpenAICompatibleClient", _StubLLMClient)

    calls: list[dict[str, Any]] = []
    run_result = _make_run_result(run_id="run-cli-success-0001")
    monkeypatch.setattr(agent_cli, "run_system_c", _recording_run_system_c(calls, run_result))

    baseline_model_id = load_config().llm.model_id
    override_model_id = "test-override-model-xyz"
    assert override_model_id != baseline_model_id

    db_path = tmp_path / "rli.db"
    result = runner.invoke(
        app,
        [
            "run",
            "--url",
            "https://example.com/jobs/7",
            "--db",
            str(db_path),
            "--model",
            override_model_id,
            "--max-steps",
            "7",
        ],
    )

    assert result.exit_code == 0, result.output

    # -- stdout carries only the decision JSON --------------------------
    decision_payload = json.loads(result.stdout)
    Decision.model_validate(decision_payload)
    assert decision_payload["posting_state"] == "open"
    assert decision_payload["recommended_action"] == "apply_now"

    # -- stderr carries the run id, not the decision ---------------------
    assert "run_id=run-cli-success-0001" in result.stderr
    assert "posting_state" not in result.stderr

    # -- --model / --max-steps reached run_system_c's call ----------------
    assert len(calls) == 1
    recorded = calls[0]
    assert recorded["max_steps"] == 7
    assert recorded["url"] == "https://example.com/jobs/7"
    assert recorded["llm"].model_id == override_model_id

    # -- the loaded Config is not mutated by the --model override ---------
    recorded_cfg = recorded["cfg"]
    assert isinstance(recorded_cfg, Config)
    assert recorded_cfg.llm.model_id == baseline_model_id
    assert recorded_cfg.llm.model_id != override_model_id


def test_run_command_without_overrides_forwards_none_to_run_system_c(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Omitting `--model`/`--max-steps` forwards `max_steps=None` and lets
    the LLM client fall back to `cfg.llm.model_id`, confirming the override
    plumbing is genuinely optional rather than always active.
    """
    monkeypatch.setattr(agent_cli, "OpenAICompatibleClient", _StubLLMClient)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        agent_cli, "run_system_c", _recording_run_system_c(calls, _make_run_result())
    )

    db_path = tmp_path / "rli.db"
    result = runner.invoke(
        app, ["run", "--url", "https://example.com/jobs/1", "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    recorded = calls[0]
    assert recorded["max_steps"] is None
    assert recorded["llm"].model_id == recorded["cfg"].llm.model_id


# ---------------------------------------------------------------------------
# 4. `trace` ordering/format
# ---------------------------------------------------------------------------


def test_trace_command_orders_by_step_index_and_renders_tokens_separately(
    tmp_path: Path,
) -> None:
    """`trace` prints steps in `step_index` order (not insertion order), with
    token counts rendered as their own fields rather than as a `:tokens=`
    suffix glued onto `decision_type`, and prints the run's identifying
    header fields.
    """
    db_path = tmp_path / "rli.db"
    run_id = "run-trace-0001"
    _seed_run_and_steps(
        db_path,
        run_id=run_id,
        system="C",
        status="completed",
        config_hash="cfg:abc123|c1:claude-x:v1",
        total_cost_usd=0.0456,
        total_latency_ms=2500,
        steps=[
            # Inserted out of step_index order on purpose.
            {
                "step_index": 2,
                "component": "controller",
                "decision_type": "controller_decision:run:eligible",
                "probe_name": "repost_history",
                "args_hash": "abcdef0123456789",
            },
            {
                "step_index": 0,
                "component": "controller",
                "decision_type": "system_version",
                "args_hash": "c1:claude-x:v1",
            },
            {
                "step_index": 1,
                "component": "model",
                "decision_type": with_tokens("investigator", 120, 45),
                "prompt_hash": "deadbeef01234567",
                "model_id": "claude-x",
                "cache_status": "hit",
                "cost_usd": 0.0,
                "latency_s": 0.012,
            },
        ],
    )

    result = runner.invoke(app, ["trace", "--run-id", run_id, "--db", str(db_path)])

    assert result.exit_code == 0, result.output

    step_lines = [line for line in result.stdout.splitlines() if line.startswith("step=")]
    assert [line.split()[0] for line in step_lines] == ["step=0", "step=1", "step=2"]

    # The header line prints the run's identifying/summary fields.
    assert "system=C" in result.stdout
    assert "status=completed" in result.stdout
    assert "config_hash=cfg:abc123|c1:claude-x:v1" in result.stdout

    # Tokens are their own fields, and the raw ":tokens=" suffix never
    # appears glued onto decision_type in the rendered output.
    investigator_line = step_lines[1]
    assert "decision_type=investigator" in investigator_line
    assert "tokens_in=120" in investigator_line
    assert "tokens_out=45" in investigator_line
    assert ":tokens=" not in result.stdout


def test_trace_command_json_emits_only_parseable_json(tmp_path: Path) -> None:
    """`trace --json` emits ONLY a JSON array on stdout — no header, no
    human-readable lines mixed in.
    """
    db_path = tmp_path / "rli.db"
    run_id = "run-trace-json-0001"
    _seed_run_and_steps(
        db_path,
        run_id=run_id,
        steps=[
            {
                "step_index": 0,
                "component": "controller",
                "decision_type": "system_version",
                "args_hash": "c1:claude-x:v1",
            },
            {
                "step_index": 1,
                "component": "model",
                "decision_type": with_tokens("investigator", 10, 5),
                "prompt_hash": "deadbeef01234567",
                "model_id": "claude-x",
                "cache_status": "miss",
                "cost_usd": 0.002,
                "latency_s": 0.5,
            },
        ],
    )

    result = runner.invoke(app, ["trace", "--run-id", run_id, "--db", str(db_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert [row["step_index"] for row in payload] == [0, 1]
    assert payload[1]["decision_type"] == with_tokens("investigator", 10, 5)


def test_trace_command_unknown_run_id_exits_nonzero_without_traceback(
    tmp_path: Path,
) -> None:
    """`trace` on a run id that does not exist fails cleanly: non-zero exit,
    a readable message naming the missing run id, and no traceback.
    """
    db_path = tmp_path / "rli.db"
    init_db(db_path)

    result = runner.invoke(app, ["trace", "--run-id", "does-not-exist", "--db", str(db_path)])

    assert result.exit_code != 0
    assert "does-not-exist" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# 5. `run` warns on stderr when System C's model calls were silently
#    swallowed (spec.md §4's `except LLMError` inside loop.py/explanation.py)
# ---------------------------------------------------------------------------


def test_run_command_warns_when_all_model_calls_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When every `component='model'` `run_steps` row for this run carries a
    non-null `error`, `run` prints a loud, specific warning to stderr naming
    the run id and the first error verbatim, while stdout still carries
    ONLY a valid, parseable `Decision` and the exit code stays 0 — the
    decision is genuine (from the deterministic fallback policy), just not
    agent-authored.
    """
    monkeypatch.setattr(agent_cli, "OpenAICompatibleClient", _StubLLMClient)

    run_id = "run-cli-all-failed-0001"
    monkeypatch.setattr(
        agent_cli,
        "run_system_c",
        _run_system_c_seeding_model_steps(
            run_id,
            [
                _model_step_row(0, error="LLM endpoint is not reachable"),
                _model_step_row(1, error="LLM endpoint is not reachable"),
            ],
        ),
    )

    db_path = tmp_path / "rli.db"
    result = runner.invoke(
        app, ["run", "--url", "https://example.com/jobs/1", "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.output

    # -- stdout carries ONLY the decision JSON ---------------------------
    decision_payload = json.loads(result.stdout)
    Decision.model_validate(decision_payload)

    # -- stderr carries a loud, specific warning --------------------------
    assert run_id in result.stderr
    assert "LLM endpoint is not reachable" in result.stderr
    assert "deterministic" in result.stderr.lower()
    assert "credentials" in result.stderr.lower()
    assert "rli agent trace --run-id" in result.stderr


def test_run_command_notes_partial_model_call_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When only some `component='model'` rows carry an `error`, `run`
    prints a shorter stderr note conveying the fraction that failed,
    without stdout being touched.
    """
    monkeypatch.setattr(agent_cli, "OpenAICompatibleClient", _StubLLMClient)

    run_id = "run-cli-partial-failed-0001"
    monkeypatch.setattr(
        agent_cli,
        "run_system_c",
        _run_system_c_seeding_model_steps(
            run_id,
            [
                _model_step_row(0, error="rate limited"),
                _model_step_row(1, error=None),
            ],
        ),
    )

    db_path = tmp_path / "rli.db"
    result = runner.invoke(
        app, ["run", "--url", "https://example.com/jobs/1", "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.output
    decision_payload = json.loads(result.stdout)
    Decision.model_validate(decision_payload)

    assert "1" in result.stderr and "2" in result.stderr
    assert run_id in result.stderr


def test_run_command_no_warning_when_model_calls_all_succeeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When every `component='model'` row has no `error` (or there are none
    at all — the legitimate zero-call short-circuit path), `run` stays
    silent on stderr beyond the ordinary `run_id=...` line: no failure
    warning is printed.
    """
    monkeypatch.setattr(agent_cli, "OpenAICompatibleClient", _StubLLMClient)

    run_id = "run-cli-all-succeeded-0001"
    monkeypatch.setattr(
        agent_cli,
        "run_system_c",
        _run_system_c_seeding_model_steps(
            run_id,
            [
                _model_step_row(0, error=None),
                _model_step_row(1, error=None),
            ],
        ),
    )

    db_path = tmp_path / "rli.db"
    result = runner.invoke(
        app, ["run", "--url", "https://example.com/jobs/1", "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.output
    decision_payload = json.loads(result.stdout)
    Decision.model_validate(decision_payload)

    assert "WARNING" not in result.stderr
    assert result.stderr.strip() == f"run_id={run_id}"


def test_run_command_no_warning_when_zero_model_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Zero `component='model'` rows (the investigator was skipped entirely,
    e.g. unresolved identity or a hard stop before the first model call) is
    not itself a degraded run, so `run` prints no failure warning.
    """
    monkeypatch.setattr(agent_cli, "OpenAICompatibleClient", _StubLLMClient)

    run_id = "run-cli-zero-model-rows-0001"
    monkeypatch.setattr(agent_cli, "run_system_c", _run_system_c_seeding_model_steps(run_id, []))

    db_path = tmp_path / "rli.db"
    result = runner.invoke(
        app, ["run", "--url", "https://example.com/jobs/1", "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.output
    decision_payload = json.loads(result.stdout)
    Decision.model_validate(decision_payload)

    assert "WARNING" not in result.stderr


# ---------------------------------------------------------------------------
# 6. `trace`'s human-readable header names the run id
# ---------------------------------------------------------------------------


def test_trace_command_header_names_the_run_id(tmp_path: Path) -> None:
    """The human-readable (non-`--json`) `trace` header leads with the run
    id, so a reader can tell which run's trace they're looking at without
    scrolling back to the invocation. The `--json` shape is untouched.
    """
    db_path = tmp_path / "rli.db"
    run_id = "run-trace-header-0001"
    _seed_run_and_steps(
        db_path,
        run_id=run_id,
        steps=[
            {
                "step_index": 0,
                "component": "controller",
                "decision_type": "system_version",
                "args_hash": "c1:claude-x:v1",
            },
        ],
    )

    result = runner.invoke(app, ["trace", "--run-id", run_id, "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    header_line = result.stdout.splitlines()[0]
    assert header_line.split()[0] == f"run_id={run_id}"
