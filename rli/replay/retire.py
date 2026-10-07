"""Reversible retirement of replay datasets (schema version 7).

A dataset built before a data or evaluation fix stays in the database as the
record of what was measured then, but it should no longer count as a live
dataset. Retiring it (`rli replay retire --dataset ID --reason TEXT`) stamps
`replay_datasets.retired_at` / `retired_reason` and nothing else: no case,
probe record or run is deleted, and `rli replay unretire --dataset ID`
clears the stamp again.

What retirement changes:

* `rli.eval.diagnostics.company_holdout_check` ignores a retired dataset when
  it looks for holdout test companies in other non-test datasets, and lists
  it as "retired, ignored" instead (a retired dataset trains and tunes
  nothing any more, so its overlap with the holdout no longer contaminates
  it);
* `rli.replay.run.run_replay` and `rli.eval.evaluate.evaluate` refuse a
  retired dataset (`RetiredDatasetError`) unless called with
  `allow_retired=True` (`--allow-retired` on the CLI).

Every reader tolerates a pre-version-7 schema (no columns): nothing is
retired there.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from rli.models.time import now_utc, to_utc_z

__all__ = [
    "Retirement",
    "RetiredDatasetError",
    "dataset_rows",
    "ensure_not_retired",
    "retire_dataset",
    "retired_datasets",
    "retirement_of",
    "unretire_dataset",
]


class Retirement(BaseModel):
    """When and why a dataset was retired."""

    model_config = ConfigDict(frozen=True)

    dataset_id: str
    retired_at: str
    reason: str | None = None

    def describe(self) -> str:
        reason = f": {self.reason}" if self.reason else ""
        return f"retired {self.retired_at}{reason}"


class RetiredDatasetError(RuntimeError):
    """A retired dataset was used without `allow_retired=True`."""

    def __init__(self, retirement: Retirement) -> None:
        self.retirement = retirement
        super().__init__(
            f"replay dataset {retirement.dataset_id!r} is {retirement.describe()}. "
            "Pass --allow-retired (allow_retired=True) to use it anyway, or "
            f"`rli replay unretire --dataset {retirement.dataset_id}`."
        )


def _has_retired_columns(conn: sqlite3.Connection) -> bool:
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(replay_datasets)")}
    except sqlite3.Error:  # pragma: no cover - defensive
        return False
    return "retired_at" in columns


def retired_datasets(conn: sqlite3.Connection) -> dict[str, Retirement]:
    """Every retired dataset, by id (empty on a pre-version-7 schema)."""
    if not _has_retired_columns(conn):
        return {}
    rows = conn.execute(
        "SELECT dataset_id, retired_at, retired_reason FROM replay_datasets "
        "WHERE retired_at IS NOT NULL ORDER BY dataset_id"
    ).fetchall()
    return {
        str(row[0]): Retirement(dataset_id=str(row[0]), retired_at=str(row[1]), reason=row[2])
        for row in rows
    }


def retirement_of(conn: sqlite3.Connection, dataset_id: str) -> Retirement | None:
    """`dataset_id`'s retirement, or None when it is not retired (or unknown)."""
    if not _has_retired_columns(conn):
        return None
    row = conn.execute(
        "SELECT retired_at, retired_reason FROM replay_datasets "
        "WHERE dataset_id = ? AND retired_at IS NOT NULL",
        (dataset_id,),
    ).fetchone()
    if row is None:
        return None
    return Retirement(dataset_id=dataset_id, retired_at=str(row[0]), reason=row[1])


def ensure_not_retired(
    conn: sqlite3.Connection, dataset_id: str, *, allow_retired: bool = False
) -> Retirement | None:
    """Raise `RetiredDatasetError` for a retired dataset unless `allow_retired`.

    Returns the retirement (None when not retired), so an allowed caller can
    still say it is using a retired dataset.
    """
    retirement = retirement_of(conn, dataset_id)
    if retirement is not None and not allow_retired:
        raise RetiredDatasetError(retirement)
    return retirement


def _require_columns(conn: sqlite3.Connection) -> None:
    if not _has_retired_columns(conn):
        raise RuntimeError(
            "this database predates schema version 7 (no replay_datasets.retired_at); "
            "run `rli init-db` first"
        )


def _require_dataset(conn: sqlite3.Connection, dataset_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM replay_datasets WHERE dataset_id = ?", (dataset_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no replay dataset {dataset_id!r}")


def retire_dataset(
    conn: sqlite3.Connection,
    dataset_id: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> Retirement:
    """Mark `dataset_id` retired. Deletes nothing.

    Re-retiring an already retired dataset keeps its original `retired_at`
    and replaces the reason. Raises `LookupError` for an unknown dataset and
    `ValueError` for an empty reason.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a retirement needs a reason")
    _require_columns(conn)
    _require_dataset(conn, dataset_id)
    stamp = to_utc_z(now if now is not None else now_utc())
    conn.execute(
        "UPDATE replay_datasets SET retired_at = COALESCE(retired_at, ?), retired_reason = ? "
        "WHERE dataset_id = ?",
        (stamp, reason, dataset_id),
    )
    conn.commit()
    retirement = retirement_of(conn, dataset_id)
    assert retirement is not None
    return retirement


def unretire_dataset(conn: sqlite3.Connection, dataset_id: str) -> Retirement | None:
    """Clear `dataset_id`'s retirement; returns the one cleared (None if it was not retired).

    Raises `LookupError` for an unknown dataset.
    """
    _require_columns(conn)
    _require_dataset(conn, dataset_id)
    previous = retirement_of(conn, dataset_id)
    conn.execute(
        "UPDATE replay_datasets SET retired_at = NULL, retired_reason = NULL WHERE dataset_id = ?",
        (dataset_id,),
    )
    conn.commit()
    return previous


def dataset_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Every dataset header, oldest first, with its retired state (`rli replay list`)."""
    has_retired = _has_retired_columns(conn)
    retired_cols = "retired_at, retired_reason" if has_retired else "NULL, NULL"
    rows = conn.execute(
        f"""
        SELECT dataset_id, created_at, split_kind, split_name, postings, companies, cases,
               {retired_cols}
        FROM replay_datasets
        ORDER BY created_at, dataset_id
        """
    ).fetchall()
    return [
        {
            "dataset_id": row[0],
            "created_at": row[1],
            "split_kind": row[2],
            "split_name": row[3],
            "postings": row[4],
            "companies": row[5],
            "cases": row[6],
            "retired_at": row[7],
            "retired_reason": row[8],
        }
        for row in rows
    ]
