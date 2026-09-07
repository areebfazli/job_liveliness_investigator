"""UTC time helpers.

Every timestamp that crosses a boundary (SQLite, JSON, cache keys) is stored
as an ISO 8601 string in UTC with a literal ``Z`` suffix and a fixed-width
microsecond fraction, so that lexical string ordering matches chronological
ordering and point-in-time replay comparisons (spec.md §3/§6) are
unambiguous. Always go through ``to_utc_z``; ``datetime.isoformat()`` alone
drops the fraction on a whole second and breaks that ordering.

Naive datetimes are rejected everywhere: a datetime without an offset cannot
be placed on the ``available_at <= T`` timeline safely.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["ensure_aware", "now_utc", "parse_utc", "to_utc_z"]


def ensure_aware(value: datetime, field_name: str = "value") -> datetime:
    """Return `value` converted to UTC; raise `ValueError` if it is naive."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{field_name} must be timezone-aware (naive datetimes break the "
            "point-in-time replay invariant in spec.md §3/§6)"
        )
    return value.astimezone(UTC)


def now_utc() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_utc_z(value: datetime) -> str:
    """Serialize a timezone-aware datetime as ISO 8601 UTC with a `Z` suffix.

    `timespec="microseconds"` is load-bearing, not cosmetic. Bare
    `datetime.isoformat()` OMITS the fractional part when `microsecond == 0`,
    which breaks the lexical-ordering invariant this whole codebase relies on
    for TEXT timestamp columns::

        "2026-01-01T12:00:00Z" > "2026-01-01T12:00:00.500000Z"   # "Z" > "."

    i.e. a whole-second timestamp would sort AFTER a later sub-second one.
    That would silently corrupt `ORDER BY fetched_at DESC` in `tool_cache`
    and every `available_at <= T` replay window (spec.md §6). A fixed-width
    6-digit fraction makes string comparison chronological again.
    """
    return ensure_aware(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a timezone-aware UTC datetime.

    Accepts the `Z` suffix as well as explicit numeric offsets. Raises
    `ValueError` for malformed input or for a timestamp with no offset.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    return ensure_aware(datetime.fromisoformat(text), "timestamp")
