"""`team_signal` — the high-cost, licensed-source probe, stubbed (spec.md §4).

spec.md §4 lists `team_signal` as returning "recent similar hires/activity
from licensed source" at `high / optional` cost, and names it "the only
source for the policy input `corroborating_hiring_signal`". PLAN.md M3
allows for exactly this situation: "`team_signal` (optional source; stub if
unlicensed)".

**No licensed enrichment source exists for this project.** Rather than
invent one — scraping a professional network would be both a terms-of-use
violation and, per spec.md §4's "no crowd reports as direct v1 evidence",
the wrong kind of evidence — this module defines the seam a real source
would plug into and ships the only honest implementation available today:

* `TeamSignalSource` — the `Protocol` a licensed provider adapter must
  satisfy: one `fetch(args, ctx) -> ProbeResult`, with the same never-raise
  contract as any probe (`rli.probes.base`).
* `NullTeamSignalSource` — always returns a STRUCTURED failure
  (`ok=False, error="no_licensed_source", retryable=False`), never an
  empty-but-successful result. That distinction is load-bearing: an
  `ok=True` with no hires would read as "checked, no corroborating hiring
  activity", i.e. a known negative, and would let
  `corroborating_hiring_signal` be populated as `False` on the strength of
  a source that was never consulted. `retryable=False` because no amount of
  retrying acquires a license.

`config.toml`'s `[team_signal].enabled` (default `False`,
`rli.config.TeamSignal`) is the deployment-level gate:
`TeamSignalProbe.eligible` reads it, and `rli.probes.registry.eligible_probes`
therefore drops this probe from the candidate list entirely, so an
unlicensed run never spends a `high`-cost step to learn what config already
knows. `corroborating_hiring_signal` simply stays `UNKNOWN`
(`rli.models.policy_inputs`), which is the correct value for a question no
source can answer.

KNOWN GAP (documented, not implemented): spec.md §4 says `team_signal` "is
eligible only when that input is unknown **and the repost/long-lived branch
is reachable**". The first half is enforced generically by
`eligible_probes` (`populates ∩ unpopulated`); the second half is a
statement about the spec.md §5 action policy, which lives in `rli.policy`
and is owned elsewhere. Wiring that reachability test in belongs with the
policy layer's "which unresolved inputs could still change the action"
work (PLAN.md M3 bullet 3) — the flag defaulting to `False` means no run
reaches this probe in the meantime, so the gap cannot currently cause an
over-eager probe call.
"""

from __future__ import annotations

from typing import ClassVar, Protocol

from pydantic import BaseModel

from rli.models.probe import ProbeResult
from rli.probes.base import Probe, ProbeContext

__all__ = [
    "NullTeamSignalSource",
    "TeamSignalArgs",
    "TeamSignalProbe",
    "TeamSignalSource",
    "team_signal",
]


class TeamSignalArgs(BaseModel):
    posting_id: str
    company_id: str


class TeamSignalSource(Protocol):
    """The interface a licensed enrichment adapter must implement.

    Implementations must obey the probe contract: never raise for a network
    or parse problem, return `ProbeResult(ok=False, error=..., retryable=...)`
    instead, and put any `ProbeClaim`s under `data["evidence"]`
    (`source_quality="enrichment"`, per spec.md §3's source ordering).
    """

    def fetch(self, args: TeamSignalArgs, ctx: ProbeContext) -> ProbeResult: ...


class NullTeamSignalSource:
    """The only source wired in today: a structured "no licensed source".

    Deliberately NOT an empty success — see the module docstring.
    """

    def fetch(self, args: TeamSignalArgs, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(
            ok=False,
            error="no_licensed_source",
            retryable=False,
            data={
                "posting_id": args.posting_id,
                "company_id": args.company_id,
                "evidence": [],
            },
        )


def team_signal(
    args: TeamSignalArgs, ctx: ProbeContext, *, source: TeamSignalSource | None = None
) -> ProbeResult:
    """Pure function backing `TeamSignalProbe.run` (spec.md §4).

    `source` is injectable so a licensed adapter (or a test double) can be
    supplied without touching this module; it defaults to
    `NullTeamSignalSource`.
    """
    return (source or NullTeamSignalSource()).fetch(args, ctx)


class TeamSignalProbe(Probe):
    """Dynamic probe: corroborating hiring activity, gated on a license."""

    name: ClassVar[str] = "team_signal"
    cost_tier: ClassVar[str] = "high"
    history_required: ClassVar[bool] = False
    populates: ClassVar[frozenset[str]] = frozenset({"corroborating_hiring_signal"})
    ArgsModel: ClassVar[type[BaseModel]] = TeamSignalArgs

    @classmethod
    def eligible(cls, ctx: ProbeContext, args: TeamSignalArgs) -> bool:
        """Eligible only where a licensed source is configured (`config.toml`)."""
        return bool(ctx.config.team_signal.enabled)

    def run(self, args: TeamSignalArgs, ctx: ProbeContext) -> ProbeResult:
        return team_signal(args, ctx)
