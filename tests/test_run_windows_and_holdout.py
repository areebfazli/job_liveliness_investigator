"""Schema v6: snapshot run windows, the stable company holdout, and the scoped FK check.

* `rli.snapshots.run_windows`: log parsing/import, live recording, the
  grid-point shift, the first-published guard bound;
* `rli.replay.build`: grid points moved out of pre-fix windows; the stable
  company holdout excluded from every non-test build and recorded;
* `rli.replay.leakage`: `capture_fetched_after_t`;
* `rli.eval.diagnostics.company_holdout_check`;
* `rli.db`: migration 5 -> 6, and migration 4 -> 5's foreign-key check scoped
  to `runs` (an unrelated older violation no longer blocks it).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
from test_db_v5_migration import _build_v4_database
from test_eval_helpers import COMPANY, OTHER_COMPANY
from test_replay_helpers import NOW, dev_everything_cutoff, mock_ats, seed_corpus

from rli.config import Config
from rli.db import SCHEMA_VERSION, SNAPSHOT_RUNS_DDL, connect, init_db, schema_version
from rli.eval.case import guard_first_published
from rli.eval.diagnostics import company_holdout_check
from rli.models.time import to_utc_z
from rli.policy.inputs import CLAIM_FIRST_PUBLISHED, CLAIM_PUBLISH_AFTER_FIRST_SEEN
from rli.policy.splits import stable_company_split_of
from rli.probes.base import ProbeClaim
from rli.replay.build import build_dataset
from rli.replay.leakage import VIOLATION_KINDS, check_dataset
from rli.replay.run import run_replay
from rli.snapshots.daily import run_daily_snapshot
from rli.snapshots.run_windows import (
    RunWindow,
    guard_bound,
    import_log_windows,
    load_stamped_windows,
    parse_daily_log,
    shift_out_of_stamped_windows,
)

H = timedelta(hours=1)
D = timedelta(days=1)

LOG = """\
2026-09-22T10:27:08Z === daily job start
2026-09-22T10:27:09Z running rli snapshot
companies: ok=350 failed=1 skipped=1 | postings: new=315 absent=690 reappeared=14
2026-09-22T13:16:04Z skipping history rebuild: not Sunday UTC
2026-09-22T13:16:04Z daily job complete
2026-09-22T10:27:09Z running rli snapshot
companies: ok=350 failed=1 skipped=1 | postings: new=315 absent=690 reappeared=14
2026-09-22T13:16:04Z skipping history rebuild: not Sunday UTC
2026-10-07T00:00:00Z === daily job start
2026-10-07T00:00:01Z running rli snapshot
"""


def _window(conn: sqlite3.Connection, start: datetime, end: datetime, stamped: int = 1) -> None:
    conn.execute(
        "INSERT INTO snapshot_runs (started_at, finished_at, stamped_at_start, source) "
        "VALUES (?, ?, ?, 'log_import')",
        (to_utc_z(start), to_utc_z(end), stamped),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# run windows
# ---------------------------------------------------------------------------


def test_parse_daily_log_reads_each_run_once_and_skips_an_unfinished_one() -> None:
    windows = parse_daily_log(LOG)
    assert windows == [
        (datetime(2026, 9, 22, 10, 27, 9, tzinfo=UTC), datetime(2026, 9, 22, 13, 16, 4, tzinfo=UTC))
    ]


def test_import_log_windows_is_idempotent_and_flags_pre_fix_runs(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    log = tmp_path / "daily-20260922.log"
    log.write_text(
        LOG + "2026-10-08T01:00:00Z running rli snapshot\n2026-10-08T01:05:00Z done\n",
        encoding="utf-8",
    )
    first = import_log_windows(conn, [log])
    again = import_log_windows(conn, [log])
    assert (first.windows, first.inserted, first.stamped_at_start) == (2, 2, 1)
    assert again.inserted == 0
    rows = conn.execute(
        "SELECT started_at, finished_at, stamped_at_start, log_file FROM snapshot_runs "
        "ORDER BY started_at"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("2026-09-22T10:27:09.000000Z", "2026-09-22T13:16:04.000000Z", 1, log.name),
        ("2026-10-08T01:00:00.000000Z", "2026-10-08T01:05:00.000000Z", 0, log.name),
    ]
    assert load_stamped_windows(conn) == [
        RunWindow(
            datetime(2026, 9, 22, 10, 27, 9, tzinfo=UTC),
            datetime(2026, 9, 22, 13, 16, 4, tzinfo=UTC),
        )
    ]


def test_a_daily_snapshot_run_records_its_own_window(conn: sqlite3.Connection, cfg: Config) -> None:
    ticks = [NOW]

    def clock() -> datetime:
        value = ticks[0]
        ticks[0] = NOW + H  # every later read is the end of the run
        return value

    run_daily_snapshot(conn, cfg, [], NOW, sleep=lambda _s: None, clock=clock)
    rows = conn.execute(
        "SELECT started_at, finished_at, stamped_at_start, source FROM snapshot_runs"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(to_utc_z(NOW), to_utc_z(NOW + H), 0, "snapshot")]
    # A post-fix run never counts as stamped-at-start.
    assert load_stamped_windows(conn) == []


def test_shift_moves_grid_points_out_of_pre_fix_windows() -> None:
    start = datetime(2026, 10, 1, 13, tzinfo=UTC)
    window = RunWindow(start, start + 18 * H)
    times = [start - D, start, start + 2 * H, start + 18 * H, start + 2 * D]
    assert shift_out_of_stamped_windows(times, [window]) == (
        start - D,
        start + 18 * H,
        start + 2 * D,
    )


def test_guard_allows_a_first_publish_up_to_the_run_end() -> None:
    """M2: a first sighting stamped at a long run's START is too early."""
    start = datetime(2026, 10, 1, 13, 37, 21, tzinfo=UTC)
    first_observed = start + timedelta(seconds=1)  # the run-start capture stamp
    window = RunWindow(start, start + 18 * H)
    claim = ProbeClaim(
        claim_type=CLAIM_FIRST_PUBLISHED,
        value=to_utc_z(start + 5 * H),
        source_url="https://boards-api.greenhouse.io/x",
        source_quality="ats_native",
        source_event_at=start + 5 * H,
        available_at=start + 20 * H,
        fetched_at=start + 20 * H,
    )
    # Without the window the guard refuses the (valid) Greenhouse date ...
    assert guard_first_published([claim], first_observed)[0].claim_type == (
        CLAIM_PUBLISH_AFTER_FIRST_SEEN
    )
    # ... with it, the bound is the run's end and the date stands.
    bound = guard_bound(first_observed, [window])
    assert bound == start + 18 * H
    assert guard_first_published([claim], bound)[0].claim_type == CLAIM_FIRST_PUBLISHED
    # A date after the run's end is still refused.
    late = claim.model_copy(update={"source_event_at": start + 19 * H})
    assert guard_first_published([late], bound)[0].claim_type == CLAIM_PUBLISH_AFTER_FIRST_SEEN
    # Outside any window the bound is the sighting itself.
    assert guard_bound(start - D, [window]) == start - D


