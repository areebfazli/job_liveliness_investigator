"""System C's evidence-cited explanation — the last step, and the least powerful one.

spec.md §2 puts one more LLM call at the very end of the pipeline:

```text
Fixed action policy
  ↓
LLM evidence-cited explanation
```

Note what is above it. The action, the posting state, the recheck interval
and the evidence-quality verdict are ALREADY DECIDED by
`rli.policy.action.decide` before this module runs. spec.md §2 is emphatic
about it — "Never rely on the LLM alone for budgets, probabilities,
stopping, permissions, or the final action" — so this module is deliberately
built so that it *cannot* change any of them: `explain` returns
`decision.model_copy(update={"reason": ..., "hypotheses": ...})`, touching
exactly two fields, and checks that nothing else moved before returning.

spec.md §9's definition of done adds the other half: "every user-facing
reason maps to evidence". PLAN.md M5 spells out the mechanism — "validate
every `reason` cites existing `evidence_ids`". A model that cites `e9` in a
run that produced six evidence items has not made a small error; it has
produced a sentence with no support, which is the exact failure mode the
citation rule exists to catch. So citations are validated against the run's
real evidence ids, invalid ones are dropped and counted, and a reason left
with no valid citation is dropped entirely.

There are two ways into this module. `explain` makes the call;
`skip_explanation` publishes the deterministic floor WITHOUT making it, for
when `rli.agent.loop` finds no room under spec.md §4's run-level cost or
latency cap. Both record their outcome through `_record_fallback`, so every
route to the deterministic reasons produces one `explanation_fallback:<why>`
row in one vocabulary.

The deterministic explanation (`rli.policy.explain_stub.reasons_from_inputs`)
is not replaced by this module — it is its floor. System A and System B
publish it as their `reason` array; System C publishes the model's version
when the model produced a citable one, and falls back to exactly the same
deterministic array when it did not. A user never sees an empty `reason`
because a model call failed.

--------------------------------------------------------------------------
JUDGMENT CALL: hypotheses survive a citation wipeout, but not a failed call
--------------------------------------------------------------------------

The two output fields are treated differently on the way out, and the
difference is not an oversight.

`reason` is a claim about the evidence, so it lives or dies by its
citations. `hypotheses` is spec.md §1's explicitly speculative channel —
"`evergreen`, `paused`, `pipeline_building`, etc. are hypotheses, not
observable states" — and it carries no `evidence_ids` field at all, by
design, in both `ExplanationOutput` and `rli.models.decision.Decision`.
There is therefore nothing to validate them against, and dropping them
because a *different* field's citations were wrong would be punishing the
speculative channel for the failure of the factual one.

So:

* the model answered and validated -> keep its hypotheses (capped,
  stripped, de-duplicated), even if every one of its reasons was dropped;
* the call failed, or the reply did not validate -> `[]`. There is no
  output, so there is nothing to keep; and inventing speculation on behalf
  of a model that never answered would be the worst possible reading of
  spec.md §1's "hypotheses, not observable states".

`MAX_HYPOTHESES` is `5`. It is a UI/attention bound, not a safety one: past
about five, a "possibilities" list stops being read and starts being
scrolled, and every extra entry dilutes the ones that matter. It is a
module constant rather than a config knob because it is a presentation
choice with no evaluation consequence — no spec.md §6 metric reads it — and
`[agent]` is already carrying enough unmeasured placeholders.

--------------------------------------------------------------------------
JUDGMENT CALL: the structured input is metadata-only, and byte-stable
--------------------------------------------------------------------------

`build_explanation_input` follows `rli.agent.investigator.
build_investigator_input`'s discipline exactly, for exactly the same two
reasons, and it reuses that module's helpers rather than restating them:

* **Determinism.** The dict feeds `Prompt.structured_input_hash()`, which is
  one third of the `(model_id, prompt_hash, structured_input_hash)` cache key
  spec.md §2 mandates and spec.md §6's replay depends on. Every mapping is
  built from literal keys (`json.dumps(..., sort_keys=True)` canonicalizes
  them), every derived collection is `sorted()`, every datetime goes through
  `rli.models.time.to_utc_z`, and nothing is serialized by `repr()`. A dict
  whose ordering wobbled would produce a cache miss per run — cost and
  unreproducibility, with no error anywhere.
* **The untrusted boundary.** Evidence enters the structured input as
  METADATA ONLY, through `rli.agent.investigator._evidence_entry`, which
  emits a `raw_excerpt_ref` id and never the excerpt text. Excerpts travel
  separately as `UntrustedBlock`s — truncated to
  `[agent].max_excerpt_chars` and THEN sanitized, in that order, so a cut
  cannot re-create a delimiter that sanitization already neutralized (see
  `rli.agent.investigator`). spec.md §2: "Treat job pages/news as untrusted
  data; delimit them in prompts."

The two helpers are imported from `rli.agent.investigator` under their
private names. That is deliberate and it is the lesser evil: the correct
fix is to promote them to documented shared helpers in that module, which
is outside this milestone's edit scope, and the alternative — a second copy
of `_evidence_entry` here — would be two renderings of an evidence item
that must agree forever, in the one place where disagreeing silently
changes a cache key. A private import is visible and greppable; a divergent
copy is not.

--------------------------------------------------------------------------
Other judgment calls
--------------------------------------------------------------------------

* **`explain` is independently callable.** It takes a `Run` and an already
  built `Decision`, not a `ProbeRunner` and a `CaseState`, so it can be
  driven directly with a scripted client and a hand-built run. `case` is
  optional for the same reason: it contributes identity fields to the
  prompt and nothing else, and the citable evidence comes from
  `decision.evidence`, which is the authoritative list the citations are
  checked against.
* **Diagnostic counts go in `error`, because there is no other column.**
  `run_steps` has no numeric column for "how many citations were invalid",
  and its declared meanings (`args_hash`, `probe_name`, `prompt_hash`) do
  not admit one. The COUNT is therefore in the queryable
  `decision_type` (`citation_invalid:<n>`, the codebase's established
  `kind:qualifier` convention) and the human-readable breakdown is in
  `error` — which is free text, and which a dropped citation legitimately
  is: something went wrong, in the model's output rather than in a probe.
* **`ReasonDraft`/`ExplanationOutput` are `extra="forbid"` but not
  `frozen=True`**, for the reason `rli.agent.investigator` records for its
  own output models: `extra="forbid"` is what turns an invented field into
  a countable validation failure, while `frozen=True` would synthesize a
  `__hash__` that raises on the first `hash()` of a model carrying `list`
  fields.
* **A drafted reason with valid citations but empty text is dropped.** An
  empty string is not a reason, and publishing one would put a blank bullet
  in front of a user with citations attached to nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError

# Private imports, deliberately — see the module docstring. `_evidence_entry`
# renders one evidence row as metadata only (no excerpt text) and
# `_json_scalar` canonicalizes a policy-input value; duplicating either would
# be two renderings that must agree forever to keep one cache key stable.
from rli.agent.investigator import _evidence_entry, _json_scalar
from rli.agent.loop import STEP_EXPLANATION, with_tokens
from rli.config import Config
from rli.llm.client import LLMClient, LLMError, LLMSchemaError, UntrustedBlock
from rli.llm.prompts import build_explanation_prompt, sanitize_untrusted
from rli.models.decision import Decision, ReasonItem
from rli.models.policy_inputs import PolicyInputs
from rli.models.time import to_utc_z

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.eval.case import CaseState
    from rli.eval.runner import Run
    from rli.policy.quality import QualityVerdict

__all__ = [
    "BUDGET_FALLBACKS",
    "MAX_HYPOTHESES",
    "STEP_CITATION_INVALID",
    "STEP_EXPLANATION_FALLBACK",
    "ExplanationOutput",
    "ReasonDraft",
    "build_explanation_input",
    "explain",
    "skip_explanation",
]

# Presentation bound on `Decision.hypotheses`; see the module docstring.
MAX_HYPOTHESES = 5

# `run_steps.decision_type` prefixes owned by this module. `STEP_EXPLANATION`
# (the model row's base token) and the `:tokens=` suffix convention live in
# `rli.agent.loop`, which owns the trace vocabulary both model-calling
# modules share.
STEP_CITATION_INVALID = "citation_invalid"  # component='controller'
STEP_EXPLANATION_FALLBACK = "explanation_fallback"  # component='controller'

# Why the deterministic reasons were used instead of the model's.
_FALLBACK_LLM_ERROR = "llm_error"
_FALLBACK_SCHEMA_ERROR = "schema_error"
_FALLBACK_ALL_CITATIONS_INVALID = "all_citations_invalid"
_FALLBACK_NO_REASONS = "no_reasons"
# The call was never made: `rli.agent.loop` found no room for it under
# spec.md §4's run-level caps. Named for the cap that bound, matching the
# controller's `StopReason` vocabulary so a trace reads consistently whether
# the budget stopped the loop or stopped the explanation.
_FALLBACK_COST_CAP = "cost_cap"
_FALLBACK_LATENCY_CAP = "latency_cap"
BUDGET_FALLBACKS = frozenset({_FALLBACK_COST_CAP, _FALLBACK_LATENCY_CAP})


# ---------------------------------------------------------------------------
# The explanation's output schema (spec.md §2: "Schema-validate model outputs")
# ---------------------------------------------------------------------------


class ReasonDraft(BaseModel):
    """One reason the model proposes, before its citations have been checked.

    Deliberately NOT `rli.models.decision.ReasonItem`: that type is the
    user-facing contract and every instance of it in a `Decision` has already
    been validated against the run's evidence. Keeping the unvalidated draft
    in its own type makes it impossible to publish one by accident, and makes
    the conversion point — `_validate_citations` — the single place the
    spec.md §9 rule is enforced.

    `evidence_ids` defaults to empty rather than being required: a model that
    omits the field has produced an UNCITED reason, which is a countable
    quality signal to drop, not a schema violation that discards the whole
    reply (including the hypotheses and every other, correctly cited reason).
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    evidence_ids: list[str] = []


