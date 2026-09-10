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

That distinction has to survive the trip through the EVIDENCE table too,
because `rli.policy.inputs` re-derives the three policy inputs from this
probe's claims rather than from its `data` (spec.md §5 sources them from
this probe; spec.md §9/§1 require every reason to map to evidence). Dated
events give the `True` answers an evidence trail for free — each event is a
claim. The `False` answer ("we searched this company and found nothing in
the window") has no event to point at, so this probe emits a
`company_events_searched` claim carrying `collection_status.searched_at`
whenever `collected` is True. Without it, "no company_events evidence at
all" and "searched, nothing found" would be the same observation to the
policy layer, and the policy would have to read `False` out of an ABSENCE —
precisely the assertion spec.md §9 forbids. With it, the honest reading of
"no evidence" stays UNKNOWN.

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
* **`value` carries the stored `materiality` as a prefix** —
  `f"{materiality}: {headline}"`, written by
  `rli.events.policy_signals.format_event_claim_value`. `material` vs.
  `minor` is what separates a `layoff` that populates
  `material_negative_event` from one that does not, the `evidence` table has
  no column for it, and re-classifying the headline downstream could
  disagree with the materiality this probe actually used. See that module's
  docstring for the full argument, including the two encodings rejected.
  Note the asymmetry with the previous bullet, which is deliberate: the
  event TYPE is a queryable identity and belongs in `claim_type`; the
  materiality is a derived attribute of one claim and rides in its value.
* **`data["last_material_event_at"]` is an ISO-Z STRING, never a bare
  `datetime`.** `derive_policy_signals` returns
  `datetime | None | Unknown`; this probe passes UNKNOWN and `None` through
  unchanged (they are the two answers the Unknown-vs-False discipline above
  exists to keep apart) but renders a real datetime with `to_utc_z`, the
  same way `data["as_of"]` is rendered. Nothing in the decision path reads
  it any more — `rli.policy.inputs` derives all three signals from this
  probe's CLAIMS — but it is still what a trace reader and the replay
  dataset record see, so it must stay unambiguous. The reason for the string
  is that `ProbeResult.data` is a JSON blob in the replay dataset:
  `rli.replay.mode` does carry a datetime envelope, but a probe payload that
  reads identically as JSON and as live Python is one less thing for a
  future reader of `replay_probe_results.data` to decode by hand, and the
  three-way UNKNOWN / null / timestamp distinction survives either way.
* **The collection-status file is resolved in three steps:**
  `args.collection_status_csv or ctx.collection_status_csv or
  DEFAULT_COLLECTION_STATUS_CSV` (`REPO_ROOT/data/events/collection_status.csv`,
  matching `scripts/collect_events.py`'s `DEFAULT_OUT_STATUS_CSV`).

  Both overrides exist, and they are not redundant. `ctx` is the RUN-level
  pin: whoever opens a run (an evaluation, a replay, `rli.eval.runner.
  open_system_runner`) chooses which pre-collected corpus the whole run
  reasons against, exactly as it chooses `ctx.conn` — and, crucially, a path
  on `ctx` is invisible to `args_hash`, so a replay dataset record stays
  portable across machines (see `rli.probes.base.ProbeContext`). `args` is
  the CALL-level override: it predates `ctx`'s field, many tests construct
  `CompanyEventsArgs` directly with no runner in sight, and it is the
  narrower, more specific statement — so it wins. Production callers leave
  both `None`.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, field_validator

from rli.config import REPO_ROOT
from rli.events.policy_signals import (
    CLAIM_EVENTS_SEARCHED,
    COLLECTION_STATUS_SOURCE_URL,
    COMPANY_EVENTS_PROBE,
    derive_policy_signals,
    format_event_claim_value,
)
from rli.events.store import CollectionStatus, events_for, read_collection_status_csv
from rli.models.probe import ProbeResult
from rli.models.time import ensure_aware, to_utc_z
from rli.policy.action import PolicyThresholds
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


def _collection_status_path(args: CompanyEventsArgs, ctx: ProbeContext) -> Path:
    """`args` override, else the run-level `ctx` pin, else the repo default.

    The precedence and the reason for both overrides are argued in the module
    docstring. Resolved in one place so the path this probe READS and the
    path a test or a replay THINKS it pinned can never be two different
    files.
    """
    return Path(
        args.collection_status_csv or ctx.collection_status_csv or DEFAULT_COLLECTION_STATUS_CSV
    )


def _load_collection_status(path: Path) -> dict[str, CollectionStatus]:
    """Read the pre-collected status CSV; a missing file is an empty map.

    "Collection has not run yet" is exactly the "not yet investigated" case
    `derive_policy_signals` reports as `UNKNOWN` — an absent file must
    therefore degrade to an empty map, never raise.
    """
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
    collection_status = _load_collection_status(_collection_status_path(args, ctx))

    # The window is resolved through `PolicyThresholds`, not read straight
    # off `[thresholds]`, because it is a POLICY threshold: it decides
    # whether an event still populates `material_negative_event`, and it is
    # part of `policy_version()`. Reading `cfg.thresholds` directly would
    # make a `[policy] negative_event_window_days` override change the
    # recorded policy version while changing no behaviour — a footgun. By
    # default the override is `None` and this resolves to the very same
    # `[thresholds]` value, so this is behaviourally identical today.
    thresholds = PolicyThresholds.coerce(ctx.config)
    material_negative_event, freeze_or_pause, last_material_event_at = derive_policy_signals(
        ctx.conn,
        args.company_id,
        args.as_of,
        collection_status,
        window_days=thresholds.negative_event_window_days,
    )

    status = collection_status.get(args.company_id)
    # Same rule derive_policy_signals applies: a search recorded AFTER
    # `as_of` cannot count as collected at `as_of` (replay discipline).
    collected = status is not None and status.searched_at <= args.as_of

    events = events_for(ctx.conn, args.company_id, args.as_of)
    claims = [
        ProbeClaim(
            claim_type=event.event_type,
            # "<materiality>: <headline>" — see the module docstring.
            value=format_event_claim_value(event.materiality, event.headline),
            source_url=event.source_url,
            raw_excerpt=event.raw_excerpt,
            source_quality="news",
            source_event_at=datetime.combine(event.event_date, time.min, tzinfo=UTC),
            available_at=event.available_at,
            fetched_at=now,
        )
        for event in events
    ]

    if collected and status is not None:
        # The collection fact the event claims hang off, so it goes FIRST.
        # It is what makes "searched, nothing found" an evidence-backed
        # `False` instead of an inference from silence (module docstring).
        #
        # `available_at = searched_at` is not cosmetic: it is what lets the
        # point-in-time gate in `rli.eval.runner.ProbeRunner.save_evidence`
        # (overridden by `rli.replay.mode`) drop this claim automatically
        # when the search post-dates T, which collapses the derived inputs
        # back to UNKNOWN — the same rule `derive_policy_signals` applies to
        # `status.searched_at > as_of`, arrived at through the generic
        # evidence gate rather than through a second copy of the comparison.
        #
        # `source_quality="enrichment"`, the lowest tier in spec.md §3, is
        # the honest label: this is our own collection bookkeeping, not an
        # observation of the employer or of the news.
        claims.insert(
            0,
            ProbeClaim(
                claim_type=CLAIM_EVENTS_SEARCHED,
                value=to_utc_z(status.searched_at),
                # A stable logical id, never the resolved local path — see
                # `rli.events.policy_signals.COLLECTION_STATUS_SOURCE_URL`.
                source_url=COLLECTION_STATUS_SOURCE_URL,
                raw_excerpt=None,
                source_quality="enrichment",
                source_event_at=status.searched_at,
                available_at=status.searched_at,
                fetched_at=now,
            ),
        )

    return ProbeResult(
        ok=True,
        data={
            "company_id": args.company_id,
            "as_of": to_utc_z(args.as_of),
            "collected": collected,
            "events": [event.model_dump(mode="json") for event in events],
            "material_negative_event": material_negative_event,
            "freeze_or_pause": freeze_or_pause,
            # UNKNOWN / None / ISO-Z string — see the module docstring.
            "last_material_event_at": (
                to_utc_z(last_material_event_at)
                if isinstance(last_material_event_at, datetime)
                else last_material_event_at
            ),
            "evidence": claims,
        },
    )


class CompanyEventsProbe(Probe):
    """Dynamic probe: dated company events from the pre-collected store (spec.md §4)."""

    # The literal lives in `rli.events.policy_signals` (the shared events
    # vocabulary `rli.policy.inputs` also reads) rather than here, because a
    # `policy -> probes` import would be an unwanted edge and a
    # `probes -> events -> probes` one an outright cycle. Binding it here is
    # what keeps the two spellings provably identical.
    name: ClassVar[str] = COMPANY_EVENTS_PROBE
    cost_tier: ClassVar[str] = "medium"
    history_required: ClassVar[bool] = False
    # `last_material_event_at` is populated by the same read of the same
    # store as `material_negative_event` — same probe, same question — so it
    # belongs here: the controller's eligibility rule ("can this probe
    # populate an unresolved input?") would otherwise call this probe
    # ineligible for a case whose ONLY open question is the event date.
    populates: ClassVar[frozenset[str]] = frozenset(
        {"material_negative_event", "freeze_or_pause", "last_material_event_at"}
    )
    ArgsModel: ClassVar[type[BaseModel]] = CompanyEventsArgs

    # No `eligible` override: board history is irrelevant here, and whether
    # the events were collected is a RESULT of the probe (`collected`), not a
    # precondition for running it — running it is how the controller learns
    # that the answer is still UNKNOWN.

    def run(self, args: CompanyEventsArgs, ctx: ProbeContext) -> ProbeResult:
        return company_events(args, ctx)
