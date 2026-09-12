"""`team_signal` probe: board-history source, claim shapes, eligibility (spec.md §4/§5)."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from rli.config import Config, TeamSignal
from rli.models.evidence import EvidenceItem
from rli.models.probe import ProbeResult
from rli.net import hash_args
from rli.policy.inputs import CLAIM_TEAM_SIGNAL, derive_policy_inputs
from rli.probes.base import ProbeContext
from rli.probes.team_signal import (
    CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE,
    NullTeamSignalSource,
    TeamSignalArgs,
    TeamSignalProbe,
    team_signal,
)

COMPANY = "acme.com"
POSTING = "greenhouse:acme:J1"
NOW = datetime(2026, 9, 7, tzinfo=UTC)
START = datetime(2026, 1, 1, tzinfo=UTC)


def _z(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _day(offset: int) -> datetime:
    return START + timedelta(days=offset)


def _company(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?, ?, ?, ?)",
        (COMPANY, "Acme", COMPANY, _z(START)),
    )


def _posting(
    conn: sqlite3.Connection,
    posting_id: str,
    *,
    job_id: str,
    team: str | None,
    title: str = "Backend Engineer",
    first_observed: datetime | None = None,
    first_seen_absent: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO postings (posting_id, company_id, ats, ats_tenant_id, ats_job_id,
                              canonical_url, title, team, location, created_at, updated_at,
                              first_observed, first_seen_absent)
        VALUES (?, ?, 'greenhouse', 'acme', ?, ?, ?, ?, 'Remote', ?, ?, ?, ?)
        """,
        (
            posting_id,
            COMPANY,
            job_id,
            f"https://boards.greenhouse.io/acme/jobs/{job_id}",
            title,
            team,
            _z(START),
            _z(START),
            None if first_observed is None else _z(first_observed),
            None if first_seen_absent is None else _z(first_seen_absent),
        ),
    )


def _capture(
    conn: sqlite3.Connection,
    captured_at: datetime,
    job_ids: list[str],
    *,
    source: str = "own",
    coverage_status: str = "complete",
) -> int:
    cursor = conn.execute(
        "INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status) "
        "VALUES (?, ?, ?, ?)",
        (COMPANY, _z(captured_at), source, coverage_status),
    )
    snapshot_id = int(cursor.lastrowid)
    for job_id in job_ids:
        conn.execute(
            "INSERT INTO board_snapshot_jobs "
            "(board_snapshot_id, job_id, title, team, location, description_hash, url) "
            "VALUES (?, ?, 'Backend Engineer', 'Engineering', 'Remote', 'h1', ?)",
            (snapshot_id, job_id, None),
        )
    return snapshot_id


def _deep_history(conn: sqlite3.Connection, *, days: int = 40) -> None:
    """Daily 'complete' captures over `days` -> history deep and fully covered."""
    for offset in range(days + 1):
        _capture(conn, _day(offset), [])


def _ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    return ctx_factory(now=lambda: NOW)


def _with_team_signal(ctx: ProbeContext, enabled: bool) -> ProbeContext:
    """A context whose frozen Config has `[team_signal].enabled = enabled`."""
    new_cfg: Config = ctx.config.model_copy(update={"team_signal": TeamSignal(enabled=enabled)})
    return dataclasses.replace(ctx, config=new_cfg)


def _args(posting_id: str = POSTING, *, as_of: datetime = NOW) -> TeamSignalArgs:
    return TeamSignalArgs(posting_id=posting_id, company_id=COMPANY, as_of=as_of)


# ---------------------------------------------------------------------------
# New roles in window -> True
# ---------------------------------------------------------------------------


