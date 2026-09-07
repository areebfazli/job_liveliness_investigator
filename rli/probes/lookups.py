"""Read-only lookups shared by the history-gated probes (PLAN.md M3).

Two things are needed identically by `rli.probes.repost_history`,
`rli.probes.requirements_drift` and `rli.probes.registry`: the `postings`
row behind a `posting_id`, and the spec.md §4 usable-history test. They live
here rather than being duplicated (or imported across probe modules as
private names) so the SELECT's column list and the history rule each have
exactly one definition.

Everything in this module is a pure read. Probes never write (see
`rli.probes.base`).
"""

from __future__ import annotations

import sqlite3

from rli.config import Config
from rli.history.features import coverage_window

__all__ = ["has_usable_history", "posting_row"]


def posting_row(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    """The `postings` row for `posting_id`, or None if there is no such posting."""
    return conn.execute(
        """
        SELECT posting_id, company_id, ats, ats_tenant_id, ats_job_id, canonical_url,
               title, team, location
        FROM postings
        WHERE posting_id = ?
        """,
        (posting_id,),
    ).fetchone()


def has_usable_history(conn: sqlite3.Connection, config: Config, company_id: str) -> bool:
    """Whether `company_id`'s board history is deep enough to reason from.

    spec.md §4: "history probes are ineligible without usable history" and
    "missing history never means flat hiring". The bar is
    `thresholds.min_history_days`, the same one
    `rli.history.features._classify_repost_pattern` uses, so the probe layer
    and the feature layer cannot disagree about what "usable" means.
    """
    return coverage_window(conn, company_id).history_days >= config.thresholds.min_history_days
