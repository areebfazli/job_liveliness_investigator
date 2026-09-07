"""System C's deterministic controller: filter, rank, budget, RUN or STOP (spec.md §2/§4).

spec.md §2 draws the line this module defends:

> **Code / learned models:** source trust, timestamps, schemas, permissions,
> caching, budgets, hard stops, probe ranking, calibration, action policy.
>
> Never rely on the LLM alone for budgets, probabilities, stopping,
> permissions, or the final action.

and spec.md §4 numbers the work: steps 4 (`controller filters
invalid/ineligible candidates`), 5 (`controller ranks candidates`) and the
hard-stop list

> - no eligible probe is worth its cost
> - budget/latency/step cap is reached
> - the same probe+arguments would repeat
> - no unresolved question could change the action

`decide` is a pure function of `(investigator output, case state, config,
probe context, budget, executed history)`. It performs no network I/O, calls
no LLM, and writes nothing; the only side-channel is the DB read inside
`rli.probes.registry.eligible_probes` (the history gate). Two calls with the
same arguments return the same `ControllerDecision`, which is what makes
spec.md §6's replay reproducible.

--------------------------------------------------------------------------
Judgment call: the model's arguments are validated, then thrown away
--------------------------------------------------------------------------

`ProbeCandidate.args` is validated against `probe_cls.ArgsModel`
(spec.md §2: "Pydantic-validate probe arguments") and then DISCARDED. The
arguments actually used come from `rli.probes.registry.build_args`, whose
own docstring states the contract: "the controller never has to trust
LLM-proposed argument values to run a probe — it validates the proposal and
rebuilds the args itself".

Two questions follow, and they have different answers.

*Why validate at all, if the value is discarded?* Because spec.md §6 lists
"invalid arguments" as an agent-efficiency metric. The validation is a
measurement of model quality, and it only measures anything if it is
actually performed. Dropping it would silently zero out one of the numbers
the C-vs-B comparison is made of.

*Why reject the candidate instead of quietly substituting the canonical
args?* Because silently repairing a malformed proposal is the same as not
measuring it: every invalid-argument event would be recorded as a
successful probe run, and the metric would read zero on a model that never
gets the arguments right. Rejecting also matches spec.md §2's placement of
"permissions" on the deterministic side — a proposal the controller could
not validate is not a proposal it should act on. The cost of the strictness
is bounded: the model is free to propose the same probe again on the next
step, and the loop's step cap bounds how long it may keep doing so.

--------------------------------------------------------------------------
Judgment call: `could_change` as `unpopulated_inputs` is exact, not an
approximation
--------------------------------------------------------------------------

`eligible_probes(ctx, case_file, unpopulated_inputs=could_change)` is the
single eligibility call, and `could_change` is
`rli.policy.inputs.could_change_action(...)` — not `PolicyInputs.
unpopulated()`. That one substitution makes the call enforce every gate
spec.md §4 states, including the one `rli.probes.team_signal`'s docstring
records as a KNOWN GAP:

> spec.md §4 says `team_signal` "is eligible only when that input is
> unknown **and the repost/long-lived branch is reachable**". The first half
> is enforced generically by `eligible_probes` (`populates ∩ unpopulated`);
> the second half is a statement about the spec.md §5 action policy [...]

Both halves reduce to one membership test. `could_change_action` returns the
unresolved inputs that could still change the recommended action, computed
by running the real policy branch function over an enumeration of the
unresolved inputs. So:

* `corroborating_hiring_signal ∈ could_change` implies it is unpopulated —
  `could_change_action` only ever returns members of
  `inputs.unpopulated()`. That is the "input is unknown" half.
* `corroborating_hiring_signal ∈ could_change` also implies there exists a
  reachable assignment of the other unresolved inputs under which its value
  moves the action. The ONLY branch of spec.md §5's policy that reads
  `corroborating_hiring_signal` is the repost/long-lived `skip` branch, so
  "some assignment makes this input matter" and "the repost/long-lived
  branch is reachable" are the same statement about the same frozen
  function. That is the "branch is reachable" half.
* Conversely, if that branch is unreachable, no assignment makes the input
  matter, and `could_change_action` excludes it — so the test is not merely
  sufficient, it is exact in both directions.

`rli.eval.system_b._team_signal_reachable` spells the same condition out by
hand (`enabled` AND unknown AND `repost_pattern == "repeated_unchanged"`
AND `long_lived is True`) because System B has no investigator and must
route without computing `could_change_action`. C computes it, so C states
the rule once, through the policy itself, and cannot drift from it.

The remaining conjunct — `[team_signal].enabled` — is not in
`could_change`; it is `TeamSignalProbe.eligible`'s own gate, applied by the
same `eligible_probes` call. Hence: one call, all gates. Nothing about
`team_signal` is special-cased in this module, and a test asserts both
directions of the licence flag.

--------------------------------------------------------------------------
Judgment call: only the BEST-ranked survivor is budget-checked
--------------------------------------------------------------------------

When the best candidate does not fit the remaining budget, the loop stops.
A cheaper survivor is NOT substituted, even though one may fit.

spec.md §4's hard stop is "no eligible probe is worth its cost", and the
ranking is the definition of "worth its cost": `score = value / cost`. The
best-ranked candidate is by construction the one with the most policy value
per dollar. Falling back to a cheaper probe would mean spending the tail of
the budget on the option the ranking already judged to be the worse deal —
buying less information for a worse rate, at the exact moment the budget is
tightest. It would also make the stop condition non-monotone in the budget
(shrinking the budget could change *which* probe runs rather than whether
one runs), which is a poor property for a system whose whole evaluation is
a cost comparison against B.

The cheap-substitution policy is not obviously wrong — it is a different,
defensible design — but it is a *ranking* change, and spec.md §4 freezes v1
ranking as "deterministic cost-aware ranking over valid candidates". If it
is wanted, it belongs in `score_candidate`, where the evaluation can see it.

--------------------------------------------------------------------------
Judgment call: the agent keeps its OWN single-unit USD ledger
--------------------------------------------------------------------------

`Budget` does not read `rli.eval.runner.Run.total_cost_usd`, and that is
deliberate. `rli.eval.runner`'s module docstring is explicit that its
`cost_usd` column is "a placeholder UNITLESS COST POINT, not a dollar": a
probe step writes `[probe_costs]` points (`low=1`, `medium=3`, `high=10`)
into it. System C also spends real dollars on LLM calls
(`LLMResponse.cost_usd`, computed from `[llm].prices`). A single accumulator
holding both would be adding cost points to dollars, and every budget
comparison against it would be meaningless.

So the agent's ledger is kept in ONE unit — dollars — and probe cost points
are converted into it by `probe_cost_usd` at a PLACEHOLDER, unmeasured rate
(`[agent].probe_cost_usd_per_point`). `Run.total_cost_usd` keeps its
existing meaning for the trace and for `rli.eval.report`'s cross-system
cost comparison, which compares A, B and C on the same point scale; the
agent's `Budget` is a separate, correctly-united quantity used only to
enforce spec.md §4's caps.

`probe_cost_usd` is three terms, and spec.md §4's ranking-lifecycle
paragraph names all three ("Combine predicted value with measured money
cost, latency, and failure rate"):

* `cost_points * probe_cost_usd_per_point` — the money term. Placeholder
  scale (see above).
* `latency_estimate_s * latency_cost_usd_per_s` — the latency term, which
  prices a slow probe against a fast one inside a single number so the
  ranking does not need a second, incomparable sort key. The rate is a
  PLACEHOLDER.
* `failure_rate_placeholder * failure_cost_usd` — the failure term. It is
  currently a CONSTANT for every probe, so today it shifts all scores by
  the same amount and cannot change the ranking; it exists so the shape of
  the formula is right and so replacing the constant with a per-probe
  measured rate (which spec.md §4 asks for once replay data exists) is a
  one-line change rather than a redesign of the score.

The first term is strictly positive (`probe_cost_usd_per_point` is
`gt=0.0` and every `[probe_costs]` tier is `gt=0`), so `cost_usd > 0` and
`value / cost_usd` can never divide by zero.

--------------------------------------------------------------------------
Judgment call: a malformed investigator output STOPS the loop
--------------------------------------------------------------------------

`output is None` (the loop could not validate the model's reply, or the
call failed) is a stop, reason `investigator_error`. It does NOT fall back
to System B's deterministic routing tree.

The temptation is real — B's tree would produce a decent probe set from the
same case state, and the run would finish with a better decision. That is
exactly why it must not happen. spec.md §6 makes C's claim empirical: "C is
worth keeping if it materially lowers investigation cost while preserving
A-like decisions", measured against a frozen B. A C that silently becomes B
whenever its model misbehaves would report B's agreement and B's cost as
C's own, on precisely the cases where C failed. The comparison would be
uninterpretable and biased in C's favour. A stop is the honest outcome: the
run still produces an action from the frozen policy over whatever evidence
the always-run pair gathered, the failure is visible in the trace, and the
metric measures what actually happened.

--------------------------------------------------------------------------
Where the decision detail goes
--------------------------------------------------------------------------

`ControllerDecision.reason` is a bare `StopReason` / `"ranked_best"` token
and nothing else, because `rli.agent.loop` concatenates it into a
`run_steps.decision_type` value (`controller_decision:<decision>:<reason>`)
that metrics queries group by. Free-text detail therefore travels in
`rejected`, which the loop serializes into the row's `error` column.

One consequence, documented rather than hidden: a stop that carries an
explanation but concerns no particular probe (today only
`investigator_error`) records it as a pseudo-rejection whose `probe` is the
empty string. `rejected` is the only free-form channel on this object, and
inventing a second one for a single case would be worse than the
convention.

`RejectReason` includes `"step_cap"`, which the current implementation
never emits: the step cap is checked as a whole-decision stop (order rule
2) before any candidate is examined, so no individual candidate is ever
rejected for it. It is kept in the literal so the reject vocabulary and the
stop vocabulary line up in the trace; the alternative (dropping it) would
make a future per-candidate step accounting a schema change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from rli.agent.investigator import ExecutedProbe, InvestigatorOutput
from rli.config import Config
from rli.eval.case import CaseState
from rli.net.client import hash_args
from rli.probes.base import Probe, ProbeContext
from rli.probes.registry import (
    DYNAMIC_PROBES,
    build_args,
    cost_value,
    eligible_probes,
    latency_estimate_s,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.llm.client import LLMResponse

__all__ = [
    "Budget",
    "ControllerDecision",
    "RankedCandidate",
    "RejectReason",
    "RejectedCandidate",
    "StopReason",
    "decide",
    "probe_cost_usd",
    "rank_candidates",
    "score_candidate",
]

RejectReason = Literal[
    "unknown_probe",
    "invalid_args",
    "ineligible",
    "duplicate",
    "no_useful_input",
    "budget_cost",
    "budget_latency",
    "step_cap",
    "over_candidate_limit",
]

StopReason = Literal[
    "investigator_stop",
    "investigator_error",
    "no_unresolved_question",
    "no_eligible_candidate",
    "step_cap",
    "cost_cap",
    "latency_cap",
]


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """One candidate the controller refused, and why."""

    probe: str
    reason: RejectReason
    detail: str


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One surviving candidate, with the arguments that would actually run.

    `args` is the CANONICAL `rli.probes.registry.build_args` value, never
    the model's proposal (see the module docstring), and `args_hash` is the
    hash of exactly those arguments — so it is directly comparable with an
    `ExecutedProbe.args_hash` from an earlier step.
    """

    probe_cls: type[Probe]
    args: BaseModel
    args_hash: str
    value: int
    cost_usd: float
    score: float