def test_new_roles_inside_window_signal_true_with_a_team_new_roles_claim(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team="Engineering",
        first_observed=NOW - timedelta(days=10),
    )
    conn.commit()

    result = team_signal(_args(), _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["corroborating_hiring_signal"] is True
    assert result.data["new_roles_30d"] == 1
    assert result.data["closures_60d"] == 0

    by_type = {c.claim_type: c for c in result.data["evidence"]}
    assert set(by_type) == {"team_new_roles", CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE}
    assert by_type["team_new_roles"].value == "1"
    assert by_type[CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE].value == "true"


# ---------------------------------------------------------------------------
# Closures in window, zero new roles -> True
# ---------------------------------------------------------------------------


def test_closures_inside_window_with_zero_new_roles_signal_true(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team="Engineering",
        first_observed=_day(0),
        first_seen_absent=NOW - timedelta(days=20),
    )
    conn.commit()

    result = team_signal(_args(), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["corroborating_hiring_signal"] is True
    assert result.data["new_roles_30d"] == 0
    assert result.data["closures_60d"] == 1

    by_type = {c.claim_type: c for c in result.data["evidence"]}
    assert set(by_type) == {"team_closures", CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE}
    assert by_type["team_closures"].value == "1"
    assert by_type[CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE].value == "true"


# ---------------------------------------------------------------------------
# No activity, deep history and coverage -> False
# ---------------------------------------------------------------------------


def test_no_activity_with_deep_history_and_coverage_signals_false(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering", first_observed=_day(0))
    conn.commit()

    result = team_signal(_args(), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["corroborating_hiring_signal"] is False
    claim_types = {c.claim_type for c in result.data["evidence"]}
    assert "team_new_roles" not in claim_types
    assert "team_closures" not in claim_types
    assert claim_types == {CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE}
    boolean_claim = next(
        c for c in result.data["evidence"] if c.claim_type == CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE
    )
    assert boolean_claim.value == "false"


# ---------------------------------------------------------------------------
# No activity, thin history -> Unknown, no boolean claim at all
# ---------------------------------------------------------------------------


def test_thin_history_with_no_activity_is_unknown_and_emits_no_boolean_claim(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _capture(conn, _day(0), [])
    _capture(conn, _day(5), [])
    _posting(conn, POSTING, job_id="J1", team="Engineering", first_observed=_day(0))
    conn.commit()

    result = team_signal(_args(), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["corroborating_hiring_signal"] is None
    claim_types = [c.claim_type for c in result.data["evidence"]]
    assert CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE not in claim_types


# ---------------------------------------------------------------------------
# No team on the posting -> company-wide pooling
# ---------------------------------------------------------------------------


def test_posting_with_no_team_pools_the_whole_company(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team=None)
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team="Sales",
        first_observed=NOW - timedelta(days=5),
    )
    conn.commit()

    result = team_signal(_args(), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["scope"] == "company"
    assert result.data["team"] is None
    assert result.data["corroborating_hiring_signal"] is True
    assert result.data["new_roles_30d"] == 1


# ---------------------------------------------------------------------------
# Team normalization
# ---------------------------------------------------------------------------


def test_team_normalization_pools_case_and_whitespace_variants(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team=" engineering ",
        first_observed=NOW - timedelta(days=5),
    )
    _posting(
        conn,
        "greenhouse:acme:J3",
        job_id="J3",
        team="ENGINEERING",
        first_observed=NOW - timedelta(days=8),
    )
    conn.commit()

    result = team_signal(_args(), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["scope"] == "team"
    assert result.data["new_roles_30d"] == 2


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_probe_metadata(conn: sqlite3.Connection, ctx_factory) -> None:
    assert TeamSignalProbe.history_required is True
    assert TeamSignalProbe.cost_tier == "high"
    assert TeamSignalProbe.populates == frozenset({"corroborating_hiring_signal"})
    assert TeamSignalProbe.allowlist(_ctx(ctx_factory).config) == []


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_eligible_is_false_when_disabled_even_with_deep_history(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    conn.commit()
    ctx = _with_team_signal(_ctx(ctx_factory), False)

    assert TeamSignalProbe.eligible(ctx, _args()) is False


def test_eligible_is_false_when_history_is_too_thin(conn: sqlite3.Connection, ctx_factory) -> None:
    _company(conn)
    _capture(conn, _day(0), [])
    _capture(conn, _day(5), [])
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    conn.commit()

    assert TeamSignalProbe.eligible(_ctx(ctx_factory), _args()) is False


def test_eligible_is_true_when_enabled_with_sufficient_history(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    conn.commit()

    assert TeamSignalProbe.eligible(_ctx(ctx_factory), _args()) is True


def test_eligible_is_false_for_a_nonexistent_posting(conn: sqlite3.Connection, ctx_factory) -> None:
    assert TeamSignalProbe.eligible(_ctx(ctx_factory), _args("nope")) is False


# ---------------------------------------------------------------------------
# Missing posting
# ---------------------------------------------------------------------------


def test_missing_posting_is_a_structured_non_retryable_failure(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    result = team_signal(_args("greenhouse:acme:missing"), _ctx(ctx_factory))

    assert result.ok is False
    assert result.retryable is False
    assert "greenhouse:acme:missing" in (result.error or "")
    assert result.data == {
        "posting_id": "greenhouse:acme:missing",
        "company_id": COMPANY,
        "evidence": [],
    }


# ---------------------------------------------------------------------------
# The boolean claim_type literal must match rli.policy.inputs exactly
# ---------------------------------------------------------------------------


def test_boolean_claim_type_matches_the_policy_layers_literal() -> None:
    assert CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE == "corroborating_hiring_signal"
    assert CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE == CLAIM_TEAM_SIGNAL


# ---------------------------------------------------------------------------
# End-to-end: the emitted claim is readable by the frozen policy-inputs layer
# ---------------------------------------------------------------------------


def test_emitted_true_claim_is_read_by_derive_policy_inputs(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    """This is the test that proves the claim actually reaches the consumer."""
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team="Engineering",
        first_observed=NOW - timedelta(days=5),
    )
    conn.commit()

    result = team_signal(_args(), _ctx(ctx_factory))
    assert result.data is not None
    claim = next(
        c for c in result.data["evidence"] if c.claim_type == CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE
    )
    item = EvidenceItem(id="e1", run_id="r1", probe="team_signal", **claim.model_dump())

    inputs = derive_policy_inputs([item], None, NOW)

    assert inputs.corroborating_hiring_signal is True


def test_emitted_false_claim_is_read_by_derive_policy_inputs(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering", first_observed=_day(0))
    conn.commit()

    result = team_signal(_args(), _ctx(ctx_factory))
    assert result.data is not None
    claim = next(
        c for c in result.data["evidence"] if c.claim_type == CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE
    )
    item = EvidenceItem(id="e1", run_id="r1", probe="team_signal", **claim.model_dump())

    inputs = derive_policy_inputs([item], None, NOW)

    assert inputs.corroborating_hiring_signal is False


# ---------------------------------------------------------------------------
# The injectable source= seam
# ---------------------------------------------------------------------------


def test_injected_source_is_used_instead_of_the_default(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    class FakeSource:
        def fetch(self, args: TeamSignalArgs, ctx: ProbeContext) -> ProbeResult:
            return ProbeResult(ok=True, data={"company_id": args.company_id, "evidence": []})

    result = team_signal(_args(), _ctx(ctx_factory), source=FakeSource())

    assert result.ok is True
    assert result.data == {"company_id": COMPANY, "evidence": []}


def test_null_team_signal_source_still_reports_no_licensed_source(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    result = NullTeamSignalSource().fetch(_args(), _ctx(ctx_factory))

    assert result.ok is False
    assert result.error == "no_licensed_source"
    assert result.retryable is False
    assert result.data == {
        "posting_id": POSTING,
        "company_id": COMPANY,
        "evidence": [],
    }


# ---------------------------------------------------------------------------
# TeamSignalArgs.as_of — required, tz-aware, and the sole input to args_hash
# (security review H4)
# ---------------------------------------------------------------------------


def test_args_requires_as_of() -> None:
    """`as_of` has no default. H4's fix depends on every caller supplying a
    real point-in-time clock rather than falling back to a hidden `now`.
    """
    with pytest.raises(ValidationError):
        TeamSignalArgs(posting_id=POSTING, company_id=COMPANY)


def test_naive_as_of_is_rejected() -> None:
    """Mirrors `CompanyEventsArgs`'s validator test: a naive `as_of` cannot be
    placed on the `available_at <= T` timeline (`rli.models.time`).
    """
    try:
        TeamSignalArgs(posting_id=POSTING, company_id=COMPANY, as_of=datetime(2026, 9, 7))
    except Exception as exc:  # pydantic.ValidationError wrapping ValueError
        assert "timezone-aware" in str(exc)
    else:  # pragma: no cover - the validator must reject this
        raise AssertionError("naive as_of must be rejected")


def test_hash_args_differs_by_as_of_and_is_stable_for_the_same_as_of() -> None:
    """`args_hash` must be a pure function of `T`: this is the property the
    whole replay lookup rests on (`rli.replay.build`/`rli.replay.run` key
    every cached probe call by `hash_args(name, **args.model_dump(mode="json"))`).
    A source that changed shape without changing `as_of` would collide with
    a different `T`'s cache entry; two different `as_of` values must not.
    """
    dumped_first = _args(as_of=NOW).model_dump(mode="json")
    dumped_again = _args(as_of=NOW).model_dump(mode="json")
    dumped_other = _args(as_of=NOW + timedelta(days=1)).model_dump(mode="json")

    first_hash = hash_args(TeamSignalProbe.name, **dumped_first)
    again_hash = hash_args(TeamSignalProbe.name, **dumped_again)
    other_hash = hash_args(TeamSignalProbe.name, **dumped_other)

    assert first_hash == again_hash
    assert first_hash != other_hash


# ---------------------------------------------------------------------------
# available_at is stamped by the observation that supports the claim — never
# by ctx.now() and never by as_of itself (security review H4, second half)
# ---------------------------------------------------------------------------


def test_new_roles_claim_is_stamped_at_the_latest_contributing_roles_first_observed(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    """`first_observed`, `as_of`, and `ctx.now()` are three distinct instants
    here on purpose — it is the only arrangement that can tell apart "stamped
    by the supporting observation" from the two wrong stamps H4 could
    regress to (the wall clock, or `as_of` itself).
    """
    _company(conn)
    _deep_history(conn)
    first_observed = NOW - timedelta(days=10)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team="Engineering",
        first_observed=first_observed,
    )
    conn.commit()
    run_clock = NOW + timedelta(days=3)
    ctx = ctx_factory(now=lambda: run_clock)

    result = team_signal(_args(as_of=NOW), ctx)

    assert result.data is not None
    by_type = {c.claim_type: c for c in result.data["evidence"]}
    new_roles_claim = by_type["team_new_roles"]
    assert new_roles_claim.available_at == first_observed
    assert new_roles_claim.available_at != NOW
    assert new_roles_claim.available_at != run_clock

    positive_claim = by_type[CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE]
    assert positive_claim.available_at == first_observed


def test_closures_claim_is_stamped_at_the_latest_contributing_first_seen_absent(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    """Same rule as the new-roles case, for the closures-only positive: the
    driving set is `activity.closures` here (zero new roles in window), so
    both the count claim and the boolean claim date from the newest
    `first_seen_absent`, not from `ctx.now()` or `as_of`.
    """
    _company(conn)
    _deep_history(conn)
    first_seen_absent = NOW - timedelta(days=20)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team="Engineering",
        first_observed=_day(0),
        first_seen_absent=first_seen_absent,
    )
    conn.commit()
    run_clock = NOW + timedelta(days=3)
    ctx = ctx_factory(now=lambda: run_clock)

    result = team_signal(_args(as_of=NOW), ctx)

    assert result.data is not None
    by_type = {c.claim_type: c for c in result.data["evidence"]}
    closures_claim = by_type["team_closures"]
    assert closures_claim.available_at == first_seen_absent
    assert closures_claim.available_at != NOW
    assert closures_claim.available_at != run_clock

    positive_claim = by_type[CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE]
    assert positive_claim.available_at == first_seen_absent


def test_negative_claim_is_stamped_at_the_last_capture_at_or_before_as_of(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    """A `false` verdict rests on the whole watched window rather than on any
    single event, so it must be dated by the capture that closes that
    window (`activity.last_capture_at`) — not by `ctx.now()` and not by
    `as_of` itself.
    """
    _company(conn)
    _deep_history(conn)  # daily captures from _day(0) through _day(40)
    _posting(conn, POSTING, job_id="J1", team="Engineering", first_observed=_day(0))
    conn.commit()
    run_clock = NOW + timedelta(days=3)
    ctx = ctx_factory(now=lambda: run_clock)

    result = team_signal(_args(as_of=NOW), ctx)

    assert result.data is not None
    assert result.data["corroborating_hiring_signal"] is False
    boolean_claim = next(
        c for c in result.data["evidence"] if c.claim_type == CORROBORATING_HIRING_SIGNAL_CLAIM_TYPE
    )
    assert boolean_claim.available_at == _day(40)
    assert boolean_claim.available_at != NOW
    assert boolean_claim.available_at != run_clock


def test_fetched_at_is_ctx_now_and_may_differ_from_as_of(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    """`fetched_at` records when the probe read the database, which is
    genuinely a different question from `as_of` (the window/claim clock) —
    see the module docstring's rule that only `fetched_at` is about the
    reader rather than about the world.
    """
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team="Engineering",
        first_observed=NOW - timedelta(days=5),
    )
    conn.commit()
    run_clock = NOW + timedelta(days=3)
    ctx = ctx_factory(now=lambda: run_clock)

    result = team_signal(_args(as_of=NOW), ctx)

    assert result.data is not None
    assert result.data["evidence"], "corpus must actually produce claims to check"
    for claim in result.data["evidence"]:
        assert claim.fetched_at == run_clock
        assert claim.fetched_at != NOW


# ---------------------------------------------------------------------------
# Invariant: every emitted claim satisfies available_at <= args.as_of
# ---------------------------------------------------------------------------


def test_every_emitted_claim_satisfies_available_at_le_as_of(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    """This is the property `rli.replay.mode.ReplayProbeRunner.save_evidence`'s
    gate depends on. Checked across several `as_of` values against one
    corpus that can produce new-role, closure, and boolean claims, rather
    than trusting the narrower per-rule tests above to cover it.
    """
    _company(conn)
    _deep_history(conn)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team="Engineering",
        first_observed=NOW - timedelta(days=10),
    )
    _posting(
        conn,
        "greenhouse:acme:J3",
        job_id="J3",
        team="Engineering",
        first_observed=_day(5),
        first_seen_absent=NOW - timedelta(days=15),
    )
    conn.commit()
    ctx = _ctx(ctx_factory)

    for as_of in (
        NOW,
        NOW - timedelta(days=60),
        _day(35),
        NOW + timedelta(days=100),
    ):
        result = team_signal(_args(as_of=as_of), ctx)
        assert result.data is not None
        for claim in result.data["evidence"]:
            assert claim.available_at <= as_of


# ---------------------------------------------------------------------------
# Regression test for security review H4's root cause: a corpus event
# strictly after as_of must not change the answer
# ---------------------------------------------------------------------------


def test_h4_regression_events_strictly_after_as_of_do_not_change_the_result(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    """H4's root cause was `TeamSignalArgs` having no `as_of`, so a single
    build-time record served every replay `T`. This corpus does not even
    run inside `rli.replay.pit.point_in_time` — it proves the probe itself
    is honest about `as_of` on a plain, growing database.
    """
    _company(conn)
    _deep_history(conn)
    first_observed = NOW - timedelta(days=10)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    _posting(
        conn,
        "greenhouse:acme:J2",
        job_id="J2",
        team="Engineering",
        first_observed=first_observed,
    )
    conn.commit()
    ctx = _ctx(ctx_factory)

    before = team_signal(_args(as_of=NOW), ctx)
    assert before.data is not None
    before_claims = {c.claim_type: c.model_dump() for c in before.data["evidence"]}

    # A capture and a new posting, both strictly AFTER as_of=NOW.
    _capture(conn, NOW + timedelta(days=1), ["J1", "J2", "J3"])
    _posting(
        conn,
        "greenhouse:acme:J3",
        job_id="J3",
        team="Engineering",
        first_observed=NOW + timedelta(days=2),
    )
    conn.commit()

    after = team_signal(_args(as_of=NOW), ctx)
    assert after.data is not None
    after_claims = {c.claim_type: c.model_dump() for c in after.data["evidence"]}

    assert after.data["corroborating_hiring_signal"] == before.data["corroborating_hiring_signal"]
    assert after.data["new_roles_30d"] == before.data["new_roles_30d"]
    assert after.data["closures_60d"] == before.data["closures_60d"]
    assert after_claims == before_claims


# ---------------------------------------------------------------------------
# Eligibility is judged at args.as_of, not at today's full history
# ---------------------------------------------------------------------------


def test_eligible_is_judged_at_as_of_not_at_the_databases_current_history(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    """A company with 40 days of history TODAY had only 10 days of it as of
    an early `as_of` — eligibility must track `as_of`, one step earlier than
    the H4 claim-stamping bug itself: a probe admitted on the strength of
    captures taken after `T` would be the same class of leak, just earlier
    in the pipeline.
    """
    _company(conn)
    _deep_history(conn)  # captures on _day(0)..._day(40)
    _posting(conn, POSTING, job_id="J1", team="Engineering")
    conn.commit()
    ctx = _ctx(ctx_factory)

    early_as_of = _day(10)
    late_as_of = _day(40)

    assert TeamSignalProbe.eligible(ctx, _args(as_of=early_as_of)) is False
    assert TeamSignalProbe.eligible(ctx, _args(as_of=late_as_of)) is True