def _own_capture(conn: sqlite3.Connection, company: str, captured_at: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO companies (company_id, name, website_domain, created_at) "
        "VALUES (?, ?, ?, '2026-09-01T00:00:00Z')",
        (company, company, company),
    )
    conn.execute(
        "INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status) "
        "VALUES (?, ?, 'own', 'complete')",
        (company, captured_at),
    )


def test_fallback_windows_cover_pre_fix_batches_no_log_covers(
    conn: sqlite3.Connection,
) -> None:
    from rli.snapshots.run_windows import add_fallback_windows, uncovered_capture_batches

    batches = (
        "2026-09-07T17:43:51.000000Z",  # next batch 6.3 h later
        "2026-09-08T00:03:00.000000Z",  # next batch > 24 h later: capped
        "2026-09-13T01:41:00.000000Z",  # covered by a logged window below
        "2026-10-07T00:00:00.000000Z",  # after the fix: never a batch to bound
    )
    for stamp in batches:
        for company in ("a.com", "b.com"):
            _own_capture(conn, company, stamp)
    _window(conn, datetime(2026, 9, 13, 1, 40, tzinfo=UTC), datetime(2026, 9, 13, 3, tzinfo=UTC))
    conn.commit()

    assert [to_utc_z(stamp) for stamp, _ in uncovered_capture_batches(conn)] == list(batches[:2])
    assert len(uncovered_capture_batches(conn)) == 2

    summary = add_fallback_windows(conn)
    assert (summary.uncovered_before, summary.inserted, summary.capped) == (2, 2, 1)
    assert summary.uncovered_after == 0
    assert add_fallback_windows(conn).inserted == 0  # idempotent
    rows = conn.execute(
        "SELECT started_at, finished_at, stamped_at_start FROM snapshot_runs "
        "WHERE source = 'capture_fallback' ORDER BY started_at"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("2026-09-07T17:43:51.000000Z", "2026-09-08T00:03:00.000000Z", 1),
        ("2026-09-08T00:03:00.000000Z", "2026-09-09T00:03:00.000000Z", 1),
    ]
    # The fallback windows are read like any pre-fix window.
    assert RunWindow(
        datetime(2026, 9, 7, 17, 43, 51, tzinfo=UTC), datetime(2026, 9, 8, 0, 3, tzinfo=UTC)
    ) in load_stamped_windows(conn)


