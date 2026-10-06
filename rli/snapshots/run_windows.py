"""Daily snapshot run windows (schema version 6, `snapshot_runs`).

Before the 2026-10-06 fix, `rli snapshot` stamped every company it captured
with the RUN's start time, although the run fetched companies one after the
other for up to 18 hours (2026-10-01). Those stamps cannot be repaired in
place — nothing records when each company was really fetched — but the run's
start and end CAN be recorded, and that bounds the truth: a capture stamped
inside a pre-fix run window was really fetched at some moment up to the
window's end.

Three consumers use that bound, all through this module:

* `rli.replay.build` moves a grid point `T` that falls inside a pre-fix
  window to the window's end (`shift_out_of_stamped_windows`), so no case is
  dated at a moment when a capture it reads might not have existed yet.
  Shift, not skip: the case still exists, at the first honest instant.
* `rli.replay.leakage` flags a replay case whose `T` falls inside a pre-fix
  window in which its company was captured (`capture_fetched_after_t`): it
  read a capture the run may not yet have made.
* `rli.eval.case.guard_first_published` compares a stated first-publication
  date with our own first sighting; a posting first observed inside a
  pre-fix window has a first sighting stamped too EARLY, so the guard allows
  dates up to the window's end (`guard_bound`).

Runs from now on are recorded live by `rli.snapshots.daily.run_daily_snapshot`
(`stamped_at_start = 0`: each company carries its real fetch time). Earlier
runs are imported once from `data/logs/daily-*.log` by
`import_log_windows` / `rli import-run-windows`, which also adds FALLBACK
windows (`add_fallback_windows`) for pre-fix capture batches that no log
covers (the first batches predate the logs).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from rli.models.time import parse_utc, to_utc_z

__all__ = [
    "FALLBACK_MAX_HOURS",
    "PRE_FIX_STAMPING_BEFORE",
    "add_fallback_windows",
    "uncovered_capture_batches",
    "FallbackSummary",
    "ImportSummary",
    "RunWindow",
    "finish_run",
    "guard_bound",
    "import_log_windows",
    "load_stamped_windows",
    "parse_daily_log",
    "shift_out_of_stamped_windows",
    "start_run",
    "window_containing",
]

#: The longest a fallback window may last: a pre-fix batch with no logged
#: window ends at the next own batch's stamp, but never more than this after
#: its own (the longest logged pre-fix run took 18.2 hours).
FALLBACK_MAX_HOURS = 24

#: When per-company capture stamps landed (commit 9960f76, 2026-10-06
#: 23:50:16 +02:00). A logged run that STARTED before this stamped every
#: capture with its start; the importer marks it `stamped_at_start = 1`.
PRE_FIX_STAMPING_BEFORE = "2026-10-06T21:50:16Z"


@dataclass(frozen=True, slots=True)
class RunWindow:
    started_at: datetime
    finished_at: datetime

    def contains(self, moment: datetime) -> bool:
        """`started_at <= moment < finished_at`."""
        return self.started_at <= moment < self.finished_at


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def load_stamped_windows(conn: sqlite3.Connection) -> list[RunWindow]:
    """Every finished run whose captures were stamped with its start.

    Empty for a database without the table (pre-version 6) — the consumers
    then behave exactly as before.
    """
    try:
        rows = conn.execute(
            "SELECT started_at, finished_at FROM snapshot_runs "
            "WHERE stamped_at_start = 1 AND finished_at IS NOT NULL "
            "ORDER BY started_at"
        ).fetchall()
    except sqlite3.Error:
        return []
    windows: list[RunWindow] = []
    for row in rows:
        try:
            start, end = parse_utc(str(row[0])), parse_utc(str(row[1]))
        except ValueError:
            continue
        if end > start:
            windows.append(RunWindow(start, end))
    return windows


def window_containing(windows: Sequence[RunWindow], moment: datetime) -> RunWindow | None:
    for window in windows:
        if window.contains(moment):
            return window
    return None


def shift_out_of_stamped_windows(
    times: Iterable[datetime], windows: Sequence[RunWindow]
) -> tuple[datetime, ...]:
    """Move every time inside a pre-fix window to the window's end; dedupe, keep order."""
    shifted: list[datetime] = []
    for moment in times:
        window = window_containing(windows, moment)
        value = window.finished_at if window is not None else moment
        if value not in shifted:
            shifted.append(value)
    return tuple(sorted(shifted))