class ExplanationOutput(BaseModel):
    """The whole reply from one explanation call.

    Every field defaults to empty, so `{}` is a VALID output meaning "I have
    nothing citable to say" — which resolves to the deterministic fallback
    rather than to an `LLMSchemaError`. The distinction matters: spec.md §6
    measures model failures, and "answered honestly with nothing" is not the
    same event as "the API broke".
    """

    model_config = ConfigDict(extra="forbid")

    reason: list[ReasonDraft] = []
    hypotheses: list[str] = []


# ---------------------------------------------------------------------------
# build_explanation_input
# ---------------------------------------------------------------------------


def _identity(case: CaseState | None) -> dict[str, Any]:
    """The posting identity, with a STABLE key set whether or not a case exists.

    `explain` is independently callable with `case=None` (see the module
    docstring), and a prompt whose key set changed with the caller would hash
    differently for the same case — so the keys are always present and the
    values are `None` when unknown.
    """
    if case is None:
        return {
            "input_url": None,
            "canonical_url": None,
            "ats": None,
            "company_id": None,
            "posting_id": None,
            "title": None,
            "team": None,
            "location": None,
        }
    return {
        "input_url": case.input_url,
        "canonical_url": case.canonical_url,
        "ats": case.ats,
        "company_id": case.company_id,
        "posting_id": case.posting_id,
        "title": case.title,
        "team": case.team,
        "location": case.location,
    }


