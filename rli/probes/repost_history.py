"""`repost_history` — the low-cost, history-gated disappearance/version probe.

spec.md §4 lists `repost_history` as a dynamic probe returning
"disappearance/reappearance + versions" at `low` cost with
`history required = yes`. This module is that probe: it reads the board
capture history already derived by `rli.history.closures` and the
per-posting capture series in `posting_snapshots`, and reports them as
`ProbeClaim`s plus a structured `data` payload.

It is a READ-ONLY probe, like every probe (`rli.probes.base`: "Do not write
to DB inside probes"). Nothing here calls `rli.history.matching.link_reposts`
or `rli.history.closures.apply_to_postings`, both of which write; the
`repost_links` row this probe surfaces is read with a plain SELECT and is
reported as a *fact* for the caller, never (re-)computed here.

Facts, not verdicts (spec.md §4: "probes return facts, not verdicts")
----------------------------------------------------------------------
This probe deliberately does NOT classify `repost_pattern` into
`repeated_unchanged` / `changed` / `none`. That classification already
exists, with its own carefully documented Unknown-handling, in
`rli.history.features._classify_repost_pattern`, and duplicating it here
would create a second source of truth that could silently disagree with the
feature layer. What this probe supplies is the raw material that
classification consumes: the interval-censored lifecycle timestamps, the
best `repost_links` row with its component scores, and the content-hash
version series.

History gating (spec.md §4: "history probes are ineligible without usable
history"; "missing history never means flat hiring")
--------------------------------------------------------------------------
`RepostHistoryProbe.eligible` is the controller-side preflight check, and
`repost_history` ALSO re-checks internally, so calling the pure function
directly can never produce history-derived claims from history that is too
thin to support them. Below `thresholds.min_history_days` the probe returns
`ok=True` with `usable_history=False` and NO claims — an honest "we cannot
tell", never a fabricated "nothing ever happened". `ok=False` is reserved
for the one genuine caller error: a `posting_id` that does not exist.

GUESSED / judgment calls made in this module
--------------------------------------------

* **Per-event `source_quality` is resolved by a fresh `board_snapshots`
  lookup, not from `PostingInterval.sources`.** `interval.sources` is the
  aggregate set of sources that ever listed the job (e.g. `("archive",
  "own")`), so it cannot say which kind of capture produced *this*
  disappearance or *this* reappearance. Attributing an archive-derived
  absence to `ats_native` merely because some other capture of the same job
  was an own capture would overstate the evidence's quality, which spec.md
  §3's source-quality ordering exists precisely to prevent. So each claim
  looks up the `source` of the specific capture at its own `captured_at`
  (`_capture_source`) and maps `'own' -> 'ats_native'`, `'archive' ->
  'archive'`. A capture whose row cannot be found at all (which should not
  happen, since the timestamp came from that very table) degrades to
  `'archive'`, the weaker of the two — never to `'ats_native'`.
* **`source_url` placeholders.** `ProbeClaim.source_url` is required and a
  board-history fact frequently has no URL of its own (the job may only
  ever have been seen inside a board listing that stored no per-job url).
  Rather than leave it empty or invent a plausible-looking ATS URL, this
  module uses obviously-synthetic, self-describing schemes in the style of
  `rli.history.closures.ARCHIVE_ONLY_URL_PLACEHOLDER`:
  `board-history:{company_id}/{job_id}` for interval-derived claims and
  `posting-snapshot:{posting_id}:{captured_at}` for version claims. A
  reader (or a later verifier) must be able to tell at a glance that the
  claim cites an internal capture record, not a fetchable page.
* **`versions` reports every `posting_snapshots` row**, including rows whose
  `content_hash` is NULL, so the caller can see capture density and gaps.
  Only consecutive rows that BOTH carry a hash can produce a
  `version_change` claim: a NULL hash is missing data, and treating a
  NULL -> hash transition as a version change would manufacture a change out
  of a coverage hole (the same failure mode `rli.history.matching`'s
  `_description_score` avoids).
* **Datetimes in `data` are `to_utc_z` strings, not `datetime` objects.**
  `ProbeResult.data` crosses JSON/cache boundaries (`tool_cache`, the case
  file), and `rli.models.time` requires that every such crossing use the
  fixed-width `Z` encoding. `data["evidence"]` keeps live `ProbeClaim`
  objects, matching `rli.probes.resolve_posting`, because the caller turns
  those into `EvidenceItem`s rather than serializing them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from rli.history.closures import build_intervals
from rli.history.features import coverage_window
from rli.models.probe import ProbeResult
from rli.models.time import parse_utc, to_utc_z
from rli.probes.base import Probe, ProbeClaim, ProbeContext
from rli.probes.lookups import has_usable_history, posting_row

__all__ = ["RepostHistoryArgs", "RepostHistoryProbe", "repost_history"]

# Self-describing, deliberately non-fetchable source_url schemes (see the
# module docstring). Mirrors rli.history.closures.ARCHIVE_ONLY_URL_PLACEHOLDER.
BOARD_HISTORY_URL_PLACEHOLDER = "board-history:{company_id}/{job_id}"
POSTING_SNAPSHOT_URL_PLACEHOLDER = "posting-snapshot:{posting_id}:{captured_at}"

SourceQuality = Literal["ats_native", "archive"]


class RepostHistoryArgs(BaseModel):
    posting_id: str


def _capture_source(conn: sqlite3.Connection, company_id: str, captured_at: datetime) -> str | None:
    """The `board_snapshots.source` of the capture at exactly `captured_at`.

    A plain point lookup rather than a range/nearest match: the timestamp
    always comes from a `board_snapshots` row this company owns, so an exact
    match is expected. `ORDER BY id` keeps the answer deterministic if two
    captures share an instant (an own and an archive capture of the same
    moment), matching `rli.history.closures.load_captures`'s tiebreak.
    """
    row = conn.execute(
        """
        SELECT source FROM board_snapshots
        WHERE company_id = ? AND captured_at = ?
        ORDER BY id
        LIMIT 1
        """,
        (company_id, to_utc_z(captured_at)),
    ).fetchone()
    return None if row is None else row["source"]


def _source_quality(source: str | None) -> SourceQuality:
    """Map a capture `source` to an evidence `source_quality`.

    Unknown provenance degrades to `'archive'`, the weaker value: claiming
    `ats_native` for a capture we cannot identify would overstate it.
    """
    return "ats_native" if source == "own" else "archive"


def _best_repost_link(conn: sqlite3.Connection, posting_id: str) -> dict[str, Any] | None:
    """The highest-scoring `repost_links` row for `posting_id`, as a plain dict.

    READ-ONLY: `rli.history.matching.link_reposts` (which writes both
    `repost_links` and `postings.replacement_job_id`) is never called from a
    probe. The tiebreak matches `rli.history.features._classify_repost_pattern`
    so both layers agree on which link is "the" link.
    """
    row = conn.execute(
        """
        SELECT new_posting_id, combined_score, component_scores, matched_at
        FROM repost_links
        WHERE old_posting_id = ?
        ORDER BY combined_score DESC, new_posting_id
        LIMIT 1
        """,
        (posting_id,),
    ).fetchone()
    if row is None:
        return None

    try:
        component_scores = json.loads(row["component_scores"])
    except ValueError:
        # A corrupt JSON blob is untrusted stored data, not a probe failure:
        # degrade to None rather than raising out of a probe.
        component_scores = None

    return {
        "new_posting_id": row["new_posting_id"],
        "combined_score": row["combined_score"],
        "component_scores": component_scores,
        "matched_at": row["matched_at"],
    }


def _version_rows(conn: sqlite3.Connection, posting_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, captured_at, source, status, content_hash, capture_url
        FROM posting_snapshots
        WHERE posting_id = ?
        ORDER BY captured_at, id
        """,
        (posting_id,),
    ).fetchall()


