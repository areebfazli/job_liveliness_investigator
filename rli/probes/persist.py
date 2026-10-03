"""Persistence helpers for probe results (spec.md §7; PLAN.md M1).

Probes themselves never write to the database (`rli.probes.base`: "Do not
write to DB inside probes; return data, let the caller persist ... so
replay can cache them"). These functions are that caller-side persistence
step, kept in one place so every write goes through the same shape.

Every timestamp column is written with `to_utc_z` so the schema's TEXT
timestamp columns keep the lexical-order-is-chronological-order invariant
(`rli.models.time.to_utc_z`; spec.md §3/§6).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from rli.models.evidence import EvidenceItem
from rli.models.time import to_utc_z
from rli.probes.base import ProbeClaim
from rli.probes.board_snapshot import BoardJob
from rli.resolvers.common import parse_flexible_datetime

__all__ = ["record_capture_attempt", "save_board_snapshot", "save_evidence"]


def _normalized_stamp(raw: str | None) -> str | None:
    """An ATS-stated date as a `to_utc_z` string, or None if absent/unparseable.

    Normalized (rather than stored raw) so the column keeps the schema's
    lexical-order-is-chronological-order invariant: Greenhouse states local
    offsets (`...-04:00`), Ashby UTC. Garbage is dropped, never guessed at.
    """
    parsed = parse_flexible_datetime(raw)
    return to_utc_z(parsed) if parsed is not None else None


def record_capture_attempt(
    conn: sqlite3.Connection,
    *,
    company_id: str,
    target: str,
    attempted_at: datetime,
    source: str = "own",
    ok: bool,
    error: str | None = None,
    retryable: bool | None = None,
) -> None:
    """Record one capture attempt (spec.md §4: coverage gaps, not absences).

    `source` is `"own"` for a live snapshot/resolver fetch or `"archive"`
    for a Wayback-derived attempt (spec.md §7 `capture_attempts.source`).
    """
    conn.execute(
        """
        INSERT INTO capture_attempts
            (company_id, target, attempted_at, source, ok, error, retryable)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            target,
            to_utc_z(attempted_at),
            source,
            1 if ok else 0,
            error,
            None if retryable is None else (1 if retryable else 0),
        ),
    )
    conn.commit()


def save_board_snapshot(
    conn: sqlite3.Connection,
    *,
    company_id: str,
    captured_at: datetime,
    coverage_status: str,
    jobs: list[BoardJob],
    source: str = "own",
) -> int:
    """Persist one board capture: a `board_snapshots` header + child job rows.

    Returns the new `board_snapshots.id`. `coverage_status` is one of
    `"complete" | "partial" | "gap"` (spec.md §7); callers pass `"gap"`
    (never a fabricated empty `"complete"` capture) when the fetch failed —
    though a failed `board_snapshot` probe more commonly produces a
    `capture_attempts` row instead of a snapshot at all (spec.md §4).
    """
    cursor = conn.execute(
        """
        INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status)
        VALUES (?, ?, ?, ?)
        """,
        (company_id, to_utc_z(captured_at), source, coverage_status),
    )
    board_snapshot_id = cursor.lastrowid
    assert board_snapshot_id is not None

    conn.executemany(
        """
        INSERT INTO board_snapshot_jobs
            (board_snapshot_id, job_id, title, team, location, description_hash, url,
             first_published, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                board_snapshot_id,
                job.job_id,
                job.title,
                job.team,
                job.location,
                job.description_hash,
                job.url,
                _normalized_stamp(job.first_published),
                _normalized_stamp(job.updated_at),
            )
            for job in jobs
        ],
    )
    conn.commit()
    return board_snapshot_id


def save_evidence(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    probe: str,
    claims: list[ProbeClaim],
    posting_id: str | None = None,
    start_index: int = 1,
) -> list[EvidenceItem]:
    """Assign run-local ids (`e{start_index}..`) to `claims` and persist them.

    This is where a probe's caller-agnostic `ProbeClaim`s become full,
    stored `EvidenceItem`s (spec.md §3): the run assigns `id`/`run_id`, and
    already knows which probe produced them. `start_index` lets a run number
    evidence continuously across multiple probe calls in the same run
    (e.g. resolve_posting's e1..e3, then board_snapshot's e4..).

    Returns the persisted `EvidenceItem`s in the same order as `claims`.
    """
    items = [
        EvidenceItem(
            id=f"e{start_index + offset}",
            run_id=run_id,
            probe=probe,
            **claim.model_dump(),
        )
        for offset, claim in enumerate(claims)
    ]

    conn.executemany(
        """
        INSERT INTO evidence
            (id, run_id, posting_id, probe, claim_type, value, source_url,
             raw_excerpt, source_quality, source_event_at, available_at, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item.id,
                item.run_id,
                posting_id,
                item.probe,
                item.claim_type,
                item.value,
                item.source_url,
                item.raw_excerpt,
                item.source_quality,
                to_utc_z(item.source_event_at) if item.source_event_at else None,
                to_utc_z(item.available_at),
                to_utc_z(item.fetched_at),
            )
            for item in items
        ],
    )
    conn.commit()
    return items