def build_explanation_input(
    *,
    case: CaseState | None,
    decision_core: Decision,
    inputs: PolicyInputs,
    quality: QualityVerdict | None,
    now: datetime,
    cfg: Config,
    policy_branch: str | None = None,
) -> tuple[dict[str, Any], list[UntrustedBlock]]:
    """Render the finished case for the explanation prompt.

    Returns `(structured_input, untrusted_blocks)`, the same pair
    `rli.agent.investigator.build_investigator_input` returns and the same
    pair `rli.llm.prompts.build_explanation_prompt` consumes. Deterministic:
    the same arguments produce a byte-identical
    `json.dumps(..., sort_keys=True)`, because that value is hashed into the
    LLM cache key (module docstring).

    What the model is shown, and nothing else:

    * `now` — echoed so a replayed prompt is reproducible from its own
      recorded input alone;
    * `identity` — which posting this is;
    * `decision` — the ALREADY-MADE decision it must explain, including the
      fired policy branch when the caller knows it, so the model can see
      *which* rule it is describing rather than inferring one;
    * `evidence_quality` — the deterministic verdict and the rule that
      produced it, which is what makes "we could not establish X" a citable,
      legitimate reason rather than a hedge;
    * `policy_inputs` — the seven spec.md §5 inputs, with UNKNOWN rendered as
      the explicit `unknown_unpopulated` marker (never `null`, which would
      erase the "checked and absent" vs "not checked" distinction the whole
      loop depends on);
    * `evidence` — the citable list, METADATA ONLY. These ids are exactly the
      ids `explain` will validate the model's citations against, so the model
      is never shown an id it may not cite.

    Arguments:
        case: the working case state, or `None` when `explain` is driven
            directly. Contributes identity fields only.
        decision_core: the `Decision` the frozen policy produced. Its
            `reason` and `hypotheses` are deliberately NOT shown — the model
            is being asked to write them, and showing it the deterministic
            draft would invite paraphrase instead of explanation.
        inputs: the policy inputs behind that decision. A separate argument
            rather than read off `decision_core`, because a `Decision` does
            not carry `PolicyInputs` and `case` may be `None`.
        quality: the evidence-quality verdict with its rule and detail, or
            `None` when the caller has only the bare grade (which is on
            `decision_core.evidence_quality` either way).
        now: the decision clock.
        cfg: loaded config; supplies `[agent].max_excerpt_chars`.
        policy_branch: `rli.policy.action.PolicyOutcome.branch`, when known.
    """
    structured_input: dict[str, Any] = {
        "now": to_utc_z(now),
        "identity": _identity(case),
        "decision": {
            "posting_state": decision_core.posting_state,
            "recommended_action": decision_core.recommended_action,
            "recheck_after_days": decision_core.recheck_after_days,
            "evidence_quality": decision_core.evidence_quality,
            "policy_branch": policy_branch,
        },
        "evidence_quality": (
            None
            if quality is None
            else {
                "quality": quality.quality,
                "rule": quality.rule,
                "detail": quality.detail,
                "contradictions": [
                    {
                        "kind": item.kind,
                        "detail": item.detail,
                        "evidence_ids": sorted(item.evidence_ids),
                    }
                    for item in quality.contradictions
                ],
            }
        ),
        "policy_inputs": {
            name: _json_scalar(getattr(inputs, name)) for name in type(inputs).model_fields
        },
        "unpopulated": sorted(inputs.unpopulated()),
        # Chronological (the run's own append order), not sorted: `e10` sorts
        # before `e2`, and the order the evidence arrived is the order a
        # reader — and the model — needs it in. Same reasoning as
        # `rli.agent.investigator`.
        "evidence": [_evidence_entry(item) for item in decision_core.evidence],
    }

    untrusted = [
        UntrustedBlock(
            source=item.id,
            # Truncate, THEN sanitize (module docstring).
            content=sanitize_untrusted((item.raw_excerpt or "")[: cfg.agent.max_excerpt_chars]),
        )
        for item in decision_core.evidence
        if item.raw_excerpt
    ]

    return structured_input, untrusted


