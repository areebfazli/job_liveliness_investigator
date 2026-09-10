"""Tests for System C's LLM evidence-cited explanation step (spec.md §2/§9).

`rli.agent.explanation.explain` is the last pipeline step: the action is
already decided, and this module may touch only `reason` and `hypotheses`.
The invariants under test throughout mirror `tests/test_policy_explain.py`'s
register for the deterministic fallback: every published reason cites at
least one real evidence id, an unusable model reply degrades to the
deterministic fallback rather than an empty `reason`, and the four ways a
reply can be unusable are each traced under their own `run_steps` label.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from rli.agent.explanation import (
    MAX_HYPOTHESES,
    STEP_CITATION_INVALID,
    STEP_CITATION_UNSUPPORTED,
    STEP_EXPLANATION_FALLBACK,
    ExplanationOutput,
    ReasonDraft,
    _clean_hypotheses,
    build_explanation_input,
    explain,
)
from rli.agent.loop import STEP_EXPLANATION, parse_tokens
from rli.config import Config
from rli.eval.runner import Run
from rli.llm.client import LLMError, LLMSchemaError, ScriptedClient
from rli.models.decision import Decision, ReasonItem
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import PolicyInputs

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _evidence(
    eid: str, *, raw_excerpt: str | None = None, claim_type: str = "posting_state"
) -> EvidenceItem:
    return EvidenceItem(
        id=eid,
        probe="resolve_posting",
        claim_type=claim_type,
        value="open",
        source_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs/1",
        raw_excerpt=raw_excerpt,
        source_quality="ats_native",
        source_event_at=NOW,
        available_at=NOW,
        fetched_at=NOW,
    )


def _decision(evidence: Sequence[EvidenceItem]) -> Decision:
    """A frozen-policy decision with no reasons/hypotheses yet, ready for `explain`."""
    return Decision(
        posting_state="open",
        recommended_action="apply_now",
        recheck_after_days=None,
        evidence_quality="strong",
        hypotheses=[],
        reason=[],
        evidence=list(evidence),
    )


def _open_run(conn: sqlite3.Connection, cfg: Config) -> Run:
    """An opened `Run`, with no `postings` row required (`explain` only calls `run.step`)."""
    run = Run(
        conn,
        cfg,
        input_url="https://boards.greenhouse.io/acme/jobs/1",
        system="C",
        config_hash="test-config-hash",
        started_at=NOW,
    )
    run.open()
    return run


def _run_steps(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index", (run_id,)
    ).fetchall()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_happy_path_keeps_only_reason_and_hypotheses_and_does_not_mutate_input(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`explain` may change `reason`/`hypotheses` only, and never the input `Decision`."""
    evidence = [_evidence("e1"), _evidence("e2")]
    decision = _decision(evidence)
    original_reason = list(decision.reason)
    original_hypotheses = list(decision.hypotheses)

    output = ExplanationOutput(
        reason=[ReasonDraft(text="The posting was still listed.", evidence_ids=["e1"])],
        hypotheses=["Possibly evergreen."],
    )
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(posting_state="open"),
        quality=None,
        fallback_reasons=[ReasonItem(text="fallback", evidence_ids=["e1"])],
    )

    assert result is not decision
    assert result.reason == [ReasonItem(text="The posting was still listed.", evidence_ids=["e1"])]
    assert result.hypotheses == ["Possibly evergreen."]

    # spec.md §2 hard invariant: nothing else moved.
    assert result.posting_state == decision.posting_state
    assert result.recommended_action == decision.recommended_action
    assert result.recheck_after_days == decision.recheck_after_days
    assert result.evidence_quality == decision.evidence_quality
    assert result.evidence == decision.evidence

    # The input Decision was not mutated in place.
    assert decision.reason == original_reason
    assert decision.hypotheses == original_hypotheses


# ---------------------------------------------------------------------------
# 2. Invalid evidence id dropped; duplicate real id is not an invalid one
# ---------------------------------------------------------------------------


