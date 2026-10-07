"""rli command-line interface."""

from __future__ import annotations

import json
import sqlite3

import typer

from rli.agent.cli import app as agent_app
from rli.archive.cli import app as archive_app
from rli.config import load_config
from rli.db import connect, connect_read_only, init_db
from rli.eval.baseline import baseline_report, write_baseline_report
from rli.eval.metrics import split_map_for_dataset
from rli.eval.report import summarize_runs
from rli.eval.system_a import run_system_a
from rli.eval.system_b import run_system_b
from rli.eval.system_r import run_system_r
from rli.history.cli import app as history_app
from rli.models.time import now_utc, parse_utc
from rli.replay.build import DEFAULT_GRID_STEP_DAYS, build_dataset
from rli.replay.leakage import check_dataset
from rli.replay.retire import (
    RetiredDatasetError,
    dataset_rows,
    ensure_not_retired,
    retire_dataset,
    retirement_of,
    unretire_dataset,
)
from rli.replay.run import (
    REPLAY_BUSY_TIMEOUT_S,
    SYSTEM_RUNNERS,
    dataset_status,
    parse_shard,
    run_replay,
)
from rli.snapshots.daily import run_daily_snapshot
from rli.snapshots.targets import DEFAULT_TARGETS_PATH, load_targets, upsert_companies