# ---------------------------------------------------------------------------
# Citation validation (spec.md §9; PLAN.md M5)
# ---------------------------------------------------------------------------


class _CitationReport(BaseModel):
    """The result of checking one `ExplanationOutput`'s citations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reasons: tuple[ReasonItem, ...] = ()
    invalid_ids: int = 0
    duplicate_ids: int = 0
    dropped_reasons: int = 0


def _validate_citations(drafts: Sequence[ReasonDraft], evidence_ids: set[str]) -> _CitationReport:
    """Keep only reasons whose citations exist; count everything discarded.

    Per drafted reason: cited ids are filtered to those that actually exist
    in this run's evidence, PRESERVING the model's order and de-duplicating.
    A reason left with no valid id — or with no text — is dropped entirely,
    because spec.md §9 makes an uncited user-facing reason unpublishable.

    `invalid_ids` counts ids that name no evidence item; `duplicate_ids`
    counts repeats of an id that does exist. They are counted separately and
    only the first is reported as `citation_invalid`: a repeat is untidy, a
    fabrication is a correctness failure, and averaging the two would hide
    how often the model cites evidence that was never collected.
    """
    reasons: list[ReasonItem] = []
    invalid = 0
    duplicates = 0
    dropped = 0

    for draft in drafts:
        kept: list[str] = []
        seen: set[str] = set()
        for candidate in draft.evidence_ids:
            if candidate not in evidence_ids:
                invalid += 1
                continue
            if candidate in seen:
                duplicates += 1
                continue
            seen.add(candidate)
            kept.append(candidate)

        text = draft.text.strip()
        if not kept or not text:
            dropped += 1
            continue
        reasons.append(ReasonItem(text=text, evidence_ids=kept))

    return _CitationReport(
        reasons=tuple(reasons),
        invalid_ids=invalid,
        duplicate_ids=duplicates,
        dropped_reasons=dropped,
    )


def _clean_hypotheses(raw: Sequence[str]) -> list[str]:
    """Strip, drop empties, de-duplicate preserving order, cap at `MAX_HYPOTHESES`."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
        if len(cleaned) >= MAX_HYPOTHESES:
            break
    return cleaned


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