def test_fabricated_citation_is_dropped_and_duplicate_real_id_is_counted_separately(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A reason citing `[real, real, fabricated]` keeps `[real]`, and `citation_invalid`
    counts only the fabrication — the repeated real id is a `duplicate_ids`, not an
    `invalid_ids` (see `_validate_citations`'s docstring)."""
    evidence = [_evidence("e1")]
    decision = _decision(evidence)
    output = ExplanationOutput(
        reason=[ReasonDraft(text="Still listed.", evidence_ids=["e1", "e1", "e9"])]
    )
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=[],
    )

    # The reason survives, keeping only the one real, de-duplicated id.
    assert result.reason == [ReasonItem(text="Still listed.", evidence_ids=["e1"])]

    rows = _run_steps(conn, run.id)
    citation_rows = [r for r in rows if r["decision_type"].startswith(STEP_CITATION_INVALID)]
    assert len(citation_rows) == 1
    # Exactly one fabricated id ("e9"); the repeated "e1" must not inflate this.
    assert citation_rows[0]["decision_type"] == f"{STEP_CITATION_INVALID}:1"
    assert "1 cited evidence id(s) do not exist" in citation_rows[0]["error"]
    assert "1 duplicate citation(s) collapsed" in citation_rows[0]["error"]


# ---------------------------------------------------------------------------
# 3. A reason whose citations are all invalid is dropped entirely
# ---------------------------------------------------------------------------


def test_reason_with_only_fabricated_citations_is_dropped_while_others_survive(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    evidence = [_evidence("e1")]
    decision = _decision(evidence)
    output = ExplanationOutput(
        reason=[
            ReasonDraft(text="Entirely made up.", evidence_ids=["e9", "e10"]),
            ReasonDraft(text="Still listed.", evidence_ids=["e1"]),
        ]
    )
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=[ReasonItem(text="fallback", evidence_ids=["e1"])],
    )

    # Only the well-cited reason survives; the fabricated one is gone entirely.
    assert result.reason == [ReasonItem(text="Still listed.", evidence_ids=["e1"])]

    rows = _run_steps(conn, run.id)
    # A usable reason remained, so no fallback path fires.
    assert not any(r["decision_type"].startswith(STEP_EXPLANATION_FALLBACK) for r in rows)
    assert any(r["decision_type"] == f"{STEP_CITATION_INVALID}:2" for r in rows)


# ---------------------------------------------------------------------------
# 3b. Citation SUPPORT guard: an existing id is not necessarily a relevant one
# ---------------------------------------------------------------------------


def test_reason_citing_unrelated_but_existing_evidence_is_dropped_as_unsupported(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A reason whose TEXT is about a company layoff, but whose only citation is an
    existing-but-unrelated `posting_state` id, has a citation that resolves and no
    support for what it says — spec.md §9 does not consider that publishable."""
    evidence = [_evidence("e1", claim_type="posting_state")]
    decision = _decision(evidence)
    output = ExplanationOutput(
        reason=[ReasonDraft(text="The company announced a layoff recently.", evidence_ids=["e1"])]
    )
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=[ReasonItem(text="fallback", evidence_ids=["e1"])],
    )

    # The reason had a valid (existing) citation, but it is unrelated to the
    # claim: it must not survive.
    assert result.reason != [
        ReasonItem(text="The company announced a layoff recently.", evidence_ids=["e1"])
    ]

    rows = _run_steps(conn, run.id)
    unsupported_rows = [r for r in rows if r["decision_type"].startswith(STEP_CITATION_UNSUPPORTED)]
    assert len(unsupported_rows) == 1
    assert unsupported_rows[0]["decision_type"] == f"{STEP_CITATION_UNSUPPORTED}:1"
    assert "company_event" in unsupported_rows[0]["error"]


def test_reason_citing_matching_claim_type_survives_support_guard(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The same layoff-shaped reason, citing an actual `layoff` evidence item, must
    survive: the citation is both real and on-topic."""
    evidence = [_evidence("e1", claim_type="layoff")]
    decision = _decision(evidence)
    output = ExplanationOutput(
        reason=[ReasonDraft(text="The company announced a layoff recently.", evidence_ids=["e1"])]
    )
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=[],
    )

    assert result.reason == [
        ReasonItem(text="The company announced a layoff recently.", evidence_ids=["e1"])
    ]
    rows = _run_steps(conn, run.id)
    assert not any(r["decision_type"].startswith(STEP_CITATION_UNSUPPORTED) for r in rows)


def test_reason_with_unclassifiable_text_survives_regardless_of_citation(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A reason whose text matches NO `FAMILY_KEYWORDS` keyword at all is kept no
    matter what it cites: the classifier is deliberately crude, and a lexicon gap
    must never cost a real reason (module docstring's judgment call)."""
    evidence = [_evidence("e1", claim_type="posting_state")]
    decision = _decision(evidence)
    text = "This role seems like a great fit for the candidate's skill set."
    output = ExplanationOutput(reason=[ReasonDraft(text=text, evidence_ids=["e1"])])
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=[],
    )

    assert result.reason == [ReasonItem(text=text, evidence_ids=["e1"])]
    rows = _run_steps(conn, run.id)
    assert not any(r["decision_type"].startswith(STEP_CITATION_UNSUPPORTED) for r in rows)


def test_all_reasons_dropped_by_mix_of_invalid_and_unsupported_falls_back(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """When every reason is dropped — one for a fabricated id, one for an
    existing-but-unsupported id — the deterministic fallback still fires, under the
    `all_citations_unsupported` label (not `all_citations_invalid`, since a support
    drop, not just an invalid-id drop, contributed to the wipeout)."""
    evidence = [_evidence("e1", claim_type="posting_state")]
    decision = _decision(evidence)
    fallback = [ReasonItem(text="Deterministic fallback.", evidence_ids=["e1"])]
    output = ExplanationOutput(
        reason=[
            ReasonDraft(text="Entirely made up.", evidence_ids=["e9"]),
            ReasonDraft(text="The company had a layoff.", evidence_ids=["e1"]),
        ],
        hypotheses=["Possibly evergreen."],
    )
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=fallback,
    )

    assert result.reason == fallback
    assert result.reason
    # Hypotheses still survive a full citation wipeout.
    assert result.hypotheses == ["Possibly evergreen."]

    rows = _run_steps(conn, run.id)
    fallback_rows = [r for r in rows if r["decision_type"].startswith(STEP_EXPLANATION_FALLBACK)]
    assert len(fallback_rows) == 1
    assert (
        fallback_rows[0]["decision_type"]
        == f"{STEP_EXPLANATION_FALLBACK}:all_citations_unsupported"
    )


# ---------------------------------------------------------------------------
# 4. The four fallback paths
# ---------------------------------------------------------------------------


def test_fallback_on_llm_error(conn: sqlite3.Connection, cfg: Config) -> None:
    evidence = [_evidence("e1")]
    decision = _decision(evidence)
    fallback = [ReasonItem(text="Deterministic fallback.", evidence_ids=["e1"])]
    client = ScriptedClient([LLMError("transport blew up")])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=fallback,
    )

    assert result.reason == fallback
    assert result.reason  # never empty when the fallback content is non-empty
    # No valid schema reply at all: hypotheses cannot survive a call that never answered.
    assert result.hypotheses == []

    rows = _run_steps(conn, run.id)
    assert any(r["decision_type"] == f"{STEP_EXPLANATION_FALLBACK}:llm_error" for r in rows)


def test_fallback_on_schema_error(conn: sqlite3.Connection, cfg: Config) -> None:
    evidence = [_evidence("e1")]
    decision = _decision(evidence)
    fallback = [ReasonItem(text="Deterministic fallback.", evidence_ids=["e1"])]
    client = ScriptedClient([LLMSchemaError("model returned nonsense")])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=fallback,
    )

    assert result.reason == fallback
    assert result.reason
    assert result.hypotheses == []

    rows = _run_steps(conn, run.id)
    assert any(r["decision_type"] == f"{STEP_EXPLANATION_FALLBACK}:schema_error" for r in rows)