class ControllerDecision(BaseModel):
    """The controller's verdict for one loop step, plus its full reasoning.

    Everything the trace needs is here: what was looked at (`considered`),
    what was refused and why (`rejected`), how the survivors ranked
    (`ranking`), and what happens next (`decision` / `reason` /
    `chosen_probe` / `chosen_args_hash`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Literal["run", "stop"]
    reason: str
    chosen_probe: str | None = None
    chosen_args_hash: str | None = None
    considered: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str, str], ...] = ()
    ranking: tuple[tuple[str, float], ...] = ()


# ---------------------------------------------------------------------------
# Cost model and budget ledger
# ---------------------------------------------------------------------------


def probe_cost_usd(probe_cls: type[Probe], cfg: Config) -> float:
    """This probe's estimated all-in cost in DOLLARS (see the module docstring).

    `cost_points * [agent].probe_cost_usd_per_point`
    `+ latency_estimate_s * [agent].latency_cost_usd_per_s`
    `+ [agent].failure_rate_placeholder * [agent].failure_cost_usd`

    Every rate is a documented PLACEHOLDER pending measurement from real
    runs, and the failure term is currently probe-independent. Strictly
    positive, so it is always a safe divisor.
    """
    agent = cfg.agent
    return (
        cost_value(probe_cls, cfg) * agent.probe_cost_usd_per_point
        + latency_estimate_s(probe_cls, cfg) * agent.latency_cost_usd_per_s
        + agent.failure_rate_placeholder * agent.failure_cost_usd
    )


@dataclass(frozen=True, slots=True)
class Budget:
    """The agent's own USD / second / step ledger.

    Immutable: `with_llm` and `with_probe` return a NEW `Budget`. The loop
    therefore cannot lose a charge to an early `break`, and a
    `ControllerDecision` can be re-derived from the exact `Budget` it saw.

    See the module docstring for why this is a separate ledger from
    `rli.eval.runner.Run.total_cost_usd` and why every number here is in
    dollars.
    """

    max_cost_usd: float
    max_latency_s: float
    max_dynamic_steps: int
    spent_cost_usd: float = 0.0
    spent_latency_s: float = 0.0
    dynamic_steps: int = 0

    @classmethod
    def from_config(cls, cfg: Config) -> Budget:
        """A fresh ledger from `[agent]`, falling back to `[budgets]`/`[thresholds]`."""
        agent = cfg.agent
        return cls(
            max_cost_usd=agent.effective_max_cost_usd(cfg),
            max_latency_s=agent.effective_max_latency_s(cfg),
            max_dynamic_steps=agent.effective_max_dynamic_steps(cfg),
        )

    def remaining_cost_usd(self) -> float:
        return self.max_cost_usd - self.spent_cost_usd

    def remaining_latency_s(self) -> float:
        return self.max_latency_s - self.spent_latency_s

    def remaining_steps(self) -> int:
        """Dynamic probe steps left (spec.md §4: "at most 4 dynamic probe steps").

        Only PROBE executions consume a step. An investigator call does not:
        the step cap bounds spend on the world, and an LLM call is already
        bounded by the cost cap it charges against. Clamped at zero: a
        negative remainder would still fail the `> 0` test correctly, but it
        would be reported into the investigator prompt and the trace as a
        negative number of steps, which reads as a bug.
        """
        return max(self.max_dynamic_steps - self.dynamic_steps, 0)

    def with_llm(self, response: LLMResponse) -> Budget:
        """Charge one LLM call: real dollars, and its measured latency.

        A cache hit costs `0.0` and its (tiny) measured lookup latency —
        `rli.llm.client.CachedClient` sets both — so replay does not consume
        the budget of the run it is replaying.
        """
        return replace(
            self,
            spent_cost_usd=self.spent_cost_usd + response.cost_usd,
            spent_latency_s=self.spent_latency_s + response.latency_ms / 1000.0,
        )

    def with_probe(self, probe_cls: type[Probe], cfg: Config) -> Budget:
        """Charge one probe execution: ESTIMATED cost and latency, plus a step.

        Estimates rather than measurements, deliberately and symmetrically
        with the pre-flight check in `decide`: the budget test that
        authorized this probe used `probe_cost_usd` / `latency_estimate_s`,
        so charging the same numbers keeps the ledger consistent with the
        gate. Charging measured wall-clock latency instead would let a probe
        that ran faster than its estimate authorize a step the controller
        had already decided was unaffordable, which is the sort of
        after-the-fact budget drift spec.md §4's caps exist to prevent.
        """
        return replace(
            self,
            spent_cost_usd=self.spent_cost_usd + probe_cost_usd(probe_cls, cfg),
            spent_latency_s=self.spent_latency_s + latency_estimate_s(probe_cls, cfg),
            dynamic_steps=self.dynamic_steps + 1,
        )

    def with_probe_retry(self, probe_cls: type[Probe], cfg: Config) -> Budget:
        """Charge one RE-execution of a probe already charged a step: money, not a step.

        `rli.agent.loop` retries a probe at most `[agent].max_probe_retries`
        times, and only while the last result was a RETRYABLE structured
        failure (spec.md §2: `{ok:false,error,retryable}`, "no uncontrolled
        retry loops"). This is how that retry is paid for.

        **The asymmetry with `with_probe` is the point, not an oversight.**
        The two caps in spec.md §4's hard-stop list bound different things:

        * the STEP cap ("Start with at most `4` dynamic probe steps") bounds
          how many distinct investigations the agent may open. Re-running
          `repost_history` after a transient failure does not open a fifth
          question; it re-asks the fourth. Charging it a step would mean a
          run that met one flaky probe silently investigated LESS than an
          identical run that did not, and spec.md §6 would see that as a
          worse decision rather than as the cost it actually is — which is
          precisely the "recovery after failures" metric it asks us to
          measure.
        * the COST and LATENCY caps bound spend on the world. A retry is a
          second real call: a second HTTP request, a second wait, a second
          charge. So it is charged in full, from the same `probe_cost_usd` /
          `latency_estimate_s` estimates `with_probe` uses, for the same
          reason (the gate and the ledger must price a probe identically).

        Together with the hard retry count in `[agent].max_probe_retries`,
        that makes retrying bounded twice over: by attempts, and by budget.

        Deliberately a `Budget` method rather than a `dataclasses.replace`
        call in the loop. The ledger is this class's invariant; a caller that
        reached in to adjust two of its six fields would be free to get the
        step accounting wrong, and the reasoning above would live in a
        comment at a call site instead of next to the field it governs.
        """
        return replace(
            self,
            spent_cost_usd=self.spent_cost_usd + probe_cost_usd(probe_cls, cfg),
            spent_latency_s=self.spent_latency_s + latency_estimate_s(probe_cls, cfg),
            # dynamic_steps deliberately unchanged — see the docstring.
        )


# ---------------------------------------------------------------------------
# Ranking (spec.md §4: "v1: deterministic cost-aware ranking")
# ---------------------------------------------------------------------------


def score_candidate(probe_cls: type[Probe], cfg: Config, could_change: set[str]) -> float:
    """Policy value per dollar: `|populates ∩ could_change| / probe_cost_usd`.

    The value term counts the OPEN QUESTIONS THAT MATTER this probe can
    answer — `could_change_action`'s output, not the raw unpopulated set —
    so a probe that would populate an input which cannot move the action
    scores zero and is not worth any cost. spec.md §4 fixes v1 at
    "deterministic cost-aware ranking over valid candidates"; the learned
    estimator it describes as an optional later upgrade would replace the
    numerator with a calibrated probability, leaving the denominator and
    this call site unchanged.
    """
    populates: frozenset[str] = getattr(probe_cls, "populates", frozenset())
    return len(populates & could_change) / probe_cost_usd(probe_cls, cfg)


def rank_candidates(candidates: Sequence[RankedCandidate]) -> list[RankedCandidate]:
    """Best first, by `(-score, probe name)`.

    The name tiebreak is not cosmetic: without it the order of two
    equally-scored candidates would be the model's proposal order, i.e. an
    LLM would be choosing which probe runs. That is exactly the "probe
    ranking" responsibility spec.md §2 assigns to code.
    """
    return sorted(candidates, key=lambda item: (-item.score, item.probe_cls.name))


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


def _stop(
    reason: StopReason,
    *,
    considered: Sequence[str] = (),
    rejected: Sequence[RejectedCandidate] = (),
    ranking: Sequence[tuple[str, float]] = (),
    notes: Sequence[tuple[str, str, str]] = (),
) -> ControllerDecision:
    """Build a stop decision.

    `notes` are raw `(probe, reason, detail)` triples appended to
    `rejected`. They exist for the pseudo-rejection convention described in
    the module docstring: a stop that carries explanatory text but concerns
    no candidate. They are deliberately NOT `RejectedCandidate`s, because
    their `reason` is a `StopReason`, not a `RejectReason`, and widening
    `RejectReason` to accommodate them would blur the two vocabularies.
    """
    return ControllerDecision(
        decision="stop",
        reason=reason,
        considered=tuple(considered),
        rejected=tuple((item.probe, item.reason, item.detail) for item in rejected)
        + tuple(notes),
        ranking=tuple(ranking),
    )


def decide(
    *,
    output: InvestigatorOutput | None,
    case: CaseState,
    cfg: Config,
    ctx: ProbeContext,
    now: datetime,
    could_change: set[str],
    executed: Sequence[ExecutedProbe],
    budget: Budget,
    investigator_error: str | None = None,
) -> ControllerDecision:
    """Decide what System C does next: run one probe, or stop.

    Deterministic, first match wins, in this order (spec.md §4's hard-stop
    list first, so a stop that is true regardless of what the model said is
    never overridden by what the model said):

    1. `could_change` empty -> `no_unresolved_question`.
    2. no steps left -> `step_cap`.
    3. `output is None` -> `investigator_error` (see the module docstring
       for why this is not a fallback to System B's routing).
    4. `output.stop` -> `investigator_stop`. Checked LAST of the four
       because it is the only one that is the model's opinion: the three
       above are facts, and a model that asks to continue must not be able
       to talk the controller past a cap.
    5. filter the candidates (see below).
    6. no survivors -> `no_eligible_candidate`; else budget-check the best.
    7. run the best.

    The per-candidate filter runs in the order spec.md §2/§4 imply, cheapest
    and most decisive first: unknown name, unresolvable identity, invalid
    arguments, ineligible, duplicate, no useful input. `no_useful_input` is
    belt-and-braces — `eligible_probes` already enforces
    `populates ∩ could_change` — and is kept because a future change to the
    registry's gate set must not be able to silently let a valueless probe
    through the controller.

    Arguments:
        now: the decision clock. Carried for signature symmetry with
            `rli.agent.investigator.build_investigator_input` and for the
            trace; argument construction deliberately reads `ctx.now()`
            instead, because that is the clock the probe execution itself
            will see and the canonical `args_hash` must match it.
        investigator_error: free text explaining a `None` output. Recorded
            as a pseudo-rejection, never folded into `reason` — see the
            module docstring.
    """
    del now  # documented above: `ctx.now()` is the clock args are built from

    # --- spec.md §4 hard stops that do not depend on the model at all -----
    if not could_change:
        return _stop("no_unresolved_question")

    if budget.remaining_steps() <= 0:
        return _stop("step_cap")

    if output is None:
        return _stop(
            "investigator_error",
            notes=[
                (
                    "",
                    "investigator_error",
                    investigator_error or "investigator produced no valid output",
                )
            ],
        )

    if output.stop:
        return _stop("investigator_stop")

    # --- step 5: filter -----------------------------------------------------
    limit = cfg.agent.max_candidates
    head = list(output.candidates[:limit])
    overflow = list(output.candidates[limit:])

    considered = [candidate.probe for candidate in head]
    rejected: list[RejectedCandidate] = [
        RejectedCandidate(
            probe=candidate.probe,
            reason="over_candidate_limit",
            detail=f"only the first {limit} candidates are examined",
        )
        for candidate in overflow
    ]

    case_file = case.case_file()
    # ONE eligibility call, all gates. See the module docstring for why
    # `could_change` (not `unpopulated`) is the right `unpopulated_inputs`.
    eligible_names: set[str] = (
        set()
        if case_file is None
        else {
            probe_cls.name
            for probe_cls in eligible_probes(
                ctx, case_file, unpopulated_inputs=set(could_change)
            )
        }
    )

    executed_keys = {(item.probe, item.args_hash) for item in executed}
    # A batch can name the same probe twice; the canonical args are identical
    # by construction, so the second naming IS spec.md §4's "the same
    # probe+arguments would repeat".
    proposed_keys: set[tuple[str, str]] = set()

    survivors: list[RankedCandidate] = []
    for candidate in head:
        name = candidate.probe
        probe_cls = DYNAMIC_PROBES.get(name)
        if probe_cls is None:
            rejected.append(
                RejectedCandidate(
                    probe=name,
                    reason="unknown_probe",
                    detail=f"not a dynamic probe; known: {sorted(DYNAMIC_PROBES)}",
                )
            )
            continue

        if case_file is None:
            rejected.append(
                RejectedCandidate(
                    probe=name, reason="ineligible", detail="identity_unresolved"
                )
            )
            continue

        # spec.md §2: "Pydantic-validate probe arguments". The validated
        # object is discarded on purpose (module docstring).
        try:
            probe_cls.ArgsModel.model_validate(candidate.args)
        except ValidationError as exc:
            rejected.append(
                RejectedCandidate(
                    probe=name,
                    reason="invalid_args",
                    detail=_compact_validation_error(exc),
                )
            )
            continue

        if name not in eligible_names:
            rejected.append(
                RejectedCandidate(
                    probe=name,
                    reason="ineligible",
                    detail="registry.eligible_probes rejected it for this case state",
                )
            )
            continue

        canonical = build_args(probe_cls, case_file, ctx)
        args_hash = hash_args(name, **canonical.model_dump(mode="json"))

        if (name, args_hash) in executed_keys:
            rejected.append(
                RejectedCandidate(
                    probe=name,
                    reason="duplicate",
                    detail=f"already executed with args_hash {args_hash[:12]}",
                )
            )
            continue

        if (name, args_hash) in proposed_keys:
            rejected.append(
                RejectedCandidate(
                    probe=name,
                    reason="duplicate",
                    detail="proposed twice in the same investigator output",
                )
            )
            continue

        populates: frozenset[str] = getattr(probe_cls, "populates", frozenset())
        value = len(populates & could_change)
        if value == 0:  # pragma: no cover - unreachable via eligible_probes
            rejected.append(
                RejectedCandidate(
                    probe=name,
                    reason="no_useful_input",
                    detail="populates nothing that could change the action",
                )
            )
            continue

        proposed_keys.add((name, args_hash))
        cost = probe_cost_usd(probe_cls, cfg)
        survivors.append(
            RankedCandidate(
                probe_cls=probe_cls,
                args=canonical,
                args_hash=args_hash,
                value=value,
                cost_usd=cost,
                score=value / cost,
            )
        )

    # --- steps 6 and 7: rank, budget-check the best, run --------------------
    ranked = rank_candidates(survivors)
    ranking = tuple((item.probe_cls.name, item.score) for item in ranked)

    if not ranked:
        return _stop(
            "no_eligible_candidate", considered=considered, rejected=rejected, ranking=ranking
        )

    best = ranked[0]
    best_latency_s = latency_estimate_s(best.probe_cls, cfg)

    if budget.spent_cost_usd + best.cost_usd > budget.max_cost_usd:
        rejected.append(
            RejectedCandidate(
                probe=best.probe_cls.name,
                reason="budget_cost",
                detail=(
                    f"{best.cost_usd:.6f} usd would exceed the remaining "
                    f"{budget.remaining_cost_usd():.6f}"
                ),
            )
        )
        return _stop("cost_cap", considered=considered, rejected=rejected, ranking=ranking)

    if budget.spent_latency_s + best_latency_s > budget.max_latency_s:
        rejected.append(
            RejectedCandidate(
                probe=best.probe_cls.name,
                reason="budget_latency",
                detail=(
                    f"{best_latency_s:.3f}s would exceed the remaining "
                    f"{budget.remaining_latency_s():.3f}s"
                ),
            )
        )
        return _stop("latency_cap", considered=considered, rejected=rejected, ranking=ranking)

    return ControllerDecision(
        decision="run",
        reason="ranked_best",
        chosen_probe=best.probe_cls.name,
        chosen_args_hash=best.args_hash,
        considered=tuple(considered),
        rejected=tuple((item.probe, item.reason, item.detail) for item in rejected),
        ranking=ranking,
    )


def _compact_validation_error(exc: ValidationError) -> str:
    """`ValidationError` as one short line for the trace.

    `str(exc)` is multi-line and embeds a docs URL; this keeps only
    `<location>: <message>` per error, which is what an operator reading a
    `run_steps.error` column needs. Capped at three errors so a wildly
    wrong proposal cannot dominate the row.
    """
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location: tuple[Any, ...] = error.get("loc", ())
        where = ".".join(str(part) for part in location) or "<root>"
        parts.append(f"{where}: {error.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid arguments"
