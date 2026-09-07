"""Verified ATS target list loading + company upsert (spec.md §4; PLAN.md M1).

`scripts/targets.csv` holds the ~78 hand-audited companies whose boards the
daily snapshot cron (`rli.snapshots.daily`) captures. This module is the
single place that parses that CSV into typed `Target` rows and upserts the
corresponding `companies` rows — kept separate from `rli.snapshots.daily` so
`rli cli load-targets` can run it independently of a capture run.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from rli.config import REPO_ROOT
from rli.models.time import to_utc_z
from rli.resolvers.common import normalize_domain

__all__ = ["DEFAULT_TARGETS_PATH", "Target", "load_targets", "upsert_companies"]

AtsName = Literal["greenhouse", "ashby", "lever"]

# Module-relative (not CWD-relative), same reasoning as rli.config.REPO_ROOT:
# a cron job invoked from any working directory must find the same file.
DEFAULT_TARGETS_PATH = REPO_ROOT / "scripts" / "targets.csv"

_VALID_ATS = ("greenhouse", "ashby", "lever")


class Target(BaseModel):
    """One verified ATS target company (one row of `scripts/targets.csv`)."""

    company_name: str
    website_domain: str
    ats: AtsName
    tenant: str
    # Computed from website_domain at construction time (spec.md §3
    # "Identity"): the normalized company website domain. Falls back to the
    # raw lowercased website_domain if normalize_domain returns None (it
    # shouldn't for a well-formed domain, but this fails closed rather than
    # raising on a merely-odd CSV value).
    company_id: str = ""

    @model_validator(mode="after")
    def _compute_company_id(self) -> Target:
        if not self.company_id:
            normalized = normalize_domain(self.website_domain)
            self.company_id = normalized if normalized is not None else self.website_domain.lower()
        return self


def load_targets(path: str | Path | None = None) -> list[Target]:
    """Parse `scripts/targets.csv` (or `path`) into a list of `Target`.

    Only the first four columns (company_name, website_domain, ats, tenant)
    are used; `open_job_count`/`checked_at` (audit-time metadata) are
    ignored. Raises `FileNotFoundError` if the CSV does not exist (mirrors
    `rli.config.load_config`'s style), and `ValueError` naming the offending
    row/company if `ats` is not one of greenhouse/ashby/lever (fail loud —
    see rli/config.py's docstring philosophy).
    """
    resolved = Path(path) if path is not None else DEFAULT_TARGETS_PATH
    if not resolved.is_file():
        raise FileNotFoundError(f"targets CSV not found at {str(resolved)!r}")

    targets: list[Target] = []
    with resolved.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_index, row in enumerate(reader, start=2):  # header is row 1
            ats = (row.get("ats") or "").strip()
            company_name = (row.get("company_name") or "").strip()
            if ats not in _VALID_ATS:
                raise ValueError(
                    f"targets CSV row {row_index} ({company_name!r}): invalid ats "
                    f"{ats!r}; expected one of {_VALID_ATS}"
                )
            targets.append(
                Target(
                    company_name=company_name,
                    website_domain=(row.get("website_domain") or "").strip(),
                    ats=ats,  # type: ignore[arg-type]
                    tenant=(row.get("tenant") or "").strip(),
                )
            )
    return targets


def upsert_companies(conn: sqlite3.Connection, targets: list[Target], *, now: datetime) -> int:
    """Insert/update a `companies` row for each target.

    On conflict, updates `name`/`website_domain` but never `created_at`
    (first-seen semantics). If two targets share the same `company_id`
    (shouldn't happen for the real CSV, but defensive), the later one in
    iteration order wins. Returns the number of target rows processed
    (`len(targets)`, not the number of new inserts).
    """
    now_str = to_utc_z(now)
    for target in targets:
        conn.execute(
            """
            INSERT INTO companies (company_id, name, website_domain, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(company_id) DO UPDATE SET
                name = excluded.name,
                website_domain = excluded.website_domain
            """,
            (target.company_id, target.company_name, target.website_domain, now_str),
        )
    conn.commit()
    return len(targets)
