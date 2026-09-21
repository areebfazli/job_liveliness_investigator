"""Hand-check sample export for repost matching (spec.md §4; PLAN.md M2 #2).

spec.md §4: "Keep thresholds configurable and validate match precision on a
hand-checked sample of 50 matches." PLAN.md M2 sends that hand-check to
`data/match_precision.md`. This module produces the CSV a human fills in:
one row per candidate pair, the component scores that produced it, and an
empty `human_verdict` column.

Three verdict columns are exported, not one, because a candidate can be
rejected for two very different reasons and a hand-checker needs to tell
them apart: `passes_thresholds` is the score-only verdict, `is_match` is the
final verdict after `rli.history.matching`'s one-to-one assignment, and
`reject_reason` (`"thresholds"` / `"assignment"`) says which stage rejected
it. `corroborated` / `title_only` show whether anything beyond the title
supported the match.

Judgment call (GUESSED): the export takes the top `n` candidates by combined
score across ALL companies — including candidates BELOW the match threshold,
each flagged in the `is_match` column — rather than only the accepted
matches. Precision is then computed over the `is_match=true` rows exactly as
the spec asks, while the near-misses sitting just under the threshold are
right there in the same file, which is the only cheap way to see whether
`[matching].combined_min` is set too high or too low. Pass `matches_only=True`
for the strict "50 accepted matches" reading.

`human_verdict` is left empty for a person to fill with
`true_positive` / `false_positive` / `unsure` (the values are listed in the
generated file's companion header comment in `data/match_precision.md`, not
enforced here — this file is an input to human judgement, not a schema).
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from rli.config import Config
from rli.history.matching import rank_matches
from rli.models.time import to_utc_z

__all__ = ["MATCH_SAMPLE_COLUMNS", "export_match_sample"]

MATCH_SAMPLE_COLUMNS = [
    "rank",
    "company_id",
    "old_posting_id",
    "old_job_id",
    "old_title",
    "old_first_seen_absent",
    "new_posting_id",
    "new_job_id",
    "new_title",
    "new_first_observed",
    "gap_days",
    "title_score",
    "team_score",
    "location_score",
    "description_score",
    "combined_score",
    "corroborated",
    "title_only",
    "passes_thresholds",
    "is_match",
    "reject_reason",
    "human_verdict",
]


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def export_match_sample(
    conn: sqlite3.Connection,
    cfg: Config,
    path: str | Path,
    n: int = 50,
    *,
    company_id: str | None = None,
    matches_only: bool = False,
) -> int:
    """Write the top `n` scored repost candidates to `path` as CSV.

    Returns the number of data rows written. The header is always written,
    so an empty history still produces a valid (header-only) file rather
    than a missing one.
    """
    # `top_n=n` rather than slicing afterwards: on a corpus-sized database
    # the full ranking is millions of `MatchCandidate` objects, and this
    # export only ever wanted the head of it. The rows are identical either
    # way (see `rank_matches`).
    candidates = rank_matches(conn, cfg, company_id, matches_only=matches_only, top_n=n)

    destination = Path(path)
    if destination.parent and not destination.parent.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(MATCH_SAMPLE_COLUMNS)
        for rank, candidate in enumerate(candidates, start=1):
            writer.writerow(
                [
                    rank,
                    candidate.company_id,
                    candidate.old_posting_id or "",
                    candidate.old_job_id,
                    candidate.old_title or "",
                    to_utc_z(candidate.old_first_seen_absent),
                    candidate.new_posting_id or "",
                    candidate.new_job_id,
                    candidate.new_title or "",
                    to_utc_z(candidate.new_first_observed),
                    f"{candidate.gap_days:.4f}",
                    _fmt(candidate.components.title),
                    _fmt(candidate.components.team),
                    _fmt(candidate.components.location),
                    _fmt(candidate.components.description),
                    _fmt(candidate.combined),
                    "true" if candidate.corroborated else "false",
                    "true" if candidate.title_only else "false",
                    "true" if candidate.passes_thresholds else "false",
                    "true" if candidate.is_match else "false",
                    candidate.reject_reason or "",
                    "",  # human_verdict: true_positive / false_positive / unsure
                ]
            )

    return len(candidates)
