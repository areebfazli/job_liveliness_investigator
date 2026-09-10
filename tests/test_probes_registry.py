"""Probe registry: eligibility matrix + deterministic cost ranking (spec.md §4)."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from rli.config import Config, TeamSignal
from rli.models.case_file import CaseFile
from rli.models.policy_inputs import PolicyInputs
from rli.probes.base import Probe, ProbeContext
from rli.probes.company_events import CompanyEventsArgs, CompanyEventsProbe
from rli.probes.registry import (
    DYNAMIC_PROBES,
    build_args,
    cost_value,
    eligible_probes,
    latency_estimate_s,
)
from rli.probes.repost_history import RepostHistoryArgs, RepostHistoryProbe
from rli.probes.requirements_drift import RequirementsDriftArgs, RequirementsDriftProbe
from rli.probes.team_signal import TeamSignalArgs, TeamSignalProbe

COMPANY = "acme.com"
POSTING = "greenhouse:acme:J1"
JOB = "J1"
NOW = datetime(2026, 9, 7, tzinfo=UTC)
START = datetime(2026, 1, 1, tzinfo=UTC)

ALL_INPUTS = set(PolicyInputs().unpopulated())


def _z(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _seed(conn: sqlite3.Connection, *, history_days: int) -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?, ?, ?, ?)",
        (COMPANY, "Acme", COMPANY, _z(START)),
    )
    conn.execute(
        """
        INSERT INTO postings (posting_id, company_id, ats, ats_tenant_id, ats_job_id,
                              canonical_url, title, team, location, created_at, updated_at)
        VALUES (?, ?, 'greenhouse', 'acme', ?, ?, 'Backend Engineer', 'Engineering',
                'Remote', ?, ?)
        """,
        (POSTING, COMPANY, JOB, "https://boards.greenhouse.io/acme/jobs/J1", _z(START), _z(START)),
    )
    for offset in (0, history_days):
        cursor = conn.execute(
            "INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status) "
            "VALUES (?, ?, 'own', 'complete')",
            (COMPANY, _z(START + timedelta(days=offset))),
        )
        conn.execute(
            "INSERT INTO board_snapshot_jobs "
            "(board_snapshot_id, job_id, title, team, location, description_hash, url) "
            "VALUES (?, ?, 'Backend Engineer', 'Engineering', 'Remote', 'h1', NULL)",
            (int(cursor.lastrowid), JOB),
        )
    conn.commit()


def _case() -> CaseFile:
    return CaseFile(
        posting_id=POSTING,
        company_id=COMPANY,
        canonical_url="https://boards.greenhouse.io/acme/jobs/J1",
        title="Backend Engineer",
    )


def _ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    return ctx_factory(now=lambda: NOW)


def _enable_team_signal(ctx: ProbeContext) -> ProbeContext:
    new_cfg: Config = ctx.config.model_copy(update={"team_signal": TeamSignal(enabled=True)})
    return dataclasses.replace(ctx, config=new_cfg)


def _disable_team_signal(ctx: ProbeContext) -> ProbeContext:
    new_cfg: Config = ctx.config.model_copy(update={"team_signal": TeamSignal(enabled=False)})
    return dataclasses.replace(ctx, config=new_cfg)


def _names(probes: list[type[Probe]]) -> list[str]:
    return [cls.name for cls in probes]


# ---------------------------------------------------------------------------
# registry contents / cost helpers
# ---------------------------------------------------------------------------


def test_registry_holds_exactly_the_four_dynamic_probes() -> None:
    assert set(DYNAMIC_PROBES) == {
        "repost_history",
        "requirements_drift",
        "company_events",
        "team_signal",
    }
    # Always-run probes are not ranked candidates.
    assert "resolve_posting" not in DYNAMIC_PROBES
    assert "board_snapshot" not in DYNAMIC_PROBES
    for name, probe_cls in DYNAMIC_PROBES.items():
        assert probe_cls.name == name


def test_cost_and_latency_come_from_probe_costs(cfg: Config) -> None:
    assert cost_value(RepostHistoryProbe, cfg) == cfg.probe_costs.low
    assert cost_value(RequirementsDriftProbe, cfg) == cfg.probe_costs.medium
    assert cost_value(CompanyEventsProbe, cfg) == cfg.probe_costs.medium
    assert cost_value(TeamSignalProbe, cfg) == cfg.probe_costs.high
    assert latency_estimate_s(RepostHistoryProbe, cfg) == cfg.probe_costs.latency_low_s
    assert latency_estimate_s(TeamSignalProbe, cfg) == cfg.probe_costs.latency_high_s


def test_build_args_uses_only_the_case_state_and_the_clock(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    ctx = _ctx(ctx_factory)
    case = _case()

    assert build_args(RepostHistoryProbe, case, ctx) == RepostHistoryArgs(posting_id=POSTING)
    assert build_args(RequirementsDriftProbe, case, ctx) == RequirementsDriftArgs(
        posting_id=POSTING
    )
    assert build_args(CompanyEventsProbe, case, ctx) == CompanyEventsArgs(
        company_id=COMPANY, as_of=NOW
    )
    assert build_args(TeamSignalProbe, case, ctx) == TeamSignalArgs(
        posting_id=POSTING, company_id=COMPANY
    )


def test_build_args_rejects_an_unregistered_probe_class(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    class StrayProbe(Probe):
        name = "json_ld"
        cost_tier = "low"
        ArgsModel = RepostHistoryArgs

        def run(self, args, ctx):  # pragma: no cover - never invoked
            raise NotImplementedError

    with pytest.raises(ValueError, match="no argument builder"):
        build_args(StrayProbe, _case(), _ctx(ctx_factory))


# ---------------------------------------------------------------------------
# eligibility matrix
# ---------------------------------------------------------------------------


def test_unpopulated_intersection_gates_every_probe(conn: sqlite3.Connection, ctx_factory) -> None:
    _seed(conn, history_days=40)
    ctx = _enable_team_signal(_ctx(ctx_factory))
    case = _case()

    # Every probe eligible when every input is unresolved.
    assert _names(eligible_probes(ctx, case, set(ALL_INPUTS))) == [
        "repost_history",
        "company_events",
        "requirements_drift",
        "team_signal",
    ]

    # Each probe drops out once the inputs it populates are resolved.
    assert _names(eligible_probes(ctx, case, {"repost_pattern"})) == [
        "repost_history",
        "requirements_drift",
    ]
    assert _names(eligible_probes(ctx, case, {"material_negative_event"})) == ["company_events"]
    assert _names(eligible_probes(ctx, case, {"freeze_or_pause"})) == ["company_events"]
    assert _names(eligible_probes(ctx, case, {"corroborating_hiring_signal"})) == ["team_signal"]

    # An input no dynamic probe can populate leaves nothing eligible.
    assert eligible_probes(ctx, case, {"posting_state", "declared_expiry"}) == []
    assert eligible_probes(ctx, case, set()) == []


def test_insufficient_history_excludes_only_the_history_probes(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    """`team_signal` is now ALSO a history probe (`history_required=True`), so
    thin history excludes it too, alongside `repost_history` and
    `requirements_drift`. `company_events` is the one dynamic probe with no
    history gate at all.
    """
    _seed(conn, history_days=5)
    ctx = _enable_team_signal(_ctx(ctx_factory))

    names = _names(eligible_probes(ctx, _case(), set(ALL_INPUTS)))
    assert names == ["company_events"]
    assert "repost_history" not in names
    assert "requirements_drift" not in names
    assert "team_signal" not in names


def test_a_company_with_no_history_at_all_excludes_the_history_probes(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    ctx = _ctx(ctx_factory)
    assert _names(eligible_probes(ctx, _case(), set(ALL_INPUTS))) == ["company_events"]


def test_team_signal_is_gated_by_config(conn: sqlite3.Connection, ctx_factory) -> None:
    _seed(conn, history_days=40)
    ctx = _ctx(ctx_factory)

    # Explicitly disabled config -> never a candidate (the shipped default is
    # now `enabled=True`, so the "off" half must build its own config rather
    # than lean on the default).
    disabled = _disable_team_signal(ctx)
    assert "team_signal" not in _names(eligible_probes(disabled, _case(), set(ALL_INPUTS)))
    assert eligible_probes(disabled, _case(), {"corroborating_hiring_signal"}) == []

    enabled = _enable_team_signal(ctx)
    assert _names(eligible_probes(enabled, _case(), {"corroborating_hiring_signal"})) == [
        "team_signal"
    ]


def test_probe_owned_eligibility_excludes_a_posting_that_does_not_exist(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    # Company history is deep enough, but the case file names an unknown
    # posting: the history probes' own `eligible` rejects it.
    _seed(conn, history_days=40)
    case = _case().model_copy(update={"posting_id": "greenhouse:acme:missing"})

    names = _names(eligible_probes(_ctx(ctx_factory), case, set(ALL_INPUTS)))
    assert names == ["company_events"]


# ---------------------------------------------------------------------------
# deterministic ranking
# ---------------------------------------------------------------------------


def test_ranking_is_cheapest_first_then_alphabetical(conn: sqlite3.Connection, ctx_factory) -> None:
    _seed(conn, history_days=40)
    ctx = _enable_team_signal(_ctx(ctx_factory))

    ranked = eligible_probes(ctx, _case(), set(ALL_INPUTS))
    costs = [cost_value(cls, ctx.config) for cls in ranked]
    assert costs == sorted(costs)
    assert costs[0] == ctx.config.probe_costs.low
    assert costs[-1] == ctx.config.probe_costs.high
    # The two medium-cost probes tie on cost and break alphabetically.
    assert _names(ranked)[1:3] == ["company_events", "requirements_drift"]


def test_ranking_is_reproducible(conn: sqlite3.Connection, ctx_factory) -> None:
    _seed(conn, history_days=40)
    ctx = _enable_team_signal(_ctx(ctx_factory))
    case = _case()

    first = eligible_probes(ctx, case, set(ALL_INPUTS))
    second = eligible_probes(ctx, case, set(ALL_INPUTS))
    assert first == second
    assert _names(first) == _names(second)
