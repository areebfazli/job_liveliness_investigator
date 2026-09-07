"""System B — the deterministic rules baseline, versioned and frozen (spec.md §6).

spec.md §6: "**B — Rules:** deterministic routing baseline", and "Freeze/version
B before evaluating C." B is the number System C has to beat: spec.md §6's
agent gate is

```text
C medium/high-cost probe use <= 70% of B
AND C action agreement with A >= B agreement with A - 2 percentage points
```

so B has exactly one job — spend as little as possible while still reaching
A-like decisions — and one hard requirement: it must be **frozen and
identifiable**, because a baseline that quietly improved while C was being
developed would make that 70% meaningless.

B shares everything with A except the routing decision: the same always-run
pair and case state (`rli.eval.case`), the same probe execution and trace
(`rli.eval.runner`), the same frozen action policy and the same deterministic
explanation (`decide_and_finish`). The routing tree below is the entire
difference.

--------------------------------------------------------------------------
The routing tree — first match wins
--------------------------------------------------------------------------

`route_detail` is a pure function of the CASE STATE (the always-run
evidence) and the configuration: no I/O, no clock, no probe output. That
purity is what keeps the spec.md §6 cost comparison honest — B must never
decide what to run by peeking at the result of a probe it has not paid for.
The tree is read as a chain of `if ... return`, in this order:

* **R0 `terminal`** — `posting_state` is `"closed"`, OR the resolver left the
  state unresolved (the UNKNOWN sentinel, or the literal `"unknown"` — in
  practice a resolver failure). -> **run NOTHING.**

  This is the rule that makes B cheaper than A, and it is not a heuristic:
  it is a proof about the frozen policy. Branch P1 (`closed -> skip`) is
  unconditional, so no evidence can change a closed posting's action. And no
  dynamic probe declares `posting_state` in its `populates` — none of them
  can — so an unresolved state stays unresolved and P2 (`wait`) stands no
  matter what B spends. Both cases are spec.md §4's own hard stop, "no
  unresolved question could change the action", reached without an
  investigator. Spending two `medium`-cost probes here would buy strictly
  nothing.

* **R1 `repost_suspicious`** — the posting is active (`open`/`reposted`) AND
  any of:
    * `publish_recency` is not `"recent"` — including UNKNOWN, i.e. no
      trustworthy PRIMARY publish date was established
      (`rli.policy.inputs`: an archive-only date leaves it UNKNOWN);
    * `features.long_lived is True`;
    * `features.first_seen_absent is not None`;
    * `features.reappeared_at is not None`.

  -> `[repost_history, requirements_drift]`, plus `company_events` under
  the condition below, plus `team_signal` under R4.

  Rationale: every one of those four is the observable shadow of the spec.md
  §5 repost branch — a stale or absent publish date, an old posting, a
  posting we have seen vanish, a posting we have seen come back. That branch
  (P4: `repeated_unchanged` + `long_lived` + no corroborating signal ->
  `skip`) is the only one that can turn an active posting into a `skip`, and
  `repost_pattern` is populated only by `repost_history` /
  `requirements_drift`. This is where B spends.

* **R2 `healthy`** — active AND `publish_recency == "recent"` AND the
  always-run evidence quality is `"strong"`. -> `[company_events]`.

  Rationale: this case is heading for P5 (`apply_now`). The only inputs that
  can move it off `apply_now` and are still reachable are
  `material_negative_event` (P5's own conjunct) and `freeze_or_pause` (P3a,
  which outranks P5) — both from `company_events`. The repost probes are
  skipped because P4 additionally requires `long_lived is True`, which R1
  would already have caught (see the R4 note below for the same argument
  spelled out), so they could not change the action here. `company_events`
  runs unconditionally in R2, even when both event inputs are already known:
  a `wait` produced by P3a must cite the freeze (spec.md §9), and the
  citation only exists if the probe emitted the evidence.

* **R3 `uncertain`** — otherwise (active, evidence mixed or weak, nothing
  repost-suspicious). -> `[repost_history]`, plus `company_events` under the
  condition below.

  Rationale: this posting is heading for `quick_apply` (P6/P7) on thin
  evidence. `repost_history` is the `low`-cost probe, and it is the cheapest
  thing that can still reach the `skip` branch; `requirements_drift`
  (`medium`) is deliberately NOT routed here, because without a repost
  signal a version diff cannot change the action on its own — this is
  precisely the "unnecessary probes" line item of spec.md §6.

* **`company_events` condition (applies to R1 and R3).** Included only when
  it can still matter: `material_negative_event` is UNKNOWN, OR
  `freeze_or_pause` is UNKNOWN, OR either of them is already known `True`.
  The UNKNOWNs are open questions. A known `True` is not — the answer is
  already in hand from the local pre-collected store — but the probe still
  runs, because that answer is about to influence the action and spec.md §9
  requires the user-facing reason to cite evidence, which only
  `company_events` emits.

  Both known-`True` cases are listed, deliberately and symmetrically:

  * `freeze_or_pause is True` drives policy branch P3a (`wait`);
  * `material_negative_event is True` is the conjunct that blocks P5
    (`apply_now`), sending an otherwise healthy case to `quick_apply`.

  Covering only the freeze half (the first version of this rule) left a
  real hole. R1 can fire on `features.long_lived is True` ALONE, while
  `publish_recency` is `"recent"` and the always-run evidence is `"strong"`
  — i.e. on a case that P5 would otherwise route to `apply_now`, and whose
  demotion to `quick_apply` is caused entirely by the known negative event.
  B would then have emitted an action shaped by a fact it never cited, and
  the user would see `quick_apply` with no mention of the layoff. That is
  not a §9 violation in the literal sense (every reason B prints still maps
  to evidence) but it is the same failure one level up: a decisive input
  with no evidence behind it. One medium-cost probe in a narrow branch is
  the right price for it.

  When both are known and both are `False`, nothing is left to learn and
  nothing needs citing, so B saves the step — which is exactly what it does
  on the live corpus for an already-searched company with no negative
  events.

* **R4 `team_signal` (an add-on to any non-terminal rule).** NEVER when
  `[team_signal].enabled` is false. When enabled, only when
  `corroborating_hiring_signal` is UNKNOWN **and** the spec.md §5
  repost/long-lived branch is actually reachable — `repost_pattern ==
  "repeated_unchanged"` AND `features.long_lived is True`. That is spec.md
  §4's own eligibility rule for this probe, verbatim: "it is eligible only
  when that input is unknown and the repost/long-lived branch is reachable".

  Note this can only ever fire inside R1, and the tree does not need a
  special case to say so: `repost_pattern == "repeated_unchanged"` requires
  a repost link, which `rli.history.features._classify_repost_pattern` only
  produces for a posting with a non-null `first_seen_absent` — which is
  itself an R1 trigger. Implementing R4 as a global add-on rather than an
  R1-only clause is therefore equivalent today, and stays correct if the
  classifier's preconditions ever change.

**Overlaps and precedence.** R1, R2 and R3 partition the active cases only
because they are read in order. A posting can be simultaneously recently
published, strongly evidenced AND previously absent — R1 wins, because the
repost question is the one whose answer can still change the action, while
R2's `apply_now` is the answer B would otherwise assume. R0 precedes both
because for a closed or unresolved posting the action is fixed. The
ordering is the rule; the conditions alone are not a function.

--------------------------------------------------------------------------
Eligibility, deduplication and the step cap
--------------------------------------------------------------------------

Routing names probes; it does not authorize them. Every routed name is put
through `rli.probes.registry.eligible_probes` before it runs, so B can never
execute a probe A would consider ineligible (no usable history, no
`team_signal` licence).

It is filtered through the SAME neutralized gate System A uses
(`rli.eval.system_a.ALL_DYNAMIC_INPUTS`), not through the case's actual
unpopulated set. Two reasons, and the first is decisive:

* Using the real unpopulated set would drop `company_events` for every
  company whose events have already been collected — because the case state
  pre-populates both of its inputs from the local store (see
  `rli.eval.case`'s judgment call) — which is exactly the case R2 and R3
  route it for, and would leave a P3a `wait` with no evidence to cite.
* B's routing tree already encodes its own "is this question still open?"
  logic, explicitly and visibly (R3's condition, R4's condition). Applying
  the registry's version on top would mean the same judgment is made twice,
  in two places, with no way to tell which one dropped a probe.

Duplicates are dropped preserving first-mention order, and the surviving
list is capped at `[thresholds].max_dynamic_steps`. **A is uncapped and B is
capped** — deliberately asymmetric: A is the reference ceiling (a budget
knob must not move the reference action), while B is a *baseline system*, in
the same class as C, and spec.md §4's step cap is a property of a system
that runs bounded investigations. Comparing an unbounded B against a bounded
C would understate C. With four dynamic probes and a cap of four the cap
does not currently bind; it exists so that lowering the cap constrains B and
C together.

The cap is applied AFTER the eligibility filter, so an ineligible probe
never consumes a step slot it was never going to use.

--------------------------------------------------------------------------
Versioning (spec.md §6: "Freeze/version B before evaluating C")
--------------------------------------------------------------------------

`B_VERSION` is the human-readable freeze label (`"b1"`). `b_rules_hash`
is the machine-checkable one: blake2b over the source of the routing
functions, `B_VERSION`, and the two configuration values the routing itself
reads (`max_dynamic_steps` and `[team_signal].enabled`). It mirrors
`rli.policy.action.policy_version`, including its trade-off — reformatting a
routing function changes the hash even when behaviour is identical, because
a false "the rules changed" is cheap to investigate and a false "nothing
changed" is a silent evaluation bug.

The version is recorded in the run TWICE, on purpose:

* `runs.config_hash` = `f"cfg:{config_hash(cfg)}|{B_VERSION}:{b_rules_hash(cfg)}"`,
  so a single column identifies "this config, these rules" — the form a
  metrics query groups by, without a join;
* a controller `run_steps` row with `decision_type='system_version'` and
  `args_hash=<rules hash>`, so the same fact is present in the canonical
  trace (spec.md §7: "`run_steps` is the canonical trace") for a reader
  walking one run rather than aggregating many.

Neither is derivable from the other by a query, and both are cheap. System A
writes the plain `f"cfg:{config_hash(cfg)}"` form: A has no rules of its own
to version — "run everything eligible" is fully described by the registry
and the config.

--------------------------------------------------------------------------
Where the routed set is recorded in the trace
--------------------------------------------------------------------------

`run_steps` has no column for "a list of probe names", and the columns it
does have keep their declared meanings (`args_hash` is an argument hash,
`probe_name` is one probe's name, `error` is for something that went
wrong). So the routed set is recorded structurally rather than stuffed into
a column:

* one controller row, `decision_type='route:<rule id>'`, naming the fired
  rule;
* one `probe_run` row per routed probe that ran (component `'probe'`, with
  `probe_name`);
* one controller `probe_skipped:<reason>` row per routed probe that did NOT
  run — `ineligible`, `step_cap`, or `identity_unresolved` — with
  `probe_name` set.

The routed list, in order, is therefore the probe/skip rows following the
route row, ordered by `step_index`; and "what B decided to do" and "what B
actually did" are separately visible, which a single joined string would
have hidden.
"""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.case import CaseState, build_case_state, extend_case_state
from rli.eval.runner import (
    STEP_PROBE_SKIPPED,
    STEP_ROUTE,
    STEP_SYSTEM_VERSION,
    Run,
    RunResult,
    config_hash,
    decide_and_finish,
    open_probe_runner,
)
from rli.eval.system_a import ALL_DYNAMIC_INPUTS
from rli.models.policy_inputs import Unknown
from rli.models.time import ensure_aware, now_utc
from rli.probes.base import Probe
from rli.probes.company_events import CompanyEventsProbe
from rli.probes.registry import DYNAMIC_PROBES, eligible_probes
from rli.probes.repost_history import RepostHistoryProbe
from rli.probes.requirements_drift import RequirementsDriftProbe
from rli.probes.team_signal import TeamSignalProbe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.probes.base import ProbeContext