def test_the_leakage_report_counts_uncovered_capture_batches(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    from rli.snapshots.run_windows import add_fallback_windows

    seed_corpus(conn)  # own captures at NOW-60/-45/-30/-10 days: all pre-fix batches
    _build(conn, cfg, "ds-cov")
    assert check_dataset(conn, "ds-cov").uncovered_capture_batches == 4
    add_fallback_windows(conn)
    assert check_dataset(conn, "ds-cov").uncovered_capture_batches == 0


# ---------------------------------------------------------------------------
# replay build + leakage
# ---------------------------------------------------------------------------


def _build(conn: sqlite3.Connection, cfg: Config, dataset_id: str, **kwargs: object):
    options: dict[str, object] = {
        "dataset_id": dataset_id,
        "split": "dev",
        "split_kind": "temporal",
        "grid_step_days": 30,
        "now": NOW,
        "cutoff": dev_everything_cutoff(),
        "use_tool_cache": False,
        "collection_status_csv": "/nonexistent/collection_status.csv",
    }
    options.update(kwargs)
    with respx.mock:
        mock_ats()
        return build_dataset(conn, cfg, **options)  # type: ignore[arg-type]


def _times(conn: sqlite3.Connection, dataset_id: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT replay_at FROM replay_cases WHERE dataset_id = ?", (dataset_id,)
        )
    }


def test_build_shifts_grid_points_out_of_a_pre_fix_window_and_replay_is_clean(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)  # own captures at NOW-60/-45/-30/-10 days
    inside = NOW - 30 * D
    start, end = inside - H, inside + 5 * H
    _window(conn, start, end)

    _build(conn, cfg, "ds-win")
    times = _times(conn, "ds-win")
    assert to_utc_z(inside) not in times
    assert to_utc_z(end) in times

    summary = run_replay(conn, cfg, dataset_id="ds-win", system="A")
    assert summary.errors == 0
    report = check_dataset(conn, "ds-win")
    assert report.counts.get("capture_fetched_after_t", 0) == 0


def test_leakage_flags_a_case_inside_a_window_whose_capture_it_reads(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    assert "capture_fetched_after_t" in VIOLATION_KINDS
    seed_corpus(conn)
    _build(conn, cfg, "ds-old")  # built BEFORE the window was known
    run_replay(conn, cfg, dataset_id="ds-old", system="A")
    assert check_dataset(conn, "ds-old").counts.get("capture_fetched_after_t", 0) == 0

    inside = NOW - 30 * D  # a grid point AND both companies' capture stamp
    _window(conn, inside - H, inside + 5 * H)
    report = check_dataset(conn, "ds-old")
    cases_at_t = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE replay_at = ? AND config_hash LIKE '%|dataset:ds-old'",
        (to_utc_z(inside),),
    ).fetchone()[0]
    assert cases_at_t > 0
    assert report.counts["capture_fetched_after_t"] == cases_at_t
    assert not report.clean


# ---------------------------------------------------------------------------
# stable company holdout
# ---------------------------------------------------------------------------


@pytest.mark.company_holdout
def test_every_non_test_build_excludes_the_stable_company_holdout(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # The shared corpus: acme.com hashes to test, globex.com does not.
    assert stable_company_split_of(COMPANY) == "test"
    assert stable_company_split_of(OTHER_COMPANY) != "test"
    seed_corpus(conn)

    summary = _build(conn, cfg, "ds-temporal")  # a TEMPORAL build
    companies = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT company_id FROM replay_cases WHERE dataset_id = 'ds-temporal'"
        )
    }
    assert companies == {OTHER_COMPANY}
    assert summary.companies_excluded >= 1
    recorded = json.loads(
        conn.execute(
            "SELECT company_holdout FROM replay_datasets WHERE dataset_id = 'ds-temporal'"
        ).fetchone()[0]
    )
    assert recorded["method"] == "company-hash"
    assert recorded["test_companies_excluded"] >= 1
    # The temporal split itself is unchanged: cases still carry their temporal split.
    splits = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT split FROM replay_cases WHERE dataset_id = 'ds-temporal'"
        )
    }
    assert splits == {"dev"}


