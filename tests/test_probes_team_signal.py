"""`team_signal` probe: the unlicensed stub and its config gate (spec.md §4)."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable

from rli.config import Config, TeamSignal
from rli.models.probe import ProbeResult
from rli.probes.base import ProbeContext
from rli.probes.team_signal import (
    NullTeamSignalSource,
    TeamSignalArgs,
    TeamSignalProbe,
    team_signal,
)

ARGS = TeamSignalArgs(posting_id="greenhouse:acme:J1", company_id="acme.com")


def _ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    return ctx_factory()


def _with_team_signal(ctx: ProbeContext, enabled: bool) -> ProbeContext:
    """A context whose frozen Config has `[team_signal].enabled = enabled`."""
    new_cfg: Config = ctx.config.model_copy(update={"team_signal": TeamSignal(enabled=enabled)})
    return dataclasses.replace(ctx, config=new_cfg)


def test_run_returns_a_structured_no_licensed_source_failure(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    result = TeamSignalProbe().run(ARGS, _ctx(ctx_factory))

    assert result.ok is False
    assert result.error == "no_licensed_source"
    assert result.retryable is False
    assert result.data == {
        "posting_id": "greenhouse:acme:J1",
        "company_id": "acme.com",
        "evidence": [],
    }


def test_pure_function_matches_the_null_source(
    conn: sqlite3.Connection, ctx_factory
) -> None:
    ctx = _ctx(ctx_factory)
    assert team_signal(ARGS, ctx) == NullTeamSignalSource().fetch(ARGS, ctx)


def test_an_injected_source_is_used_instead(conn: sqlite3.Connection, ctx_factory) -> None:
    class FakeSource:
        def fetch(self, args: TeamSignalArgs, ctx: ProbeContext) -> ProbeResult:
            return ProbeResult(ok=True, data={"company_id": args.company_id, "evidence": []})

    result = team_signal(ARGS, _ctx(ctx_factory), source=FakeSource())
    assert result.ok is True
    assert result.data == {"company_id": "acme.com", "evidence": []}


def test_eligible_follows_the_config_flag(conn: sqlite3.Connection, ctx_factory) -> None:
    ctx = _ctx(ctx_factory)
    # The shipped config leaves the probe disabled (no licensed source).
    assert ctx.config.team_signal.enabled is False
    assert TeamSignalProbe.eligible(ctx, ARGS) is False
    assert TeamSignalProbe.eligible(_with_team_signal(ctx, True), ARGS) is True


def test_spec_metadata(conn: sqlite3.Connection, ctx_factory) -> None:
    assert TeamSignalProbe.cost_tier == "high"
    assert TeamSignalProbe.history_required is False
    assert TeamSignalProbe.populates == frozenset({"corroborating_hiring_signal"})
    assert TeamSignalProbe.allowlist(_ctx(ctx_factory).config) == []
