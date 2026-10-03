"""`board_snapshot` — the always-run current-openings probe (spec.md §4).

Input `{ats, tenant}`. Fetches the ATS's board-listing endpoint and returns
every currently open job (`job_id, title, team, location, url,
description_hash`, plus the ATS's own stated dates where the API documents
them) for Greenhouse, Ashby, or Lever. Pure: on failure it
returns `ProbeResult(ok=False, ...)` and does NOT write to the database —
the caller records a `capture_attempts` row via `rli.probes.persist`
(spec.md §4: "on failure return ok=false and the caller records a
capture_attempts row").
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel

from rli.models.probe import ProbeResult
from rli.probes.base import Probe, ProbeContext
from rli.resolvers import ashby, greenhouse, lever

__all__ = ["BoardJob", "BoardSnapshotArgs", "BoardSnapshotProbe", "board_snapshot"]

AtsName = Literal["greenhouse", "ashby", "lever"]


class BoardSnapshotArgs(BaseModel):
    ats: AtsName
    tenant: str


class BoardJob(BaseModel):
    """One open job in a board snapshot, normalized across ATSes."""

    job_id: str
    title: str | None = None
    team: str | None = None
    location: str | None = None
    url: str | None = None
    description_hash: str | None = None
    # The ATS's own stated dates, as the raw strings the listing returned
    # (spec.md §3 source policy): Greenhouse `first_published` / `updated_at`
    # and Ashby `publishedAt` (mapped onto `first_published`) are documented
    # `ats_native` fields and are present on every job in the BOARD listing,
    # not only on the single-job endpoint. Lever's dates are undocumented and
    # untrusted, so a Lever job always leaves both None. Persisted to
    # `board_snapshot_jobs` (normalized to UTC-Z) so a replay at `T` can cite
    # the date as our own capture saw it, available from that capture's time.
    first_published: str | None = None
    updated_at: str | None = None


def _from_greenhouse(job: greenhouse.GreenhouseJob) -> BoardJob:
    return BoardJob(
        job_id=job.id,
        title=job.title,
        team=job.departments[0] if job.departments else None,
        location=job.location,
        url=job.absolute_url,
        description_hash=job.content_hash,
        first_published=job.first_published,
        updated_at=job.updated_at,
    )


def _from_ashby(job: ashby.AshbyJob) -> BoardJob:
    return BoardJob(
        job_id=job.id,
        title=job.title,
        team=job.team or job.department,
        location=job.location,
        url=job.job_url,
        description_hash=job.content_hash,
        # Ashby documents only `publishedAt` (no updatedAt).
        first_published=job.published_at,
    )


def _from_lever(job: lever.LeverJob) -> BoardJob:
    return BoardJob(
        job_id=job.id,
        title=job.text,
        team=job.team,
        location=job.location,
        url=job.hosted_url,
        description_hash=job.content_hash,
    )


def board_snapshot(ats: AtsName, tenant: str, ctx: ProbeContext) -> ProbeResult:
    """Pure function backing `BoardSnapshotProbe.run` (spec.md §4)."""
    net = ctx.net_client("board_snapshot")

    if ats == "greenhouse":
        fetch = greenhouse.fetch_board(net, tenant)
        convert = _from_greenhouse
    elif ats == "ashby":
        fetch = ashby.fetch_board(net, tenant)
        convert = _from_ashby
    else:
        fetch = lever.fetch_board(net, tenant)
        convert = _from_lever

    if not fetch.ok:
        return ProbeResult(
            ok=False,
            error=fetch.error,
            retryable=fetch.retryable,
            data={"ats": ats, "tenant": tenant, "jobs": []},
        )
    if fetch.data is None:
        return ProbeResult(
            ok=False,
            error="board listing response could not be parsed",
            retryable=False,
            data={"ats": ats, "tenant": tenant, "jobs": []},
        )

    jobs = [convert(job) for job in fetch.data]
    return ProbeResult(
        ok=True,
        data={"ats": ats, "tenant": tenant, "jobs": jobs},
    )


class BoardSnapshotProbe(Probe):
    """Always-run probe: current open jobs for one ATS tenant (spec.md §4)."""

    name: ClassVar[str] = "board_snapshot"
    cost_tier: ClassVar[str] = "low"
    history_required: ClassVar[bool] = False
    ArgsModel: ClassVar[type[BaseModel]] = BoardSnapshotArgs

    def run(self, args: BoardSnapshotArgs, ctx: ProbeContext) -> ProbeResult:
        return board_snapshot(args.ats, args.tenant, ctx)
