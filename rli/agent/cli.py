"""`rli.agent` CLI — System C's sub-app: run the bounded agent loop and inspect its trace.

Not wired into the main `rli` entrypoint here — a different agent owns
`rli/cli.py`. Until it is mounted, run it directly::

    uv run python -m rli.agent.cli run --url https://example.com/jobs/123
    uv run python -m rli.agent.cli trace --run-id <run_id>

spec.md §7 is explicit about where the story of a run lives: "`run_steps` is
the canonical trace". `run` drives `rli.agent.loop.run_system_c` end to end
and prints the resulting `Decision`; `trace` reads that same canonical
`run_steps` table back out for a given run id, in human-readable form or as
JSON.

To mount into `rli/cli.py`::

    from rli.agent.cli import app as agent_app
    app.add_typer(agent_app, name="agent")
"""

from __future__ import annotations

import json
import sqlite3

import typer

from rli.agent.loop import parse_tokens, run_system_c
from rli.config import load_config
from rli.db import connect, init_db
from rli.llm.client import (
    CachedClient,
    LLMError,
    OpenAICompatibleClient,
    close_llm_client,
)

__all__ = ["app"]

app = typer.Typer(
    name="agent",
    help="System C: the bounded LLM agent loop, plus its run_steps trace (spec.md §2/§4/§7).",
    no_args_is_help=True,
)

DEFAULT_DB_PATH = "./data/rli.db"

# `run_steps` columns whose non-null values are rendered as a `key=value`
# token on a `trace` line, in this fixed order. `decision_type` is handled
# separately (it also carries the `parse_tokens` suffix) and `step_index` /
# `component` are always printed, so neither appears here.
_STEP_DETAIL_COLUMNS = (
    "probe_name",
    "args_hash",
    "model_id",
    "prompt_hash",
    "cache_status",
    "cost_usd",
    "latency_s",
    "error",
)

# Columns truncated to their first 12 characters when rendered — long hashes
# that only need to be recognizable, not read in full, on a trace line.
_TRUNCATED_COLUMNS = ("args_hash", "prompt_hash")


@app.command("run")
def run_command(
    url: str = typer.Option(..., "--url", help="The job posting URL to investigate."),
    db: str = typer.Option(
        DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file (created if missing)."
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Override llm.model_id for this run only. Default: use the configured model.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Override the agent/thresholds dynamic-step cap for this run only "
        "(spec.md §4's bounded budget). Default: use the configured cap.",
    ),
) -> None:
    """Investigate one URL with System C and print the spec.md §1 decision as JSON.

    STDOUT carries the decision JSON and nothing else, so the command can be
    piped into `jq`; the run id (pass it to `trace` to inspect the canonical
    `run_steps` record of this run) goes to stderr.
    """
    cfg = load_config()
    init_db(db)
    conn = connect(db)
    llm: CachedClient | None = None
    try:
        llm = CachedClient(OpenAICompatibleClient.from_config(cfg, model_id=model), conn)
        try:
            # A credentials failure does NOT take this path:
            # `OpenAICompatibleClient` never contacts the endpoint while it is
            # being constructed, so construction never throws, and
            # `run_system_c`/`loop.py::_call_investigator` deliberately catch
            # `LLMError` internally (spec.md §4) so the run still reaches a
            # valid `Decision`. This `except` remains correct for a
            # construction-time failure or any other `LLMError` that isn't
            # swallowed before it can propagate here; see the `run_steps`
            # check below for how a swallowed model-call failure is reported.
            result = run_system_c(conn, cfg, url, llm, max_steps=max_steps)
        except LLMError as exc:
            typer.echo(
                f"LLM call failed: {exc}\n"
                f"Check that [llm].base_url ({cfg.llm.base_url}) is serving "
                f"model {model or cfg.llm.model_id!r} — e.g. `ollama serve` for a "
                f"local endpoint — and, for a remote one, that ${cfg.llm.api_key_env} "
                "is exported.",
                err=True,
            )
            raise typer.Exit(code=2) from exc
        typer.echo(f"run_id={result.run_id}", err=True)
        _warn_if_model_calls_failed(conn, result.run_id)
        typer.echo(result.decision.model_dump_json(indent=2))
    finally:
        # This command BUILT the client, so it closes it: the connection pool
        # is released when the command ends rather than whenever the object
        # happens to be collected.
        if llm is not None:
            close_llm_client(llm)
        conn.close()


