"""The `company_events` policy rule, and the shared vocabulary around it.

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

--------------------------------------------------------------------------
Why the RULE is factored out of the STORE access
--------------------------------------------------------------------------

There are two paths to these three signals and they must not be allowed to
disagree:

1. the **probe path** — `rli.probes.company_events` reads the pre-collected
   `company_events` table plus `collection_status.csv` and reports the triple
   in its `ProbeResult.data`;
2. the **claims path** — `rli.policy.inputs.derive_policy_inputs` recomputes
   the triple from the `EvidenceItem`s that probe emitted, because spec.md §9
   requires every user-facing reason to map to evidence and spec.md §5
   sources these three inputs from the `company_events` probe.

`signals_from_facts` is the single rule both use. It takes abstract
`EventFact`s (type, date, materiality) and knows nothing about SQLite,
CSV files or the evidence table, so the two callers differ only in how they
obtain the facts — never in how the facts are interpreted.
`derive_policy_signals` is then the thin store-reading adapter it always
was: read `collection_status`, read `events_for`, delegate.

That property is directly testable, and is tested
(`tests/test_policy_inputs.py`'s "cannot disagree" case): for one company at
one `as_of`, the claims path and the store path return the same triple.

--------------------------------------------------------------------------
JUDGMENT CALL: materiality rides on the claim's `value` as a prefix
--------------------------------------------------------------------------

The claims path needs each event's `materiality`, and the `evidence` table
has a FIXED schema with no materiality column. Rather than migrate the
schema for one probe, `company_events` encodes it as a machine-readable
prefix on the claim's `value`:

    f"{materiality}: {headline}"      e.g. "material: Acme lays off 200"

`format_event_claim_value` writes it, `parse_event_claim_value` reads it
back. The parser splits on the FIRST `":"` only, lowercases/strips the
token, and accepts *exactly* `"material"` or `"minor"`. Anything else means
the value was not written by this version of the probe (an older dataset
record, a hand-written fixture, an evidence row from another source), in
which case it returns `None` for the materiality **and the ORIGINAL,
untouched value as the headline** — a headline that merely happens to
contain a colon ("Layoffs: what we know") must not have half of itself eaten
by a parser that guessed wrong.

An unparsed materiality on a `layoff`/`shutdown` claim is resolved to
**material** by the claims adapter in `rli.policy.inputs` — deliberately
fail-safe. Guessing `"minor"` would silently drop a `wait` on a real layoff,
which is exactly the bug class this whole refactor exists to fix; guessing
`"material"` can only over-warn, and over-warning is visible and arguable
while a missing warning is neither. (`signals_from_facts` itself takes the
materiality it is given: the fail-safe belongs at the boundary where the
information was lost, and is documented at both ends.)

Two alternatives were considered and rejected:

* **A claim_type suffix** (`"layoff:material"`). `claim_type` is an identity
  used well outside this module — `rli.eval.metrics`' claim families,
  `rli.policy.explain_stub`, and every `WHERE claim_type = ?` query — so
  encoding a second dimension into it would silently reclassify evidence for
  all of them.
* **Re-running `rli.events.store.classify_materiality` at policy time.**
  It could reach a different answer than the one the probe stored (the
  classifier may change; the stored row may predate it), which would
  reintroduce exactly the probe-vs-policy disagreement this module was
  factored to make impossible.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import NamedTuple, get_args

from rli.events.store import CollectionStatus, EventType, events_for
from rli.models.policy_inputs import UNKNOWN, Unknown
from rli.models.time import ensure_aware

#: `(material_negative_event, freeze_or_pause, last_material_event_at)`.
#: Named so every layer that carries the triple around
#: (`rli.probes.company_events`, `rli.policy.inputs`) names the same type
#: rather than restating a three-element tuple.
EventSignals = tuple[bool | Unknown, bool | Unknown, datetime | None | Unknown]

_MATERIAL_NEGATIVE_TYPES = frozenset({"layoff", "shutdown"})
_FREEZE_TYPES = frozenset({"hiring_freeze", "hiring_pause"})

# ---------------------------------------------------------------------------
# The shared `company_events` vocabulary
# ---------------------------------------------------------------------------
#
# These constants live HERE, in `rli.events`, rather than in
# `rli.probes.company_events`, because both the probe that WRITES the claims
# and `rli.policy.inputs`, which READS them, need them. `rli.policy.inputs`
# already imports this module; importing `rli.probes.*` from it would add a
# policy -> probes edge, and importing `rli.probes.*` from `rli.events.*`
# would add a probes -> events -> probes cycle. Hence
# `COMPANY_EVENTS_PROBE` is a plain string literal here rather than
# `CompanyEventsProbe.name` — the probe class asserts the two agree by
# setting `name = COMPANY_EVENTS_PROBE`.

#: `Probe.name` of the probe that emits every claim described below.
COMPANY_EVENTS_PROBE = "company_events"

#: `claim_type` of the collection-status claim `company_events` emits when it
#: actually searched. It is what makes the `False` answer ("we checked this
#: company and found nothing in the window") evidence-backed, rather than an
#: assertion resting on the ABSENCE of evidence — which spec.md §9 would not
#: let the policy make. Without such a claim the honest reading of "no
#: `company_events` evidence" is UNKNOWN, never False.
CLAIM_EVENTS_SEARCHED = "company_events_searched"

#: `source_url` of that collection-status claim: a STABLE LOGICAL identifier
#: for the pre-collected status file, never the resolved local path. Evidence
#: rows are persisted and replayed, and a machine-local absolute path (or a
#: test's `tmp_path`) baked into a stored row would make the row unportable
#: and the replay dataset machine-specific. The scheme is `file:` + the
#: repo-relative canonical location, which reads correctly regardless of
#: which checkout — or which `--collection-status-csv` override — produced it.
COLLECTION_STATUS_SOURCE_URL = "file:data/events/collection_status.csv"

#: The `claim_type` values `company_events` emits for INDIVIDUAL events (it
#: uses the raw `event_type` as the claim type — see that module). Derived
#: from the `EventType` literal so a ninth event type is covered here the
#: moment it is added to the store, with no second list to update.
EVENT_CLAIM_TYPES: frozenset[str] = frozenset(get_args(EventType))

_MATERIALITY_TOKENS = frozenset({"material", "minor"})


class EventFact(NamedTuple):
    """One dated company event, reduced to the three fields the rule reads.

    Deliberately NOT `rli.events.store.CompanyEvent`: the point of this type
    is that `signals_from_facts` can be fed from an `EvidenceItem` just as
    easily as from a database row, so it must not carry anything (source
    url, excerpt, `collected_at`) that only one of the two paths has.
    """

    event_type: str
    event_date: date
    #: `"material"` / `"minor"` — see the module docstring on how the claims
    #: path recovers this, and what it does when it cannot.
    materiality: str


def format_event_claim_value(materiality: str, headline: str) -> str:
    """Render a per-event claim `value`: `"<materiality>: <headline>"`.

    The inverse of `parse_event_claim_value`. Both live here so the writer
    (`rli.probes.company_events`) and the reader (`rli.policy.inputs`) cannot
    drift apart on the separator or on the spacing.
    """
    return f"{materiality}: {headline}"


def parse_event_claim_value(value: str) -> tuple[str | None, str]:
    """Split a per-event claim `value` into `(materiality | None, headline)`.

    Splits on the FIRST `":"` only, and accepts the prefix only when it is
    exactly `"material"` or `"minor"` (case-insensitively, ignoring
    surrounding whitespace). Any other value is treated as "not written by
    this probe version": the materiality is `None` and the headline is the
    ORIGINAL string, unmodified — see the module docstring for why the
    parser must not eat a colon out of a headline it does not recognise.
    """
    head, separator, tail = value.partition(":")
    if not separator:
        return None, value
    token = head.strip().lower()
    if token not in _MATERIALITY_TOKENS:
        return None, value
    return token, tail.strip()


def signals_from_facts(
    facts: Iterable[EventFact],
    as_of: datetime,
    *,
    window_days: int,
    searched: bool,
) -> EventSignals:
    """The `EventSignals` rule, over abstract facts. The ONE copy of it.

    Arguments:
        facts: every event known to the caller for this company that is
            already visible at `as_of`. Callers apply the `available_at <=
            as_of` replay gate themselves — `events_for` does it in SQL, and
            the claims path gets it from `ProbeRunner.save_evidence`'s gate —
            because only they hold the timestamp it reads.
        as_of: the decision clock. Must be timezone-aware.
        window_days: the negative-event lookback (`PolicyThresholds.
            negative_event_window_days`).
        searched: whether this company's events have been searched at all,
            AS OF `as_of`. `False` short-circuits to all-UNKNOWN.

    * `searched=False` -> `(UNKNOWN, UNKNOWN, UNKNOWN)` immediately, whatever
      `facts` contains. "Not yet investigated" (spec.md §4) is a statement
      about the SEARCH, not about the rows: a company we never searched can
      still have a row in the store from some other collection pass, and
      reading that row as "checked, none found" would answer a question
      nobody asked. The date is UNKNOWN for exactly the same reason the
      booleans are.
    * Otherwise, facts are filtered to `event_date >= as_of.date() -
      timedelta(days=window_days)`, and:
        - `material_negative_event` is `True` iff any such fact has
          `event_type in {"layoff", "shutdown"}` and `materiality ==
          "material"`, else `False` (checked, none found).
        - `freeze_or_pause` is `True` iff any such fact has
          `event_type in {"hiring_freeze", "hiring_pause"}`, else `False`.
        - `last_material_event_at` is the MAX `event_date` among the facts
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
    if not searched:
        return UNKNOWN, UNKNOWN, UNKNOWN

    window_start = as_of.date() - timedelta(days=window_days)
    in_window = [fact for fact in facts if fact.event_date >= window_start]

    material = [
        fact
        for fact in in_window
        if fact.event_type in _MATERIAL_NEGATIVE_TYPES and fact.materiality == "material"
    ]
    material_negative_event = bool(material)
    freeze_or_pause = any(fact.event_type in _FREEZE_TYPES for fact in in_window)
    last_material_event_at = (
        datetime.combine(max(fact.event_date for fact in material), time.min, tzinfo=UTC)
        if material
        else None
    )

    return material_negative_event, freeze_or_pause, last_material_event_at


