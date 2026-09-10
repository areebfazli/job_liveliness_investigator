"""Dynamic-probe registry: eligibility filtering + cost ranking (PLAN.md M3).

This is steps 4 and 5 of spec.md §4's agent loop — "controller filters
invalid/ineligible candidates" and "controller ranks candidates" — as pure,
deterministic code. It contains no LLM call and no network access: the
investigator proposes candidates, this module decides which of them may run
and in what order.

Scope: the FOUR dynamic probes of spec.md §4's "Dynamic probes" table only.
`resolve_posting` and `board_snapshot` are in the "Always run" table; they
are never ranked candidates, so registering them here would let the
controller "choose" work that has already happened unconditionally.

Eligibility rule (spec.md §4, verbatim)
---------------------------------------
    An **unresolved question** is a policy input (§5) that is still
    unpopulated. The controller computes the set of unpopulated policy
    inputs; a candidate probe is eligible only if it can populate at least
    one of them. This makes the stop rule deterministic and testable.

`eligible_probes` implements exactly that as `populates & unpopulated_inputs`,
plus the two other gates the same section states:

* "history probes are ineligible without usable history" — applied
  generically from `Probe.history_required` and
  `rli.probes.lookups.has_usable_history`, so it holds for any future
  history-gated probe even if that probe defines no `eligible` of its own;
* each probe class's own optional `eligible(ctx, args)` classmethod, which
  is where a probe states any gate only it knows about (`TeamSignalProbe`
  reads `[team_signal].enabled`; the two history probes re-state the
  history rule). Delegating rather than special-casing `TeamSignalProbe` by
  name here keeps one source of truth per probe: a new gate is added to the
  probe, and this module needs no edit.

Both gates are applied, not one or the other — the generic history gate is
a floor that a probe's own `eligible` can tighten but never loosen.

Ranking (v1)
------------
spec.md §4 "Probe ranking lifecycle" says: "**v1:** deterministic cost-aware
ranking over valid candidates", with a learned estimator listed only as an
*optional* later upgrade to be removed if it does not beat the deterministic
baseline. So v1 sorts by `(cost_value, name)` ascending: cheapest first,
ties broken alphabetically. The name tiebreak is not cosmetic — it makes the
order independent of `DYNAMIC_PROBES`'s insertion order and of set/dict
iteration, which is what "deterministic and testable" requires. The
candidate list is likewise iterated in sorted-name order BEFORE filtering,
so nothing about the output depends on how this dict happens to be written.

`cost_value` / `latency_estimate_s` read `[probe_costs]` (`rli.config.
ProbeCosts`), whose numbers are documented placeholders pending measurement
from real runs; the controller checks their running totals against
`[budgets].max_cost_usd` / `max_latency_s`.
"""

from __future__ import annotations

from pydantic import BaseModel

from rli.config import Config
from rli.models.case_file import CaseFile
from rli.probes.base import Probe, ProbeContext
from rli.probes.company_events import CompanyEventsArgs, CompanyEventsProbe
from rli.probes.lookups import has_usable_history
from rli.probes.repost_history import RepostHistoryArgs, RepostHistoryProbe
from rli.probes.requirements_drift import RequirementsDriftArgs, RequirementsDriftProbe
from rli.probes.team_signal import TeamSignalArgs, TeamSignalProbe

__all__ = [
    "DYNAMIC_PROBES",
    "build_args",
    "cost_value",
    "eligible_probes",
    "latency_estimate_s",
]

# The spec.md §4 "Dynamic probes" table, and only that table.
DYNAMIC_PROBES: dict[str, type[Probe]] = {
    "repost_history": RepostHistoryProbe,
    "requirements_drift": RequirementsDriftProbe,
    "company_events": CompanyEventsProbe,
    "team_signal": TeamSignalProbe,
}


def cost_value(probe_cls: type[Probe], config: Config) -> int:
    """This probe's numeric cost from `[probe_costs]` (spec.md §4 cost column)."""
    return config.probe_costs.value_for(probe_cls.cost_tier)


def latency_estimate_s(probe_cls: type[Probe], config: Config) -> float:
    """This probe's rough wall-clock latency estimate from `[probe_costs]`."""
    return config.probe_costs.latency_for(probe_cls.cost_tier)


def build_args(probe_cls: type[Probe], case_state: CaseFile, ctx: ProbeContext) -> BaseModel:
    """Construct `probe_cls.ArgsModel` from the current case state.

    Every dynamic probe's arguments are fully determined by the case file
    (plus the clock, for `company_events`' replay boundary), so the
    controller never has to trust LLM-proposed argument values to run a
    probe — it validates the proposal and rebuilds the args itself
    (spec.md §2: "Pydantic-validate probe arguments").
    """
    if probe_cls is RepostHistoryProbe:
        return RepostHistoryArgs(posting_id=case_state.posting_id)
    if probe_cls is RequirementsDriftProbe:
        return RequirementsDriftArgs(posting_id=case_state.posting_id)
    if probe_cls is CompanyEventsProbe:
        return CompanyEventsArgs(company_id=case_state.company_id, as_of=ctx.now())
    if probe_cls is TeamSignalProbe:
        return TeamSignalArgs(posting_id=case_state.posting_id, company_id=case_state.company_id)
    # Defensive: unreachable for anything in DYNAMIC_PROBES. Raising beats
    # returning a plausible-looking default, which would run a probe with
    # arguments nobody chose.
    raise ValueError(f"no argument builder for probe class {probe_cls!r}")


def eligible_probes(
    ctx: ProbeContext, case_state: CaseFile, unpopulated_inputs: set[str]
) -> list[type[Probe]]:
    """The runnable dynamic probes for this case, cheapest first.

    See the module docstring for the three gates and the ranking rule. The
    result is a deterministic function of `(config, db state, case_state,
    unpopulated_inputs)`: calling it twice with the same inputs returns the
    same list in the same order.
    """
    eligible: list[type[Probe]] = []

    # Sorted-by-name iteration BEFORE filtering, so the candidate order never
    # depends on DYNAMIC_PROBES' insertion order.
    for name in sorted(DYNAMIC_PROBES):
        probe_cls = DYNAMIC_PROBES[name]

        # spec.md §4: eligible only if it can populate an unresolved question.
        if not (probe_cls.populates & unpopulated_inputs):  # type: ignore[attr-defined]
            continue

        # spec.md §4: history probes are ineligible without usable history.
        if probe_cls.history_required and not has_usable_history(
            ctx.conn, ctx.config, case_state.company_id
        ):
            continue

        # Probe-owned gates (licensing, posting existence, ...).
        probe_eligible = getattr(probe_cls, "eligible", None)
        if probe_eligible is not None and not probe_eligible(
            ctx, build_args(probe_cls, case_state, ctx)
        ):
            continue

        eligible.append(probe_cls)

    eligible.sort(key=lambda cls: (cost_value(cls, ctx.config), cls.name))
    return eligible