def _company(conn: sqlite3.Connection, company: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO companies (company_id, name, website_domain, created_at) "
        "VALUES (?, ?, ?, '2026-09-01T00:00:00Z')",
        (company, company, company),
    )


def _posting(conn: sqlite3.Connection, posting: str, company: str, created: str) -> None:
    _company(conn, company)
    conn.execute(
        "INSERT INTO postings (posting_id, company_id, ats, canonical_url, created_at, "
        "first_observed) VALUES (?, ?, 'greenhouse', ?, ?, ?)",
        (posting, company, f"https://x/{posting}", created, created),
    )


def _dataset(conn: sqlite3.Connection, dataset: str, kind: str, method: str | None) -> None:
    conn.execute(
        "INSERT INTO replay_datasets (dataset_id, created_at, split_kind, split_name, "
        "grid_step_days, postings, companies, cases, split_method, split_seed) "
        "VALUES (?, '2026-09-02T00:00:00.000000Z', ?, 'dev', 7, 1, 1, 1, ?, ?)",
        (dataset, kind, method, None if method is None else 20260607),
    )


def _case(conn: sqlite3.Connection, dataset: str, posting: str, company: str) -> None:
    conn.execute(
        "INSERT INTO replay_cases (dataset_id, posting_id, replay_at, company_id, "
        "canonical_url, built_at, split) VALUES (?, ?, 't', ?, 'u', 'b', 'dev')",
        (dataset, posting, company),
    )


def _seed_with(acme: str, globex: str) -> int:
    """A seed under which the stable hash puts acme.com / globex.com in these splits."""
    for seed in range(1, 2000):
        if (
            stable_company_split_of(COMPANY, seed=seed) == acme
            and stable_company_split_of(OTHER_COMPANY, seed=seed) == globex
        ):
            return seed
    raise AssertionError("no such seed")  # pragma: no cover


@pytest.mark.company_holdout
def test_a_company_split_build_excludes_the_holdout_and_its_check_is_clean(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """End to end, production mode: build with the exclusion, then the check is CLEAN."""
    seed = _seed_with("test", "dev")
    seed_corpus(conn)
    summary = _build(conn, cfg, "ds-company", split_kind="company", seed=seed)
    companies = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT company_id FROM replay_cases WHERE dataset_id = 'ds-company'"
        )
    }
    assert companies == {OTHER_COMPANY}
    assert summary.companies_excluded >= 1
    recorded = json.loads(
        conn.execute(
            "SELECT company_holdout FROM replay_datasets WHERE dataset_id = 'ds-company'"
        ).fetchone()[0]
    )
    assert recorded["seed"] == seed and recorded["test_companies_excluded"] >= 1

    check = company_holdout_check(conn, dataset_id="ds-company", split_kind="company")
    assert check.applicable and not check.reconstructed
    assert check.test_companies >= 1
    assert check.clean, check.describe()
    assert check.build_exclusion is not None