__all__ = [
    "B_VERSION",
    "BRule",
    "RouteDecision",
    "b_config_hash",
    "b_rules_hash",
    "route",
    "route_detail",
    "run_system_b",
    "select_routed_probes",
]

# The freeze label. Bump it (and only then) when the routing tree changes in
# a way that should be treated as a NEW baseline rather than a bug fix to
# this one; `b_rules_hash` detects the change either way.
B_VERSION = "b1"

BRule = Literal[
    "R0_terminal",
    "R1_repost_suspicious",
    "R2_healthy",
    "R3_uncertain",
]

_ACTIVE_STATES = ("open", "reposted")


class RouteDecision(BaseModel):
    """One routing decision: the rule that fired, why, and the probes it names.

    `probes` are probe NAMES, in the order B wants them run, already
    de-duplicated. They are not yet eligibility-filtered or capped — routing
    names probes, it does not authorize them (see the module docstring).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: BRule
    probes: tuple[str, ...] = ()
    reason: str = ""


# ---------------------------------------------------------------------------
# The routing tree (pure)
# ---------------------------------------------------------------------------


def _repost_suspicious(case: CaseState) -> str | None:
    """R1's disjunction; returns the reason that matched, or None (see docstring)."""
    if case.inputs.publish_recency != "recent":
        return "no trustworthy recent primary publish date"
    features = case.features
    if features is None:
        return None
    if features.long_lived is True:
        return "posting history says long_lived"
    if features.first_seen_absent is not None:
        return "posting has been observed absent from the board"
    if features.reappeared_at is not None:
        return "posting has been observed reappearing"
    return None


