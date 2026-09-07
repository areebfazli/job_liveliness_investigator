"""rli.history — the M2 evidence layer over board snapshots (PLAN.md M2).

Three modules, run in this order:

1. `closures` — interval-censored closure derivation from every
   `board_snapshots` row (own + archive), projected onto the `postings`
   lifecycle columns by `apply_to_postings`. Keeps `last_seen_open` /
   `first_seen_absent` as a bracket and never fabricates a `closed_at`
   (spec.md §4).
2. `matching` — repost/version matching between a disappeared posting and
   later postings of the SAME company, writing `postings.replacement_job_id`
   and the `repost_links` audit table (`schema_ext.sql`).
3. `features` — per-company and per-posting history features, always
   carrying `history_days` / `history_coverage`, with the
   `rli.models.policy_inputs.UNKNOWN` sentinel wherever history is too thin
   to have an opinion.

IMPORT DIRECTION (a constraint, not an observation): `rli.archive.backfill`
imports `rli.history.titles`, which executes THIS module, which eagerly
re-exports `closures` / `matching` / `features` / `sample`. Nothing in
`rli.history` may therefore import `rli.archive.*` at module scope — that
would be an import cycle. `titles` itself is stdlib-only and imports nothing
from this package, so the dependency is one-way by construction as long as
that rule holds.

`titles` is the shared job-title quality policy (what counts as a real job
title rather than scraped link text like "Apply"). It is imported by BOTH
`matching` and `rli.archive.backfill`, so extraction-time and match-time
rejection can never drift apart; see its module docstring.

`sample` exports the hand-checked 50-match precision CSV that spec.md §4
requires before the matching thresholds can be trusted, and `cli` is the
`rebuild` / `sample` Typer sub-app.

All three correlate `board_snapshot_jobs` to `postings` through
`(company_id, ats_job_id)`, since board snapshots carry no ATS/tenant
column; see `closures`'s module docstring for that judgment call and its
known limitation.
"""

from __future__ import annotations

from rli.history.closures import (
    ClosureApplySummary,
    PostingInterval,
    apply_to_postings,
    build_intervals,
)
from rli.history.features import (
    CompanyHistoryFeatures,
    CoverageWindow,
    PostingHistoryFeatures,
    company_features,
    coverage_window,
    posting_features,
)
from rli.history.matching import (
    MatchCandidate,
    RepostLinkSummary,
    assign_one_to_one,
    link_reposts,
    rank_matches,
    score_pair,
)
from rli.history.sample import export_match_sample
from rli.history.titles import (
    dominant_title_fraction,
    is_degenerate_page,
    is_junk_title,
    junk_title_reason,
)

__all__ = [
    "ClosureApplySummary",
    "CompanyHistoryFeatures",
    "CoverageWindow",
    "MatchCandidate",
    "PostingHistoryFeatures",
    "PostingInterval",
    "RepostLinkSummary",
    "apply_to_postings",
    "assign_one_to_one",
    "build_intervals",
    "company_features",
    "coverage_window",
    "dominant_title_fraction",
    "export_match_sample",
    "is_degenerate_page",
    "is_junk_title",
    "junk_title_reason",
    "link_reposts",
    "posting_features",
    "rank_matches",
    "score_pair",
]
