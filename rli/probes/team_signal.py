"""`team_signal` — corroborating hiring activity, from our OWN board history.

spec.md §4's dynamic-probes table describes `team_signal` as "recent similar
hires/activity from licensed source" and names it "the only source for the
policy input `corroborating_hiring_signal`". spec.md §5's **Amendment
2026-09-10** supersedes the first half of that sentence and keeps the second:

> `corroborating_hiring_signal` source | licensed enrichment only |
> `team_signal` probe derived from own + archive board snapshots: new roles
> on the same team in the last 30 days, or closures on the same team in the
> last 60 days; unknown when team history < `min_history_days`

The amendment exists because replay on real data showed the original policy
left the agent with no reachable question: the `skip` branch needed a hiring
signal that no licensed feed was ever going to supply. So the probe is now
sourced from data this project already holds, `BoardHistoryTeamSignalSource`
reads it through `rli.history.features.team_activity`, and
`[team_signal].enabled` defaults to `True`. It is still a READ-ONLY probe
(`rli.probes.base`: "Do not write to DB inside probes"), and it makes no
network call at all — its `[allowlists].team_signal` is correctly empty.

The `TeamSignalSource` Protocol and `NullTeamSignalSource` survive the
change. They are no longer "the only honest implementation available"; they
are the seam an ALTERNATE source (a licensed feed, a test double, a
deployment that wants the probe to fail loudly) plugs into without touching
this module or its caller.

Facts, not verdicts (spec.md §4)
--------------------------------
The counting lives in `rli.history.features.team_activity`, which returns
counts and the events behind them and nothing else. This module owns exactly
one judgment — whether those counts clear the configured minimums — because
that judgment is a *policy input*, not a history feature, and
`rli.policy.inputs` is forbidden from inferring it from board activity on
its own ("`corroborating_hiring_signal` has exactly one source").

Unknown is a first-class answer
-------------------------------
Three outcomes, not two: activity seen (`True`), no activity seen over
history deep and dense enough for that absence to be an observation
(`False`), and otherwise Unknown. spec.md §4's "missing history never means
flat hiring" is the whole reason the middle case has its own preconditions:
a company we have watched for a week, or watched with 10% coverage, can
easily show zero new roles because we were not looking.

GUESSED / judgment calls made in this module
--------------------------------------------

* **The boolean claim's `claim_type` is the literal string
  `"corroborating_hiring_signal"`, duplicated here as
  `CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE` rather than imported from
  `rli.policy.inputs.CLAIM_TEAM_SIGNAL`.** `rli.policy.inputs._team_signal`
  selects claims by `claim_type == CLAIM_TEAM_SIGNAL and probe ==
  "team_signal"`, so any other spelling (`"team_hiring_signal"`, say) would
  be invisible to the only reader that exists and would silently defeat the
  feature — an emitted claim that populates nothing. The literal is
  duplicated rather than imported to keep the dependency arrow pointing one
  way: `rli.policy` reads probe output, and a probe module importing from
  the policy layer would invert that (and risk an import cycle through
  `rli.policy.action`). The two literals MUST stay byte-for-byte equal; the
  constant is exported so a test can assert exactly that.
* **`value` is `"true"` / `"false"`, lowercase.** `_team_signal` lowercases
  and strips before matching against `_TRUE_VALUES` / `_FALSE_VALUES`, so
  case does not strictly matter — but a claim a human reads should not rely
  on the reader's normalization to be legible.
* **No claim at all under Unknown.** An absent claim is the honest "not
  asserted": `_team_signal` returns UNKNOWN when it finds none, which is
  precisely the intended meaning, and it is the only encoding that cannot be
  misread. Emitting a claim with `value="unknown"` would be worse than
  useless — it would be indistinguishable from a malformed `true`/`false`
  in the same `else: return UNKNOWN` fallthrough, while adding a row to the
  user-facing evidence list that says nothing.
* **`history_days` / `history_coverage` on a team-scoped answer are
  COMPANY-WIDE numbers** (see `rli.history.features`' module docstring).
  Board captures have no per-team cadence — `board_snapshots` carries a
  `company_id` and no team column — so "how long have we been watching this
  team" is not a quantity this schema can express. The company-wide window
  is the honest available proxy, and the amendment's `min_history_days` bar
  is applied against it.
* **`source_quality` degrades to `archive`, never up to `ats_native`.**
  Same convention as `rli.probes.repost_history._source_quality`: the weaker
  value wins whenever provenance is not fully first-party. A count claim is
  `archive` only when EVERY posting that contributed to it is archive-only
  (`posting_id` minted by `rli.history.closures.apply_to_postings` as
  `"archive:{company_id}:{job_id}"`), and `ats_native` as soon as one real
  own-captured posting is in the count. The negative claim is judged against
  the whole in-scope posting set instead, because that is what backs it —
  and an EMPTY in-scope set (no postings at all, yet enough history to say
  so) yields `archive`, the weaker label, since there is no first-party
  observation behind the assertion.
* **`source_event_at=None` on every claim.** These are aggregates over a
  window, not single dated events; stamping one with the newest contributing
  posting's timestamp would let a downstream "freshest claim wins" ordering
  treat a 30-day aggregate as a point observation.
* **`source_url` is a self-describing, deliberately non-fetchable
  placeholder** (`TEAM_HISTORY_URL_PLACEHOLDER`), in the style of
  `rli.probes.repost_history.BOARD_HISTORY_URL_PLACEHOLDER` and
  `rli.history.closures.ARCHIVE_ONLY_URL_PLACEHOLDER`. `ProbeClaim.
  source_url` is required, and there is no page anywhere that shows "this
  team's last 30 days"; a plausible-looking board URL would be a fabricated
  citation. `_all_` stands in for the team segment under `scope='company'`.
* **A missing `posting_id` is `ok=False`, everything else is `ok=True`.**
  Matching `rli.probes.repost_history`: a posting that does not exist is a
  caller bug, while thin history is a successful observation of "we cannot
  tell". This probe reads only the local database, so beyond that one case
  there is nothing left that can fail.

Eligibility (spec.md §4)
------------------------
`TeamSignalProbe.history_required` is now `True` — the probe is a history
probe, so `rli.probes.registry.eligible_probes` applies the generic
`has_usable_history` floor to it — and `eligible` re-states the same
`thresholds.min_history_days` bar against `team_activity`, plus the
`[team_signal].enabled` kill switch. Both gates run (see
`rli.probes.registry`'s docstring); the duplication is the module's existing
convention, and it means calling `TeamSignalProbe.eligible` directly is as
safe as going through the registry.

RESOLVED (was a KNOWN GAP): spec.md §4 says `team_signal` "is eligible only
when that input is unknown **and the repost/long-lived branch is
reachable**". Both halves are now enforced:
`rli.policy.inputs.could_change_action` computes the unresolved inputs that
could still move the action by running the real `rli.policy.action._branch`
over an enumeration, and `rli.agent.controller` passes ITS output — not the
raw `unpopulated()` set — as `eligible_probes`' `unpopulated_inputs`. Since
the `skip` branch is the only branch reading `corroborating_hiring_signal`,
"this input could change the action" and "the repost/long-lived branch is
reachable" are the same statement about the same frozen function; see
`rli.agent.controller`'s docstring for the argument in both directions. The
evaluation systems bypass it deliberately and knowingly, not accidentally:
System A passes `ALL_DYNAMIC_INPUTS` to make the gate vacuous (that is its
definition — every available probe), and System B hand-codes the same
condition in `rli.eval.system_b._team_signal_reachable` because it has no
investigator. With `enabled` no longer defaulting to `False`, this matters
in practice rather than in theory.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Protocol

from pydantic import BaseModel

from rli.config import Config
from rli.history.features import TeamActivity, TeamActivityEvent, team_activity
from rli.models.probe import ProbeResult
from rli.models.time import to_utc_z
from rli.probes.base import Probe, ProbeClaim, ProbeContext
from rli.probes.lookups import posting_row

__all__ = [
    "CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE",
    "TEAM_HISTORY_URL_PLACEHOLDER",
    "BoardHistoryTeamSignalSource",
    "NullTeamSignalSource",
    "TeamSignalArgs",
    "TeamSignalProbe",
    "TeamSignalSource",
    "team_signal",
]

# MUST stay byte-for-byte equal to `rli.policy.inputs.CLAIM_TEAM_SIGNAL`.
# Duplicated, not imported, on purpose — see the module docstring. If these
# two literals ever diverge, this probe's boolean claim becomes invisible to
# the only code that reads it and `corroborating_hiring_signal` silently
# stays UNKNOWN forever.
CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE = "corroborating_hiring_signal"

# Self-describing, deliberately non-fetchable source_url scheme (module
# docstring). Mirrors rli.probes.repost_history.BOARD_HISTORY_URL_PLACEHOLDER;
# the literal `/team/` segment keeps it distinguishable from that one.
TEAM_HISTORY_URL_PLACEHOLDER = "board-history:{company_id}/team/{team}"

# Stands in for the team segment when the posting carries no team and
# `team_activity` widened the scope to the whole company.
COMPANY_SCOPE_TEAM_SEGMENT = "_all_"

# How many contributing roles a count claim quotes in its raw_excerpt. The
# excerpt exists to make a number checkable by eye, not to be exhaustive.
_EXCERPT_EVENTS = 5

SourceQuality = Literal["ats_native", "archive"]


class TeamSignalArgs(BaseModel):
    posting_id: str
    company_id: str


class TeamSignalSource(Protocol):
    """The seam an alternate `team_signal` implementation plugs into.

    `BoardHistoryTeamSignalSource` is the default and implements spec.md
    §5's Amendment 2026-09-10 from first-party board history. This Protocol
    stays because the amendment does not forbid a better source: a licensed
    enrichment feed, a per-deployment adapter, or a test double can be
    supplied through `team_signal(..., source=...)` without editing this
    module or any caller.

    Implementations must obey the probe contract: never raise for a network
    or parse problem, return `ProbeResult(ok=False, error=..., retryable=...)`
    instead, and put any `ProbeClaim`s under `data["evidence"]`. To populate
    `corroborating_hiring_signal` an implementation must emit a CLAIM whose
    `claim_type` is `CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE` with `value` in
    `"true"`/`"false"`. That claim is the ONLY thing that can populate the
    policy input: `rli.policy.inputs._team_signal` reads it out of the
    evidence, and nothing reads `data`. Setting
    `data["corroborating_hiring_signal"]` as well is expected — the trace
    and `rli.replay.mode`'s codec want it — but it decides nothing, and an
    implementation that sets ONLY the blob leaves the input UNKNOWN.

    That asymmetry is deliberate and is the fix for security review H4.
    `rli.eval.case.extend_case_state` used to read the blob whenever the
    claim path came back UNKNOWN, which is exactly what replay's
    `available_at <= T` gate produces at an archive-era `T` — so a
    build-time boolean decided pre-`T` cases with no evidence behind it. A
    claim carries `available_at` and can be gated; a blob cannot. A licensed
    source should additionally use `source_quality="enrichment"` (spec.md
    §3's source ordering).
    """

    def fetch(self, args: TeamSignalArgs, ctx: ProbeContext) -> ProbeResult: ...


class NullTeamSignalSource:
    """A source that always reports a structured "no source configured".

    No longer the default (see the module docstring) — it is kept as the
    explicit "this deployment has no team signal" adapter, and as the
    reference for what a source that cannot answer must return.

    Deliberately NOT an empty success. `ok=True` with no claims would read as
    "checked, no corroborating hiring activity", i.e. a known negative, and
    would let `corroborating_hiring_signal` be populated as `False` on the
    strength of a source that was never consulted. `retryable=False` because
    retrying does not configure a source.
    """

    def fetch(self, args: TeamSignalArgs, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(
            ok=False,
            error="no_licensed_source",
            retryable=False,
            data={
                "posting_id": args.posting_id,
                "company_id": args.company_id,
                "evidence": [],
            },
        )


# ---------------------------------------------------------------------------
# Board-history source
# ---------------------------------------------------------------------------


def _event_label(event: TeamActivityEvent) -> str:
    """`"Title (timestamp)"`, falling back to the posting id when untitled.

    A NULL `postings.title` is missing data, not an empty title; naming the
    posting id keeps the excerpt checkable instead of printing `None`.
    """
    title = event.title or f"<untitled {event.posting_id}>"
    return f"{title} ({to_utc_z(event.at)})"


def _events_excerpt(events: tuple[TeamActivityEvent, ...]) -> str:
    excerpt = "; ".join(_event_label(event) for event in events[:_EXCERPT_EVENTS])
    remainder = len(events) - _EXCERPT_EVENTS
    return f"{excerpt} (+{remainder} more)" if remainder > 0 else excerpt


def _events_quality(events: tuple[TeamActivityEvent, ...]) -> SourceQuality:
    """`archive` only when EVERY contributing posting is archive-derived.

    One own-captured posting in the count is enough to make the count itself
    an `ats_native` observation. The empty tuple degrades to `archive`, the
    weaker value, matching `rli.probes.repost_history._source_quality`'s
    convention — it is never reached for a count claim, which is only
    emitted for a non-zero count.
    """
    return "archive" if all(event.archive_only for event in events) else "ats_native"


def _scope_quality(activity: TeamActivity) -> SourceQuality:
    """Provenance of the WHOLE in-scope posting set, for the negative claim.

    A `False` answer rests on every posting we did NOT see move, not on any
    particular event, so it is archive-quality exactly when the entire set is
    archive-derived. An empty set (`0 == 0`) also yields `archive`: there is
    no first-party observation behind the assertion at all, and the rule is
    always to degrade rather than to promote.
    """
    return (
        "archive" if activity.archive_only_postings == activity.in_scope_postings else "ats_native"
    )


def _decide(activity: TeamActivity, cfg: Config) -> bool | None:
    """`True` / `False` / `None` (Unknown) for `corroborating_hiring_signal`.

    spec.md §5's amendment, read literally: activity in EITHER window is a
    signal. The negative needs more than a pair of zeros — it needs the
    zeros to be an observation (deep enough history, dense enough coverage),
    or it would turn a coverage hole into "this team is not hiring".
    """
    settings = cfg.team_signal
    if (
        activity.new_roles_30d >= settings.min_new_roles
        or activity.closures_60d >= settings.min_closures
    ):
        return True
    if (
        activity.new_roles_30d == 0
        and activity.closures_60d == 0
        and activity.history_days >= cfg.thresholds.min_history_days
        and activity.history_coverage >= settings.min_coverage
    ):
        return False
    return None


def _decision_excerpt(activity: TeamActivity, cfg: Config, hiring_signal: bool) -> str:
    """Everything that produced the answer, so a reader need not re-derive it."""
    settings = cfg.team_signal
    scope = (
        f"scope=team team={activity.team!r}"
        if activity.scope == "team"
        else "scope=company (posting carries no team; pooled company-wide)"
    )
    verdict = (
        "hiring activity observed"
        if hiring_signal
        else "no hiring activity, over history deep and dense enough to say so"
    )
    return (
        f"{verdict}: {scope}; "
        f"new_roles_{settings.new_roles_window_days}d={activity.new_roles_30d} "
        f"(min {settings.min_new_roles}); "
        f"closures_{settings.closures_window_days}d={activity.closures_60d} "
        f"(min {settings.min_closures}); "
        f"open_roles_now={activity.open_roles_now}; "
        f"history_days={activity.history_days:.2f} "
        f"(min {cfg.thresholds.min_history_days}); "
        f"history_coverage={activity.history_coverage:.3f} "
        f"(min {settings.min_coverage})"
    )


class BoardHistoryTeamSignalSource:
    """The default source: spec.md §5's amendment, from own + archive snapshots.

    Reads `rli.history.features.team_activity` and turns it into claims. No
    network access, no writes, and no failure mode beyond a `posting_id`
    that does not exist.
    """

    def fetch(self, args: TeamSignalArgs, ctx: ProbeContext) -> ProbeResult:
        now = ctx.now()

        row = posting_row(ctx.conn, args.posting_id)
        if row is None:
            # The one genuine caller error, reported the way
            # `rli.probes.repost_history` reports it.
            return ProbeResult(
                ok=False,
                error=f"no posting row for posting_id={args.posting_id!r}",
                retryable=False,
                data={
                    "posting_id": args.posting_id,
                    "company_id": args.company_id,
                    "evidence": [],
                },
            )

        activity = team_activity(ctx.conn, args.company_id, row["team"], now, ctx.config)
        source_url = TEAM_HISTORY_URL_PLACEHOLDER.format(
            company_id=args.company_id,
            team=activity.team or COMPANY_SCOPE_TEAM_SEGMENT,
        )

        new_roles_quality = _events_quality(activity.new_roles)
        closures_quality = _events_quality(activity.closures)

        claims: list[ProbeClaim] = []
        if activity.new_roles_30d > 0:
            claims.append(
                ProbeClaim(
                    claim_type="team_new_roles",
                    value=str(activity.new_roles_30d),
                    source_url=source_url,
                    raw_excerpt=_events_excerpt(activity.new_roles),
                    source_quality=new_roles_quality,
                    # An aggregate over a window has no single event date.
                    source_event_at=None,
                    available_at=now,
                    fetched_at=now,
                )
            )
        if activity.closures_60d > 0:
            claims.append(
                ProbeClaim(
                    claim_type="team_closures",
                    value=str(activity.closures_60d),
                    source_url=source_url,
                    raw_excerpt=_events_excerpt(activity.closures),
                    source_quality=closures_quality,
                    source_event_at=None,
                    available_at=now,
                    fetched_at=now,
                )
            )

        hiring_signal = _decide(activity, ctx.config)
        if hiring_signal is not None:
            if hiring_signal:
                # Attribute the positive to whichever count actually drove it;
                # if new roles met their minimum they are the evidence, else
                # the closures are.
                quality = (
                    new_roles_quality
                    if activity.new_roles_30d >= ctx.config.team_signal.min_new_roles
                    else closures_quality
                )
            else:
                quality = _scope_quality(activity)
            claims.append(
                ProbeClaim(
                    claim_type=CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE,
                    value="true" if hiring_signal else "false",
                    source_url=source_url,
                    raw_excerpt=_decision_excerpt(activity, ctx.config, hiring_signal),
                    source_quality=quality,
                    source_event_at=None,
                    available_at=now,
                    fetched_at=now,
                )
            )
        # Unknown emits nothing: an absent claim IS the honest "not asserted"
        # (module docstring), and an absent claim now means an UNKNOWN input,
        # full stop -- no reader falls back to the blob. It still stays None
        # rather than False so a trace reader cannot misread "not asserted"
        # as a negative.

        return ProbeResult(
            ok=True,
            data={
                "posting_id": args.posting_id,
                "company_id": args.company_id,
                "team": activity.team,
                "scope": activity.scope,
                "history_days": activity.history_days,
                "history_coverage": activity.history_coverage,
                # Field names carry the amendment's default windows; the
                # windows actually applied are [team_signal].*_window_days.
                "new_roles_30d": activity.new_roles_30d,
                "closures_60d": activity.closures_60d,
                "open_roles_now": activity.open_roles_now,
                # A real bool, or None for Unknown — never a string. For
                # the trace and `rli.replay.mode`'s codec only: NO policy
                # input is derived from it (see the module docstring on H4).
                # The claim above is what decides the input.
                "corroborating_hiring_signal": hiring_signal,
                "evidence": claims,
            },
        )


def team_signal(
    args: TeamSignalArgs, ctx: ProbeContext, *, source: TeamSignalSource | None = None
) -> ProbeResult:
    """Pure function backing `TeamSignalProbe.run` (spec.md §4/§5).

    `source` is injectable so an alternate adapter (or a test double) can be
    supplied without touching this module; it defaults to
    `BoardHistoryTeamSignalSource`, which implements spec.md §5's Amendment
    2026-09-10 from first-party board history.
    """
    return (source or BoardHistoryTeamSignalSource()).fetch(args, ctx)


class TeamSignalProbe(Probe):
    """Dynamic probe: corroborating hiring activity from board history."""

    name: ClassVar[str] = "team_signal"
    cost_tier: ClassVar[str] = "high"
    history_required: ClassVar[bool] = True
    populates: ClassVar[frozenset[str]] = frozenset({"corroborating_hiring_signal"})
    ArgsModel: ClassVar[type[BaseModel]] = TeamSignalArgs

    @classmethod
    def eligible(cls, ctx: ProbeContext, args: TeamSignalArgs) -> bool:
        """Kill switch, posting existence, and the usable-history bar.

        spec.md §5's amendment: "unknown when team history <
        `min_history_days`". Below that bar the probe can only ever return
        Unknown, so running it would spend a `high`-cost step to learn what
        this check already knows. Mirrors `RepostHistoryProbe.eligible`'s
        re-statement of the generic history gate — both gates run
        (`rli.probes.registry`), which is harmless and is the convention here.
        """
        if not ctx.config.team_signal.enabled:
            return False
        row = posting_row(ctx.conn, args.posting_id)
        if row is None:
            return False
        activity = team_activity(ctx.conn, args.company_id, row["team"], ctx.now(), ctx.config)
        return activity.history_days >= ctx.config.thresholds.min_history_days

    def run(self, args: TeamSignalArgs, ctx: ProbeContext) -> ProbeResult:
        return team_signal(args, ctx)