def _events_still_matter(case: CaseState) -> bool:
    """R1/R3's `company_events` condition (see the module docstring)."""
    inputs = case.inputs
    if isinstance(inputs.material_negative_event, Unknown):
        return True
    if isinstance(inputs.freeze_or_pause, Unknown):
        return True
    # Known True on EITHER signal: nothing left to learn, but the value is
    # about to shape the action (P3a `wait` for a freeze; the blocked P5
    # `apply_now` for a material negative event) and that needs citable
    # evidence, which only this probe emits. See the module docstring for
    # why covering only the freeze half left a reachable hole.
    return inputs.freeze_or_pause is True or inputs.material_negative_event is True


def _team_signal_reachable(case: CaseState, cfg: Config) -> bool:
    """R4 — spec.md §4's own eligibility rule for `team_signal`."""
    if not cfg.team_signal.enabled:
        return False
    if not isinstance(case.inputs.corroborating_hiring_signal, Unknown):
        return False
    features = case.features
    return (
        case.inputs.repost_pattern == "repeated_unchanged"
        and features is not None
        and features.long_lived is True
    )


def route_detail(case: CaseState, cfg: Config) -> RouteDecision:
    """The System B routing tree — pure, deterministic, first match wins.

    Reads only the CASE STATE (the always-run evidence and what it derives)
    and the configuration. No database, no network, no clock, and above all
    no output of any probe B has not yet run.
    """
    state = case.inputs.posting_state
    resolved = None if isinstance(state, Unknown) else state

    # R0 — nothing any probe can populate could change the action. Written
    # as "not active" rather than as an explicit closed/unknown test so a
    # future `PostingState` value is terminal by default: routing spend at a
    # state this tree has never reasoned about is the wrong default.
    if resolved not in _ACTIVE_STATES:
        return RouteDecision(
            rule="R0_terminal",
            probes=(),
            reason=(
                "posting_state is closed and the policy's skip branch is unconditional"
                if resolved == "closed"
                else "posting_state is unresolved and no dynamic probe populates it, so "
                "the policy's wait branch stands whatever we spend"
            ),
        )

    # Everything below is an active posting (`_ACTIVE_STATES`).
    names: list[str] = []
    rule: BRule
    reason: str

    suspicion = _repost_suspicious(case)
    strong = case.quality is not None and case.quality.quality == "strong"

    if suspicion is not None:
        rule = "R1_repost_suspicious"
        reason = f"repost-suspicious: {suspicion}"
        names += [RepostHistoryProbe.name, RequirementsDriftProbe.name]
        if _events_still_matter(case):
            names.append(CompanyEventsProbe.name)
    elif case.inputs.publish_recency == "recent" and strong:
        rule = "R2_healthy"
        reason = (
            "recently published with strong always-run evidence; only a dated "
            "negative event or freeze can move this off apply_now"
        )
        names.append(CompanyEventsProbe.name)
    else:
        rule = "R3_uncertain"
        reason = (
            "active with mixed/weak evidence and no repost suspicion; spend only the "
            "low-cost repost probe"
        )
        names.append(RepostHistoryProbe.name)
        if _events_still_matter(case):
            names.append(CompanyEventsProbe.name)

    if _team_signal_reachable(case, cfg):
        names.append(TeamSignalProbe.name)

    # De-duplicate, preserving first-mention order.
    ordered = tuple(dict.fromkeys(names))
    return RouteDecision(rule=rule, probes=ordered, reason=reason)