app = typer.Typer(
    name="rli",
    help="Role-Liveness Investigator — evidence-backed job posting liveness analysis.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
replay_app = typer.Typer(
    name="replay",
    help="Point-in-time replay (spec.md §6): build a dataset, replay a system, audit leakage.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
eval_app = typer.Typer(
    name="eval",
    help="Evaluation reports (spec.md §6): A/B baseline metrics and posting-behavior curves.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
app.add_typer(agent_app, name="agent")
app.add_typer(archive_app, name="archive")
app.add_typer(history_app, name="history")
app.add_typer(replay_app, name="replay")
app.add_typer(eval_app, name="eval")

# Default DB path shared by every command below, for consistency.
_DEFAULT_DB_PATH = "./data/rli.db"


@app.callback()
def _main() -> None:
    """Role-Liveness Investigator CLI."""


@app.command("init-db")
def init_db_command(
    path: str = typer.Option(
        "./data/rli.db",
        "--path",
        help="Path to the SQLite database file to create/initialize.",
    ),
) -> None:
    """Create the SQLite schema at PATH (idempotent)."""
    init_db(path)
    typer.echo(f"Initialized database at {path}")


@app.command("load-targets")
def load_targets_command(
    targets: str = typer.Option(
        str(DEFAULT_TARGETS_PATH),
        "--targets",
        help="Path to the targets CSV (company_name, website_domain, ats, tenant, ...).",
    ),
    db: str = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite database file.",
    ),
) -> None:
    """Load verified ATS targets from CSV and upsert their `companies` rows."""
    init_db(db)
    conn = connect(db)
    try:
        loaded = load_targets(targets)
        count = upsert_companies(conn, loaded, now=now_utc())
        typer.echo(f"Upserted {count} companies from {targets}")
    finally:
        conn.close()


def _matches_only(target_tenant: str, only: list[str] | None) -> bool:
    if not only:
        return True
    tenant_lower = target_tenant.lower()
    # Each --only value may itself be a comma-separated list, so both
    # `--only a --only b` and `--only a,b` are accepted.
    wanted = {item.strip().lower() for value in only for item in value.split(",") if item.strip()}
    return tenant_lower in wanted


@app.command("snapshot")
def snapshot_command(
    targets: str = typer.Option(
        str(DEFAULT_TARGETS_PATH),
        "--targets",
        help="Path to the targets CSV (company_name, website_domain, ats, tenant, ...).",
    ),
    db: str = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite database file.",
    ),
    only: list[str] = typer.Option(
        None,
        "--only",
        help="Restrict to these tenants (repeatable, and/or comma-separated), matched "
        "case-insensitively against the targets CSV's tenant column.",
    ),
) -> None:
    """Run the daily board-snapshot capture for the given targets (spec.md §4/§5)."""
    cfg = load_config()
    init_db(db)
    conn = connect(db)
    try:
        loaded = load_targets(targets)
        filtered = [t for t in loaded if _matches_only(t.tenant, only)]
        upsert_companies(conn, filtered, now=now_utc())
        summary = run_daily_snapshot(conn, cfg, filtered, now=now_utc(), clock=now_utc)
        typer.echo(summary.describe())
    finally:
        conn.close()


@app.command("import-run-windows")
def import_run_windows_command(
    logs: str = typer.Option(
        "data/logs", "--logs", help="Directory holding the daily job's daily-*.log files."
    ),
    stamped_at_start_before: str = typer.Option(
        "2026-10-06T21:50:16Z",
        "--stamped-at-start-before",
        help="Runs that STARTED before this instant stamped every capture with the run's "
        "start (pre-fix); they are recorded with stamped_at_start=1.",
    ),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """One-off: record past daily snapshot runs' windows from their logs (schema v6).

    Then gives every pre-fix capture batch that no logged window covers a
    conservative fallback window (its stamp to the next own batch, at most
    24 h). Idempotent (a window already recorded is skipped). Read by the
    replay builder, the leakage checker and the first-published guard
    (rli.snapshots.run_windows).
    """
    from pathlib import Path

    from rli.models.time import parse_utc
    from rli.snapshots.run_windows import add_fallback_windows, import_log_windows

    try:
        parse_utc(stamped_at_start_before)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--stamped-at-start-before") from exc

    paths = sorted(Path(logs).glob("daily-*.log"))
    if not paths:
        raise typer.BadParameter(f"no daily-*.log files under {logs}", param_hint="--logs")
    init_db(db)
    conn = connect(db)
    try:
        summary = import_log_windows(conn, paths, stamped_at_start_before=stamped_at_start_before)
        typer.echo(summary.describe())
        fallback = add_fallback_windows(conn, stamped_at_start_before=stamped_at_start_before)
        typer.echo(fallback.describe())
    finally:
        conn.close()


@app.command("snapshot-status")
def snapshot_status_command(
    db: str = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite database file.",
    ),
) -> None:
    """Print each company's most recent board-snapshot capture, plus the coverage-gap count."""
    init_db(db)
    conn = connect(db)
    try:
        rows = conn.execute(
            """
            SELECT
                c.company_id AS company_id,
                latest.captured_at AS last_captured_at,
                latest.coverage_status AS coverage_status
            FROM companies c
            LEFT JOIN (
                SELECT bs.company_id, bs.captured_at, bs.coverage_status
                FROM board_snapshots bs
                INNER JOIN (
                    SELECT company_id, MAX(captured_at) AS max_captured_at
                    FROM board_snapshots
                    GROUP BY company_id
                ) m ON m.company_id = bs.company_id AND m.max_captured_at = bs.captured_at
            ) latest ON latest.company_id = c.company_id
            ORDER BY c.company_id
            """
        ).fetchall()
        for row in rows:
            typer.echo(
                f"{row['company_id']}: last_captured_at={row['last_captured_at']} "
                f"coverage_status={row['coverage_status']}"
            )

        gap_count = conn.execute(
            "SELECT COUNT(*) AS n FROM capture_attempts WHERE ok = 0"
        ).fetchone()["n"]
        typer.echo(f"coverage gaps (failed capture_attempts): {gap_count}")
    finally:
        conn.close()


# The offline (no-LLM) spec.md §6 systems this CLI can drive directly. System
# R is C's controller without the LLM (rli.eval.system_r); C is handled by
# `replay run` (and the API) because it needs a model client.
_SYSTEMS = ("A", "B", "R")


def _normalized_system(system: str) -> str:
    """Validate `--system`, accepting either case, or raise a typer error.

    `typer.BadParameter` rather than a bare `ValueError` so the user gets the
    usual "Invalid value for '--system'" message and exit code 2, instead of
    a traceback.
    """
    normalized = system.strip().upper()
    if normalized not in _SYSTEMS:
        raise typer.BadParameter(
            f"unknown system {system!r}; expected one of {', '.join(_SYSTEMS)}",
            param_hint="--system",
        )
    return normalized


@app.command("run")
def run_command(
    system: str = typer.Option(
        ...,
        "--system",
        help="Which system to run: A (full probes), B (deterministic rules) or R "
        "(C's controller with no LLM: every eligible probe in rank order).",
    ),
    url: str = typer.Option(..., "--url", help="The job posting URL to investigate."),
    db: str = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite database file.",
    ),
) -> None:
    """Investigate one URL and print the spec.md §1 decision as JSON.

    STDOUT carries the decision JSON and nothing else, so the command can be
    piped into `jq` or a benchmark harness; the run id (an internal trace
    handle, spec.md §1: "The internal run trace, not the user output") goes
    to stderr.

    Key order matches spec.md §1 exactly because it is
    `Decision.model_dump(mode="json")` and `rli.models.decision.Decision`
    declares its fields in that order.
    """
    chosen = _normalized_system(system)
    cfg = load_config()
    init_db(db)
    conn = connect(db)
    try:
        runner = {"A": run_system_a, "B": run_system_b, "R": run_system_r}[chosen]
        result = runner(conn, cfg, url)
        typer.echo(
            f"run_id={result.run_id} system={result.system} "
            f"probes={','.join(result.probes_run) or '(none)'}"
            + (f" route={result.route_rule}" if result.route_rule else ""),
            err=True,
        )
        typer.echo(json.dumps(result.decision.model_dump(mode="json"), indent=2))
    finally:
        conn.close()