def _note(run: Run, decision_type: str, now: datetime, error: str | None = None) -> None:
    """One controller `run_steps` row, with the run clock as `created_at`.

    Written through `Run.step` rather than `ProbeRunner.note` because this
    module deliberately does not take a `ProbeRunner`: the explanation runs
    after the probe machinery has closed and needs none of it.
    """
    run.step(
        component="controller",
        decision_type=decision_type,
        error=error,
        created_at=now,
    )


def _record_fallback(run: Run, why: str, now: datetime, kept: int, detail: str = "") -> None:
    """The one place an `explanation_fallback:<why>` row is written.

    Shared by `explain` (the model answered unusably) and `skip_explanation`
    (the model was never asked), so every way of falling back to the
    deterministic reasons produces the same row shape in the same vocabulary.
    A reader grepping `explanation_fallback:` sees all of them, and a metrics
    query counting them cannot miss one because it was recorded elsewhere.
    """
    reason = (
        f"the model explanation was unusable: {why}"
        if why not in BUDGET_FALLBACKS
        else f"the explanation call was not affordable: {detail or why}"
    )
    _note(
        run,
        f"{STEP_EXPLANATION_FALLBACK}:{why}",
        now,
        error=f"using the deterministic explanation ({kept} reason(s)) because {reason}",
    )