def guard_bound(first_observed: datetime | None, windows: Sequence[RunWindow]) -> datetime | None:
    """The first-sighting bound the first-published guard should use.

    A posting first observed inside a pre-fix window carries the run's START
    as its first sighting, although it may have been fetched up to the run's
    end; a stated first-publication date up to that end is therefore still
    consistent with our sighting.
    """
    if first_observed is None:
        return None
    window = window_containing(windows, first_observed)
    return window.finished_at if window is not None else first_observed


# ---------------------------------------------------------------------------
# Writing (live)
# ---------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, started_at: datetime) -> int | None:
    """Record a live snapshot run's start; `None` when the table is missing."""
    try:
        cursor = conn.execute(
            "INSERT INTO snapshot_runs (started_at, stamped_at_start, source) "
            "VALUES (?, 0, 'snapshot')",
            (to_utc_z(started_at),),
        )
        conn.commit()
    except sqlite3.Error:
        return None
    return int(cursor.lastrowid) if cursor.lastrowid is not None else None


def finish_run(conn: sqlite3.Connection, run_id: int | None, finished_at: datetime) -> None:
    if run_id is None:
        return
    try:
        conn.execute(
            "UPDATE snapshot_runs SET finished_at = ? WHERE id = ?",
            (to_utc_z(finished_at), run_id),
        )
        conn.commit()
    except sqlite3.Error:
        return


# ---------------------------------------------------------------------------
# Importing past runs from the daily job's logs
# ---------------------------------------------------------------------------

_STAMPED_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
_SNAPSHOT_START = "running rli snapshot"
_JOB_START = "=== daily job start"


def parse_daily_log(text: str) -> list[tuple[datetime, datetime]]:
    """`(start, end)` of every snapshot run in one `daily-*.log`.

    The daily job logs `<ts> running rli snapshot`, then the snapshot's
    summary line (no timestamp), then its next step with a timestamp. The
    run's window is from that start line to the NEXT timestamped line. A
    start followed by no timestamped line before the next run begins (the
    job died) yields no window — never the next run's start as its end.
    Duplicated log text (the logs repeat a run's block) is deduplicated.
    """
    stamped: list[tuple[datetime, str]] = []
    for line in text.splitlines():
        match = _STAMPED_LINE.match(line.strip())
        if match is None:
            continue
        try:
            stamped.append((parse_utc(match.group(1)), match.group(2)))
        except ValueError:
            continue
    windows: list[tuple[datetime, datetime]] = []
    for index, (moment, message) in enumerate(stamped):
        if not message.startswith(_SNAPSHOT_START):
            continue
        end = None
        for later, later_message in stamped[index + 1 :]:
            if later_message.startswith((_SNAPSHOT_START, _JOB_START)):
                break  # the next run began: this one never logged an end
            if later >= moment:
                end = later
                break
        if end is None:
            continue
        window = (moment, end)
        if window not in windows:
            windows.append(window)
    return windows


@dataclass(frozen=True, slots=True)
class ImportSummary:
    files: int
    windows: int
    inserted: int
    stamped_at_start: int

    def describe(self) -> str:
        return (
            f"run windows: files={self.files} windows={self.windows} "
            f"inserted={self.inserted} (already present: {self.windows - self.inserted}) "
            f"stamped_at_start={self.stamped_at_start}"
        )


def import_log_windows(
    conn: sqlite3.Connection,
    paths: Iterable[str | Path],
    *,
    stamped_at_start_before: str = PRE_FIX_STAMPING_BEFORE,
) -> ImportSummary:
    """Insert every logged run window (idempotent: `UNIQUE (started_at, source)`).

    A run that started before `stamped_at_start_before` is marked
    `stamped_at_start = 1`. Commits once at the end.
    """
    cutoff = parse_utc(stamped_at_start_before)
    files = windows = inserted = stamped = 0
    for path in sorted(Path(p) for p in paths):
        files += 1
        for start, end in parse_daily_log(path.read_text(encoding="utf-8", errors="replace")):
            windows += 1
            flag = 1 if start < cutoff else 0
            stamped += flag
            cursor = conn.execute(
                "INSERT OR IGNORE INTO snapshot_runs "
                "(started_at, finished_at, stamped_at_start, source, log_file) "
                "VALUES (?, ?, ?, 'log_import', ?)",
                (to_utc_z(start), to_utc_z(end), flag, path.name),
            )
            inserted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    conn.commit()
    return ImportSummary(files=files, windows=windows, inserted=inserted, stamped_at_start=stamped)


