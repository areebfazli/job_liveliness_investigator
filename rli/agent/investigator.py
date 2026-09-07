"""System C's LLM investigator: its output schema and its input builder (spec.md §2/§4).

spec.md §2 puts exactly one LLM call at this point in the pipeline:

```text
Case file
  ↓
LLM Investigator
  - contradictions
  - unresolved questions
  - candidate probes + arguments
  ↓
Controller                                         deterministic
```

and spec.md §4 numbers it step 3 of the agent loop: "investigator ->
conflicts, unresolved questions, candidate probes+args, or STOP". This
module owns both halves of that boundary:

* `InvestigatorOutput` (and its parts) — the schema the model's reply is
  validated against, i.e. spec.md §2's "Schema-validate model outputs";
* `build_investigator_input` — the deterministic, canonically serializable
  view of the case that is handed to the model.

It deliberately contains no decision logic. Nothing here chooses a probe,
spends a budget, or stops the loop; that is `rli.agent.controller`, per
spec.md §2's "Never rely on the LLM alone for budgets, probabilities,
stopping, permissions, or the final action". `InvestigatorOutput.stop` is a
*request*, not a stop: the controller decides whether to honour it, and it
does so only after its own hard stops have already been checked.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

**The output models are `extra="forbid"` but not `frozen=True`.** The house
value-object config is `ConfigDict(frozen=True, extra="forbid")`, and
`extra="forbid"` is the load-bearing half here: it is what turns a model
that invented a field into a *validation failure* the loop can count and
trace, rather than a silently ignored key. `frozen=True` is dropped because
every one of these models carries `list` fields; pydantic's `frozen=True`
also synthesizes `__hash__`, which would advertise hashability that raises
`TypeError` on the first `hash()` of a real instance. These objects are
parsed once per step and read; nothing mutates them.

**`Contradiction` here is not `rli.policy.quality.Contradiction`.** The
name is reused deliberately and the two are never imported together. The
policy one is a *derived, deterministic* finding about evidence timestamps
and sources; this one is whatever the model claims to have noticed in
unstructured text. Merging them would let an LLM assertion enter the
deterministic quality verdict, which spec.md §2 forbids.

**`build_investigator_input` is a pure, total function of its arguments and
the DB read behind `eligible_probes`, and its output is order-stable.**
That is not cosmetic: spec.md §2 requires LLM outputs to be cached by
`(model_id, prompt_hash, structured_input_hash)` "for exact benchmark
replay", and `structured_input_hash` is a hash of this dict. A dict whose
key or list order wobbled between two identical cases would produce a cache
miss per run and silently defeat the replay guarantee of spec.md §6. So:
every mapping is built with literal keys (`json.dumps(..., sort_keys=True)`
canonicalizes them), every derived collection is `sorted()`, every datetime
goes through `rli.models.time.to_utc_z` (fixed `microseconds` precision),
and nothing is serialized by `repr()`.

The two lists that are NOT sorted are `evidence` and `probes_already_run`.
Both are already deterministic — they are the run's own append order — and
both are *chronological*, which is the order the model needs to reason
about "what did we learn, and in what order?". Sorting the evidence by its
run-local id would additionally be wrong-looking: those ids are `"e1"`,
`"e2"`, ... `"e10"`, and a lexicographic sort puts `e10` second.

**Evidence text never enters `structured_input`.** Each evidence item
contributes a `raw_excerpt_ref` (its own id) when an excerpt exists, and
the excerpt itself travels as a separate `UntrustedBlock`. spec.md §2:
"Treat job pages/news as untrusted data; delimit them in prompts." Putting
the excerpt in the structured JSON would place attacker-controlled text
*outside* the `<untrusted>` delimiters and directly adjacent to the fields
the controller trusts, which is precisely the confusion the delimiters
exist to prevent. The `ref` keeps the two halves joinable by the model
without merging them in the prompt.

**Excerpts are truncated FIRST, then sanitized.** `sanitize_untrusted` is
the last transform applied, so no later step can re-create a delimiter that
sanitization already neutralized. Truncating afterwards could cut the
rewritten form of `</untrusted` back into something closing-tag-shaped;
truncating first can only leave a harmless *prefix* of a tag, which cannot
close or open a block.

**Unknown policy inputs are rendered as the string `"unknown_unpopulated"`,
not as `null`.** `rli.models.policy_inputs` draws a distinction the whole
loop depends on: `declared_expiry=None` means "checked, the publisher
declared no expiry" and `declared_expiry=UNKNOWN` means "not checked". JSON
`null` for both would erase exactly the distinction the investigator is
being asked to reason about.

**The probe catalogue's `eligible` flag comes from `eligible_probes`; its
`ineligible_reason` is explanatory text only.** There is one authority on
eligibility (`rli.probes.registry.eligible_probes`) and both this module
and `rli.agent.controller` call it. The reason string re-tests the same
gates purely to say *why* in the prompt; it is never consulted by any
decision, so it cannot become a second, drifting source of truth. If the
two ever disagreed, the flag wins and the string is merely unhelpful.

**`could_change_action`, not `unpopulated`, is what is passed to
`eligible_probes`.** See `rli.agent.controller` for the full argument; the
catalogue shown to the model must match the gate the controller will apply,
or the model would be invited to propose probes that are already dead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.case import CaseState
from rli.llm.client import UntrustedBlock
from rli.llm.prompts import sanitize_untrusted
from rli.models.policy_inputs import Unknown
from rli.models.time import to_utc_z
from rli.probes.base import Probe, ProbeContext
from rli.probes.lookups import has_usable_history
from rli.probes.registry import (
    DYNAMIC_PROBES,
    cost_value,
    eligible_probes,
    latency_estimate_s,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.models.case_file import CaseFile
    from rli.models.evidence import EvidenceItem

__all__ = [
    "UNKNOWN_INPUT_MARKER",
    "Contradiction",
    "ExecutedProbe",
    "InvestigatorOutput",
    "ProbeCandidate",
    "UnresolvedQuestion",
    "build_investigator_input",
]

# The JSON rendering of `rli.models.policy_inputs.UNKNOWN`. A string rather
# than `null` — see the module docstring.
UNKNOWN_INPUT_MARKER = "unknown_unpopulated"

# Decimal places the two advisory budget floats are rounded to before they
# reach the hashed `structured_input`. See `build_investigator_input`'s
# caller contract for why any float that reaches the cache key is a hazard.
_BUDGET_PRECISION = 6


# ---------------------------------------------------------------------------
# The investigator's output schema (spec.md §2: "Schema-validate model outputs")
# ---------------------------------------------------------------------------


class Contradiction(BaseModel):
    """One conflict the model claims to have found in the evidence.

    Advisory. It never reaches the action policy or the evidence quality
    verdict; those are `rli.policy`'s deterministic job.
    """

    model_config = ConfigDict(extra="forbid")

    description: str
    evidence_ids: list[str] = []


class UnresolvedQuestion(BaseModel):
    """One question the model believes is still open.

    `policy_input` is expected to name a `rli.models.policy_inputs`
    field, but it is typed `str` and NOT validated against that set: the
    authoritative unresolved-question set is computed deterministically by
    `rli.policy.inputs.could_change_action` and handed to the model, so a
    mismatch here is a model quality signal to record, not an input to any
    gate. Rejecting the whole output over it would discard the candidates,
    which are the part the controller actually uses.
    """

    model_config = ConfigDict(extra="forbid")

    policy_input: str
    why: str


class ProbeCandidate(BaseModel):
    """One probe the model proposes running next, with its proposed arguments.

    `args` is validated by the controller against `probe_cls.ArgsModel`
    (spec.md §2: "Pydantic-validate probe arguments") and then DISCARDED in
    favour of `rli.probes.registry.build_args`. See
    `rli.agent.controller` for why.
    """

    model_config = ConfigDict(extra="forbid")

    probe: str
    args: dict[str, Any] = {}
    argument: str = ""
    expected_inputs: list[str] = []


class InvestigatorOutput(BaseModel):
    """The whole reply from one investigator call.

    Every field defaults to empty/false, so a model that answers with `{}`
    is a VALID output meaning "nothing to add, no candidates" — which the
    controller resolves to a `no_eligible_candidate` stop. Requiring the
    fields would turn a terse-but-honest answer into an
    `investigator_error`, and spec.md §6 measures those separately.
    """

    model_config = ConfigDict(extra="forbid")

    contradictions: list[Contradiction] = []
    unresolved_questions: list[UnresolvedQuestion] = []
    candidates: list[ProbeCandidate] = []
    hypotheses: list[str] = []
    stop: bool = False
    stop_reason: str | None = None


# ---------------------------------------------------------------------------
# The loop's memory of what it has already spent a step on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutedProbe:
    """One dynamic probe this run has already executed.

    Defined here rather than in `rli.agent.controller` because both the
    controller (which reads it, to enforce spec.md §4's "the same
    probe+arguments would repeat" hard stop) and `rli.agent.loop` (which
    writes it) need it, and the loop already imports this module for
    `build_investigator_input`.

    `args_hash` is `rli.net.client.hash_args(probe, **canonical_args)` over
    the CANONICAL arguments from `rli.probes.registry.build_args` — never
    the model's proposal — so the repeat check compares what was actually
    run against what would actually be run.

    `ok` / `error` / `retryable` mirror `rli.models.probe.ProbeResult`'s
    structured-failure contract (spec.md §2), so the investigator can see
    that a probe was attempted and failed rather than re-proposing it
    blind. A failed attempt still counts as executed: spec.md §4's repeat
    stop is about the call, not about its success, and "no uncontrolled
    retry loops" is enforced by the loop's single-retry rule.
    """

    probe: str
    args_hash: str
    args: dict[str, Any]
    ok: bool
    error: str | None = None
    retryable: bool = False


# ---------------------------------------------------------------------------
# build_investigator_input
# ---------------------------------------------------------------------------


def _json_scalar(value: Any) -> Any:
    """Canonicalize one policy-input / evidence value for JSON.

    `Unknown` becomes the explicit marker string, datetimes become
    fixed-precision UTC `Z` strings, and everything else is already a JSON
    scalar. Deliberately total and deliberately narrow: an unexpected type
    falls through to `str()` rather than raising, because a malformed value
    must not be able to take down the loop before the controller's hard
    stops have run.
    """
    if isinstance(value, Unknown):
        return UNKNOWN_INPUT_MARKER
    if isinstance(value, datetime):
        return to_utc_z(value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _evidence_entry(item: EvidenceItem) -> dict[str, Any]:
    """One evidence row as the model sees it — metadata only, no excerpt text."""
    return {
        "id": item.id,
        "probe": item.probe,
        "claim_type": item.claim_type,
        "value": item.value,
        "source_url": item.source_url,
        "source_quality": item.source_quality,
        "source_event_at": (
            None if item.source_event_at is None else to_utc_z(item.source_event_at)
        ),
        "available_at": to_utc_z(item.available_at),
        "fetched_at": to_utc_z(item.fetched_at),
        # The id again, or None. See the module docstring: the excerpt text
        # itself only ever travels inside an `<untrusted>` block.
        "raw_excerpt_ref": item.id if item.raw_excerpt else None,
    }


def _ineligible_reason(
    probe_cls: type[Probe],
    ctx: ProbeContext,
    case_file: CaseFile,
    could_change: set[str],
) -> str:
    """Best-effort explanation of why `probe_cls` is not eligible right now.

    PROMPT TEXT ONLY — never a gate. `eligible_probes` is the authority; this
    walks the same gates in the same order purely to name the first one that
    fails. See the module docstring.
    """
    populates: frozenset[str] = getattr(probe_cls, "populates", frozenset())
    if not (populates & could_change):
        return "cannot_populate_any_open_question"
    if probe_cls.history_required and not has_usable_history(
        ctx.conn, ctx.config, case_file.company_id
    ):
        return "no_usable_history"
    if probe_cls is DYNAMIC_PROBES.get("team_signal") and not ctx.config.team_signal.enabled:
        return "team_signal_disabled_in_config"
    return "probe_specific_gate"


def _probe_catalogue(
    cfg: Config,
    ctx: ProbeContext,
    case_file: CaseFile | None,
    could_change: set[str],
) -> list[dict[str, Any]]:
    """The dynamic-probe catalogue, sorted by name, with live eligibility.

    When the identity is unresolved (`case_file is None`) no probe can be
    argument-built at all — `build_args` needs a `posting_id` and a
    `company_id` — so every probe is reported ineligible with that reason,
    which is exactly the `ineligible`/`identity_unresolved` rejection the
    controller would produce.
    """
    if case_file is None:
        eligible_names: set[str] = set()
    else:
        eligible_names = {
            probe_cls.name
            for probe_cls in eligible_probes(
                ctx, case_file, unpopulated_inputs=set(could_change)
            )
        }

    catalogue: list[dict[str, Any]] = []
    for name in sorted(DYNAMIC_PROBES):
        probe_cls = DYNAMIC_PROBES[name]
        populates: frozenset[str] = getattr(probe_cls, "populates", frozenset())
        eligible = name in eligible_names
        catalogue.append(
            {
                "name": name,
                "cost_tier": probe_cls.cost_tier,
                "cost_points": cost_value(probe_cls, cfg),
                "latency_estimate_s": latency_estimate_s(probe_cls, cfg),
                "history_required": probe_cls.history_required,
                "populates": sorted(populates),
                "args_schema": probe_cls.ArgsModel.model_json_schema(),
                "eligible": eligible,
                "ineligible_reason": (
                    None
                    if eligible
                    else (
                        "identity_unresolved"
                        if case_file is None
                        else _ineligible_reason(probe_cls, ctx, case_file, could_change)
                    )
                ),
            }
        )
    return catalogue


def build_investigator_input(
    case: CaseState,
    cfg: Config,
    *,
    ctx: ProbeContext,
    now: datetime,
    could_change: set[str],
    executed: Sequence[ExecutedProbe],
    steps_remaining: int,
    cost_remaining_usd: float,
    latency_remaining_s: float,
) -> tuple[dict[str, Any], list[UntrustedBlock]]:
    """Render the case for the investigator prompt.

    Returns `(structured_input, untrusted_blocks)`. The first is handed to
    `rli.llm.prompts.build_investigator_prompt` as JSON; the second is
    emitted separately inside `<untrusted>` delimiters.

    The result is deterministic — the same arguments and the same DB state
    produce a byte-identical `json.dumps(..., sort_keys=True)` — because it
    feeds `Prompt.structured_input_hash()`, which is a third of the LLM
    cache key spec.md §2 mandates and spec.md §6's replay depends on.

    Arguments:
        case: the working case state (`rli.eval.case.CaseState`).
        cfg: loaded config; supplies the excerpt cap and the cost table.
        ctx: probe context, for the eligibility gates and `build_args`.
        now: the decision clock, echoed so a replayed prompt is reproducible
            from its own recorded input alone.
        could_change: `rli.policy.inputs.could_change_action`'s output —
            the unresolved questions that can still move the action.
        executed: every dynamic probe already run this run.
        steps_remaining / cost_remaining_usd / latency_remaining_s: the
            controller's ledger (`rli.agent.controller.Budget`). Shown so
            the model can propose proportionately, NOT so it can enforce
            anything: spec.md §2's "Never rely on the LLM alone for budgets
            ... or stopping" means the caps are re-checked in code whatever
            the model does with these numbers.

            CALLER CONTRACT, and it is load-bearing: these three values land
            in the hashed `structured_input`, so each must be a pure
            function of the case and the loop position. A MEASURED quantity
            here — wall-clock latency, or dollars that a cache hit charges
            differently from a live call — makes the prompt for step 2
            onwards differ between two identical runs, and between a run and
            its replay. The result is a permanent `llm_cache` miss on every
            multi-step run, which defeats spec.md §2's cache and spec.md
            §6's exact replay while looking merely expensive rather than
            broken. `rli.agent.loop` therefore passes probe-only remainders
            derived from `[probe_costs]`/`[agent]` estimates, and documents
            why at length. The two floats are additionally rounded to
            `_BUDGET_PRECISION` here, as defence in depth for a future
            caller that forgets: rounding cannot rescue a value that is
            wrong by dollars, but it does kill the float-noise class of the
            bug at the one place that feeds the hash.
    """
    case_file = case.case_file()

    identity = {
        "input_url": case.input_url,
        "canonical_url": case.canonical_url,
        "ats": case.ats,
        "tenant": case.tenant,
        "job_id": case.job_id,
        "posting_id": case.posting_id,
        "company_id": case.company_id,
        "title": case.title,
        "team": case.team,
        "location": case.location,
        "posting_row_exists": case.posting_row_exists,
        # The single fact that decides whether ANY dynamic probe can run.
        "identity_resolved": case_file is not None,
    }

    policy_inputs = {
        name: _json_scalar(getattr(case.inputs, name))
        for name in type(case.inputs).model_fields
    }

    # `evidence_quality` is deliberately not a `PolicyInputs` field (see
    # `rli.models.policy_inputs`); it is a derived verdict, shown alongside
    # so the model can read it but never listed as an open question.
    quality = (
        None
        if case.quality is None
        else {
            "quality": case.quality.quality,
            "rule": case.quality.rule,
            "detail": case.quality.detail,
        }
    )

    structured_input: dict[str, Any] = {
        "now": to_utc_z(now),
        "identity": identity,
        "evidence": [_evidence_entry(item) for item in case.evidence],
        "policy_inputs": policy_inputs,
        "evidence_quality": quality,
        "unpopulated": sorted(case.inputs.unpopulated()),
        "could_change_action": sorted(could_change),
        "probe_catalogue": _probe_catalogue(cfg, ctx, case_file, could_change),
        "probes_already_run": [
            {
                "probe": item.probe,
                "args_hash": item.args_hash,
                "args": {key: _json_scalar(value) for key, value in item.args.items()},
                "ok": item.ok,
                "error": item.error,
                "retryable": item.retryable,
            }
            for item in executed
        ],
        "budget": {
            "steps_remaining": steps_remaining,
            # Rounded, never passed through raw — see the caller contract in
            # this function's docstring. `_BUDGET_PRECISION` is far finer
            # than any budget decision made from these numbers and coarser
            # than the float noise a measured quantity carries.
            "cost_remaining_usd": round(cost_remaining_usd, _BUDGET_PRECISION),
            "latency_remaining_s": round(latency_remaining_s, _BUDGET_PRECISION),
        },
    }

    untrusted = [
        UntrustedBlock(
            source=item.id,
            # Truncate, THEN sanitize — see the module docstring.
            content=sanitize_untrusted(
                (item.raw_excerpt or "")[: cfg.agent.max_excerpt_chars]
            ),
        )
        for item in case.evidence
        if item.raw_excerpt
    ]

    return structured_input, untrusted
