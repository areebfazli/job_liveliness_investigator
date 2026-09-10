"""Derive the `company_events` policy signals from the pre-collected store.

`derive_policy_signals` answers three questions at once —
`material_negative_event`, `freeze_or_pause`, and the DATE of the most
recent material negative event (spec.md §5's Amendment 2026-09-10 adds the
row "open + material negative event after last refresh -> wait", which needs
the date, not merely the boolean). They are returned together, as one
`EventSignals` triple, because they are three readings of ONE filtered event
list: computing the date separately would risk a caller windowing it
differently from the boolean and producing the impossible pair
`material_negative_event=True` with no date.

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
from datetime import UTC, datetime, time, timedelta

from rli.events.store import CollectionStatus, events_for
from rli.models.policy_inputs import UNKNOWN, Unknown
from rli.models.time import ensure_aware

#: `(material_negative_event, freeze_or_pause, last_material_event_at)`.
#: Named so every layer that carries the triple around
#: (`rli.eval.case.CaseState`, `rli.policy.inputs.derive_policy_inputs`)
#: names the same type rather than restating a three-element tuple.
EventSignals = tuple[bool | Unknown, bool | Unknown, datetime | None | Unknown]

_MATERIAL_NEGATIVE_TYPES = frozenset({"layoff", "shutdown"})
_FREEZE_TYPES = frozenset({"hiring_freeze", "hiring_pause"})


def derive_policy_signals(
    conn: sqlite3.Connection,
    company_id: str,
    as_of: datetime,
    collection_status: dict[str, CollectionStatus],
    *,
    window_days: int,
) -> EventSignals:
    """Return the `EventSignals` triple for `company_id` at `as_of`.

    * If `company_id` is absent from `collection_status`, OR its recorded
      `searched_at` is *after* `as_of` (a search performed after `as_of`
      cannot count as available at `as_of` — this is a deliberate extension
      of the `available_at <= T` replay discipline from spec.md §3/§6 to the
      collection-status record itself, not just to individual events): both
      signals return `UNKNOWN` ("not yet investigated" per spec.md §4), and
      so does `last_material_event_at` — the date is UNKNOWN for exactly the
      same reason the boolean is.
    * Otherwise, events visible at `as_of` (`events_for`, which already
      applies `available_at <= as_of`) are filtered to
      `event_date >= as_of.date() - timedelta(days=window_days)`, and:
        - `material_negative_event` is `True` iff any such event has
          `event_type in {"layoff", "shutdown"}` and `materiality ==
          "material"`, else `False` (checked, none found).
        - `freeze_or_pause` is `True` iff any such event has
          `event_type in {"hiring_freeze", "hiring_pause"}`, else `False`.
        - `last_material_event_at` is the MAX `event_date` among the events
          that made `material_negative_event` true, as midnight UTC (the
          same `datetime.combine(event_date, time.min, tzinfo=UTC)`
          simplification `rli.events.store.upsert_event` and
          `rli.probes.company_events` already apply — `company_events`
          stores a calendar date, so this is not a claim about the hour).
          `None` when the company was checked and no qualifying event was
          found, which keeps the pair `(False, None)` distinguishable from
          the unchecked `(UNKNOWN, UNKNOWN)`.

    The MAX (not the min) is the right reduction: the policy asks "has the
    posting been refreshed SINCE the bad news", so the most recent piece of
    bad news is the one that has to be answered. Taking an older event would
    let a refresh that predates the latest layoff still count as "refreshed
    since".

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
        return UNKNOWN, UNKNOWN, UNKNOWN

    window_start = as_of.date() - timedelta(days=window_days)
    events = [e for e in events_for(conn, company_id, as_of) if e.event_date >= window_start]

    material = [
        e
        for e in events
        if e.event_type in _MATERIAL_NEGATIVE_TYPES and e.materiality == "material"
    ]
    material_negative_event = bool(material)
    freeze_or_pause = any(e.event_type in _FREEZE_TYPES for e in events)
    last_material_event_at = (
        datetime.combine(max(e.event_date for e in material), time.min, tzinfo=UTC)
        if material
        else None
    )

    return material_negative_event, freeze_or_pause, last_material_event_at


__all__ = ["EventSignals", "derive_policy_signals"]