def derive_policy_signals(
    conn: sqlite3.Connection,
    company_id: str,
    as_of: datetime,
    collection_status: dict[str, CollectionStatus],
    *,
    window_days: int,
) -> EventSignals:
    """`signals_from_facts` over the pre-collected store — the STORE path.

    The store-reading adapter around the rule above, and nothing more:

    * `searched` is `True` iff `company_id` is present in
      `collection_status` AND its recorded `searched_at` is not after
      `as_of`. A search performed after `as_of` cannot count as available at
      `as_of` — a deliberate extension of the `available_at <= T` replay
      discipline from spec.md §3/§6 to the collection-status record itself,
      not just to individual events. `rli.probes.company_events` computes its
      `collected` flag with the identical expression, and the
      `company_events_searched` claim it emits is what carries this same fact
      onto the claims path.
    * the facts are `events_for(conn, company_id, as_of)`, which has already
      applied `available_at <= as_of` in SQL, projected onto `EventFact`.

    Everything about WHAT the signals mean lives in `signals_from_facts`;
    this function only decides where the facts come from.
    """
    as_of = ensure_aware(as_of, "as_of")

    status = collection_status.get(company_id)
    searched = status is not None and status.searched_at <= as_of

    # The store is not read at all for an unsearched company: the rule would
    # discard the facts anyway, so the query would be pure waste. Behaviour
    # is identical either way, which is why the short-circuit that MATTERS
    # (the one that decides the answer) lives in the rule and this one is
    # only an optimization.
    facts = (
        [
            EventFact(event.event_type, event.event_date, event.materiality)
            for event in events_for(conn, company_id, as_of)
        ]
        if searched
        else []
    )
    return signals_from_facts(facts, as_of, window_days=window_days, searched=searched)


__all__ = [
    "CLAIM_EVENTS_SEARCHED",
    "COLLECTION_STATUS_SOURCE_URL",
    "COMPANY_EVENTS_PROBE",
    "EVENT_CLAIM_TYPES",
    "EventFact",
    "EventSignals",
    "derive_policy_signals",
    "format_event_claim_value",
    "parse_event_claim_value",
    "signals_from_facts",
]