# ---------------------------------------------------------------------------
# Fallback windows for pre-fix capture batches no log covers
# ---------------------------------------------------------------------------


def _own_batches(conn: sqlite3.Connection) -> list[tuple[datetime, int]]:
    """Every distinct own `captured_at` (a batch) with its company count, oldest first."""
    batches: list[tuple[datetime, int]] = []
    for row in conn.execute(
        "SELECT captured_at, COUNT(*) FROM board_snapshots WHERE source = 'own' "
        "GROUP BY captured_at ORDER BY captured_at"
    ):
        try:
            batches.append((parse_utc(str(row[0])), int(row[1])))
        except ValueError:
            continue
    return batches


def _recorded_windows(conn: sqlite3.Connection) -> list[tuple[datetime, datetime]]:
    try:
        rows = conn.execute(
            "SELECT started_at, finished_at FROM snapshot_runs WHERE finished_at IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return []
    windows = []
    for row in rows:
        try:
            windows.append((parse_utc(str(row[0])), parse_utc(str(row[1]))))
        except ValueError:
            continue
    return windows


def _covered(stamp: datetime, windows: Sequence[tuple[datetime, datetime]]) -> bool:
    return any(start <= stamp <= end for start, end in windows)


def uncovered_capture_batches(
    conn: sqlite3.Connection, *, stamped_at_start_before: str = PRE_FIX_STAMPING_BEFORE
) -> list[tuple[datetime, int]]:
    """Pre-fix own capture batches that NO recorded window covers: `(stamp, companies)`.

    Such a batch's captures were stamped with its run's start, but nothing
    says when the run ended, so neither the replay builder nor the leakage
    checker can bound them.
    """
    cutoff = parse_utc(stamped_at_start_before)
    windows = _recorded_windows(conn)
    return [
        (stamp, count)
        for stamp, count in _own_batches(conn)
        if stamp < cutoff and not _covered(stamp, windows)
    ]


@dataclass(frozen=True, slots=True)
class FallbackSummary:
    uncovered_before: int
    inserted: int
    uncovered_after: int
    capped: int

    def describe(self) -> str:
        return (
            f"fallback windows: pre-fix capture batches without a logged window="
            f"{self.uncovered_before}, fallback windows inserted={self.inserted} "
            f"(capped at {FALLBACK_MAX_HOURS} h: {self.capped}), still uncovered="
            f"{self.uncovered_after}"
        )


def add_fallback_windows(
    conn: sqlite3.Connection,
    *,
    stamped_at_start_before: str = PRE_FIX_STAMPING_BEFORE,
    max_hours: int = FALLBACK_MAX_HOURS,
) -> FallbackSummary:
    """Give every uncovered pre-fix capture batch a conservative window.

    The window runs from the batch's stamp to the NEXT own batch's stamp (the
    run cannot have still been capturing once the next one started), capped
    at `max_hours`. There is no real per-company fetch time to do better
    with: the daily snapshot never wrote `tool_cache`, and its
    `capture_attempts` rows carry the same run-start stamp. Idempotent
    (`UNIQUE (started_at, source)`); recorded `stamped_at_start = 1`,
    `source = 'capture_fallback'`. Commits once.
    """
    uncovered = uncovered_capture_batches(conn, stamped_at_start_before=stamped_at_start_before)
    stamps = [stamp for stamp, _ in _own_batches(conn)]
    cap = timedelta(hours=max_hours)
    inserted = capped = 0
    for stamp, _count in uncovered:
        later = [other for other in stamps if other > stamp]
        end = min(later[0], stamp + cap) if later else stamp + cap
        if not later or later[0] > stamp + cap:
            capped += 1
        cursor = conn.execute(
            "INSERT OR IGNORE INTO snapshot_runs "
            "(started_at, finished_at, stamped_at_start, source) "
            "VALUES (?, ?, 1, 'capture_fallback')",
            (to_utc_z(stamp), to_utc_z(end)),
        )
        inserted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    conn.commit()
    after = uncovered_capture_batches(conn, stamped_at_start_before=stamped_at_start_before)
    return FallbackSummary(
        uncovered_before=len(uncovered),
        inserted=inserted,
        uncovered_after=len(after),
        capped=capped,
    )