def _warn_if_model_calls_failed(conn: sqlite3.Connection, run_id: str) -> None:
    """Warn on stderr when System C's model calls were silently swallowed.

    `_call_investigator` (loop.py) and `explain` (explanation.py) both catch
    `LLMError` so the run still reaches a valid `Decision` per spec.md §4 —
    which means a run against an unreachable LLM endpoint still exits
    0 with an ordinary-looking Decision on stdout, printed from the
    deterministic `rli.policy.explain_stub` fallback rather than from the
    agent, with no other signal that the model never actually answered. This
    inspects this run's own `run_steps` rows (`component='model'`) for that
    condition and reports it — stdout stays untouched (Decision JSON only).
    """
    model_rows = conn.execute(
        "SELECT error FROM run_steps WHERE run_id = ? AND component = 'model'",
        (run_id,),
    ).fetchall()

    if not model_rows:
        # Zero model calls is the legitimate no-LLM path: loop.py skips the
        # investigator when identity is unresolved, or a hard stop fires
        # before the first model call. That's not a degraded run, so this
        # stays silent rather than crying wolf on every ordinary short-circuit.
        return

    failures = [row["error"] for row in model_rows if row["error"] is not None]
    total = len(model_rows)
    failed = len(failures)

    if failed == 0:
        return

    if failed == total:
        typer.echo(
            f"WARNING: all {total} of System C's model call(s) failed for run "
            f"{run_id} — the decision printed above came from the "
            "deterministic frozen policy with fallback reasons (rli.policy."
            "explain_stub), NOT from the agent. First error: "
            f"{failures[0]!r}. This usually means the configured LLM "
            "endpoint is unreachable or rejected our credentials. Run "
            f"`rli agent trace --run-id {run_id}` for the full picture.",
            err=True,
        )
    else:
        typer.echo(
            f"WARNING: {failed} of {total} model calls failed for run "
            f"{run_id}; the decision may be partly degraded. Run "
            f"`rli agent trace --run-id {run_id}` for the full picture.",
            err=True,
        )


def _render_step_line(row: sqlite3.Row) -> str:
    parts = [f"step={row['step_index']}", f"component={row['component']}"]

    decision_type = row["decision_type"]
    tokens = parse_tokens(decision_type)
    if tokens is not None:
        marker = decision_type.rfind(":tokens=")
        base = decision_type[:marker]
        input_tokens, output_tokens = tokens
        parts.append(f"decision_type={base}")
        parts.append(f"tokens_in={input_tokens}")
        parts.append(f"tokens_out={output_tokens}")
    else:
        parts.append(f"decision_type={decision_type}")

    for column in _STEP_DETAIL_COLUMNS:
        value = row[column]
        if value is None or value == "":
            continue
        if column in _TRUNCATED_COLUMNS:
            value = str(value)[:12]
        parts.append(f"{column}={value}")

    return " ".join(parts)


@app.command("trace")
def trace_command(
    run_id: str = typer.Option(..., "--run-id", help="The run id to inspect (see `run`'s stderr)."),
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the run_steps rows as a JSON array instead of human-readable lines. "
        "Suppresses the run header, so stdout is clean JSON only.",
    ),
) -> None:
    """Print the spec.md §7 canonical `run_steps` trace for one run."""
    init_db(db)
    conn = connect(db)
    try:
        run_row = conn.execute(
            """
            SELECT system, status, config_hash, total_cost_usd, total_latency_ms
            FROM runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if run_row is None:
            typer.echo(f"no run found with id {run_id!r}", err=True)
            raise typer.Exit(code=1)

        step_rows = conn.execute(
            """
            SELECT step_index, component, decision_type, probe_name, args_hash,
                   prompt_hash, model_id, cache_status, cost_usd, latency_s, error
            FROM run_steps
            WHERE run_id = ?
            ORDER BY step_index
            """,
            (run_id,),
        ).fetchall()

        if as_json:
            typer.echo(json.dumps([dict(row) for row in step_rows], indent=2))
            return

        typer.echo(
            f"run_id={run_id} system={run_row['system']} status={run_row['status']} "
            f"config_hash={run_row['config_hash']} "
            f"total_cost_usd={run_row['total_cost_usd']} "
            f"total_latency_ms={run_row['total_latency_ms']}"
        )
        for row in step_rows:
            typer.echo(_render_step_line(row))
    finally:
        conn.close()


if __name__ == "__main__":
    app()