@pytest.mark.company_holdout
def test_exclude_companies_from_combines_with_the_holdout(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed = _seed_with("test", "dev")
    seed_corpus(conn)
    temporal = _build(conn, cfg, "ds-t", seed=seed)  # the holdout drops acme.com
    assert temporal.companies == 1
    summary = _build(
        conn,
        cfg,
        "ds-c",
        split_kind="company",
        seed=seed,
        exclude_companies_from=("ds-t",),
    )
    # globex.com by --exclude-companies-from, acme.com by the stable holdout.
    assert summary.companies_excluded == 2
    assert summary.cases == 0
    header = conn.execute(
        "SELECT exclude_companies_from, company_holdout FROM replay_datasets "
        "WHERE dataset_id = 'ds-c'"
    ).fetchone()
    assert header["exclude_companies_from"] == '["ds-t"]'
    assert json.loads(header["company_holdout"])["test_companies_excluded"] >= 1
    # Every non-test dataset in the DB stays clear of the holdout.
    assert company_holdout_check(conn, dataset_id="ds-c", split_kind="company").clean


def test_company_holdout_check_flags_test_companies_in_another_non_test_dataset(
    conn: sqlite3.Connection,
) -> None:
    # acme.com hashes to test, other.com to dev (the stable company split).
    assert stable_company_split_of("acme.com") == "test"
    assert stable_company_split_of("other.com") == "dev"
    _posting(conn, "pa", "acme.com", "2026-09-01T00:00:00Z")
    _posting(conn, "pb", "other.com", "2026-09-01T00:00:00Z")
    _dataset(conn, "company-ds", "company", "company-hash")
    _dataset(conn, "temporal-ds", "temporal", "temporal")
    _case(conn, "company-ds", "pb", "other.com")
    conn.commit()

    clean = company_holdout_check(conn, dataset_id="company-ds", split_kind="company")
    assert clean.applicable and clean.clean and clean.test_companies == 1
    assert not clean.reconstructed

    # A temporal dataset that used the test company for development.
    _case(conn, "temporal-ds", "pa", "acme.com")
    conn.commit()
    breached = company_holdout_check(conn, dataset_id="company-ds", split_kind="company")
    assert breached.breaches == {"temporal-ds": {"companies": 1, "cases": 1}}
    assert breached.self_overlap == {}
    assert "BREACHED" in breached.describe()
    assert not company_holdout_check(
        conn, dataset_id="temporal-ds", split_kind="temporal"
    ).applicable


def test_a_reconstructed_split_ignores_postings_first_seen_after_the_build(
    conn: sqlite3.Connection,
) -> None:
    """Today's greedy split moves o0.com to test because of NEW postings; the build's did not."""
    from rli.eval.baseline import load_split_map

    for index in range(4):
        _posting(conn, f"o{index}", f"o{index}.com", "2026-09-01T00:00:00Z")
    _posting(conn, "old-1", "shared.com", "2026-09-01T00:00:00Z")
    _dataset(conn, "greedy-ds", "company", None)  # built 2026-09-02, no frozen split
    _case(conn, "greedy-ds", "o0", "o0.com")
    # Two postings of a new company, first seen AFTER the build.
    for index in range(2):
        _posting(conn, f"late-{index}", "late.com", "2026-10-01T00:00:00Z")
    conn.commit()

    today = load_split_map(conn, cutoff=datetime(2026, 9, 2, tzinfo=UTC), split_kind="company")
    assert today["o0"] == "test"  # the overcount this check must not make

    check = company_holdout_check(conn, dataset_id="greedy-ds", split_kind="company")
    assert check.reconstructed
    assert "RECONSTRUCTED as of the dataset's build" in check.describe()
    assert check.test_companies == 1  # shared.com, as at the build
    assert check.self_overlap == {}
    assert check.clean


# ---------------------------------------------------------------------------
# migrations
# ---------------------------------------------------------------------------


def _squash(sql: str) -> str:
    lines = [line.split("--")[0] for line in sql.splitlines()]
    return "".join("".join(lines).split())


def test_snapshot_runs_ddl_is_identical_in_schema_and_migration() -> None:
    from rli.db import _schema_sql

    schema = _schema_sql()
    block = schema[schema.index("CREATE TABLE IF NOT EXISTS snapshot_runs") :]
    assert _squash(block) == _squash(SNAPSHOT_RUNS_DDL)


def test_version_5_upgrades_to_6_and_a_second_run_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "v5.sqlite3"
    init_db(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DROP TABLE snapshot_runs")
        conn.execute("ALTER TABLE replay_datasets DROP COLUMN company_holdout")
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    finally:
        conn.close()

    init_db(path)
    init_db(path)
    conn = connect(path)
    try:
        assert schema_version(conn) == SCHEMA_VERSION == 6
        assert conn.execute("SELECT COUNT(*) FROM snapshot_runs").fetchone()[0] == 0
        columns = {row[1] for row in conn.execute("PRAGMA table_info(replay_datasets)")}
        assert "company_holdout" in columns
    finally:
        conn.close()


def test_migration_4_to_5_ignores_an_unrelated_foreign_key_violation(tmp_path: Path) -> None:
    """L1: the rebuild of `runs` checks only `runs` and its children."""
    path = tmp_path / "v4.sqlite3"
    _build_v4_database(path)
    conn = sqlite3.connect(str(path))  # foreign keys OFF: the bad row goes in
    try:
        conn.execute(
            "INSERT INTO capture_attempts (company_id, target, attempted_at, source, ok) "
            "VALUES ('ghost.com', 'x', '2026-09-01T00:00:00Z', 'own', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    init_db(path)
    conn = connect(path)
    try:
        assert schema_version(conn) == SCHEMA_VERSION
        assert (
            "'R'" in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'runs'").fetchone()[0]
        )
    finally:
        conn.close()


def test_migration_4_to_5_still_refuses_a_violation_involving_runs(tmp_path: Path) -> None:
    path = tmp_path / "v4.sqlite3"
    _build_v4_database(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO run_steps (run_id, step_index, component, decision_type, created_at) "
            "VALUES ('no-such-run', 1, 'probe', 'probe_run', '2026-09-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="foreign-key"):
        init_db(path)
    conn = connect(path)
    try:
        assert schema_version(conn) == 4
    finally:
        conn.close()