def _version_change_claims(
    rows: list[sqlite3.Row], posting_id: str, now: datetime
) -> list[ProbeClaim]:
    """One `version_change` claim per content-hash transition (see docstring)."""
    claims: list[ProbeClaim] = []
    previous: sqlite3.Row | None = None
    for row in rows:
        if row["content_hash"] is None:
            # Missing data, not a version: skip without breaking the chain,
            # so a capture that failed to hash does not fabricate a change
            # against the next real hash it precedes.
            continue
        if previous is not None and previous["content_hash"] != row["content_hash"]:
            captured_at = parse_utc(row["captured_at"])
            claims.append(
                ProbeClaim(
                    claim_type="version_change",
                    value=f"{previous['content_hash']}->{row['content_hash']}",
                    source_url=row["capture_url"]
                    or POSTING_SNAPSHOT_URL_PLACEHOLDER.format(
                        posting_id=posting_id, captured_at=row["captured_at"]
                    ),
                    source_quality=_source_quality(row["source"]),
                    source_event_at=captured_at,
                    available_at=captured_at,
                    fetched_at=now,
                )
            )
        previous = row
    return claims


def repost_history(posting_id: str, ctx: ProbeContext) -> ProbeResult:
    """Pure function backing `RepostHistoryProbe.run` (spec.md §4)."""
    now = ctx.now()

    row = posting_row(ctx.conn, posting_id)
    if row is None:
        return ProbeResult(
            ok=False,
            error=f"no posting row for posting_id={posting_id!r}",
            retryable=False,
            data={"posting_id": posting_id, "evidence": []},
        )

    company_id = row["company_id"]
    coverage = coverage_window(ctx.conn, company_id)
    minimum = ctx.config.thresholds.min_history_days

    if coverage.history_days < minimum:
        # spec.md §4: history probes are ineligible without usable history,
        # and missing history never means flat hiring. This is a successful
        # observation of "we cannot tell", not a failure and not a negative.
        return ProbeResult(
            ok=True,
            data={
                "posting_id": posting_id,
                "company_id": company_id,
                "usable_history": False,
                "history_days": coverage.history_days,
                "history_coverage": coverage.history_coverage,
                "evidence": [],
            },
        )

    ats_job_id = row["ats_job_id"]
    interval = None
    if ats_job_id is not None:
        interval = next(
            (i for i in build_intervals(ctx.conn, company_id) if i.job_id == ats_job_id),
            None,
        )

    claims: list[ProbeClaim] = []
    if interval is not None:
        placeholder_url = BOARD_HISTORY_URL_PLACEHOLDER.format(
            company_id=company_id, job_id=interval.job_id
        )
        source_url = interval.url or placeholder_url

        if interval.first_seen_absent is not None:
            claims.append(
                ProbeClaim(
                    claim_type="disappeared_interval",
                    value=(
                        f"last_seen_open={to_utc_z(interval.last_seen_open)} "
                        f"first_seen_absent={to_utc_z(interval.first_seen_absent)}"
                    ),
                    source_url=source_url,
                    source_quality=_source_quality(
                        _capture_source(ctx.conn, company_id, interval.first_seen_absent)
                    ),
                    source_event_at=interval.first_seen_absent,
                    available_at=interval.first_seen_absent,
                    fetched_at=now,
                )
            )

        if interval.reappeared_at is not None:
            absent_at = _opt_z(interval.first_seen_absent) or "unknown"
            claims.append(
                ProbeClaim(
                    claim_type="reappeared",
                    value=(
                        f"first_seen_absent={absent_at} "
                        f"reappeared_at={to_utc_z(interval.reappeared_at)}"
                    ),
                    source_url=source_url,
                    source_quality=_source_quality(
                        _capture_source(ctx.conn, company_id, interval.reappeared_at)
                    ),
                    source_event_at=interval.reappeared_at,
                    available_at=interval.reappeared_at,
                    fetched_at=now,
                )
            )

    version_rows = _version_rows(ctx.conn, posting_id)
    claims.extend(_version_change_claims(version_rows, posting_id, now))

    return ProbeResult(
        ok=True,
        data={
            "posting_id": posting_id,
            "company_id": company_id,
            "usable_history": True,
            "history_days": coverage.history_days,
            "history_coverage": coverage.history_coverage,
            "first_seen_absent": _opt_z(interval.first_seen_absent if interval else None),
            "reappeared_at": _opt_z(interval.reappeared_at if interval else None),
            "closure_absent_at": _opt_z(interval.closure_absent_at if interval else None),
            "censoring": interval.censoring if interval else None,
            "gap_days": interval.gap_days if interval else None,
            "repost_link": _best_repost_link(ctx.conn, posting_id),
            "versions": [
                {
                    "captured_at": version["captured_at"],
                    "source": version["source"],
                    "content_hash": version["content_hash"],
                }
                for version in version_rows
            ],
            "evidence": claims,
        },
    )


def _opt_z(value: datetime | None) -> str | None:
    return None if value is None else to_utc_z(value)


class RepostHistoryProbe(Probe):
    """Dynamic probe: disappearance/reappearance + versions (spec.md §4)."""

    name: ClassVar[str] = "repost_history"
    cost_tier: ClassVar[str] = "low"
    history_required: ClassVar[bool] = True
    populates: ClassVar[frozenset[str]] = frozenset({"repost_pattern"})
    ArgsModel: ClassVar[type[BaseModel]] = RepostHistoryArgs

    @classmethod
    def eligible(cls, ctx: ProbeContext, args: RepostHistoryArgs) -> bool:
        """Controller-side preflight: usable history exists for this posting.

        spec.md §4: "history probes are ineligible without usable history."
        A posting that does not exist is likewise ineligible — there is
        nothing to investigate — rather than an error at ranking time.
        `run` re-checks this independently (see the module docstring).
        """
        row = posting_row(ctx.conn, args.posting_id)
        if row is None:
            return False
        return has_usable_history(ctx.conn, ctx.config, row["company_id"])

    def run(self, args: RepostHistoryArgs, ctx: ProbeContext) -> ProbeResult:
        return repost_history(args.posting_id, ctx)
