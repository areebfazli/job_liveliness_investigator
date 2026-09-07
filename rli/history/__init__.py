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

`sample` exports the hand-checked 50-match precision CSV that spec.md §4
requires before the matching thresholds can be trusted.

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
    link_reposts,
    rank_matches,
    score_pair,
)
from rli.history.sample import export_match_sample

__all__ = [
    "ClosureApplySummary",
    "CompanyHistoryFeatures",
    "CoverageWindow",
    "MatchCandidate",
    "PostingHistoryFeatures",
    "PostingInterval",
    "RepostLinkSummary",
    "apply_to_postings",
    "build_intervals",
    "company_features",
    "coverage_window",
    "export_match_sample",
    "link_reposts",
    "posting_features",
    "rank_matches",
    "score_pair",
]