@app.command("runs-summary")
def runs_summary_command(
    system: str = typer.Option(
        ...,
        "--system",
        help="Which system to summarize: A, B or R.",
    ),
    db: str = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite database file.",
    ),
) -> None:
    """Print the spec.md §6 baseline figures for one system's recorded runs."""
    chosen = _normalized_system(system)
    init_db(db)
    conn = connect(db)
    try:
        typer.echo(summarize_runs(conn, chosen).describe())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rli replay ... (spec.md §6 "Replay"; PLAN.md M4)
# ---------------------------------------------------------------------------


def _optional_moment(value: str | None, name: str):
    """Parse an optional ISO-8601 CLI option into an aware UTC datetime.

    `typer.BadParameter` rather than a bare `ValueError` so a mistyped
    timestamp produces the usual "Invalid value for '--cutoff'" message and
    exit code 2 instead of a traceback.
    """
    if value is None:
        return None
    try:
        return parse_utc(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint=name) from exc


@replay_app.command("build")
def replay_build_command(
    dataset: str = typer.Option(
        ..., "--dataset", help="Id to give this dataset (rebuilding an id replaces it)."
    ),
    split: str = typer.Option(
        "dev",
        "--split",
        help="Which split to draw postings from: dev or validation. 'test' is refused "
        "(PLAN.md M4 keeps the final holdout untouched until M6).",
    ),
    split_kind: str = typer.Option(
        "company",
        "--split-kind",
        help="temporal (spec.md §6 temporal holdout) or company (company holdout).",
    ),
    grid_days: int = typer.Option(
        DEFAULT_GRID_STEP_DAYS,
        "--grid-days",
        help="Spacing of the evaluation grid, in days. The endpoint "
        "min(first_seen_absent, now) is always included as well.",
    ),
    limit_postings: int | None = typer.Option(
        None,
        "--limit-postings",
        help="Cap the number of postings, taken round-robin across companies.",
    ),
    cutoff: str | None = typer.Option(
        None, "--cutoff", help="Temporal-split cutoff (ISO 8601 UTC). Defaults to now."
    ),
    validation_cutoff: str | None = typer.Option(
        None, "--validation-cutoff", help="Temporal-split validation cutoff (ISO 8601 UTC)."
    ),
    notes: str | None = typer.Option(None, "--notes", help="Free text stored on the dataset."),
    exclude_companies_from: list[str] = typer.Option(
        [],
        "--exclude-companies-from",
        help="Dataset id whose companies this dataset must not contain (repeatable). Build "
        "the company-holdout dataset with --split-kind company --exclude-companies-from "
        "<temporal dev dataset> so the two are company-disjoint.",
    ),
    company_split_method: str = typer.Option(
        "hash",
        "--company-split-method",
        help="hash (stable per company; default) or greedy (legacy, balanced by posting "
        "count, drifts as the corpus grows).",
    ),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Collect the cached full-probe record for a point-in-time replay dataset.

    This is the ONE replay command that makes live network calls: one System A
    run per selected posting (spec.md §6: "results come only from the cached
    full-probe record" — this is where that record comes from). Every later
    step reads it and reaches the network never.

    The split each case was drawn from is frozen onto the dataset, so later
    evaluation reads it back rather than re-splitting a grown corpus. The
    company holdout is built company-disjoint from the temporal dev dataset:

        rli replay build --dataset company-dev --split-kind company \\
            --exclude-companies-from dev-temporal
    """
    if split_kind not in ("temporal", "company"):
        raise typer.BadParameter(
            f"unknown split kind {split_kind!r}; expected 'temporal' or 'company'",
            param_hint="--split-kind",
        )
    if company_split_method not in ("hash", "greedy"):
        raise typer.BadParameter(
            f"unknown company split method {company_split_method!r}; expected 'hash' or 'greedy'",
            param_hint="--company-split-method",
        )
    cfg = load_config()
    init_db(db)
    conn = connect(db)
    try:
        summary = build_dataset(
            conn,
            cfg,
            dataset_id=dataset,
            split=split,
            split_kind=split_kind,  # type: ignore[arg-type]
            grid_step_days=grid_days,
            limit_postings=limit_postings,
            cutoff=_optional_moment(cutoff, "--cutoff"),
            validation_cutoff=_optional_moment(validation_cutoff, "--validation-cutoff"),
            notes=notes,
            exclude_companies_from=tuple(exclude_companies_from),
            company_split_method=company_split_method,  # type: ignore[arg-type]
        )
        typer.echo(summary.describe())
    finally:
        conn.close()


@replay_app.command("run")
def replay_run_command(
    system: str = typer.Option(
        ...,
        "--system",
        help="Which system to replay: A (full probes), B (rules), C (LLM agent, PLAN.md M5), "
        "or R (C's controller with no LLM; offline like A and B).",
    ),
    dataset: str = typer.Option(..., "--dataset", help="Replay dataset id to run against."),
    limit_cases: int | None = typer.Option(
        None, "--limit-cases", help="Stop after N cases (a smoke run; not a sample)."
    ),
    keep_previous: bool = typer.Option(
        False,
        "--keep-previous",
        help="Keep this dataset's earlier replay runs for this system instead of "
        "replacing them. Leaves duplicate cases for the baseline report to collapse.",
    ),
    resume: bool | None = typer.Option(
        None,
        "--resume/--no-resume",
        help="Skip cases with a prior non-quota-affected completed run for this "
        "(dataset, system). Defaults to on for --system C, off otherwise.",
    ),
    stop_on_quota: bool | None = typer.Option(
        None,
        "--stop-on-quota/--no-stop-on-quota",
        help="Stop the moment a case's run shows daily LLM-quota exhaustion, deleting "
        "that case's run so `--resume` redoes it later. Defaults to on for --system C, "
        "off otherwise.",
    ),
    shard: str | None = typer.Option(
        None,
        "--shard",
        help="Walk only shard I of N (I/N, 0-based, e.g. 0/4) so N processes can replay "
        "one dataset concurrently. Always resumes; refuses --no-resume and ignores "
        "replacement, and never deletes another shard's runs.",
    ),
    rpm: int | None = typer.Option(
        None,
        "--rpm",
        min=0,
        help="LLM requests/minute for THIS process (System C), overriding "
        "[llm].requests_per_minute. For N shard workers pass floor(total/N). Affects "
        "pacing only; runs.config_hash is unchanged.",
    ),
    busy_timeout: float = typer.Option(
        REPLAY_BUSY_TIMEOUT_S,
        "--busy-timeout",
        min=0.0,
        help="Seconds a write waits on a locked database before failing.",
    ),
    allow_retired: bool = typer.Option(
        False,
        "--allow-retired",
        help="Replay a dataset retired with `rli replay retire` anyway.",
    ),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Replay one system over every (posting, T) case of a dataset."""
    raw_system = system.strip().upper()
    if raw_system == "C":
        chosen = "C"
    else:
        chosen = _normalized_system(system)

    if chosen != "C" and chosen not in SYSTEM_RUNNERS:  # pragma: no cover - defensive
        raise typer.BadParameter(
            f"system {chosen!r} has no shipped replay runner", param_hint="--system"
        )

    shard_spec = None
    if shard is not None:
        try:
            shard_spec = parse_shard(shard)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--shard") from exc
        if resume is False:
            raise typer.BadParameter(
                "--shard cannot be combined with --no-resume: a sharded worker must skip "
                "completed cases, never re-run or replace them",
                param_hint="--shard",
            )

    effective_resume = resume if resume is not None else (chosen == "C" or shard_spec is not None)
    effective_stop_on_quota = stop_on_quota if stop_on_quota is not None else (chosen == "C")

    cfg = load_config()
    init_db(db)
    conn = connect(db, busy_timeout_ms=int(busy_timeout * 1000))
    llm_inner = None
    try:
        # Refuse a retired dataset before any model client is built.
        _refuse_retired(conn, dataset, allow_retired=allow_retired)
        runner = None
        if chosen == "C":
            from rli.agent.loop import make_system_c  # lazy: no agent stack for A/B
            from rli.llm.client import CachedClient

            # ONE model client for the whole walk, so `requests_per_minute`
            # paces this process's calls across cases, not just within one
            # case (a client per case restarts its throttle every case).
            llm_inner = _replay_llm_client(cfg, rpm)
            inner = llm_inner

            def _cached(conn_, _cfg):
                return CachedClient(inner, conn_)

            runner = make_system_c(llm_factory=_cached)

        summary = run_replay(
            conn,
            cfg,
            dataset_id=dataset,
            system=chosen,
            runner=runner,
            limit_cases=limit_cases,
            replace=not keep_previous,
            resume=effective_resume,
            stop_on_quota=effective_stop_on_quota,
            shard=shard_spec,
            allow_retired=allow_retired,
        )
        typer.echo(summary.describe())
        if summary.stopped_reason == "quota_exhausted":
            raise typer.Exit(code=3)
        if summary.violations:
            raise typer.Exit(code=1)
    finally:
        if llm_inner is not None:
            from rli.llm.client import close_llm_client

            close_llm_client(llm_inner)
        conn.close()


def _replay_llm_client(cfg, rpm: int | None):
    """The live model client for `replay run --system C` (a test seam).

    `rpm` overrides `[llm].requests_per_minute` for THIS client only. The
    `cfg` the replay itself runs under is left untouched on purpose:
    `runs.config_hash` fingerprints the whole config, and a pacing knob that
    differs per worker must not split one replay's runs into several
    configurations.
    """
    from rli.llm.client import OpenAICompatibleClient

    if rpm is not None:
        cfg = cfg.model_copy(
            update={"llm": cfg.llm.model_copy(update={"requests_per_minute": rpm})}
        )
    return OpenAICompatibleClient.from_config(cfg)


@replay_app.command("check")
def replay_check_command(
    dataset: str = typer.Option(..., "--dataset", help="Replay dataset id to audit."),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Audit a dataset's replay runs for future leakage (spec.md §6: 0 target).

    Exits nonzero when any violation is found, so it can gate a milestone from
    a script without the caller parsing the output.
    """
    init_db(db)
    conn = connect(db)
    try:
        report = check_dataset(conn, dataset)
        typer.echo(report.describe())
        if not report.clean:
            raise typer.Exit(code=1)
    finally:
        conn.close()


@replay_app.command("status")
def replay_status_command(
    dataset: str = typer.Option(..., "--dataset", help="Replay dataset id to report on."),
    shards: int | None = typer.Option(
        None,
        "--shards",
        min=1,
        help="Also split each system's counts by `replay run --shard I/N` shard (N shards).",
    ),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Per-system case totals for a replay dataset: total / completed / remaining.

    A quota-cut run counts as still remaining (see `rli.replay.run.dataset_status`),
    which is what makes this the right thing to check before resuming a multi-day
    System C replay against a free-tier LLM API's daily quota.
    """
    init_db(db)
    conn = connect(db)
    try:
        status = dataset_status(conn, dataset_id=dataset, shards=shards)
        retirement = retirement_of(conn, dataset)
        if retirement is not None:
            typer.echo(f"dataset {dataset}: {retirement.describe()}")
        typer.echo(status.describe())
    finally:
        conn.close()


def _refuse_retired(conn: sqlite3.Connection, dataset: str, *, allow_retired: bool) -> None:
    """`--dataset` is a bad parameter when it names a retired dataset (no override)."""
    try:
        retirement = ensure_not_retired(conn, dataset, allow_retired=allow_retired)
    except RetiredDatasetError as exc:
        raise typer.BadParameter(str(exc), param_hint="--dataset") from exc
    if retirement is not None:
        typer.echo(f"warning: dataset {dataset} is {retirement.describe()}", err=True)


@replay_app.command("list")
def replay_list_command(
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Every replay dataset, oldest first, with its size and retired state. Read-only."""
    try:
        conn = connect_read_only(db)
    except sqlite3.OperationalError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    try:
        rows = dataset_rows(conn)
    finally:
        conn.close()
    if not rows:
        typer.echo("no replay datasets")
        return
    for row in rows:
        if row["retired_at"]:
            reason = f": {row['retired_reason']}" if row["retired_reason"] else ""
            state = f"RETIRED {row['retired_at']}{reason}"
        else:
            state = "active"
        typer.echo(
            f"{row['dataset_id']}  {row['split_kind']}/{row['split_name']}  "
            f"postings={row['postings']} companies={row['companies']} cases={row['cases']}  "
            f"created={row['created_at']}  {state}"
        )


@replay_app.command("retire")
def replay_retire_command(
    dataset: str = typer.Option(..., "--dataset", help="Replay dataset id to retire."),
    reason: str = typer.Option(..., "--reason", help="Why it is retired (recorded)."),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Mark a dataset retired (reversible; deletes nothing).

    A retired dataset is ignored by the company-holdout breach check and refused
    by `replay run` / `eval run` unless `--allow-retired` is passed.
    """
    init_db(db)
    conn = connect(db)
    try:
        retirement = retire_dataset(conn, dataset, reason=reason)
    except LookupError as exc:
        raise typer.BadParameter(str(exc), param_hint="--dataset") from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--reason") from exc
    finally:
        conn.close()
    typer.echo(f"dataset {dataset}: {retirement.describe()}")


@replay_app.command("unretire")
def replay_unretire_command(
    dataset: str = typer.Option(..., "--dataset", help="Replay dataset id to un-retire."),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Clear a dataset's retired state."""
    init_db(db)
    conn = connect(db)
    try:
        previous = unretire_dataset(conn, dataset)
    except LookupError as exc:
        raise typer.BadParameter(str(exc), param_hint="--dataset") from exc
    finally:
        conn.close()
    if previous is None:
        typer.echo(f"dataset {dataset}: was not retired; nothing changed")
    else:
        typer.echo(f"dataset {dataset}: active again (was {previous.describe()})")


# ---------------------------------------------------------------------------
# rli eval ... (spec.md §6 "Metrics"; PLAN.md M4)
# ---------------------------------------------------------------------------


@eval_app.command("baseline")
def eval_baseline_command(
    dataset: str = typer.Option(..., "--dataset", help="Replay dataset id to report on."),
    out: str = typer.Option(..., "--out", help="Path to write the Markdown report to."),
    split_kind: str | None = typer.Option(
        None,
        "--split-kind",
        help="Split to gate on. Defaults to the one recorded on the dataset.",
    ),
    cutoff: str | None = typer.Option(
        None,
        "--cutoff",
        help="Temporal-split cutoff (ISO 8601 UTC), for a deliberate re-split. By default "
        "the split assignment frozen on the dataset at build time is used.",
    ),
    validation_cutoff: str | None = typer.Option(
        None, "--validation-cutoff", help="Temporal-split validation cutoff (ISO 8601 UTC)."
    ),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Write the A/B baseline report for one replay dataset (PLAN.md M4).

    Never touches the `test` split: `rli.eval.baseline` refuses it outright
    (PLAN.md M4: "keep final holdouts untouched until M6"). Splits come from
    `rli.eval.metrics.split_map_for_dataset`: the assignment frozen on the
    dataset at build time (or, for an older dataset, reconstructed as of its
    build), unless `--split-kind` / `--cutoff` / `--validation-cutoff` ask for
    a deliberate re-split.
    """
    cfg = load_config()
    init_db(db)
    conn = connect(db)
    try:
        if split_kind is not None and split_kind not in ("temporal", "company"):
            raise typer.BadParameter(
                f"unknown split kind {split_kind!r}; expected 'temporal' or 'company'",
                param_hint="--split-kind",
            )
        splits, _kind = split_map_for_dataset(
            conn,
            dataset_id=dataset,
            split_kind=split_kind,  # type: ignore[arg-type]
            cutoff=_optional_moment(cutoff, "--cutoff"),
            validation_cutoff=_optional_moment(validation_cutoff, "--validation-cutoff"),
        )
        report = baseline_report(conn, cfg, dataset_id=dataset, splits=splits)
        path = write_baseline_report(out, report)
        typer.echo(report.describe())
        typer.echo(f"wrote {path}", err=True)
    finally:
        conn.close()


@eval_app.command("behavior")
def eval_behavior_command(
    out: str = typer.Option(..., "--out", help="Path to write the Markdown report to."),
    company: str | None = typer.Option(
        None, "--company", help="Restrict the analysis to one company_id."
    ),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Write the censoring-aware posting-behavior report (spec.md §5/§6).

    Closures are interval-censored and open postings are right-censored, so
    the curves come from lifelines' interval-censored and Kaplan-Meier
    fitters; no exact `closed_at` is ever invented (spec.md §4).
    """
    # Imported here, not at module scope: lifelines pulls in pandas, scipy and
    # autograd, which costs seconds on every `rli --help`. This is the only
    # command that needs them.
    from rli.eval.survival import behavior_report, write_behavior_report

    cfg = load_config()
    init_db(db)
    conn = connect(db)
    try:
        report = behavior_report(conn, cfg, company_id=company)
        path = write_behavior_report(out, report)
        typer.echo(report.describe())
        typer.echo(f"wrote {path}", err=True)
    finally:
        conn.close()


def _validated_split_kind(value: str | None) -> str | None:
    """Validate `--split-kind`, or `None` to let the dataset row decide.

    `typer.BadParameter` rather than a bare `ValueError` so a typo produces
    the usual "Invalid value for '--split-kind'" message and exit code 2
    instead of a traceback, matching `replay build` above.
    """
    if value is None:
        return None
    if value not in ("temporal", "company"):
        raise typer.BadParameter(
            f"unknown split kind {value!r}; expected 'temporal' or 'company'",
            param_hint="--split-kind",
        )
    return value


def _eval_connection(db: str, *, read_only: bool):
    """The evaluation's connection: `mode=ro` (and no `init_db`) under `--read-only`."""
    if read_only:
        try:
            return connect_read_only(db)
        except sqlite3.OperationalError as exc:
            raise typer.BadParameter(str(exc), param_hint="--db") from exc
    init_db(db)
    return connect(db)


@eval_app.command("run")
def eval_run_command(
    dataset: str = typer.Option(..., "--dataset", help="Replay dataset id to evaluate."),
    # Mirrors `rli.eval.evaluate.DEFAULT_REPORT_PATH`. Spelled as a literal
    # because a Typer default is evaluated at import time, and importing the
    # evaluation stack on every `rli --help` is exactly what the lazy imports
    # inside the command bodies exist to avoid.
    out: str = typer.Option(
        "reports/evaluation.md", "--out", help="Path to write the Markdown report to."
    ),
    with_c: bool = typer.Option(
        False,
        "--with-c",
        help="Also evaluate System C. Without a reachable LLM endpoint at the "
        "configured llm.base_url (or an injected client) C is recorded as "
        "'not run: no LLM endpoint configured' and no model call is attempted.",
    ),
    with_r: bool = typer.Option(
        False,
        "--with-r",
        help="Replay System R (C's controller, no LLM; offline) when the dataset has no "
        "R runs. Existing R runs are scored either way.",
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help="Open the database with SQLite mode=ro and write nothing: no replay, no "
        "holdout trace marker. Systems without runs are reported as absent.",
    ),
    split_kind: str | None = typer.Option(
        None,
        "--split-kind",
        help="Split to gate on. Defaults to the one recorded on the dataset.",
    ),
    cutoff: str | None = typer.Option(
        None,
        "--cutoff",
        help="Temporal-split cutoff (ISO 8601 UTC) for a deliberate re-split over today's "
        "corpus. By default the split frozen on the dataset at build time is used.",
    ),
    validation_cutoff: str | None = typer.Option(
        None, "--validation-cutoff", help="Temporal-split validation cutoff (ISO 8601 UTC)."
    ),
    rerun: bool = typer.Option(
        False,
        "--rerun/--no-rerun",
        help="Re-run every system through the offline replay instead of reusing the "
        "dataset's existing runs.",
    ),
    survival: bool = typer.Option(
        True,
        "--survival/--no-survival",
        help="Include the corpus-wide posting-behaviour summary. --no-survival skips the "
        "lifelines import entirely.",
    ),
    allow_retired: bool = typer.Option(
        False,
        "--allow-retired",
        help="Evaluate a dataset retired with `rli replay retire` anyway (the report says so).",
    ),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Write the spec.md §6 final evaluation report for one replay dataset (PLAN.md M6).

    Makes no network calls and never builds a dataset: a system with no runs
    for the dataset is replayed offline, and everything else is reused.

    This is the final evaluation, so it READS the `test` holdout when the
    dataset's cases fall in it, and records that read in the run trace, in
    the agent gate's notes and in the report's Limitations. The build-time
    refusals in `rli.replay.build` and `rli.eval.baseline` are untouched.
    """
    # Imported here, not at module scope: the evaluation stack reaches
    # lifelines (survival) and scikit-learn (the C2 ranker), both of which
    # cost seconds to import and neither of which any other command needs.
    from rli.eval.evaluate import evaluate, write_evaluation_report

    if read_only and rerun:
        raise typer.BadParameter(
            "--rerun replays systems; --read-only forbids it", param_hint="--rerun"
        )
    cfg = load_config()
    conn = _eval_connection(db, read_only=read_only)
    try:
        report = evaluate(
            conn,
            cfg,
            dataset_id=dataset,
            split_kind=_validated_split_kind(split_kind),  # type: ignore[arg-type]
            cutoff=_optional_moment(cutoff, "--cutoff"),
            validation_cutoff=_optional_moment(validation_cutoff, "--validation-cutoff"),
            with_c=with_c,
            with_r=with_r,
            rerun=rerun,
            read_only=read_only,
            include_survival=survival,
            allow_retired=allow_retired,
        )
        path = write_evaluation_report(out, report)
        typer.echo(report.describe())
        typer.echo(f"wrote {path}", err=True)
    except RetiredDatasetError as exc:
        raise typer.BadParameter(str(exc), param_hint="--dataset") from exc
    except LookupError as exc:
        # `run_replay` raises this for a dataset with no cases — i.e. a
        # dataset id that was never built. A bad parameter, not a crash.
        raise typer.BadParameter(str(exc), param_hint="--dataset") from exc
    finally:
        conn.close()


@eval_app.command("gates")
def eval_gates_command(
    dataset: str = typer.Option(..., "--dataset", help="Replay dataset id to grade."),
    split_kind: str | None = typer.Option(
        None,
        "--split-kind",
        help="Split to gate on. Defaults to the one recorded on the dataset.",
    ),
    cutoff: str | None = typer.Option(
        None,
        "--cutoff",
        help="Temporal-split cutoff (ISO 8601 UTC) for a deliberate re-split. By default "
        "the split frozen on the dataset at build time is used.",
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help="Open the database with SQLite mode=ro and write nothing.",
    ),
    allow_retired: bool = typer.Option(
        False,
        "--allow-retired",
        help="Grade a dataset retired with `rli replay retire` anyway.",
    ),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Print the two spec.md §6 gate verdicts for one replay dataset.

    Always exits 0: a failing — or unproven, or ungraded — gate is a FINDING
    to be read, not a CLI error to be swallowed by a shell's `set -e`. The
    verdict line says which it is.

    Skips the survival summary (nothing in either gate reads it), so this is
    the cheap way to ask "where do the gates stand".
    """
    from rli.eval.evaluate import evaluate

    cfg = load_config()
    conn = _eval_connection(db, read_only=read_only)
    try:
        report = evaluate(
            conn,
            cfg,
            dataset_id=dataset,
            split_kind=_validated_split_kind(split_kind),  # type: ignore[arg-type]
            cutoff=_optional_moment(cutoff, "--cutoff"),
            include_survival=False,
            read_only=read_only,
            allow_retired=allow_retired,
        )
        typer.echo(f"agent gate: {report.agent_gate.status.upper()}")
        typer.echo(report.agent_gate.describe())
        typer.echo("")
        typer.echo(report.llm_value.describe())
        typer.echo("")
        typer.echo(f"product gate: {report.product_gate.status.upper()}")
        typer.echo(report.product_gate.describe())
    except RetiredDatasetError as exc:
        raise typer.BadParameter(str(exc), param_hint="--dataset") from exc
    except LookupError as exc:
        raise typer.BadParameter(str(exc), param_hint="--dataset") from exc
    finally:
        conn.close()


if __name__ == "__main__":
    app()


@app.command("load-events")
def load_events(
    path: str = typer.Option("data/events/company_events.csv", "--path", help="Events CSV."),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="SQLite database path."),
) -> None:
    """Load pre-collected dated company events (spec.md §4) into `company_events`."""
    from rli.events.store import load_events_csv

    conn = connect(db)
    try:
        n = load_events_csv(path, conn)
    finally:
        conn.close()
    typer.echo(f"Loaded {n} events from {path}")