def route(case: CaseState, cfg: Config) -> list[str]:
    """The routed probe names, in order. Thin wrapper over `route_detail`.

    `route_detail` is the primary form because the fired rule has to reach
    the trace, and re-deriving "which rule produced this list" from the list
    alone would be a second implementation of the tree.
    """
    return list(route_detail(case, cfg).probes)


def select_routed_probes(
    case: CaseState, cfg: Config, ctx: ProbeContext, routed: Sequence[str]
) -> tuple[list[type[Probe]], list[tuple[str, str]]]:
    """Apply the eligibility gates and the step cap to a routed name list.

    Returns `(probe classes to run, [(dropped name, reason), ...])`. See the
    module docstring for why the eligibility gate is A's neutralized one and
    why the cap is applied last.
    """
    case_file = case.case_file()
    if case_file is None:
        return [], [(name, "identity_unresolved") for name in routed]

    allowed = {
        probe_cls.name
        for probe_cls in eligible_probes(
            ctx, case_file, unpopulated_inputs=set(ALL_DYNAMIC_INPUTS)
        )
    }

    selected: list[type[Probe]] = []
    dropped: list[tuple[str, str]] = []
    cap = cfg.thresholds.max_dynamic_steps
    for name in routed:
        if name not in allowed:
            dropped.append((name, "ineligible"))
            continue
        if len(selected) >= cap:
            dropped.append((name, "step_cap"))
            continue
        selected.append(DYNAMIC_PROBES[name])
    return selected, dropped


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