def skip_explanation(
    *,
    run: Run,
    now: datetime,
    decision: Decision,
    fallback_reasons: Sequence[ReasonItem],
    cap: str,
    detail: str = "",
) -> Decision:
    """Publish the deterministic explanation WITHOUT making the model call.

    `rli.agent.loop` calls this when spec.md §4's run-level cost or latency
    cap leaves no room for the explanation call. spec.md §4's hard stop is
    stated for the run, not for the loop, so the last stage of the pipeline
    is budgeted like every other — see that module's docstring for why
    letting it run unbudgeted would make the cap advisory and understate C's
    cost in exactly the comparison spec.md §6's agent gate is built on.

    The result has the same shape as any other fallback: the deterministic
    reasons (`rli.policy.explain_stub`, already computed by
    `rli.eval.runner.decide_and_finish`) and NO hypotheses. Hypotheses are
    the model's speculative channel; there was no model answer, so there is
    nothing to keep — the same rule `explain` applies when a call fails.

    Arguments:
        run: the `Run` whose trace records the skip.
        now: the run clock, written as the row's `created_at`.
        decision: the frozen policy's decision. Read-only.
        fallback_reasons: the deterministic reasons to publish.
        cap: `"cost_cap"` or `"latency_cap"` — which budget bound.
        detail: human-readable numbers for the trace's `error` column.
    """
    reasons = list(fallback_reasons)
    _record_fallback(run, cap, now, len(reasons), detail)
    return decision.model_copy(update={"reason": reasons, "hypotheses": []})