def test_fallback_on_all_citations_invalid_keeps_hypotheses(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A schema-valid reply survived; its hypotheses are kept even though every
    reason was dropped for having only fabricated citations."""
    evidence = [_evidence("e1")]
    decision = _decision(evidence)
    fallback = [ReasonItem(text="Deterministic fallback.", evidence_ids=["e1"])]
    output = ExplanationOutput(
        reason=[ReasonDraft(text="Entirely made up.", evidence_ids=["e9"])],
        hypotheses=["Possibly evergreen."],
    )
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=fallback,
    )

    assert result.reason == fallback
    assert result.reason
    assert result.hypotheses == ["Possibly evergreen."]

    rows = _run_steps(conn, run.id)
    assert any(
        r["decision_type"] == f"{STEP_EXPLANATION_FALLBACK}:all_citations_invalid" for r in rows
    )


def test_fallback_on_no_reasons_keeps_hypotheses(conn: sqlite3.Connection, cfg: Config) -> None:
    """A schema-valid reply with an empty `reason` list still keeps its hypotheses."""
    evidence = [_evidence("e1")]
    decision = _decision(evidence)
    fallback = [ReasonItem(text="Deterministic fallback.", evidence_ids=["e1"])]
    output = ExplanationOutput(reason=[], hypotheses=["Possibly evergreen."])
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    result = explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=fallback,
    )

    assert result.reason == fallback
    assert result.reason
    assert result.hypotheses == ["Possibly evergreen."]

    rows = _run_steps(conn, run.id)
    assert any(r["decision_type"] == f"{STEP_EXPLANATION_FALLBACK}:no_reasons" for r in rows)


# ---------------------------------------------------------------------------
# 5. Hypotheses hygiene
# ---------------------------------------------------------------------------


def test_clean_hypotheses_strips_deduplicates_and_caps() -> None:
    raw = [
        "  Maybe evergreen.  ",
        "",
        "   ",
        "Maybe evergreen.",
        "Could be seasonal.",
        "A",
        "B",
        "C",
        "D",
    ]
    cleaned = _clean_hypotheses(raw)

    assert cleaned == ["Maybe evergreen.", "Could be seasonal.", "A", "B", "C"]
    assert len(cleaned) == MAX_HYPOTHESES


# ---------------------------------------------------------------------------
# 6. Prompt hygiene / determinism
# ---------------------------------------------------------------------------


def test_prompt_carries_no_raw_excerpt_text_and_untrusted_sources_are_real_ids(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    secret_text = "SECRET_EXCERPT_TEXT_MUST_NOT_LEAK_INTO_STRUCTURED_INPUT"
    evidence = [_evidence("e1", raw_excerpt=secret_text), _evidence("e2")]
    decision = _decision(evidence)
    output = ExplanationOutput(reason=[ReasonDraft(text="Still listed.", evidence_ids=["e1"])])
    client = ScriptedClient([output])
    run = _open_run(conn, cfg)

    explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=[],
    )

    assert len(client.calls) == 1
    prompt, schema = client.calls[0]
    assert schema is ExplanationOutput
    assert prompt.template_id == "explanation"

    serialized = json.dumps(prompt.structured_input)
    assert secret_text not in serialized

    known_ids = {item.id for item in evidence}
    assert prompt.untrusted, "the excerpt-bearing item should have produced an untrusted block"
    for block in prompt.untrusted:
        assert block.source in known_ids


def test_build_explanation_input_is_deterministic(cfg: Config) -> None:
    evidence = [_evidence("e1", raw_excerpt="An excerpt."), _evidence("e2")]
    decision = _decision(evidence)
    kwargs = dict(
        case=None,
        decision_core=decision,
        inputs=PolicyInputs(posting_state="open", publish_recency="recent"),
        quality=None,
        now=NOW,
        cfg=cfg,
        policy_branch="branch_x",
    )

    first_structured, first_untrusted = build_explanation_input(**kwargs)
    second_structured, second_untrusted = build_explanation_input(**kwargs)

    assert json.dumps(first_structured, sort_keys=True) == json.dumps(
        second_structured, sort_keys=True
    )
    assert first_untrusted == second_untrusted


# ---------------------------------------------------------------------------
# 7. The model row
# ---------------------------------------------------------------------------


def test_model_row_records_prompt_hash_and_recoverable_tokens(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    evidence = [_evidence("e1")]
    decision = _decision(evidence)
    output = ExplanationOutput(reason=[ReasonDraft(text="Still listed.", evidence_ids=["e1"])])
    client = ScriptedClient([output], input_tokens=321, output_tokens=64)
    run = _open_run(conn, cfg)

    explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=[],
    )

    rows = _run_steps(conn, run.id)
    model_rows = [r for r in rows if r["component"] == "model"]
    assert len(model_rows) == 1
    row = model_rows[0]
    assert row["decision_type"].startswith(STEP_EXPLANATION)
    assert row["model_id"] is not None
    assert row["prompt_hash"] is not None
    assert row["error"] is None
    assert parse_tokens(row["decision_type"]) == (321, 64)


def test_model_row_has_non_null_error_on_llm_failure(conn: sqlite3.Connection, cfg: Config) -> None:
    evidence = [_evidence("e1")]
    decision = _decision(evidence)
    client = ScriptedClient([LLMError("network blew up")])
    run = _open_run(conn, cfg)

    explain(
        llm=client,
        run=run,
        cfg=cfg,
        now=NOW,
        decision=decision,
        inputs=PolicyInputs(),
        quality=None,
        fallback_reasons=[],
    )

    rows = _run_steps(conn, run.id)
    model_rows = [r for r in rows if r["component"] == "model"]
    assert len(model_rows) == 1
    row = model_rows[0]
    # The row still exists, with a real model id, a prompt hash, and no
    # token suffix on the decision_type -- but a non-null error.
    assert row["decision_type"] == STEP_EXPLANATION
    assert row["model_id"] is not None
    assert row["prompt_hash"] is not None
    assert row["error"] is not None
    assert parse_tokens(row["decision_type"]) is None
