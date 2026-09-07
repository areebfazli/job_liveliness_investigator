"""Validation tests for rli.models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from rli.models import (
    UNKNOWN,
    CaseFile,
    Decision,
    EvidenceItem,
    PolicyInputs,
    ProbeResult,
    ReasonItem,
    Unknown,
    parse_utc,
    to_utc_z,
)

# ---------------------------------------------------------------------------
# EvidenceItem
# ---------------------------------------------------------------------------


def _valid_evidence_kwargs() -> dict:
    now = datetime.now(UTC)
    return {
        "id": "e1",
        "probe": "resolve_posting",
        "claim_type": "first_published",
        "value": "2026-08-20T10:00:00Z",
        "source_url": "https://boards.greenhouse.io/acme/jobs/123",
        "raw_excerpt": "Posted on Aug 20, 2026",
        "source_quality": "ats_native",
        "source_event_at": now,
        "available_at": now,
        "fetched_at": now,
    }


def test_evidence_item_accepts_valid_data() -> None:
    item = EvidenceItem(**_valid_evidence_kwargs())
    assert item.source_quality == "ats_native"


def test_evidence_item_rejects_bad_source_quality() -> None:
    kwargs = _valid_evidence_kwargs()
    kwargs["source_quality"] = "not_a_real_quality"
    with pytest.raises(ValidationError):
        EvidenceItem(**kwargs)


@pytest.mark.parametrize("field", ["available_at", "fetched_at", "source_event_at"])
def test_evidence_item_rejects_naive_datetime(field: str) -> None:
    kwargs = _valid_evidence_kwargs()
    kwargs[field] = datetime(2026, 8, 20, 10, 0, 0)  # naive
    with pytest.raises(ValidationError):
        EvidenceItem(**kwargs)


def test_evidence_item_normalizes_non_utc_offset() -> None:
    kwargs = _valid_evidence_kwargs()
    non_utc = datetime(2026, 8, 20, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    kwargs["available_at"] = non_utc
    item = EvidenceItem(**kwargs)
    assert item.available_at == non_utc.astimezone(UTC)
    assert item.available_at.utcoffset() == timedelta(0)


def test_evidence_item_optional_fields_default_to_none() -> None:
    kwargs = _valid_evidence_kwargs()
    kwargs["raw_excerpt"] = None
    kwargs["source_event_at"] = None
    kwargs.pop("run_id", None)
    item = EvidenceItem(**kwargs)
    assert item.raw_excerpt is None
    assert item.source_event_at is None
    assert item.run_id is None


def test_evidence_item_run_id_accepts_none_and_value() -> None:
    kwargs = _valid_evidence_kwargs()
    item = EvidenceItem(**kwargs, run_id=None)
    assert item.run_id is None

    item2 = EvidenceItem(**kwargs, run_id="run-123")
    assert item2.run_id == "run-123"


# ---------------------------------------------------------------------------
# PolicyInputs
# ---------------------------------------------------------------------------

EXPECTED_POLICY_INPUT_FIELDS = {
    "posting_state",
    "publish_recency",
    "material_negative_event",
    "freeze_or_pause",
    "declared_expiry",
    "repost_pattern",
    "corroborating_hiring_signal",
}


def test_policy_inputs_unpopulated_returns_exactly_seven_fields() -> None:
    inputs = PolicyInputs()
    assert inputs.unpopulated() == EXPECTED_POLICY_INPUT_FIELDS
    assert "evidence_quality" not in inputs.unpopulated()


def test_policy_inputs_known_absent_values_are_populated() -> None:
    inputs = PolicyInputs(
        declared_expiry=None,
        material_negative_event=False,
        freeze_or_pause=False,
        corroborating_hiring_signal=False,
        repost_pattern="none",
        posting_state="unknown",
    )
    unpopulated = inputs.unpopulated()
    assert "declared_expiry" not in unpopulated
    assert "material_negative_event" not in unpopulated
    assert "freeze_or_pause" not in unpopulated
    assert "corroborating_hiring_signal" not in unpopulated
    assert "repost_pattern" not in unpopulated
    assert "posting_state" not in unpopulated
    # publish_recency was never set, so it remains unpopulated.
    assert unpopulated == {"publish_recency"}


def test_policy_inputs_json_round_trip_preserves_unknown_and_known_absent() -> None:
    inputs = PolicyInputs(
        declared_expiry=None,
        material_negative_event=False,
        freeze_or_pause=False,
        corroborating_hiring_signal=False,
        repost_pattern="none",
        posting_state="unknown",
    )
    assert inputs.publish_recency is UNKNOWN

    restored = PolicyInputs.model_validate_json(inputs.model_dump_json())
    assert restored.publish_recency is UNKNOWN
    assert restored.declared_expiry is None
    assert restored.material_negative_event is False
    assert restored.freeze_or_pause is False
    assert restored.corroborating_hiring_signal is False
    assert restored.repost_pattern == "none"
    assert restored.posting_state == "unknown"
    assert restored.unpopulated() == {"publish_recency"}


def test_policy_inputs_naive_declared_expiry_rejected() -> None:
    with pytest.raises(ValidationError):
        PolicyInputs(declared_expiry=datetime(2026, 8, 20, 10, 0, 0))


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _valid_evidence_item() -> EvidenceItem:
    return EvidenceItem(**_valid_evidence_kwargs())


def test_decision_valid_construction() -> None:
    decision = Decision(
        posting_state="open",
        recommended_action="apply_now",
        recheck_after_days=14,
        evidence_quality="strong",
        hypotheses=["still hiring"],
        reason=[ReasonItem(text="Posted recently", evidence_ids=["e1"])],
        evidence=[_valid_evidence_item()],
    )
    assert decision.posting_state == "open"
    assert decision.recommended_action == "apply_now"


def test_decision_rejects_invalid_recommended_action() -> None:
    with pytest.raises(ValidationError):
        Decision(
            posting_state="open",
            recommended_action="not_a_real_action",
            evidence_quality="strong",
        )


def test_decision_rejects_invalid_posting_state() -> None:
    with pytest.raises(ValidationError):
        Decision(
            posting_state="not_a_real_state",
            recommended_action="apply_now",
            evidence_quality="strong",
        )


def test_reason_item_requires_text_and_evidence_ids() -> None:
    with pytest.raises(ValidationError):
        ReasonItem(text="missing evidence ids")

    with pytest.raises(ValidationError):
        ReasonItem(evidence_ids=["e1"])

    item = ReasonItem(text="ok", evidence_ids=["e1"])
    assert item.text == "ok"
    assert item.evidence_ids == ["e1"]


def test_decision_mutable_defaults_are_independent_per_instance() -> None:
    d1 = Decision(posting_state="open", recommended_action="apply_now", evidence_quality="strong")
    d2 = Decision(posting_state="open", recommended_action="apply_now", evidence_quality="strong")

    d1.hypotheses.append("mutated")
    d1.reason.append(ReasonItem(text="mutated", evidence_ids=["e1"]))
    d1.evidence.append(_valid_evidence_item())

    assert d2.hypotheses == []
    assert d2.reason == []
    assert d2.evidence == []


# ---------------------------------------------------------------------------
# ProbeResult
# ---------------------------------------------------------------------------


def test_probe_result_ok_minimal_construction() -> None:
    result = ProbeResult(ok=True)
    assert result.ok is True
    assert result.retryable is False
    assert result.error is None


def test_probe_result_failure_shape() -> None:
    result = ProbeResult(ok=False, error="timeout", retryable=True)
    assert result.ok is False
    assert result.error == "timeout"
    assert result.retryable is True


def test_probe_result_retryable_defaults_to_false() -> None:
    result = ProbeResult(ok=False, error="boom")
    assert result.retryable is False


# ---------------------------------------------------------------------------
# CaseFile
# ---------------------------------------------------------------------------


def test_case_file_constructs_with_defaults() -> None:
    case = CaseFile(
        posting_id="p1",
        company_id="acme.com",
        canonical_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    assert case.evidence == []
    assert isinstance(case.policy_inputs, PolicyInputs)


def test_case_file_policy_inputs_default_is_independent_per_instance() -> None:
    c1 = CaseFile(
        posting_id="p1", company_id="acme.com", canonical_url="https://example.com/1"
    )
    c2 = CaseFile(
        posting_id="p2", company_id="acme.com", canonical_url="https://example.com/2"
    )
    c1.policy_inputs = c1.policy_inputs.model_copy(update={"posting_state": "open"})
    assert c2.policy_inputs.posting_state is UNKNOWN
    assert c1.policy_inputs is not c2.policy_inputs


# ---------------------------------------------------------------------------
# rli.models.time
# ---------------------------------------------------------------------------


def test_to_utc_z_produces_z_suffixed_string() -> None:
    dt = datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC)
    text = to_utc_z(dt)
    assert text.endswith("Z")
    assert "+00:00" not in text


def test_to_utc_z_converts_non_utc_offset() -> None:
    dt = datetime(2026, 8, 20, 15, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    text = to_utc_z(dt)
    assert text == "2026-08-20T10:00:00.000000Z"


def test_to_utc_z_always_emits_a_fixed_width_microsecond_fraction() -> None:
    """The fraction is never elided — that is what keeps TEXT ordering sane.

    `datetime.isoformat()` drops the fractional part when `microsecond == 0`,
    which would make a whole-second stamp sort AFTER a later sub-second one.
    """
    whole = to_utc_z(datetime(2026, 8, 20, 10, 0, 0, 0, tzinfo=UTC))
    sub = to_utc_z(datetime(2026, 8, 20, 10, 0, 0, 500000, tzinfo=UTC))
    assert whole == "2026-08-20T10:00:00.000000Z"
    assert sub == "2026-08-20T10:00:00.500000Z"
    assert len(whole) == len(sub)


def test_parse_utc_round_trips_to_utc_z() -> None:
    dt = datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC)
    text = to_utc_z(dt)
    parsed = parse_utc(text)
    assert parsed == dt


@pytest.mark.parametrize(
    "bad_value",
    ["not-a-timestamp", "2026-08-20T10:00:00"],  # malformed / no offset
)
def test_parse_utc_raises_on_bad_input(bad_value: str) -> None:
    with pytest.raises(ValueError):
        parse_utc(bad_value)


def test_to_utc_z_raises_on_naive_datetime() -> None:
    with pytest.raises(ValueError):
        to_utc_z(datetime(2026, 8, 20, 10, 0, 0))


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        # plain whole seconds
        (
            datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 20, 10, 0, 1, tzinfo=UTC),
        ),
        # regression: whole second vs. a LATER sub-second stamp in the same
        # second. Without a fixed-width fraction this comparison inverts,
        # because "Z" (0x5A) > "." (0x2E).
        (
            datetime(2026, 8, 20, 10, 0, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 20, 10, 0, 0, 500000, tzinfo=UTC),
        ),
        # ... and across a second boundary from a sub-second stamp
        (
            datetime(2026, 8, 20, 10, 0, 0, 999999, tzinfo=UTC),
            datetime(2026, 8, 20, 10, 0, 1, 0, tzinfo=UTC),
        ),
    ],
)
def test_to_utc_z_lexical_ordering_matches_chronological_ordering(
    earlier: datetime, later: datetime
) -> None:
    """`ORDER BY <text timestamp>` in schema.sql depends on exactly this."""
    assert earlier < later
    assert to_utc_z(earlier) < to_utc_z(later)


def test_unknown_enum_sentinel_identity() -> None:
    assert UNKNOWN is Unknown.UNKNOWN
