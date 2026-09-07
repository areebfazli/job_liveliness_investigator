"""`company_events` — the medium-cost, replay-safe dated-events probe.

spec.md §4 lists `company_events` as a dynamic probe returning "dated
layoffs, freezes, funding, expansion" at `medium` cost with
`history required = no`, and is explicit about how it must work:

    For historical `company_events`, pre-collect dated events and replay by
    `available_at`; do not live-search during benchmark replay.

So this probe makes **no network calls at all**. It reads the pre-collected
`company_events` table (through `rli.events.store.events_for`, which already
enforces `available_at <= as_of`) and the pre-collected
`collection_status.csv` written by `scripts/collect_events.py`. Live search
happens in that collection script, never here — which is also why the probe
always returns `ok=True`: there is no transport that could fail, and every
"nothing to report" case is a legitimate fact rather than a probe failure.

Unknown vs. False (the whole point of the collection-status file)
-----------------------------------------------------------------
`rli.events.policy_signals.derive_policy_signals` returns the `UNKNOWN`
sentinel when a company has not been searched (or was searched only *after*
`as_of`, which point-in-time replay must treat as not-yet-searched), and
`False` when it was searched and nothing qualifying was found. This probe
passes both straight through — it never re-derives them and never collapses
`UNKNOWN` into `False`. A missing `collection_status.csv` is therefore an
empty status map, i.e. "no company has been investigated yet", not an
error.

GUESSED / judgment calls made in this module
--------------------------------------------

* **Every replay-visible event becomes a claim, regardless of
  `negative_event_window_days`.** The two boolean policy signals are
  windowed (an ancient layoff must not block `apply_now` forever — see
  `derive_policy_signals`'s own docstring), but the EVIDENCE list is not:
  an investigator reading the case file should see a funding round from
  last year even though it is outside the negative-event window. Filtering
  evidence by the boolean signals' window would hide facts the LLM is
  explicitly asked to interpret (spec.md §2). The window is still applied,
  unchanged, to the booleans.
* **`available_at` is the event's own `available_at`, never `ctx.now()`.**
  spec.md §3/§4: a fact becomes visible to a replay at the moment it became
  verifiable — for a pre-collected news article, its publish time (see
  `rli.events.store`'s module docstring for why that is not backdating).
  Stamping `ctx.now()` here would forward-date every historical article to
  the probe run and silently break every point-in-time replay window.
  `fetched_at` is `ctx.now()`, because that IS when this probe read it.
* **`source_event_at` is midnight UTC on `event_date`.** `company_events`
  stores only a calendar date for when the underlying event happened
  (`rli.events.store.upsert_event` uses the same
  `datetime.combine(event_date, time.min, tzinfo=UTC)`), so this is that
  same documented simplification, not a claim about the hour.
* **`claim_type` is the raw `event_type`** (`"layoff"`, `"hiring_freeze"`,
  ...) rather than a single generic `"company_event"` type. spec.md §4
  requires probes to return facts, not verdicts, and the event type is a
  fact printed on the source; folding it into the `value` string would make
  it unqueryable in the `evidence` table.
* **`collection_status_csv` defaults to the real repo path**
  (`REPO_ROOT/data/events/collection_status.csv`, matching
  `scripts/collect_events.py`'s `DEFAULT_OUT_STATUS_CSV`). The argument
  exists so tests can point at a fixture without monkeypatching module
  state; production callers leave it `None`.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, field_validator

from rli.config import REPO_ROOT
from rli.events.policy_signals import derive_policy_signals
from rli.events.store import CollectionStatus, events_for, read_collection_status_csv
from rli.models.probe import ProbeResult
from rli.models.time import ensure_aware, to_utc_z
from rli.probes.base import Probe, ProbeClaim, ProbeContext

__all__ = ["CompanyEventsArgs", "CompanyEventsProbe", "company_events"]

# Mirrors scripts/collect_events.py's DEFAULT_OUT_STATUS_CSV. Derived from
# REPO_ROOT (module-relative) rather than the process CWD, for the same
# reason rli.config resolves config.toml that way.
DEFAULT_COLLECTION_STATUS_CSV = REPO_ROOT / "data" / "events" / "collection_status.csv"


class CompanyEventsArgs(BaseModel):
    company_id: str
    as_of: datetime
    # Test/replay override of the pre-collected status file; None = the real path.
    collection_status_csv: str | None = None

    @field_validator("as_of")
    @classmethod
    def _as_of_tz_aware_utc(cls, value: datetime) -> datetime:
        # A naive `as_of` cannot be placed on the `available_at <= T`
        # timeline (rli.models.time), and every other model on that timeline
        # (CompanyEvent, CollectionStatus) rejects one too.
        return ensure_aware(value, "as_of")


def _load_collection_status(args: CompanyEventsArgs) -> dict[str, CollectionStatus]:
    """Read the pre-collected status CSV; a missing file is an empty map.

    "Collection has not run yet" is exactly the "not yet investigated" case
    `derive_policy_signals` reports as `UNKNOWN` — an absent file must
    therefore degrade to an empty map, never raise.
    """
    path = (
        Path(args.collection_status_csv)
        if args.collection_status_csv
        else DEFAULT_COLLECTION_STATUS_CSV
    )
    if not path.is_file():
        return {}
    return read_collection_status_csv(path)


def company_events(args: CompanyEventsArgs, ctx: ProbeContext) -> ProbeResult:
    """Pure function backing `CompanyEventsProbe.run` (spec.md §4).

    Always `ok=True`: this probe never touches the network, so there is no
    failure mode to report. "Not yet collected" is a fact (`collected=False`
    plus `UNKNOWN` signals), not an error.
    """
    now = ctx.now()
    collection_status = _load_collection_status(args)

    material_negative_event, freeze_or_pause = derive_policy_signals(
        ctx.conn,
        args.company_id,
        args.as_of,
        collection_status,
        window_days=ctx.config.thresholds.negative_event_window_days,
    )

    status = collection_status.get(args.company_id)
    # Same rule derive_policy_signals applies: a search recorded AFTER
    # `as_of` cannot count as collected at `as_of` (replay discipline).
    collected = status is not None and status.searched_at <= args.as_of

    events = events_for(ctx.conn, args.company_id, args.as_of)
    claims = [
        ProbeClaim(
            claim_type=event.event_type,
            value=event.headline,
            source_url=event.source_url,
            raw_excerpt=event.raw_excerpt,
            source_quality="news",
            source_event_at=datetime.combine(event.event_date, time.min, tzinfo=UTC),
            available_at=event.available_at,
            fetched_at=now,
        )
        for event in events
    ]

    return ProbeResult(
        ok=True,
        data={
            "company_id": args.company_id,
            "as_of": to_utc_z(args.as_of),
            "collected": collected,
            "events": [event.model_dump(mode="json") for event in events],
            "material_negative_event": material_negative_event,
            "freeze_or_pause": freeze_or_pause,
            "evidence": claims,
        },
    )


class CompanyEventsProbe(Probe):
    """Dynamic probe: dated company events from the pre-collected store (spec.md §4)."""

    name: ClassVar[str] = "company_events"
    cost_tier: ClassVar[str] = "medium"
    history_required: ClassVar[bool] = False
    populates: ClassVar[frozenset[str]] = frozenset(
        {"material_negative_event", "freeze_or_pause"}
    )
    ArgsModel: ClassVar[type[BaseModel]] = CompanyEventsArgs

    # No `eligible` override: board history is irrelevant here, and whether
    # the events were collected is a RESULT of the probe (`collected`), not a
    # precondition for running it — running it is how the controller learns
    # that the answer is still UNKNOWN.

    def run(self, args: CompanyEventsArgs, ctx: ProbeContext) -> ProbeResult:
        return company_events(args, ctx)