# The functions whose source defines B's behaviour. A change to any of them
# must change `b_rules_hash()`; a change anywhere else in this module
# (docstrings included) must not.
_VERSIONED_FUNCTIONS = (
    _repost_suspicious,
    _events_still_matter,
    _team_signal_reachable,
    route_detail,
    select_routed_probes,
)


def b_rules_hash(cfg: Config | None = None) -> str:
    """A stable id for "these routing rules, with these knobs".

    Hashes `B_VERSION`, the source of every function in
    `_VERSIONED_FUNCTIONS`, and the two configuration values the routing
    itself reads: `[thresholds].max_dynamic_steps` (the cap) and
    `[team_signal].enabled` (R4's licence gate). Nothing else from the
    config is included — the rest is already covered by
    `rli.eval.runner.config_hash` and by
    `rli.policy.action.policy_version`, and duplicating it here would make
    the rules hash change for reasons that are not the rules.

    `cfg=None` loads the project config, mirroring `policy_version`.
    Requires importable source (a source checkout, not a zipimport).
    """
    if cfg is None:
        from rli.config import load_config

        cfg = load_config()

    digest = hashlib.blake2b(digest_size=16)
    digest.update(B_VERSION.encode("utf-8"))
    digest.update(b"\x00")
    for function in _VERSIONED_FUNCTIONS:
        digest.update(inspect.getsource(function).encode("utf-8"))
        digest.update(b"\x00")
    digest.update(
        (
            f"max_dynamic_steps={cfg.thresholds.max_dynamic_steps}|"
            f"team_signal_enabled={cfg.team_signal.enabled}"
        ).encode()
    )
    return digest.hexdigest()


def b_config_hash(cfg: Config) -> str:
    """`runs.config_hash` for a System B run: config fingerprint + rules version."""
    return f"cfg:{config_hash(cfg)}|{B_VERSION}:{b_rules_hash(cfg)}"


# ---------------------------------------------------------------------------
# run_system_b
# ---------------------------------------------------------------------------


def run_system_b(
    conn: sqlite3.Connection,
    cfg: Config,
    url: str,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    use_tool_cache: bool = True,
    collection_status_csv: str | Path | None = None,
) -> RunResult:
    """Run System B against `url`. Same signature and contract as `run_system_a`.

    The only differences from A are which probes run (the routing tree
    above), the two extra controller trace rows (`system_version` and
    `route:<rule>`), and the composed `runs.config_hash`.
    """
    moment = ensure_aware(now, "now") if now is not None else now_utc()

    with Run(
        conn,
        cfg,
        input_url=url,
        system="B",
        config_hash=b_config_hash(cfg),
        started_at=moment,
        run_id=run_id,
    ) as run:
        with open_probe_runner(
            conn, cfg, run, moment, sleep=sleep, use_tool_cache=use_tool_cache
        ) as probes:
            # Recorded before anything else, so even a run that fails
            # mid-investigation says which rules produced it.
            probes.note(STEP_SYSTEM_VERSION, args_hash=b_rules_hash(cfg))

            case = build_case_state(
                conn,
                cfg,
                url=url,
                now=moment,
                probes=probes,
                collection_status_csv=collection_status_csv,
            )
            run.set_posting_id(case.posting_id)

            decision_route = route_detail(case, cfg)
            probes.note(f"{STEP_ROUTE}:{decision_route.rule}")

            selected, dropped = select_routed_probes(
                case, cfg, probes.ctx, decision_route.probes
            )
            for name, why in dropped:
                probes.note(f"{STEP_PROBE_SKIPPED}:{why}", probe_name=name)

            if selected:
                extend_case_state(case, selected, probes=probes)

            final = decide_and_finish(
                probes,
                evidence=case.evidence,
                inputs=case.inputs,
                features=case.features,
                failures=case.failures,
            )

    return RunResult(
        run_id=run.id,
        system="B",
        decision=final,
        probes_run=tuple(probe_cls.name for probe_cls in selected),
        route_rule=decision_route.rule,
    )
