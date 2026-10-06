"""System C's bounded agent loop — the driver that spends the budget (spec.md §2/§4).

spec.md §2 draws System C as a pipeline and spec.md §4 numbers it. This
module is steps 3-10 of that numbered list, and nothing else:

```text
1. resolver + board snapshot            <- rli.eval.case.build_case_state
2. build case state                     <- rli.eval.case.build_case_state
3. investigator -> ... or STOP          <- rli.agent.investigator + rli.llm
4. controller filters candidates        <- rli.agent.controller.decide
5. controller ranks candidates          <- rli.agent.controller.decide
6. execute best probe                   <- rli.eval.case.extend_case_state
7. append evidence                      <- rli.eval.case.extend_case_state
8. repeat until STOP / budget / step cap <- THIS MODULE
9. fixed action policy                  <- rli.eval.runner.decide_and_finish
10. evidence-cited explanation          <- rli.agent.explanation.explain
```

Read that column twice: almost every line points somewhere else. The loop
owns the *sequencing*, the *ledger* and the *trace*, and it owns nothing
else. It does not choose a probe (the controller does), it does not build
arguments (`rli.probes.registry.build_args` does), it does not execute a
probe (`rli.eval.case.extend_case_state` does, exactly as it does for
System A and System B), and it does not pick the action (`decide_and_finish`
does, from the one frozen policy spec.md §6 requires all systems to share).

That delegation is the whole design. spec.md §6 makes the A/B/C comparison
meaningful only if "the ONLY thing that differs between systems is which
probes they choose to run" (`rli.eval.runner`); a hand-rolled probe
execution here would silently make a probe behave differently under C than
under A, and the resulting agreement number would be measuring the
divergence rather than the agent.

--------------------------------------------------------------------------
JUDGMENT CALL: the pre-flight `decide(output=None)` — a stop we can know
before we pay for it
--------------------------------------------------------------------------

Every iteration calls `rli.agent.controller.decide` TWICE: once before the
model call with `output=None`, and once after it with the model's real
output. The first call costs nothing and is not a duplication of controller
logic — it is a *query* of it.

`decide` checks its THREE model-independent hard stops FIRST (spec.md §4's
"no unresolved question could change the action", "step cap is reached",
and — as of the paragraph below — "no eligible probe is worth its
cost"/"the same probe+arguments would repeat"), and only then notices
`output is None`. So the pre-flight has exactly two possible outcomes:

* it returns a stop whose reason is anything **other than**
  `investigator_error` — that stop is a fact about the case and the ledger,
  true no matter what a model would have replied. The loop honours it,
  records it, and breaks WITHOUT calling the LLM.
* it returns `investigator_error` — which here means only "no hard stop
  fired". The pre-flight decision is discarded and the real work begins.

The alternative was to re-test `could_change` emptiness and
`budget.remaining_steps()` in this module before calling the model. That is
two copies of two rules, in two files, that must agree forever — and the
copy in the loop would be the one nobody updates. This way the controller
stays the single authority on stopping (spec.md §2: "Never rely on the LLM
alone for ... stopping" — and, equally, never rely on two implementations),
and System C still never pays for a model call whose answer the controller
would throw away.

**Why a third hard stop, and why here specifically.** A 1,287-case System C
replay measured what the two-stop pre-flight above was still missing: after
the controller ran the only eligible probe (usually `company_events`), THIS
loop dutifully asked the investigator again; the model re-proposed the same
probe, because there was nothing else left to propose; the per-candidate
filter (see `rli.agent.controller`'s "step 6" in `decide`'s docstring)
rejected it `duplicate`; and the run then stopped `no_eligible_candidate` —
927 duplicate rejections and 1,015 such stops in that replay, meaning
roughly 40% of every investigator call this system made bought literally
nothing, because the answer ("nothing new to try") was already computable
from `(case state, executed history)` alone. `rli.agent.controller.decide`
now answers that question itself, as `StopReason` `no_new_eligible_probe`,
checked alongside the other two model-independent stops and therefore
caught by this exact pre-flight mechanism with NO change to this module: the
third stop rides the same "query, don't duplicate" pipe the first two
already built. See that function's docstring for the full judgment call,
including why `case.case_file() is None` is deliberately left to the
loop's OWN identity precondition below rather than folded in here too.

--------------------------------------------------------------------------
JUDGMENT CALL: the LLM call has its own cost gate
--------------------------------------------------------------------------

`controller.decide`'s budget gate prices the chosen PROBE
(`probe_cost_usd`), because that is the thing it is authorizing. An
investigator call is also real money charged against the same spec.md §4
budget, and nothing in `decide` knows about it — by the time `decide` runs,
the call has already been made.

So the loop gates the model call itself, through
`_ModelLedger.unaffordable_cap`, against the largest `LLMResponse.cost_usd`
(and latency) this run has observed so far. When the remainder could not
cover another call of the size we have actually been billed for, the loop
stops with `cost_cap` (or `latency_cap`). `run_system_c` applies the SAME
gate to the explanation — one rule, one estimator, two call sites.

Why the largest observed call rather than a configured estimate: a
configured `[agent].expected_llm_call_usd` would be one more unmeasured
placeholder to keep in sync with a price table that already exists, and it
would be wrong on the first day. The observed maximum is free, is always in
the right unit, and is *self-correcting* — a run whose prompts grow (more
evidence each step) raises its own estimate. It is deliberately zero before
the first call: a run must be allowed to make one investigator call and
find out what it costs, and a cache hit (which costs `0.0`, see
`rli.llm.client.CachedClient`) correctly never raises the bar, so a fully
cached replay is never stopped by this gate.

--------------------------------------------------------------------------
JUDGMENT CALL / DEVIATION: the budget SHOWN to the model excludes LLM spend
--------------------------------------------------------------------------

`build_investigator_input` takes `steps_remaining`, `cost_remaining_usd` and
`latency_remaining_s` and puts them verbatim into the `structured_input`
that `Prompt.structured_input_hash()` hashes. The obvious values to pass are
`budget.remaining_cost_usd()` and `budget.remaining_latency_s()`. Passing
them is WRONG, and measurably so — it was caught end to end before this
module was finished.

The enforcement ledger charges every investigator call
(`Budget.with_llm`) its real `cost_usd` and its **measured wall-clock**
`latency_ms`. Feed those into the prompt and the prompt for step 2 onwards
stops being a function of the case:

* `latency_remaining_s` carries a measured float
  (`54.99793975400098`), so two identical live runs of the same posting
  produce two different `structured_input_hash` values. Every step-2 call is
  a permanent cache MISS, for every case, forever.
* `cost_remaining_usd` diverges between an original run and its replay by
  construction: `CachedClient` charges `0.0` for a hit and the live client
  charges real dollars, so the replay's step-2 prompt can never equal the
  original's (`0.483` vs `0.479972` on the fixture that found this).

spec.md §2 requires LLM outputs to be cached by `(model_id, prompt_hash,
structured_input_hash)` "for exact benchmark replay", and spec.md §6 allows
a live call only "on cache miss (e.g. a changed investigator prompt)". A
System C whose every multi-step run re-bills the API is not replayable at
all, and the failure is silent — it looks like a slow, expensive benchmark,
not like a bug.

So the loop reports the PROBE-ONLY remainders: what is left after the
probes charged so far, ignoring what the model calls have cost, rounded to
`_REPORTED_PRECISION` decimal places. Every term in that number is a
deterministic estimate from `[probe_costs]` and `[agent]`
(`probe_cost_usd` / `latency_estimate_s`), so it is a pure function of the
case and the loop position — which is exactly what a cache key needs.

Three things make this safe rather than merely convenient:

1. **Enforcement is untouched.** `budget` still charges every LLM call, and
   every hard stop — the controller's cost/latency gates and this module's
   `_ModelLedger` gate — is tested against the TRUE ledger. spec.md
   §4's caps bound the whole run, model spend included.
2. **The number was always advisory.** `build_investigator_input`'s own
   docstring: "Shown so the model can propose proportionately, NOT so it can
   enforce anything ... the caps are re-checked in code whatever the model
   does with these numbers."
3. **It is the more truthful number for the question being asked.** The
   model is choosing a PROBE. What it can spend is the probe budget; it
   cannot make the investigator call cheaper by proposing differently, and
   `steps_remaining` — the cap that actually binds the loop — is already
   exact and unaffected.

The residual cost, stated plainly: the reported remainder OVERSTATES what is
truly left, by the run's model spend to date. A model could therefore
propose a probe the controller then rejects on budget, wasting a step. That
is bounded by the step cap, visible in the trace as a `budget_cost`
rejection, and strictly preferable to an unreplayable benchmark.

The real fix belongs one level down — `build_investigator_input` should not
accept a value that can be nondeterministic — but `rli/agent/investigator.py`
is outside this milestone's edit scope, so the constraint is enforced at the
one call site and documented here.

--------------------------------------------------------------------------
JUDGMENT CALL: the budget bounds the RUN, so it bounds the explanation too
--------------------------------------------------------------------------

spec.md §4's hard stop is "budget/latency/step cap is reached", and the
subject of that sentence is the run, not the loop. The explanation
(spec.md §2's last pipeline stage) is a second LLM call and therefore a
second real charge, so `_run_loop` returns its final `_ModelLedger` and
`run_system_c` gates the explanation on it with the SAME estimator the loop
uses for the investigator: the largest model call this run has actually been
billed for. When the remainder cannot cover another one, the call is skipped
and the deterministic reasons `decide_and_finish` already produced are
published instead, via `rli.agent.explanation.skip_explanation` — so a
budget-skipped explanation appears in the trace as
`explanation_fallback:cost_cap` (or `:latency_cap`), in the same vocabulary
as every other fallback, and the user-facing output has the same shape as
any other fallback: deterministic reasons, no hypotheses.

The alternative is seductive and wrong: let the explanation run unbudgeted,
because "the decision is already made, so this call cannot change anything
that matters". It cannot change the ACTION, but it absolutely changes the
COST — and cost is the number spec.md §6's agent gate is computed from ("C
medium/high-cost probe use <= 70% of B ... Report absolute cost/latency
too"). A cap that the last stage of every single run is allowed to step over
is not a cap; it is an advisory note, and it would understate C's spend by
one model call per posting across the whole benchmark. Worse, the overrun is
invisible: the run reports `status='completed'` and a total that quietly
exceeds its own configured maximum.

This was a real defect, found by the test pass and not by construction: the
first version of `_run_loop` kept its ledger local and documented that as
deliberate ("nothing after the loop may spend against spec.md §4's caps"),
which was simply false about the code one function up.

The estimator's zero-start behaviour is preserved and is load-bearing here:
before any model call has been billed, `max_cost_usd_seen` is `0.0` and no
gate fires, so a run whose loop made no LLM call at all (an unresolved
identity, `max_steps=0`) still gets its explanation. Both caps are gated,
cost first, because spec.md §4 names both and the estimator costs nothing to
extend from one to the other.

--------------------------------------------------------------------------
JUDGMENT CALL: tokens ride in `decision_type`, because there is no column
--------------------------------------------------------------------------

`rli.llm.client.LLMResponse` carries `input_tokens` / `output_tokens`, and
`db/schema.sql`'s `run_steps` has no column for either — its numeric
columns are `cost_usd` and `latency_s`, and its text columns
(`probe_name`, `args_hash`, `prompt_hash`, `model_id`, `error`) all have
declared meanings that a token count would violate.

The two honest options were a schema migration or an encoding. This
milestone's edit scope is `rli/agent/`, a migration would invalidate every
existing trace, and `run_steps.decision_type` already carries structured,
colon-separated qualifiers throughout the codebase
(`policy_decision:<branch>:<rule>`, `probe_skipped:<reason>`,
`route:<rule>`, `replay_violation:<kind>`). So a model step records

```text
investigator:tokens=<input>/<output>
explanation:tokens=<input>/<output>
```

and `parse_tokens` is the one reader. The suffix is OMITTED entirely on a
failed call that produced no usage, rather than written as `0/0`: "we were
billed for nothing" and "we do not know what we were billed for" are
different facts, and a metrics query summing `0/0` rows would quietly
under-report the token bill instead of reporting a gap.

The cost of the encoding is stated plainly: a query grouping by
`decision_type` must strip the suffix (`decision_type LIKE 'investigator%'`,
or `parse_tokens`). If token accounting ever becomes load-bearing for a
gate rather than for a report, it should become a column.

--------------------------------------------------------------------------
JUDGMENT CALL: `runs.final_decision` is written twice
--------------------------------------------------------------------------

spec.md §2's pipeline order is "fixed action policy -> LLM evidence-cited
explanation": the policy decides, then the explanation describes what was
decided. `rli.eval.runner.decide_and_finish` is where the frozen policy
lives for A, B and C alike, and it *closes the run* as its last act —
`Run.finish` writes `runs.final_decision` and flips the status.

The explanation therefore necessarily happens after the row is already
written, and `_rewrite_final_decision` issues one `UPDATE` to replace it
with the enriched `Decision` (the same object, plus LLM-authored `reason`
and `hypotheses`) and to refresh the totals with the explanation step's own
cost and latency.

The two alternatives were both worse:

* **Explain first, then decide.** The explanation would land in `run_steps`
  BEFORE the `policy_decision` row, so the canonical trace (spec.md §7)
  would assert that C explained a decision it had not yet made — a false
  statement about the pipeline spec.md §2 defines, and one that a reader
  auditing "did the LLM choose the action?" would have to disprove by
  reading this file.
* **Re-implement `decide_and_finish` here**, so C can compose its own final
  `Decision` before closing. That splits the single frozen-policy code path
  spec.md §6 demands into two, which is precisely the failure mode the
  shared function exists to prevent.

Rewriting one row is the smallest honest option, and `rli/eval/` is outside
this milestone's edit scope in any case. `Run._close` is idempotent, so the
`UPDATE` cannot race the close; it is written as raw SQL here, next to the
reasoning, rather than as a new `Run` method nobody else would call.

--------------------------------------------------------------------------
JUDGMENT CALL: a retry costs money, not a step
--------------------------------------------------------------------------

A retryable structured probe failure (spec.md §2: `{ok, error, retryable}`)
is re-executed at most `[agent].max_probe_retries` times — the config
default is `1`, and `0` disables retrying — and the re-execution is charged
to the cost and latency ledgers but NOT to the dynamic-step counter
(`Budget.with_probe_retry`, added to `rli.agent.controller` for exactly
this).

The asymmetry is the point. spec.md §4's step cap ("at most `4` dynamic
probe steps") bounds how many DISTINCT investigations the agent may open;
retrying `repost_history` after a transient failure is not a fifth
question, it is the same question asked again. Charging it a step would
mean a run that hit one flaky probe silently investigated less than a run
that did not — the failure would show up as a worse decision rather than as
a cost. Charging its money and its seconds is what actually bounds it
(spec.md §2: "no uncontrolled retry loops"), together with the hard retry
count.

The retried `ExecutedProbe` REPLACES its predecessor in `executed` rather
than being appended, so the duplicate key the controller enforces stays
one-per-`(probe, args_hash)`. Appending would make the controller reject
the probe as a duplicate of its own failed attempt — which is right in
general and wrong here, since a retry is the loop's decision, not the
model's.

--------------------------------------------------------------------------
JUDGMENT CALL: an unresolved identity stops the loop before the first call
--------------------------------------------------------------------------

When `CaseState.case_file()` is `None` (no `posting_id` / `company_id`),
`rli.probes.registry.build_args` cannot build arguments for ANY dynamic
probe, and `controller.decide` rejects every candidate as
`ineligible`/`identity_unresolved` — so the model call is guaranteed to buy
nothing. The loop therefore records the same `probe_skipped:
identity_unresolved` step System A and System B write, plus the
`controller_decision:stop:no_eligible_candidate` row the controller would
have produced, and never opens the loop.

This is a loop PRECONDITION, not a second copy of a controller rule: the
controller's answer is unchanged and is still what the trace records. It is
stated here because the pre-flight above cannot express it — `decide` needs
a candidate list to reject, and the whole point is not to pay for one.

--------------------------------------------------------------------------
Where each decision goes in the trace
--------------------------------------------------------------------------

`run_steps` is the canonical trace (spec.md §7) and it has no column for "a
list of candidates" or "a list of rejections". Following the argument
`rli.eval.system_b` makes for its routed set, they are recorded
STRUCTURALLY — one row each, in the order the controller reasoned:

* `candidate_rejected:<reason>` (controller) — one per rejected candidate,
  `probe_name` set, the free-text detail in `error`. These precede the
  decision row, because that is the order the controller produced them:
  a reader walking `step_index` sees what was refused before seeing what
  was chosen, which is the reasoning, not just the result.
* `controller_decision:<run|stop>:<reason>` (controller) — one per
  iteration, with `probe_name`/`args_hash` when a probe was chosen.
* `investigator[:tokens=i/o]` (model) — one per investigator call, with
  `prompt_hash`, `model_id`, `cache_status`, `cost_usd`, `latency_s`, and
  `error` when the call failed.
* `probe_retry:<attempt>` (controller) — written BEFORE each re-execution,
  carrying the PREVIOUS attempt's error, so the trace says what was being
  recovered from rather than only that a recovery happened.
* `args_mismatch` (controller) — a defensive row that must never appear
  (see `_execute_chosen`).
* `system_version` (controller) — C's freeze identity, mirroring System B.

`probe_run` rows come from `rli.eval.runner.ProbeRunner.execute` unchanged,
and `policy_decision` from `decide_and_finish`, so a C trace is directly
comparable with an A or B trace over those rows.

--------------------------------------------------------------------------
Other judgment calls
--------------------------------------------------------------------------

* **`case.quality` is re-derived at the top of every iteration and assigned
  back.** `rli.eval.case.extend_case_state` re-derives `case.inputs` from
  the grown evidence but deliberately does NOT touch `case.quality` — it is
  documented as leaving mid-loop derivation to the controller. But
  `rli.agent.investigator.build_investigator_input` READS `case.quality`
  and puts it in the prompt, and `could_change_action` takes the quality as
  a parameter. Without the assignment, step 2's prompt would show step 1's
  evidence-quality verdict: a stale `weak` shown next to fresh evidence
  that made it `strong`, silently, with no error anywhere and a cache key
  that hashes the stale value. So the loop derives it and writes it back,
  every iteration, before anything reads it.
* **`runs.config_hash` includes the model id and the prompt version.**
  `c_config_hash` is `cfg:<config>|c1:<model>:<prompt version>`, the same
  shape `rli.eval.system_b.b_config_hash` uses for its rules hash and for
  the same reason: a metrics query groups by that one column, and C's
  decisions are a function of the model and of the prompt contract as much
  as of the config. Two runs that differ only in `[llm].model_id` are not
  the same system and must not aggregate together.
* **A KNOWN UNIT COLLISION, inherited and documented rather than hidden.**
  `rli.eval.runner` states that `run_steps.cost_usd` is "a placeholder
  UNITLESS COST POINT, not a dollar" for probe rows. A model row's
  `cost_usd` is a REAL dollar amount from `[llm.prices]`. `runs.
  total_cost_usd` for a System C run is therefore a sum of two units and is
  NOT comparable with System A's or System B's total. The per-step rows
  stay correct and separable (`component='probe'` vs `component='model'`),
  which is how a cross-system cost comparison must compute it; `rli.eval.
  report` already filters to `component='probe'` for its per-probe table
  and should do the same for its per-run cost. Recorded here because it is
  a property of C that a reader of `runs.total_cost_usd` must know, and
  because `rli/eval/` is outside this milestone's edit scope.
* **`RunResult.probes_run` lists each probe ONCE, not once per attempt.**
  It is the set of investigations C chose to open, which is what spec.md
  §6's "medium/high-cost probe count" gate compares against System B. A
  retry is fully visible in the trace (two `probe_run` rows and a
  `probe_retry` row); folding it into this tuple would double-count a flaky
  probe against C in the one metric the agent gate turns on.
* **`route_rule` stays `None`.** It names System B's fired routing rule
  (`rli.eval.runner.RunResult`); C has no routing tree.
* **The module needs no API key and no vendor SDK.** The live client is
  built by `_default_llm_factory`, called only when no `llm` and no
  `llm_factory` were supplied; it is
  `rli.llm.client.OpenAICompatibleClient`, a plain httpx POST to the
  `[llm].base_url` configured for this deployment (a local Ollama by
  default, which needs no credential at all).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclasses_replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from rli.agent.controller import Budget, ControllerDecision
from rli.agent.controller import decide as controller_decide
from rli.agent.investigator import (
    ExecutedProbe,
    InvestigatorOutput,
    build_investigator_input,
)
from rli.config import Config
from rli.eval.case import CaseState, build_case_state, extend_case_state
from rli.eval.runner import (
    STEP_PROBE_SKIPPED,
    STEP_SYSTEM_VERSION,
    ProbeRunner,
    ReplayHook,
    Run,
    RunResult,
    config_hash,
    decide_and_finish,
    open_system_runner,
    replay_run_shape,
)
from rli.llm.client import CachedClient, LLMClient, LLMError, OpenAICompatibleClient, Prompt
from rli.llm.prompts import PROMPT_VERSION, build_investigator_prompt
from rli.models.decision import Decision
from rli.models.policy_inputs import UNKNOWN
from rli.net.client import hash_args
from rli.policy.action import decide as policy_decide
from rli.policy.inputs import could_change_action, last_publish_or_refresh
from rli.policy.quality import evidence_quality_detail
from rli.probes.base import Probe, ProbeContext
from rli.probes.registry import DYNAMIC_PROBES, build_args

__all__ = [
    "C_VERSION",
    "STEP_ARGS_MISMATCH",
    "STEP_CANDIDATE_REJECTED",
    "STEP_CONTROLLER_DECISION",
    "STEP_EXPLANATION",
    "STEP_INVESTIGATOR",
    "STEP_PROBE_RETRY",
    "STEP_RUN_FLAG_INVESTIGATOR_ERROR",
    "c_config_hash",
    "investigator_error_run_ids",
    "make_system_c",
    "Proposer",
    "parse_tokens",
    "run_agent_loop",
    "run_system_c",
    "with_tokens",
]

# The freeze label for System C's loop, mirroring `rli.eval.system_b.B_VERSION`.
# Bump it when the loop's behaviour changes in a way that should be treated as
# a NEW agent rather than a bug fix to this one.
C_VERSION = "c1"

# `run_steps.decision_type` vocabulary owned by this module and by
# `rli.agent.explanation`; see the module docstring's trace section.
STEP_INVESTIGATOR = "investigator"  # component='model'
STEP_EXPLANATION = "explanation"  # component='model'; written by rli.agent.explanation
STEP_CONTROLLER_DECISION = "controller_decision"  # component='controller'
STEP_CANDIDATE_REJECTED = "candidate_rejected"  # component='controller'
STEP_PROBE_RETRY = "probe_retry"  # component='controller'
STEP_ARGS_MISMATCH = "args_mismatch"  # component='controller'; must never appear
# component='controller', error NULL; written ONCE per run whose investigator
# call failed (unusable output: not JSON, schema-invalid, provider error).
# The run still completes — the controller stops (`investigator_error`) and
# the frozen policy decides on the evidence so far, so its status and action
# look like any other run's. This flag is what makes such a run COUNTABLE:
# `SELECT DISTINCT run_id FROM run_steps WHERE decision_type = ?`
# (`investigator_error_run_ids`). Its `error` column is left NULL on purpose:
# the failure itself is already recorded once, on the `component='model'`
# investigator step, and `rli.eval.report` counts error rows.
STEP_RUN_FLAG_INVESTIGATOR_ERROR = "run_flag:investigator_error"

# The `decision_type` token suffix (see the module docstring). Kept as a
# constant so `with_tokens` and `parse_tokens` cannot drift apart.
_TOKENS_MARKER = ":tokens="

# Decimal places the budget remainders are rounded to before they enter the
# investigator prompt (and therefore the LLM cache key). Six, matching the
# `:.6f` precision `rli.agent.controller` already prints usd at. Rounding is
# belt to the braces of the probe-only accounting described in the module
# docstring: the terms are deterministic config estimates, and rounding keeps
# them so even if a future cost model introduces a long float tail.
_REPORTED_PRECISION = 6


# ---------------------------------------------------------------------------
# The token-suffix convention
# ---------------------------------------------------------------------------


def with_tokens(base: str, input_tokens: int, output_tokens: int) -> str:
    """`"investigator"` -> `"investigator:tokens=1200/48"` (see the module docstring)."""
    return f"{base}{_TOKENS_MARKER}{input_tokens}/{output_tokens}"


def parse_tokens(decision_type: str) -> tuple[int, int] | None:
    """`(input_tokens, output_tokens)` from a model row's `decision_type`, or `None`.

    `None` means "this row carries no token accounting" — either it is not a
    model row, or it is a failed call that produced no usage. Total and
    non-raising by design: this is read by metrics queries over historical
    traces, and one malformed row must not abort a report. `rfind` is used
    so the marker is located from the right, which keeps the function
    correct if a future base token ever contains a colon.
    """
    marker = decision_type.rfind(_TOKENS_MARKER)
    if marker < 0:
        return None
    head, separator, tail = decision_type[marker + len(_TOKENS_MARKER) :].partition("/")
    if not separator:
        return None
    try:
        return int(head), int(tail)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------


def c_config_hash(cfg: Config, model_id: str) -> str:
    """`runs.config_hash` for a System C run: config + loop version + model + prompt.

    `cfg:<config fingerprint>|c1:<model id>:<prompt version>`, composed the
    way `rli.eval.system_b.b_config_hash` composes its rules hash. See the
    module docstring for why the model and the prompt contract belong in the
    column a metrics query groups by.
    """
    return f"cfg:{config_hash(cfg)}|{C_VERSION}:{model_id}:{PROMPT_VERSION}"


# ---------------------------------------------------------------------------
# Client and replay-keyword resolution
# ---------------------------------------------------------------------------


def _default_llm_factory(conn: sqlite3.Connection, cfg: Config) -> LLMClient:
    """The live client: `CachedClient(OpenAICompatibleClient.from_config(cfg), conn)`.

    Built LAZILY — this function is only called when the caller supplied
    neither `llm` nor `llm_factory`. Constructing the client is what reads
    the API key out of the environment and opens an httpx connection pool,
    so importing this module, and every test that injects its own client,
    stays free of both. A failure to build the client propagates unchanged:
    an agent that silently degraded to "no model" would report System C's
    cost while measuring nothing.
    """
    return CachedClient(OpenAICompatibleClient.from_config(cfg), conn)


def _resolve_llm(
    conn: sqlite3.Connection,
    cfg: Config,
    llm: LLMClient | None,
    llm_factory: Callable[[sqlite3.Connection, Config], LLMClient] | None,
) -> LLMClient:
    """Explicit client, else the injected factory, else the live default."""
    if llm is not None:
        return llm
    factory = llm_factory if llm_factory is not None else _default_llm_factory
    return factory(conn, cfg)


def _resolve_replay(replay: ReplayHook | None, replay_ctx: ReplayHook | None) -> ReplayHook | None:
    """Accept `replay=` and `replay_ctx=` as synonyms; reject a conflicting pair.

    `rli.eval.runner.ReplayHook` documents itself as "passed as the single
    `replay=` keyword", and `run_system_a` / `run_system_b` now take exactly
    that — so `replay=` is canonical and `replay_ctx=` is an alias.

    The alias exists because this module and the `replay=` keyword on A and B
    were written concurrently by different agents, and a System C that could
    only be called by the name that lost the coin flip would be undroppable
    into `{"A": run_system_a, "B": run_system_b, "C": make_system_c(llm)}`.
    Accepting both costs three lines and cannot be wrong; guessing costs a
    broken dispatch table found at replay time.

    Both given and different is a caller bug, not something to reconcile: the
    two hooks would name two datasets and two values of `T`, and silently
    picking one would produce a run whose `runs.replay_at` disagreed with the
    evidence gate that built it. Compared by identity — a `ReplayHook` carries
    a closure, so two hooks for the same dataset are equal only when they are
    the same object.
    """
    if replay is not None and replay_ctx is not None and replay is not replay_ctx:
        raise ValueError(
            "run_system_c received two different replay hooks via replay= and "
            "replay_ctx=; they are synonyms, so pass exactly one (replay= is "
            "the canonical keyword)"
        )
    return replay if replay is not None else replay_ctx


@dataclass(frozen=True, slots=True)
class _ModelLedger:
    """The loop's spend state, in the form the rest of the run needs it.

    `_run_loop` returns one of these instead of returning `None`, because
    spec.md §4's caps bound the RUN and the explanation is another model call
    (see the module docstring). It carries the enforcement `Budget` plus the
    two "what would the next model call cost?" estimators the gate is made
    of.

    The estimators are the largest values this run has actually been BILLED,
    not configured guesses: they are free, always in the right unit, and
    self-correcting as prompts grow. They start at `0.0` so the first call is
    always allowed — a run must be permitted to make one call and find out
    what it costs. A cache hit costs `0.0` and correctly never raises either
    bar (`rli.llm.client.CachedClient`), so a fully cached replay is never
    gated.
    """

    budget: Budget
    max_cost_usd_seen: float = 0.0
    max_latency_s_seen: float = 0.0

    def charge_model_call(self, cost_usd: float, latency_s: float) -> _ModelLedger:
        """Charge one model call to the ledger and update the estimators."""
        return dataclasses_replace(
            self,
            budget=dataclasses_replace(
                self.budget,
                spent_cost_usd=self.budget.spent_cost_usd + cost_usd,
                spent_latency_s=self.budget.spent_latency_s + latency_s,
            ),
            max_cost_usd_seen=max(self.max_cost_usd_seen, cost_usd),
            max_latency_s_seen=max(self.max_latency_s_seen, latency_s),
        )

    def unaffordable_cap(self) -> tuple[str, str] | None:
        """`(cap name, human detail)` when another model call will not fit; else `None`.

        Cost is checked before latency so a run that has exhausted both is
        reported as `cost_cap`, which is the cap spec.md §6's agent gate is
        computed against and therefore the more informative of the two.
        """
        if self.budget.remaining_cost_usd() < self.max_cost_usd_seen:
            return (
                "cost_cap",
                f"{self.budget.remaining_cost_usd():.6f} usd remains, which is less "
                f"than the largest model call this run has been billed for "
                f"({self.max_cost_usd_seen:.6f} usd); no budget for another call",
            )
        if self.budget.remaining_latency_s() < self.max_latency_s_seen:
            return (
                "latency_cap",
                f"{self.budget.remaining_latency_s():.3f}s remains, which is less "
                f"than the slowest model call this run has made "
                f"({self.max_latency_s_seen:.3f}s); no latency budget for another call",
            )
        return None


def _compact_error(exc: BaseException) -> str:
    """One short line for a `run_steps.error` column.

    Whitespace-collapsed and capped, in the spirit of
    `rli.agent.controller._compact_validation_error`: a pydantic
    `ValidationError` is multi-line and embeds a docs URL, and an operator
    reading the trace needs the first line of it, not all of it.
    """
    text = " ".join(str(exc).split())
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:400]}"


# ---------------------------------------------------------------------------
# One investigator call
# ---------------------------------------------------------------------------


def _call_investigator(
    *,
    llm: LLMClient,
    run: Run,
    prompt: Prompt,
    moment: datetime,
) -> tuple[InvestigatorOutput | None, str | None, float, float]:
    """Make one investigator call and write its `component='model'` step row.

    Returns `(output, investigator_error, cost_usd, latency_s)`. `output` is
    `None` exactly when `investigator_error` is set, which is the shape
    `rli.agent.controller.decide` takes.

    NOTHING is re-raised. spec.md §4 requires the run to reach a valid
    `Decision` regardless, and `rli.agent.controller`'s docstring is explicit
    that a malformed investigator output is a STOP (never a fallback to
    System B's routing tree, which would report B's numbers as C's). Both
    `LLMError` — whose subclass `LLMSchemaError` covers "the model answered
    nonsense" — and a bare pydantic `ValidationError` are caught: the client
    normalizes provider failures into `LLMError`, but a `ValidationError`
    escaping a future client implementation must not crash a run either.

    A failed call is charged `0.0` cost and `0.0` latency. That is the honest
    number when the call never returned a usage record; it is also, and
    deliberately, an UNDER-report if a live call failed after the provider
    had already billed for input tokens. The alternative — inventing an
    estimate — would put a fabricated number in the one column spec.md §6's
    cost metric is computed from.
    """
    prompt_hash = prompt.prompt_hash(InvestigatorOutput)
    args_hash = prompt.structured_input_hash()

    try:
        response = llm.complete_structured(prompt, InvestigatorOutput)
    except (LLMError, ValidationError) as exc:
        detail = _compact_error(exc)
        run.step(
            component="model",
            # No `:tokens=` suffix: there is no usage to report, and `0/0`
            # would read as "billed for nothing" (module docstring).
            decision_type=STEP_INVESTIGATOR,
            prompt_hash=prompt_hash,
            args_hash=args_hash,
            model_id=llm.model_id,
            cost_usd=0.0,
            latency_s=0.0,
            error=detail,
            created_at=moment,
        )
        return None, detail, 0.0, 0.0

    parsed = response.parsed
    if not isinstance(parsed, InvestigatorOutput):  # pragma: no cover - client contract
        # `complete_structured` is contractually an instance of the requested
        # schema (`rli.llm.client._validate_parsed`). Treated as a structured
        # failure rather than an assertion, for the same reason as above: the
        # run must still produce a Decision.
        detail = f"client returned {type(parsed).__name__}, not InvestigatorOutput"
        run.step(
            component="model",
            decision_type=STEP_INVESTIGATOR,
            prompt_hash=prompt_hash,
            args_hash=args_hash,
            model_id=response.model_id,
            cache_status=response.cache_status,
            cost_usd=response.cost_usd,
            latency_s=response.latency_ms / 1000.0,
            error=detail,
            created_at=moment,
        )
        return None, detail, response.cost_usd, response.latency_ms / 1000.0

    run.step(
        component="model",
        decision_type=with_tokens(STEP_INVESTIGATOR, response.input_tokens, response.output_tokens),
        prompt_hash=prompt_hash,
        args_hash=args_hash,
        model_id=response.model_id,
        cache_status=response.cache_status,
        cost_usd=response.cost_usd,
        latency_s=response.latency_ms / 1000.0,
        created_at=moment,
    )
    return parsed, None, response.cost_usd, response.latency_ms / 1000.0


# ---------------------------------------------------------------------------
# One probe execution, with its bounded retry
# ---------------------------------------------------------------------------


def _executed_entry(
    probe_cls: type[Probe], args: dict[str, object], args_hash: str, case: CaseState
) -> ExecutedProbe:
    """The `ExecutedProbe` record for whatever `case.probe_results` now holds.

    Reads the result out of the case state rather than taking it as an
    argument, so it can never describe a different execution than the one
    `extend_case_state` just recorded.
    """
    result = case.probe_results[probe_cls.name]
    return ExecutedProbe(
        probe=probe_cls.name,
        args_hash=args_hash,
        args=dict(args),
        ok=result.ok,
        error=result.error,
        retryable=result.retryable,
    )


def _execute_chosen(
    *,
    decision: ControllerDecision,
    case: CaseState,
    cfg: Config,
    probes: ProbeRunner,
    budget: Budget,
    executed: list[ExecutedProbe],
) -> Budget:
    """Execute the controller's chosen probe (plus its bounded retry); return the ledger.

    The execution itself is `rli.eval.case.extend_case_state` — the SAME call
    System A and System B make — so a probe cannot behave differently under C
    (see the module docstring). Everything around it is bookkeeping:

    * the canonical arguments are rebuilt with
      `rli.probes.registry.build_args` and their hash is compared with the
      one the controller chose. They are the same function of the same case
      file and the same `ctx.now()`, so a mismatch is impossible — and is
      therefore recorded as an `args_mismatch` controller row rather than
      swallowed, because the only way it can happen is a real bug in
      `build_args` determinism, which would silently break spec.md §4's
      "the same probe+arguments would repeat" hard stop and the replay
      dataset's `(probe, args_hash)` keys at the same time. The execution
      still proceeds under the ACTUAL hash, so the trace records what ran.
    * the retry is bounded by `[agent].max_probe_retries` and gated on
      `ok=False and retryable=True` (spec.md §2's structured failure).
    """
    name = decision.chosen_probe
    assert name is not None  # a 'run' decision always names a probe
    probe_cls = DYNAMIC_PROBES[name]

    case_file = case.case_file()
    assert case_file is not None  # guaranteed by the loop's identity precondition

    canonical = build_args(probe_cls, case_file, probes.ctx)
    canonical_args = canonical.model_dump(mode="json")
    args_hash = hash_args(name, **canonical_args)
    if args_hash != decision.chosen_args_hash:  # pragma: no cover - defensive
        probes.note(
            STEP_ARGS_MISMATCH,
            probe_name=name,
            args_hash=args_hash,
            error=(
                f"controller chose args_hash {decision.chosen_args_hash} but "
                f"build_args now yields {args_hash}; running the rebuilt arguments"
            ),
        )

    extend_case_state(case, [probe_cls], probes=probes)
    budget = budget.with_probe(probe_cls, cfg)

    entry = _executed_entry(probe_cls, canonical_args, args_hash, case)
    slot = len(executed)
    executed.append(entry)

    # spec.md §2: "no uncontrolled retry loops" — a hard count, and only for
    # a failure the probe itself declared retryable.
    attempt = 0
    while attempt < cfg.agent.max_probe_retries and not entry.ok and entry.retryable:
        attempt += 1
        probes.note(
            f"{STEP_PROBE_RETRY}:{attempt}",
            probe_name=name,
            args_hash=args_hash,
            # The PREVIOUS attempt's error: this row says what is being
            # recovered from, not merely that a recovery happened.
            error=entry.error or "unspecified retryable probe failure",
        )
        extend_case_state(case, [probe_cls], probes=probes)
        # Cost and latency, but NOT a step — see the module docstring.
        budget = budget.with_probe_retry(probe_cls, cfg)
        entry = _executed_entry(probe_cls, canonical_args, args_hash, case)
        # REPLACE, so the controller's duplicate key stays one per
        # (probe, args_hash).
        executed[slot] = entry

    return budget


# ---------------------------------------------------------------------------
# Closing the run
# ---------------------------------------------------------------------------


def _rewrite_final_decision(run: Run, decision: Decision) -> None:
    """Replace `runs.final_decision` (and the totals) with the enriched decision.

    `decide_and_finish` already closed the run; `Run._close` is idempotent,
    so `Run.finish` cannot be called a second time and this is one direct
    `UPDATE` instead. See the module docstring for why the explanation
    necessarily runs after the close and why the alternatives were worse.

    The totals are refreshed from the `Run`'s own accumulators, which have
    kept growing across the explanation's step rows, so
    `runs.total_cost_usd` / `runs.total_latency_ms` still equal the sum of
    this run's `run_steps` — the invariant `rli.eval.report` relies on.
    `status` and `finished_at` are deliberately NOT touched: the run
    completed when the policy decided, and the explanation cannot un-complete
    it.
    """
    run.conn.execute(
        """
        UPDATE runs
           SET final_decision = ?, total_cost_usd = ?, total_latency_ms = ?
         WHERE id = ?
        """,
        (
            decision.model_dump_json(),
            run.total_cost_usd,
            int(round(run.total_latency_s * 1000.0)),
            run.id,
        ),
    )
    run.conn.commit()


# ---------------------------------------------------------------------------
# run_system_c
# ---------------------------------------------------------------------------


def run_system_c(
    conn: sqlite3.Connection,
    cfg: Config,
    url: str,
    llm: LLMClient | None = None,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    use_tool_cache: bool = True,
    collection_status_csv: str | Path | None = None,
    max_steps: int | None = None,
    llm_factory: Callable[[sqlite3.Connection, Config], LLMClient] | None = None,
    replay: ReplayHook | None = None,
    replay_ctx: ReplayHook | None = None,
) -> RunResult:
    """Run System C against `url`: the bounded agent loop, then the explanation.

    Callable exactly like `run_system_a` / `run_system_b` — `(conn, cfg, url,
    *, now=..., run_id=..., sleep=..., use_tool_cache=...,
    collection_status_csv=..., replay=...)` — which is why `llm` is the only
    extra positional, carries a default, and is followed by keyword-only
    parameters. `make_system_c` binds `llm` and returns exactly the A/B
    shape.

    Arguments:
        conn: the corpus connection. C writes only `runs`, `run_steps`,
            `evidence` (+ `tool_cache`, `llm_cache`) — see `rli.eval.runner`'s
            write invariant.
        cfg: loaded configuration.
        url: the job URL to investigate.
        llm: the structured-output client. `None` resolves via `llm_factory`,
            then via the live
            `CachedClient(OpenAICompatibleClient.from_config(cfg), conn)`,
            constructed lazily so importing this module never needs an API
            key.
        now: decision clock override; must be timezone-aware. Defaults to the
            current UTC time, or to `T` when replaying.
        run_id: `runs.id` override, for deterministic tests/replay.
        sleep: injected into the retry backoff and the per-host rate limiter.
        use_tool_cache: `False` disconnects `tool_cache` entirely.
        collection_status_csv: pins the pre-collected company-event
            collection-status file for this run's `ProbeContext`, i.e. for the
            `company_events` probe (`rli.eval.runner.open_system_runner`).
        max_steps: override for `Budget.max_dynamic_steps` — spec.md §4's
            "at most `4` dynamic probe steps" — for a cheap smoke run,
            WITHOUT moving `[agent]`/`[thresholds]` for every other system.
            `0` runs the always-run pair, stops immediately, and still
            explains.
        llm_factory: builds the client from `(conn, cfg)` when `llm` is
            `None`. The injection point for a replay runner that wants a
            `CachedClient` over a scripted or a live inner client.
        replay: `None` for a live run; a `rli.eval.runner.ReplayHook` puts
            this run in replay mode (spec.md §6) exactly as it does for A
            and B.
        replay_ctx: a synonym for `replay`. See `_resolve_replay`.
    """
    hook = _resolve_replay(replay, replay_ctx)
    client = _resolve_llm(conn, cfg, llm, llm_factory)

    if max_steps is not None and max_steps < 0:
        raise ValueError(f"max_steps must be >= 0, got {max_steps}")

    moment, mode, run_config_hash, replay_at = replay_run_shape(
        hook, now, c_config_hash(cfg, client.model_id)
    )

    probes_run: list[str] = []
    with Run(
        conn,
        cfg,
        input_url=url,
        system="C",
        config_hash=run_config_hash,
        started_at=moment,
        run_id=run_id,
        mode=mode,
        replay_at=replay_at,
    ) as run:
        with open_system_runner(
            conn,
            cfg,
            run,
            moment,
            replay=hook,
            sleep=sleep,
            use_tool_cache=use_tool_cache,
            collection_status_csv=collection_status_csv,
        ) as probes:
            # Recorded before anything else, exactly as System B records its
            # rules hash, so even a run that fails mid-investigation says
            # which agent produced it.
            run.step(
                component="controller",
                decision_type=STEP_SYSTEM_VERSION,
                args_hash=f"{C_VERSION}:{client.model_id}:{PROMPT_VERSION}",
                model_id=client.model_id,
                created_at=moment,
            )

            # -- spec.md §4 steps 1-2: the always-run pair ------------------
            case = build_case_state(
                conn,
                cfg,
                url=url,
                now=moment,
                probes=probes,
            )
            run.set_posting_id(case.posting_id)

            budget = Budget.from_config(cfg)
            if max_steps is not None:
                budget = dataclasses_replace(budget, max_dynamic_steps=max_steps)

            executed: list[ExecutedProbe] = []
            ledger = _run_loop(
                case=case,
                cfg=cfg,
                probes=probes,
                run=run,
                llm=client,
                moment=moment,
                budget=budget,
                executed=executed,
                probes_run=probes_run,
            )

            # -- spec.md §4 step 9: the ONE frozen action policy ------------
            # Also closes the run (`Run.finish`); see `_rewrite_final_decision`.
            quality = evidence_quality_detail(case.evidence, case.inputs, case.failures, cfg)
            case.quality = quality
            decision = decide_and_finish(
                probes,
                evidence=case.evidence,
                inputs=case.inputs,
                features=case.features,
                failures=case.failures,
            )

        # -- spec.md §4 step 10: the evidence-cited explanation -------------
        # Imported here, not at module level: `rli.agent.explanation` imports
        # this module's trace vocabulary, and a function-level import is how
        # the package keeps that graph acyclic (the same pattern
        # `rli.policy.inputs` uses for `rli.policy.action._branch`).
        from rli.agent.explanation import explain, skip_explanation

        outcome = policy_decide(
            case.inputs,
            quality.quality,
            moment,
            cfg,
            long_lived=case.features.long_lived if case.features is not None else UNKNOWN,
            # Derived from the evidence at each call site rather than cached
            # on `CaseState`; the choice is argued in `rli.policy.action`'s
            # "two keyword inputs" section.
            last_refreshed_at=last_publish_or_refresh(case.evidence),
        )
        # The explanation is a model call, and spec.md §4's caps bound the
        # RUN. Same gate, same estimator, as the loop's own — see the module
        # docstring. `_run_loop` returns the ledger precisely so this line can
        # exist.
        unaffordable = ledger.unaffordable_cap()
        if unaffordable is not None:
            cap, detail = unaffordable
            final = skip_explanation(
                run=run,
                now=moment,
                decision=decision,
                # Already the deterministic reasons, and already
                # `hypotheses=[]` — `decide_and_finish` builds exactly the
                # fallback shape (`rli.eval.runner`).
                fallback_reasons=decision.reason,
                cap=cap,
                detail=detail,
            )
        else:
            final = explain(
                llm=client,
                run=run,
                cfg=cfg,
                now=moment,
                decision=decision,
                inputs=case.inputs,
                quality=quality,
                # The deterministic reasons `decide_and_finish` already
                # computed (`rli.policy.explain_stub`). Passed rather than
                # recomputed, so a fallback explanation is byte-identical to
                # what A and B print.
                fallback_reasons=decision.reason,
                case=case,
                policy_branch=outcome.branch,
            )
        _rewrite_final_decision(run, final)

    return RunResult(
        run_id=run.id,
        system="C",
        decision=final,
        probes_run=tuple(probes_run),
        # `route_rule` names System B's routing rule; C has none.
    )


class Proposer(Protocol):
    """A deterministic stand-in for the investigator (spec.md §4 step 3).

    Called with the CURRENT case state, the controller's `could_change` set
    and the probes already executed; returns the candidate list the
    controller then filters, ranks and budgets exactly as it does a model's
    output. `rli.eval.system_r` supplies one that proposes every eligible
    probe in rank order, which is how System R reuses C's controller,
    eligibility, ranking and loop with zero model calls.
    """

    def __call__(
        self,
        *,
        case: CaseState,
        cfg: Config,
        ctx: ProbeContext,
        could_change: set[str],
        executed: Sequence[ExecutedProbe],
    ) -> InvestigatorOutput: ...


def _run_loop(
    *,
    case: CaseState,
    cfg: Config,
    probes: ProbeRunner,
    run: Run,
    llm: LLMClient | None,
    moment: datetime,
    budget: Budget,
    executed: list[ExecutedProbe],
    probes_run: list[str],
    propose: Proposer | None = None,
) -> _ModelLedger:
    """spec.md §4 steps 3-8: the bounded loop. Mutates `case`/`executed`/`probes_run`.

    `propose=None` (System C) asks the LLM investigator `llm` at step 3.
    A `Proposer` (System R) replaces that call: no model is called, no model
    step is written, and the model-call budget gate (step 3c) is skipped
    because there is no model call to pay for. Every other line — the
    pre-flight hard stops, the controller's filter/rank/budget, probe
    execution and retry — is the same code for both.

    Split out of `run_system_c` so the run's lifecycle (open, always-run
    pair, policy, explanation, close) reads as one page and the loop reads as
    another. Everything else it produces is in the case state, in the trace,
    or in the two lists it is handed.

    It RETURNS the final `_ModelLedger`, and that is not incidental: the
    explanation is another model call, spec.md §4's caps bound the run rather
    than the loop, and a ledger that died here would let the last stage of
    every run step over its own cost cap. See the module docstring.

    The iteration count needs no `while True` guard: every path through the
    body either breaks or executes exactly one probe, and `Budget.with_probe`
    increments `dynamic_steps`, which the controller's `step_cap` stop then
    bounds. The `for` over `range(max_dynamic_steps + 1)` is a belt to that
    braces — one extra iteration so the final pass can observe the exhausted
    step budget and record the stop, and a hard ceiling so a future bug in
    the ledger cannot produce an unbounded loop against a paid API.
    """
    ledger = _ModelLedger(budget=budget)
    # What the investigator calls have cost so far. Subtracted back out of the
    # remainders REPORTED to the model — never out of the ones ENFORCED — so
    # the prompt stays a deterministic function of the case. See the module
    # docstring's deviation note; this is the whole mechanism.
    llm_cost_usd = 0.0
    llm_latency_s = 0.0

    # See the module docstring: with no identity, no probe's arguments can be
    # built, so the model call is guaranteed to buy nothing.
    if case.case_file() is None:
        probes.note(
            f"{STEP_PROBE_SKIPPED}:identity_unresolved",
            error=(
                "no posting_id/company_id could be resolved, so no dynamic probe's "
                "arguments can be built; the investigator is not called"
            ),
        )
        probes.note(f"{STEP_CONTROLLER_DECISION}:stop:no_eligible_candidate")
        return ledger

    for _iteration in range(ledger.budget.max_dynamic_steps + 1):
        # -- step 3a: the decision context, from the CURRENT case state -----
        # `extend_case_state` re-derives `case.inputs` but NOT `case.quality`,
        # and `build_investigator_input` reads `case.quality` — so it is
        # re-derived and written back here, every iteration, before anything
        # reads it (module docstring).
        quality = evidence_quality_detail(case.evidence, case.inputs, case.failures, cfg)
        case.quality = quality
        long_lived = case.features.long_lived if case.features is not None else UNKNOWN
        # Cheap linear scan over evidence already in hand, recomputed each
        # iteration rather than cached on `CaseState` (see
        # `rli.policy.action`'s "two keyword inputs" section). It is also
        # invariant across the loop in practice — no dynamic probe emits
        # publish or refresh evidence — which is why `could_change_action`
        # holds it FIXED instead of enumerating it.
        last_refreshed_at = last_publish_or_refresh(case.evidence)

        outcome = policy_decide(
            case.inputs,
            quality.quality,
            moment,
            cfg,
            long_lived=long_lived,
            last_refreshed_at=last_refreshed_at,
        )
        could_change = could_change_action(
            case.inputs,
            outcome.recommended_action,
            quality=quality.quality,
            long_lived=long_lived,
            last_refreshed_at=last_refreshed_at,
            now=moment,
            cfg=cfg,
        )

        # -- step 3b: the pre-flight (module docstring) ---------------------
        preflight = controller_decide(
            output=None,
            case=case,
            cfg=cfg,
            ctx=probes.ctx,
            now=moment,
            could_change=could_change,
            executed=executed,
            budget=ledger.budget,
        )
        if preflight.decision == "stop" and preflight.reason != "investigator_error":
            _trace_decision(probes, preflight)
            break

        output: InvestigatorOutput | None
        investigator_error: str | None
        if propose is not None:
            # -- step 3 (System R): a deterministic proposal, no model ------
            output = propose(
                case=case,
                cfg=cfg,
                ctx=probes.ctx,
                could_change=could_change,
                executed=executed,
            )
            investigator_error = None
        else:
            output, investigator_error, ledger, llm_cost_usd, llm_latency_s = _investigate(
                case=case,
                cfg=cfg,
                probes=probes,
                run=run,
                llm=llm,
                moment=moment,
                could_change=could_change,
                executed=executed,
                ledger=ledger,
                llm_cost_usd=llm_cost_usd,
                llm_latency_s=llm_latency_s,
            )
            if output is None and investigator_error is None:
                # The model-call budget gate fired; the stop row is written.
                break

        # -- steps 4-6: the controller decides ------------------------------
        decision = controller_decide(
            output=output,
            case=case,
            cfg=cfg,
            ctx=probes.ctx,
            now=moment,
            could_change=could_change,
            executed=executed,
            budget=ledger.budget,
            investigator_error=investigator_error,
        )
        _trace_decision(probes, decision)
        if investigator_error is not None:
            # Decision logic untouched; this only marks the run (see
            # `STEP_RUN_FLAG_INVESTIGATOR_ERROR`). An investigator error is
            # always a stop, so the flag is written at most once per run.
            probes.note(STEP_RUN_FLAG_INVESTIGATOR_ERROR)

        if decision.decision == "stop":
            break

        # -- steps 6-7: execute exactly one probe, append its evidence ------
        name = decision.chosen_probe
        if name is None or name not in DYNAMIC_PROBES:  # pragma: no cover - defensive
            probes.note(
                f"{STEP_CONTROLLER_DECISION}:stop:no_eligible_candidate",
                error=f"controller returned decision='run' with chosen_probe={name!r}",
            )
            break

        ledger = dataclasses_replace(
            ledger,
            budget=_execute_chosen(
                decision=decision,
                case=case,
                cfg=cfg,
                probes=probes,
                budget=ledger.budget,
                executed=executed,
            ),
        )
        probes_run.append(name)

    return ledger


#: The public name of the bounded loop, for System R (`rli.eval.system_r`).
run_agent_loop = _run_loop


def _investigate(
    *,
    case: CaseState,
    cfg: Config,
    probes: ProbeRunner,
    run: Run,
    llm: LLMClient | None,
    moment: datetime,
    could_change: set[str],
    executed: list[ExecutedProbe],
    ledger: _ModelLedger,
    llm_cost_usd: float,
    llm_latency_s: float,
) -> tuple[InvestigatorOutput | None, str | None, _ModelLedger, float, float]:
    """System C's step 3c-3d: the model-call budget gate, then one investigator call.

    Returns `(output, investigator_error, ledger, llm_cost_usd, llm_latency_s)`.
    `output is None and investigator_error is None` means the budget gate
    stopped the loop (its `controller_decision:stop:<cap>` row is written
    here); the caller breaks.
    """
    if llm is None:  # pragma: no cover - run_system_c always resolves a client
        raise ValueError("the LLM investigator path needs an LLM client")

    # -- step 3c: can we afford another investigator call? --------------
    # The same gate `run_system_c` applies to the explanation, from the
    # same estimator — one rule, two call sites.
    unaffordable = ledger.unaffordable_cap()
    if unaffordable is not None:
        cap, detail = unaffordable
        probes.note(f"{STEP_CONTROLLER_DECISION}:stop:{cap}", error=detail)
        return None, None, ledger, llm_cost_usd, llm_latency_s

    # -- step 3d: the investigator call ---------------------------------
    structured_input, untrusted = build_investigator_input(
        case,
        cfg,
        ctx=probes.ctx,
        now=moment,
        could_change=could_change,
        executed=executed,
        steps_remaining=ledger.budget.remaining_steps(),
        # Probe-only, rounded — NOT `budget.remaining_*()`. See the
        # module docstring: those carry measured wall-clock latency and
        # cache-dependent LLM cost, which would make this prompt (and
        # therefore its cache key) nondeterministic.
        cost_remaining_usd=round(
            ledger.budget.remaining_cost_usd() + llm_cost_usd, _REPORTED_PRECISION
        ),
        latency_remaining_s=round(
            ledger.budget.remaining_latency_s() + llm_latency_s, _REPORTED_PRECISION
        ),
    )
    prompt = build_investigator_prompt(structured_input=structured_input, untrusted=untrusted)
    output, investigator_error, cost_usd, latency_s = _call_investigator(
        llm=llm, run=run, prompt=prompt, moment=moment
    )
    # The ENFORCEMENT ledger charges the call in full: spec.md §4's caps
    # bound the whole run, model spend included.
    ledger = ledger.charge_model_call(cost_usd, latency_s)
    if output is None and investigator_error is None:  # pragma: no cover - defensive
        investigator_error = "investigator produced no valid output"
    return (
        output,
        investigator_error,
        ledger,
        llm_cost_usd + cost_usd,
        llm_latency_s + latency_s,
    )


def _trace_decision(probes: ProbeRunner, decision: ControllerDecision) -> None:
    """Write one controller decision to the trace: rejections first, then the verdict.

    The order is the order the controller reasoned in, so a reader walking
    `step_index` sees what was refused before what was chosen (module
    docstring). `ControllerDecision.rejected` is `(probe, reason, detail)`
    triples; a pseudo-rejection — a stop that carries explanatory text but
    concerns no candidate, today only `investigator_error` — has an empty
    probe name, which becomes a NULL `probe_name` rather than an empty
    string, because `run_steps.probe_name` means "the probe this row is
    about" and `''` is not a probe.
    """
    for probe_name, reason, detail in decision.rejected:
        probes.note(
            f"{STEP_CANDIDATE_REJECTED}:{reason}",
            probe_name=probe_name or None,
            error=detail or None,
        )
    probes.note(
        f"{STEP_CONTROLLER_DECISION}:{decision.decision}:{decision.reason}",
        probe_name=decision.chosen_probe,
        args_hash=decision.chosen_args_hash,
    )


def investigator_error_run_ids(
    conn: sqlite3.Connection, run_ids: Sequence[str] | None = None
) -> set[str]:
    """Runs whose investigator call failed (`STEP_RUN_FLAG_INVESTIGATOR_ERROR`).

    `run_ids=None` searches every run. Runs traced before the flag existed are
    not found here; for those, `controller_decision:stop:investigator_error`
    marks the same event.
    """
    if run_ids is None:
        rows = conn.execute(
            "SELECT DISTINCT run_id FROM run_steps WHERE decision_type = ?",
            (STEP_RUN_FLAG_INVESTIGATOR_ERROR,),
        ).fetchall()
        return {str(row[0]) for row in rows}
    found: set[str] = set()
    ids = list(run_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        found.update(
            str(row[0])
            for row in conn.execute(
                f"SELECT DISTINCT run_id FROM run_steps "
                f"WHERE decision_type = ? AND run_id IN ({placeholders})",
                (STEP_RUN_FLAG_INVESTIGATOR_ERROR, *chunk),
            )
        )
    return found


# ---------------------------------------------------------------------------
# The A/B-shaped adapter
# ---------------------------------------------------------------------------


def make_system_c(
    llm: LLMClient | None = None,
    *,
    llm_factory: Callable[[sqlite3.Connection, Config], LLMClient] | None = None,
    max_steps: int | None = None,
) -> Callable[..., RunResult]:
    """Bind C's extra parameters and return a callable with the exact A/B shape.

    `run_system_a` and `run_system_b` are `(conn, cfg, url, *, now, run_id,
    sleep, use_tool_cache, collection_status_csv, replay)`. System C needs a
    model client, a step override and a client factory that A and B have no
    concept of, so a dispatch table like

    ```python
    {"A": run_system_a, "B": run_system_b, "C": make_system_c(llm)}
    ```

    would otherwise need a special case for exactly one entry. This closure
    removes it: the returned function takes the A/B parameters and nothing
    else, and a caller that has never heard of an LLM can invoke it.

    A plain closure rather than `functools.partial`: `partial` would keep the
    extra keywords visible and overridable at the call site, which is the
    opposite of the point, and it would leave the object without a usable
    `__name__` in a traceback. `__name__` / `__qualname__` / `__doc__` are
    set so the adapter reads as `run_system_c` wherever a dispatch table
    prints it.
    """

    def run(
        conn: sqlite3.Connection,
        cfg: Config,
        url: str,
        *,
        now: datetime | None = None,
        run_id: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        use_tool_cache: bool = True,
        collection_status_csv: str | Path | None = None,
        replay: ReplayHook | None = None,
    ) -> RunResult:
        return run_system_c(
            conn,
            cfg,
            url,
            llm,
            now=now,
            run_id=run_id,
            sleep=sleep,
            use_tool_cache=use_tool_cache,
            collection_status_csv=collection_status_csv,
            max_steps=max_steps,
            llm_factory=llm_factory,
            replay=replay,
        )

    run.__name__ = "run_system_c"
    run.__qualname__ = "run_system_c"
    run.__doc__ = run_system_c.__doc__
    return run
