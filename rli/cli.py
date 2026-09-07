"""rli command-line interface."""

from __future__ import annotations

import json

import typer

from rli.archive.cli import app as archive_app
from rli.config import load_config
from rli.db import connect, init_db
from rli.eval.baseline import baseline_report, load_split_map, write_baseline_report
from rli.eval.report import summarize_runs
from rli.eval.system_a import run_system_a
from rli.eval.system_b import run_system_b
from rli.history.cli import app as history_app
from rli.models.time import now_utc, parse_utc
from rli.replay.build import DEFAULT_GRID_STEP_DAYS, build_dataset
from rli.replay.leakage import check_dataset
from rli.replay.run import SYSTEM_RUNNERS, run_replay
from rli.snapshots.daily import run_daily_snapshot
from rli.snapshots.targets import DEFAULT_TARGETS_PATH, load_targets, upsert_companies

app = typer.Typer(
    name="rli",
    help="Role-Liveness Investigator — evidence-backed job posting liveness analysis.",
    no_args_is_help=True,
)
replay_app = typer.Typer(
    name="replay",
    help="Point-in-time replay (spec.md §6): build a dataset, replay a system, audit leakage.",
    no_args_is_help=True,
)
eval_app = typer.Typer(
    name="eval",
    help="Evaluation reports (spec.md §6): A/B baseline metrics and posting-behavior curves.",
    no_args_is_help=True,
)
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
    wanted = {
        item.strip().lower()
        for value in only
        for item in value.split(",")
        if item.strip()
    }
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
        summary = run_daily_snapshot(conn, cfg, filtered, now=now_utc())
        typer.echo(summary.describe())
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


# The spec.md §6 systems this CLI can drive. C / C2 land in PLAN.md M5.
_SYSTEMS = ("A", "B")


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
        help="Which system to run: A (full probes) or B (deterministic rules).",
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
        runner = run_system_a if chosen == "A" else run_system_b
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
        help="Which system to summarize: A or B.",
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
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Collect the cached full-probe record for a point-in-time replay dataset.

    This is the ONE replay command that makes live network calls: one System A
    run per selected posting (spec.md §6: "results come only from the cached
    full-probe record" — this is where that record comes from). Every later
    step reads it and reaches the network never.
    """
    if split_kind not in ("temporal", "company"):
        raise typer.BadParameter(
            f"unknown split kind {split_kind!r}; expected 'temporal' or 'company'",
            param_hint="--split-kind",
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
        )
        typer.echo(summary.describe())
    finally:
        conn.close()


@replay_app.command("run")
def replay_run_command(
    system: str = typer.Option(
        ..., "--system", help="Which system to replay: A (full probes) or B (rules)."
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
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Replay one system over every (posting, T) case of a dataset."""
    chosen = _normalized_system(system)
    if chosen not in SYSTEM_RUNNERS:  # pragma: no cover - defensive
        raise typer.BadParameter(
            f"system {chosen!r} has no shipped replay runner", param_hint="--system"
        )
    cfg = load_config()
    init_db(db)
    conn = connect(db)
    try:
        summary = run_replay(
            conn,
            cfg,
            dataset_id=dataset,
            system=chosen,
            limit_cases=limit_cases,
            replace=not keep_previous,
        )
        typer.echo(summary.describe())
        if summary.violations:
            raise typer.Exit(code=1)
    finally:
        conn.close()


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
        help="Temporal-split cutoff (ISO 8601 UTC). Defaults to the dataset's created_at, "
        "which reproduces the assignment the build used.",
    ),
    validation_cutoff: str | None = typer.Option(
        None, "--validation-cutoff", help="Temporal-split validation cutoff (ISO 8601 UTC)."
    ),
    db: str = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
) -> None:
    """Write the A/B baseline report for one replay dataset (PLAN.md M4).

    Never touches the `test` split: `rli.eval.baseline` refuses it outright
    (PLAN.md M4: "keep final holdouts untouched until M6").
    """
    cfg = load_config()
    init_db(db)
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT split_kind, created_at FROM replay_datasets WHERE dataset_id = ?",
            (dataset,),
        ).fetchone()
        kind = split_kind or (row["split_kind"] if row is not None else "company")
        if kind not in ("temporal", "company"):
            raise typer.BadParameter(
                f"unknown split kind {kind!r}; expected 'temporal' or 'company'",
                param_hint="--split-kind",
            )
        moment = _optional_moment(cutoff, "--cutoff")
        if moment is None:
            moment = parse_utc(row["created_at"]) if row is not None else now_utc()

        splits = load_split_map(
            conn,
            cutoff=moment,
            validation_cutoff=_optional_moment(validation_cutoff, "--validation-cutoff"),
            split_kind=kind,  # type: ignore[arg-type]
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


if __name__ == "__main__":
    app()
