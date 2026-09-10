"""`team_signal` probe: board-history source, claim shapes, eligibility (spec.md §4/§5)."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from rli.config import Config, TeamSignal
from rli.models.evidence import EvidenceItem
from rli.models.probe import ProbeResult
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


def _args(posting_id: str = POSTING) -> TeamSignalArgs:
    return TeamSignalArgs(posting_id=posting_id, company_id=COMPANY)


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
