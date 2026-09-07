"""Derive `material_negative_event` / `freeze_or_pause` from `company_events`.

Mirrors `rli.models.policy_inputs`'s own Unknown-vs-False distinction:
`False` means the company WAS searched and no qualifying event was found
("checked, none found"); `Unknown` means the company has not yet been
searched at all ("not yet investigated" — an unresolved question per
spec.md §4). Do not conflate the two: a probe/controller that treats
"nothing found" as "not checked" would re-run the same search forever, and
one that treats "not checked" as a known negative would silently let a
freeze/layoff slip past the policy.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from rli.events.store import CollectionStatus, events_for
from rli.models.policy_inputs import UNKNOWN, Unknown
from rli.models.time import ensure_aware

_MATERIAL_NEGATIVE_TYPES = frozenset({"layoff", "shutdown"})
_FREEZE_TYPES = frozenset({"hiring_freeze", "hiring_pause"})


def derive_policy_signals(
    conn: sqlite3.Connection,
    company_id: str,
    as_of: datetime,
    collection_status: dict[str, CollectionStatus],
    *,
    window_days: int,
) -> tuple[bool | Unknown, bool | Unknown]:
    """Return `(material_negative_event, freeze_or_pause)` for `company_id` at `as_of`.

    * If `company_id` is absent from `collection_status`, OR its recorded
      `searched_at` is *after* `as_of` (a search performed after `as_of`
      cannot count as available at `as_of` — this is a deliberate extension
      of the `available_at <= T` replay discipline from spec.md §3/§6 to the
      collection-status record itself, not just to individual events): both
      signals return `UNKNOWN` ("not yet investigated" per spec.md §4).
    * Otherwise, events visible at `as_of` (`events_for`, which already
      applies `available_at <= as_of`) are filtered to
      `event_date >= as_of.date() - timedelta(days=window_days)`, and:
        - `material_negative_event` is `True` iff any such event has
          `event_type in {"layoff", "shutdown"}` and `materiality ==
          "material"`, else `False` (checked, none found).
        - `freeze_or_pause` is `True` iff any such event has
          `event_type in {"hiring_freeze", "hiring_pause"}`, else `False`.

    JUDGMENT CALL: spec.md §5 only explicitly names a lookback window for
    `freeze_or_pause` ("open + explicit freeze/pause ... unresolved current
    status -> wait"). This function reuses the same `window_days` for
    `material_negative_event` too, since a materially large but very stale
    layoff (e.g. from years ago) should not perpetually block `apply_now`.
    This is a defensible default, not a spec requirement — revisit if
    replay/outcome data suggests layoffs and freezes should decay on
    different timescales.
    """
    as_of = ensure_aware(as_of, "as_of")

    status = collection_status.get(company_id)
    if status is None or status.searched_at > as_of:
        return UNKNOWN, UNKNOWN

    window_start = as_of.date() - timedelta(days=window_days)
    events = [e for e in events_for(conn, company_id, as_of) if e.event_date >= window_start]

    material_negative_event = any(
        e.event_type in _MATERIAL_NEGATIVE_TYPES and e.materiality == "material" for e in events
    )
    freeze_or_pause = any(e.event_type in _FREEZE_TYPES for e in events)

    return material_negative_event, freeze_or_pause


__all__ = ["derive_policy_signals"]