def explain(
    *,
    llm: LLMClient,
    run: Run,
    cfg: Config,
    now: datetime,
    decision: Decision,
    inputs: PolicyInputs,
    quality: QualityVerdict | None,
    fallback_reasons: Sequence[ReasonItem],
    case: CaseState | None = None,
    policy_branch: str | None = None,
) -> Decision:
    """Attach LLM-authored, evidence-cited reasons to an already-made decision.

    Returns a NEW `Decision` — `decision` is never mutated — differing from
    its input in `reason` and `hypotheses` and in nothing else. spec.md §2
    forbids the model from choosing the action, so that invariant is checked
    explicitly before returning rather than merely intended.

    The call is never allowed to break the run. `LLMError` (including its
    `LLMSchemaError` subclass) and a bare pydantic `ValidationError` are
    caught, traced, and resolved to the deterministic fallback; so are a
    schema-valid reply with no reasons at all, and one whose every reason
    lost its citations. In each case a controller row records WHICH of the
    of them happened, because spec.md §6 measures "citation support" and each
    is a different failure with a different fix. `skip_explanation` adds two
    more reasons to the same vocabulary for the case where the call was never
    affordable at all.

    Arguments:
        llm: the structured-output client. The same one the loop used, so a
            `CachedClient` serves both templates from one `llm_cache`.
        run: the open (or already closed) `Run`; this writes `run_steps` rows
            and nothing else.
        cfg: loaded configuration.
        now: the run clock, written as every row's `created_at`.
        decision: the frozen policy's decision. Read-only.
        inputs: the policy inputs behind it, for the prompt.
        quality: the evidence-quality verdict, for the prompt.
        fallback_reasons: the deterministic reasons
            (`rli.policy.explain_stub.reasons_from_inputs`), already computed
            by `rli.eval.runner.decide_and_finish`. Passed rather than
            recomputed so a fallback explanation is byte-identical to the one
            System A and System B publish for the same case.
        case: the working case state, for the prompt's identity fields.
            Optional; `explain` is independently callable without one.
        policy_branch: the fired `rli.policy.action` branch, when known.
    """
    structured_input, untrusted = build_explanation_input(
        case=case,
        decision_core=decision,
        inputs=inputs,
        quality=quality,
        now=now,
        cfg=cfg,
        policy_branch=policy_branch,
    )
    prompt = build_explanation_prompt(structured_input=structured_input, untrusted=untrusted)
    prompt_hash = prompt.prompt_hash(ExplanationOutput)
    args_hash = prompt.structured_input_hash()

    output: ExplanationOutput | None = None
    failure: str | None = None

    try:
        response = llm.complete_structured(prompt, ExplanationOutput)
    except (LLMError, ValidationError) as exc:
        # `LLMSchemaError` subclasses `LLMError`; the two are separated here
        # only so the trace says whether the API broke or the model did.
        failure = (
            _FALLBACK_SCHEMA_ERROR
            if isinstance(exc, LLMSchemaError | ValidationError)
            else _FALLBACK_LLM_ERROR
        )
        run.step(
            component="model",
            # No `:tokens=` suffix — no usage was returned. See
            # `rli.agent.loop`'s token-convention judgment call.
            decision_type=STEP_EXPLANATION,
            prompt_hash=prompt_hash,
            args_hash=args_hash,
            model_id=llm.model_id,
            cost_usd=0.0,
            latency_s=0.0,
            error=f"{type(exc).__name__}: {' '.join(str(exc).split())[:400]}",
            created_at=now,
        )
    else:
        parsed = response.parsed
        if isinstance(parsed, ExplanationOutput):
            output = parsed
        else:  # pragma: no cover - client contract
            failure = _FALLBACK_SCHEMA_ERROR
        run.step(
            component="model",
            decision_type=with_tokens(
                STEP_EXPLANATION, response.input_tokens, response.output_tokens
            ),
            prompt_hash=prompt_hash,
            args_hash=args_hash,
            model_id=response.model_id,
            cache_status=response.cache_status,
            cost_usd=response.cost_usd,
            latency_s=response.latency_ms / 1000.0,
            error=(
                None
                if output is not None
                else f"client returned {type(parsed).__name__}, not ExplanationOutput"
            ),
            created_at=now,
        )

    # -- citation validation (spec.md §9) -----------------------------------
    reasons: list[ReasonItem] = []
    hypotheses: list[str] = []

    if output is not None:
        report = _validate_citations(output.reason, {item.id for item in decision.evidence})
        if report.invalid_ids:
            # The count is in `decision_type` (queryable); the breakdown is in
            # `error`, which is the only free-text column and which a
            # fabricated citation legitimately belongs in. See the docstring.
            _note(
                run,
                f"{STEP_CITATION_INVALID}:{report.invalid_ids}",
                now,
                error=(
                    f"{report.invalid_ids} cited evidence id(s) do not exist in this "
                    f"run's evidence; {report.dropped_reasons} reason(s) dropped for "
                    f"having no valid citation left; {report.duplicate_ids} duplicate "
                    f"citation(s) collapsed"
                ),
            )
        reasons = list(report.reasons)
        # Hypotheses survive a citation wipeout — see the module docstring.
        hypotheses = _clean_hypotheses(output.hypotheses)

        if not output.reason:
            failure = _FALLBACK_NO_REASONS
        elif not reasons:
            failure = _FALLBACK_ALL_CITATIONS_INVALID

    if failure is not None:
        reasons = list(fallback_reasons)
        _record_fallback(run, failure, now, len(reasons))

    enriched = decision.model_copy(update={"reason": reasons, "hypotheses": hypotheses})

    # spec.md §2: the LLM may not choose the action. `model_copy` cannot move
    # these fields given the update above, which is exactly why the check is
    # cheap — and why it is worth keeping: it turns "we were careful here"
    # into "this is enforced", and it will fail loudly if someone widens the
    # update dict. A raised error, not an `assert`: `python -O` must not be
    # able to switch off the guarantee spec.md §2 rests on.
    if (
        enriched.posting_state != decision.posting_state
        or enriched.recommended_action != decision.recommended_action
        or enriched.recheck_after_days != decision.recheck_after_days
        or enriched.evidence_quality != decision.evidence_quality
        or enriched.evidence != decision.evidence
    ):  # pragma: no cover - structurally unreachable
        raise RuntimeError(
            "the explanation changed a decided field; spec.md §2 forbids the LLM "
            "from choosing the action, the posting state, the recheck interval, "
            "the evidence quality or the evidence"
        )
    return enriched
