"""`company_events` store — spec.md §4 `company_events` probe / PLAN.md M2.

`company_events` (rli/db/schema.sql) has exactly six real columns:
`id, company_id, event_type, event_at, available_at, source_url,
description`. `CompanyEvent` below carries more fields than that (headline,
raw_excerpt, source_quality, materiality, collected_at); everything not
covered by a real column is packed into `description` as JSON, matching the
schema.sql-documented convention that "JSON-shaped columns ... are stored as
TEXT (JSON-encoded)".

`available_at` vs. `event_date`
--------------------------------
spec.md §3 says: "Do not backdate current discoveries merely because the
underlying event happened earlier." That rule is about a *live* discovery —
if we find out about an old layoff today via a live web search, `available_at`
must be *today* (the moment the fact became verifiable to us), not the date
the layoff itself happened.

Pre-collected historical news articles are a different situation. The
article's publish date/time is not something we are inventing after the
fact — it is an externally verifiable timestamp printed on the source
itself. Setting `available_at` to that printed publish date/time (rather
than to whenever the collection script happened to run) is therefore not a
backdating violation; it is *required* for correct point-in-time replay
(spec.md §4/§6): a company_events row must become visible to a replay at
time T exactly when a real historical searcher could plausibly have found
that article, which is its publish time, not our collection time.

`collected_at` is tracked separately and records when the collection
script/agent actually wrote the row — useful for auditing the collection
process, but never used as a replay-visibility bound.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from rli.models.time import ensure_aware, parse_utc, to_utc_z

EventType = Literal[
    "layoff",
    "hiring_freeze",
    "hiring_pause",
    "funding",
    "expansion",
    "acquisition",
    "shutdown",
    "other",
]

Materiality = Literal["material", "minor"]

_NEGATIVE_TYPES = frozenset({"hiring_freeze", "hiring_pause", "shutdown"})

_EVENTS_CSV_HEADER = [
    "company_id",
    "event_type",
    "event_date",
    "available_at",
    "source_url",
    "headline",
    "raw_excerpt",
    "collected_at",
]

_STATUS_CSV_HEADER = ["company_id", "searched_at", "queries_run", "events_found"]


class CompanyEvent(BaseModel):
    """A single dated company event (spec.md §4 `company_events` probe).

    `event_date` is the calendar date the underlying event happened (a
    layoff, freeze, funding round, ...). `available_at` is when that fact
    became verifiably available to this system — see the module docstring
    for why, for a pre-collected news article, that is the article's
    publish date/time rather than collection time.
    """

    model_config = ConfigDict(frozen=False)

    company_id: str
    event_type: EventType
    event_date: date
    available_at: datetime
    source_url: str
    headline: str
    raw_excerpt: str | None = None
    source_quality: Literal["news"] = "news"
    materiality: Materiality
    collected_at: datetime

    @field_validator("available_at", "collected_at")
    @classmethod
    def _tz_aware_utc(cls, value: datetime, info: ValidationInfo) -> datetime:
        return ensure_aware(value, info.field_name)


class CollectionStatus(BaseModel):
    """Per-company record of when/how thoroughly `company_events` collection ran.

    Stored as a CSV (not a DB table — the task requires a file, not a schema
    change) so that `derive_policy_signals` can distinguish "not yet
    searched" (company absent entirely) from "searched, found nothing"
    (present with `events_found == 0`).
    """

    model_config = ConfigDict(frozen=False)

    company_id: str
    searched_at: datetime
    queries_run: int
    events_found: int

    @field_validator("searched_at")
    @classmethod
    def _tz_aware_utc(cls, value: datetime, info: ValidationInfo) -> datetime:
        return ensure_aware(value, info.field_name)


_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_HEADCOUNT_RE = re.compile(
    r"(\d[\d,]*)\s*(?:employees|jobs|positions|staff|workers|roles)", re.IGNORECASE
)


def classify_materiality(
    event_type: EventType, headline: str, raw_excerpt: str | None
) -> Materiality:
    """Classify a company event as `"material"` or `"minor"` (spec.md §5).

    Rule:

    * `hiring_freeze`, `hiring_pause`, `shutdown` -> always `"material"`
      (an explicit freeze/pause, or a shutdown, is unambiguously material —
      there is no magnitude to check).
    * `layoff` -> parse `headline + " " + (raw_excerpt or "")` for:
        (a) a percentage (e.g. "10% of staff") -> material if any found
            percentage is >= 10;
        (b) a headcount followed by "employees|jobs|positions|staff|
            workers|roles" (e.g. "150 employees") -> material if any found
            count is >= 100.
      If neither pattern is found, default to `"material"`. This is a
      DELIBERATE CONSERVATIVE JUDGMENT CALL, not a confident classification:
      a layoff report with no extractable magnitude is treated as material
      rather than silently guessed as minor, because understating a layoff's
      materiality is the worse failure mode for the `apply_now` /
      `material_negative_event` policy signal. Revisit if this proves too
      aggressive once real collected data is reviewed.
    * `funding`, `expansion`, `acquisition`, `other` -> always `"minor"`
      (these are not negative events at all; they simply are not the signal
      this function reports on — the negative-event *policy* signal itself
      is computed in `policy_signals.py` from `event_type`, not from this
      `materiality` field, for those types).
    """
    if event_type in _NEGATIVE_TYPES:
        return "material"

    if event_type == "layoff":
        text = f"{headline} {raw_excerpt or ''}"

        percentages = [float(m.group(1)) for m in _PERCENT_RE.finditer(text)]
        if any(p >= 10 for p in percentages):
            return "material"

        counts = [int(m.group(1).replace(",", "")) for m in _HEADCOUNT_RE.finditer(text)]
        if any(c >= 100 for c in counts):
            return "material"

        if percentages or counts:
            # A magnitude was found but fell below both thresholds.
            return "minor"

        # No extractable magnitude at all: conservative default (see docstring).
        return "material"

    # funding / expansion / acquisition / other: not negative events.
    return "minor"


def _pack_description(event: CompanyEvent) -> str:
    return json.dumps(
        {
            "headline": event.headline,
            "raw_excerpt": event.raw_excerpt,
            "source_quality": event.source_quality,
            "materiality": event.materiality,
            "collected_at": to_utc_z(event.collected_at),
        },
        sort_keys=True,
    )


def _row_to_event(row: sqlite3.Row) -> CompanyEvent:
    """Inverse of the DB mapping in `upsert_event`."""
    packed = json.loads(row["description"])
    event_at = parse_utc(row["event_at"])
    return CompanyEvent(
        company_id=row["company_id"],
        event_type=row["event_type"],
        event_date=event_at.date(),
        available_at=parse_utc(row["available_at"]),
        source_url=row["source_url"],
        headline=packed["headline"],
        raw_excerpt=packed["raw_excerpt"],
        source_quality=packed["source_quality"],
        materiality=packed["materiality"],
        collected_at=parse_utc(packed["collected_at"]),
    )


def upsert_event(conn: sqlite3.Connection, event: CompanyEvent) -> int:
    """Insert or update a `company_events` row, returning its `id`.

    Natural dedupe key: `(company_id, event_type, event_date, source_url)`.
    A row matching all four is UPDATEd in place; otherwise a new row is
    INSERTed. `event_at` is stored as midnight UTC on `event_date`
    (`datetime.combine(event_date, time.min, tzinfo=UTC)`) since the table
    only has room for a single TEXT timestamp per date-only fact.
    """
    event_at = to_utc_z(datetime.combine(event.event_date, time.min, tzinfo=UTC))
    available_at = to_utc_z(event.available_at)
    description = _pack_description(event)

    existing = conn.execute(
        "SELECT id FROM company_events "
        "WHERE company_id = ? AND event_type = ? AND event_at = ? AND source_url = ?",
        (event.company_id, event.event_type, event_at, event.source_url),
    ).fetchone()

    if existing is not None:
        conn.execute(
            "UPDATE company_events SET available_at = ?, description = ? WHERE id = ?",
            (available_at, description, existing["id"]),
        )
        conn.commit()
        return int(existing["id"])

    cursor = conn.execute(
        "INSERT INTO company_events "
        "(company_id, event_type, event_at, available_at, source_url, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event.company_id, event.event_type, event_at, available_at, event.source_url, description),
    )
    conn.commit()
    return int(cursor.lastrowid)


def events_for(conn: sqlite3.Connection, company_id: str, as_of: datetime) -> list[CompanyEvent]:
    """Return events for `company_id` visible to a replay at `as_of`.

    Point-in-time replay discipline (spec.md §3/§6): only rows with
    `available_at <= as_of` are returned. The comparison is done in SQL on
    the TEXT columns directly (`to_utc_z(as_of)` bound into `<=`), not by
    parsing every row's `available_at` in Python and comparing datetimes —
    the fixed-width `to_utc_z` encoding makes TEXT lexical order match
    chronological order (rli/models/time.py), and comparing that way in SQL
    is how the rest of the codebase enforces this invariant.
    """
    as_of = ensure_aware(as_of, "as_of")
    rows = conn.execute(
        "SELECT * FROM company_events WHERE company_id = ? AND available_at <= ? ORDER BY event_at",
        (company_id, to_utc_z(as_of)),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def load_events_csv(path: str | Path, conn: sqlite3.Connection) -> int:
    """Load a `company_events.csv` (header: see `_EVENTS_CSV_HEADER`) into the DB.

    `materiality` and `source_quality` are not columns in this CSV — they
    are computed (`classify_materiality`) / constant (`"news"`), matching
    `write_events_csv`'s output shape. Returns the number of rows loaded.
    """
    path = Path(path)
    count = 0
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_excerpt = row["raw_excerpt"] or None
            event = CompanyEvent(
                company_id=row["company_id"],
                event_type=row["event_type"],
                event_date=date.fromisoformat(row["event_date"]),
                available_at=parse_utc(row["available_at"]),
                source_url=row["source_url"],
                headline=row["headline"],
                raw_excerpt=raw_excerpt,
                source_quality="news",
                materiality=classify_materiality(row["event_type"], row["headline"], raw_excerpt),
                collected_at=parse_utc(row["collected_at"]),
            )
            upsert_event(conn, event)
            count += 1
    return count


def write_events_csv(events: list[CompanyEvent], path: str | Path) -> None:
    """Write `events` to `path` in the exact shape `load_events_csv` reads back."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_EVENTS_CSV_HEADER)
        writer.writeheader()
        for event in events:
            writer.writerow(
                {
                    "company_id": event.company_id,
                    "event_type": event.event_type,
                    "event_date": event.event_date.isoformat(),
                    "available_at": to_utc_z(event.available_at),
                    "source_url": event.source_url,
                    "headline": event.headline,
                    "raw_excerpt": event.raw_excerpt or "",
                    "collected_at": to_utc_z(event.collected_at),
                }
            )


def write_collection_status_csv(rows: list[CollectionStatus], path: str | Path) -> None:
    """Write collection-status rows to `path` (header: see `_STATUS_CSV_HEADER`)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_STATUS_CSV_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "company_id": row.company_id,
                    "searched_at": to_utc_z(row.searched_at),
                    "queries_run": row.queries_run,
                    "events_found": row.events_found,
                }
            )


def read_collection_status_csv(path: str | Path) -> dict[str, CollectionStatus]:
    """Read a collection-status CSV, keyed by `company_id` (last row wins on duplicates)."""
    path = Path(path)
    result: dict[str, CollectionStatus] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            result[row["company_id"]] = CollectionStatus(
                company_id=row["company_id"],
                searched_at=parse_utc(row["searched_at"]),
                queries_run=int(row["queries_run"]),
                events_found=int(row["events_found"]),
            )
    return result


__all__ = [
    "CollectionStatus",
    "CompanyEvent",
    "EventType",
    "Materiality",
    "classify_materiality",
    "events_for",
    "load_events_csv",
    "read_collection_status_csv",
    "upsert_event",
    "write_collection_status_csv",
    "write_events_csv",
]
