"""Repost / version matching for disappeared postings (spec.md §4, PLAN.md M2 #2).

spec.md §4: "Match reposted/versioned roles using title, team, location, and
description similarity. Keep thresholds configurable and validate match
precision on a hand-checked sample of 50 matches." `rli.history.sample`
produces that sample from the ranked output of this module.

CONFIG CHOICE (option (a) of the two offered): the per-component minimums
already in `[thresholds]` — `repost_title_similarity`,
`repost_description_similarity`, `repost_team_similarity`,
`repost_location_similarity` — are reused as-is rather than duplicated into
a near-identical `[matching]` table, and only the two genuinely new knobs
are added to the SAME `Thresholds` model / `[thresholds]` table:
`repost_combined_min` and `repost_max_gap_days`. Both are given defaults in
`rli.config.Thresholds` so configs that predate them keep validating
(`extra="forbid"` is untouched).

Scoring (all GUESSED weights/shapes, placeholders pending the hand-checked
50-match validation in PLAN.md M2):

* **title** — the mean of two complementary measures, because each fails
  where the other works: normalized token-set **Jaccard** (order-insensitive,
  robust to "Engineer, Backend" vs "Backend Engineer", but blind to spelling
  drift) and stdlib `difflib.SequenceMatcher.ratio()` (character-level, so
  it catches "Sr." vs "Senior" but is fooled by reordering). No third-party
  fuzzy-match dependency is used or wanted.
* **team** / **location** — the same combined measure over the normalized
  strings, so an exact normalized match scores 1.0 and near-misses degrade
  smoothly instead of falling off a cliff.
* **description** — `board_snapshot_jobs` stores only a `description_hash`,
  never the raw text, so text similarity is **a documented no-op fallback**:
  equal hashes score 1.0 (a very strong signal — byte-identical job
  content), unequal hashes score 0.0, and a hash missing on either side
  scores `None` = "unknown component". Should raw descriptions ever be
  stored, `_description_score` is the one place to extend.

A component that is `None` (unknown on either side) is EXCLUDED from the
combined score and its weight is renormalized across the known components,
rather than being scored 0. Scoring a missing field as a mismatch would
manufacture evidence of difference out of missing data — the same failure
mode spec.md §4 forbids for coverage gaps.

Combined score = weighted mean over KNOWN components with weights
`title 0.45, description 0.25, team 0.15, location 0.15`.

Match decision (`is_match`), in order:

1. Hard gates, applied before scoring — a candidate failing any of these is
   never scored or returned at all:
   - **same `company_id`** (a cross-company candidate is excluded outright,
     regardless of how well it scores),
   - the candidate is a different job than the old posting,
   - the old posting actually disappeared (`first_seen_absent` is set),
   - the candidate first appeared strictly AFTER the old posting's
     `last_seen_open` (a repost cannot predate the original's last open
     sighting),
   - `gap_days` = (candidate `first_observed` - old `first_seen_absent`) in
     days is `<= repost_max_gap_days`. There is deliberately no lower bound
     beyond the previous rule: a repost may legitimately appear inside the
     old posting's censoring interval `(last_seen_open, first_seen_absent]`,
     which yields a small negative gap.
   - both sides have a title (title is the mandatory component; a titleless
     candidate can never be matched).
2. `title >= thresholds.repost_title_similarity` AND
   `combined >= thresholds.repost_combined_min`.

The remaining three per-component thresholds are NOT additional veto gates —
requiring, say, an exact location match would reject the very common
"same role reposted with a broadened location" case. They are used to record
which components INDEPENDENTLY corroborate a match (`MatchCandidate.passed`,
persisted in `repost_links.component_scores` and exported in the hand-check
CSV), and `rli.history.features` uses `passed['title']` + `passed
['description']` to distinguish a `'repeated_unchanged'` repost from a
`'changed'` one.

`link_reposts` write rules:

* Candidates are applied in DESCENDING combined-score order, so within a run
  the BEST match wins for a given old posting.
* An existing non-NULL `postings.replacement_job_id` is NEVER overwritten —
  whether it was set by an earlier run of this function or by another part
  of the system. Re-running is therefore idempotent and non-destructive; to
  re-link, clear the column first.
* `reappeared_at` is set on the disappeared posting (only when NULL) to the
  matched candidate's `first_observed`. Semantic note: `reappeared_at`
  strictly means "this same job id was listed again"; a repost is a
  DIFFERENT job id, so this is a deliberate widening of the column to "the
  role was observed live again", consistent with spec.md §5 listing
  `reappeared_at` and `replacement_job_id` side by side as the repost
  signals.
* Every accepted match is also written to `repost_links` (see
  `rli/history/schema_ext.sql`) with its component scores as JSON, so a
  linking decision can be audited long after the thresholds change.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.history.closures import PostingInterval, build_intervals
from rli.models.time import now_utc, to_utc_z

__all__ = [
    "COMPONENT_WEIGHTS",
    "ComponentScores",
    "MatchCandidate",
    "RepostLinkSummary",
    "link_reposts",
    "normalize_text",
    "rank_matches",
    "score_pair",
]

# GUESSED (placeholder) component weights, renormalized over the components
# that are known for a given pair. Title dominates because it is the only
# component guaranteed present; description is second because an equal hash
# is near-conclusive; team and location are weak, high-variance fields
# (a team rename or a "Remote (US)" -> "Remote" edit is common and benign).
COMPONENT_WEIGHTS: dict[str, float] = {
    "title": 0.45,
    "description": 0.25,
    "team": 0.15,
    "location": 0.15,
}

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_text(value: str | None) -> str:
    """Lowercase, replace every non-alphanumeric run with a single space, strip.

    Deliberately crude and dependency-free: it folds punctuation and casing
    ("Sr. Engineer (Remote)" -> "sr engineer remote") without stemming or a
    stopword list, both of which would need tuning data this project does
    not have yet.
    """
    if value is None:
        return ""
    return _NON_ALNUM.sub(" ", value.lower()).strip()


def _combined_similarity(left: str | None, right: str | None) -> float | None:
    """Mean of token-set Jaccard and `SequenceMatcher.ratio()`; None if unknown."""
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return None
    if a == b:
        return 1.0

    tokens_a, tokens_b = set(a.split()), set(b.split())
    union = tokens_a | tokens_b
    jaccard = len(tokens_a & tokens_b) / len(union) if union else 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    return (jaccard + ratio) / 2.0


def _description_score(left: str | None, right: str | None) -> float | None:
    """Description component: hash equality only (see module docstring).

    Raw description text is not stored anywhere in the schema, so there is
    no text-similarity path to fall back to. Returning `None` for a missing
    hash keeps "we cannot tell" distinct from "they differ".
    """
    if not left or not right:
        return None
    return 1.0 if left == right else 0.0


class ComponentScores(BaseModel):
    """Per-component similarity in [0, 1]; `None` = unknown on either side."""

    model_config = ConfigDict(frozen=True)

    title: float | None = None
    team: float | None = None
    location: float | None = None
    description: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "title": self.title,
            "team": self.team,
            "location": self.location,
            "description": self.description,
        }


class MatchCandidate(BaseModel):
    """One scored (disappeared posting, later posting) pair for one company."""

    model_config = ConfigDict(frozen=True)

    company_id: str

    old_job_id: str
    old_posting_id: str | None
    old_title: str | None
    old_first_seen_absent: datetime
    old_last_seen_open: datetime

    new_job_id: str
    new_posting_id: str | None
    new_title: str | None
    new_first_observed: datetime

    components: ComponentScores
    combined: float
    # Per-component "does this component independently corroborate the
    # match" flags, from the `[thresholds].repost_*_similarity` minimums.
    # Components that are unknown are absent from this mapping.
    passed: dict[str, bool]
    # Days from the old posting's `first_seen_absent` to the candidate's
    # `first_observed`. May be negative when the repost appeared inside the
    # old posting's censoring interval.
    gap_days: float
    is_match: bool


@dataclass
class RepostLinkSummary:
    """Counters for one `link_reposts` run."""

    candidates_scored: int = 0
    matches: int = 0
    links_written: int = 0
    links_already_present: int = 0
    replacements_set: int = 0
    replacements_kept: int = 0
    reappeared_at_set: int = 0
    unresolved_postings: int = 0

    def describe(self) -> str:
        return (
            f"scored={self.candidates_scored} matches={self.matches} "
            f"links_written={self.links_written} "
            f"links_already_present={self.links_already_present} "
            f"replacement_set={self.replacements_set} "
            f"replacement_kept={self.replacements_kept} "
            f"reappeared_at_set={self.reappeared_at_set} "
            f"unresolved={self.unresolved_postings}"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_pair(old: PostingInterval, new: PostingInterval, cfg: Config) -> MatchCandidate | None:
    """Score one candidate pair, or return None if a hard gate rejects it.

    Hard gates (see module docstring): same company, different job, the old
    posting really disappeared, the candidate appeared after the old
    posting's last open sighting, the gap is within
    `repost_max_gap_days`, and both sides carry a title.
    """
    thresholds = cfg.thresholds

    if old.company_id != new.company_id:
        return None
    if old.job_id == new.job_id:
        return None
    if old.first_seen_absent is None:
        return None
    if new.first_observed <= old.last_seen_open:
        return None
    if not old.title or not new.title:
        return None

    gap_days = (new.first_observed - old.first_seen_absent).total_seconds() / 86400.0
    if gap_days > thresholds.repost_max_gap_days:
        return None

    components = ComponentScores(
        title=_combined_similarity(old.title, new.title),
        team=_combined_similarity(old.team, new.team),
        location=_combined_similarity(old.location, new.location),
        description=_description_score(old.description_hash, new.description_hash),
    )

    scores = components.as_dict()
    known = {name: value for name, value in scores.items() if value is not None}
    if "title" not in known:  # unreachable given the title gate; defensive
        return None

    weight_total = sum(COMPONENT_WEIGHTS[name] for name in known)
    combined = sum(COMPONENT_WEIGHTS[name] * value for name, value in known.items()) / weight_total

    minimums = {
        "title": thresholds.repost_title_similarity,
        "team": thresholds.repost_team_similarity,
        "location": thresholds.repost_location_similarity,
        "description": thresholds.repost_description_similarity,
    }
    passed = {name: value >= minimums[name] for name, value in known.items()}

    is_match = passed["title"] and combined >= thresholds.repost_combined_min

    return MatchCandidate(
        company_id=old.company_id,
        old_job_id=old.job_id,
        old_posting_id=old.posting_id,
        old_title=old.title,
        old_first_seen_absent=old.first_seen_absent,
        old_last_seen_open=old.last_seen_open,
        new_job_id=new.job_id,
        new_posting_id=new.posting_id,
        new_title=new.title,
        new_first_observed=new.first_observed,
        components=components,
        combined=combined,
        passed=passed,
        gap_days=gap_days,
        is_match=is_match,
    )


def rank_matches(
    conn: sqlite3.Connection,
    cfg: Config,
    company_id: str | None = None,
    *,
    matches_only: bool = False,
    intervals: list[PostingInterval] | None = None,
) -> list[MatchCandidate]:
    """Score every surviving candidate pair, best combined score first.

    Pairs are formed only WITHIN a company: the outer loop groups intervals
    by `company_id`, so a cross-company pair is never even constructed, let
    alone scored. Ties are broken by `(old_job_id, new_job_id)` so the
    ranking is reproducible for the hand-checked sample.

    `intervals` may be supplied to avoid recomputing closures when the
    caller already has them; otherwise they are derived here.
    """
    all_intervals = (
        intervals if intervals is not None else build_intervals(conn, company_id)
    )

    by_company: dict[str, list[PostingInterval]] = {}
    for interval in all_intervals:
        if company_id is not None and interval.company_id != company_id:
            continue
        by_company.setdefault(interval.company_id, []).append(interval)

    candidates: list[MatchCandidate] = []
    for company_intervals in by_company.values():
        disappeared = [i for i in company_intervals if i.first_seen_absent is not None]
        for old in disappeared:
            for new in company_intervals:
                scored = score_pair(old, new, cfg)
                if scored is None:
                    continue
                if matches_only and not scored.is_match:
                    continue
                candidates.append(scored)

    candidates.sort(key=lambda c: (-c.combined, c.old_job_id, c.new_job_id))
    return candidates


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def link_reposts(
    conn: sqlite3.Connection,
    cfg: Config,
    company_id: str | None = None,
    *,
    now: datetime | None = None,
) -> RepostLinkSummary:
    """Persist accepted matches to `postings` and `repost_links`.

    Run `rli.history.closures.apply_to_postings` first: a candidate whose
    `posting_id` is unresolved (no `postings` row exists for its
    `(company_id, job_id)`) cannot be linked, because both
    `postings.replacement_job_id` and `repost_links` are foreign keys into
    `postings`. Such candidates are skipped and counted in
    `unresolved_postings` rather than silently dropped.
    """
    reference = now or now_utc()
    summary = RepostLinkSummary()

    ranked = rank_matches(conn, cfg, company_id)
    summary.candidates_scored = len(ranked)

    for candidate in ranked:
        if not candidate.is_match:
            continue
        summary.matches += 1

        if candidate.old_posting_id is None or candidate.new_posting_id is None:
            summary.unresolved_postings += 1
            continue
        if candidate.old_posting_id == candidate.new_posting_id:
            continue

        existing = conn.execute(
            "SELECT replacement_job_id, reappeared_at FROM postings WHERE posting_id = ?",
            (candidate.old_posting_id,),
        ).fetchone()
        if existing is None:  # pragma: no cover - resolved ids always exist
            summary.unresolved_postings += 1
            continue

        already_linked = conn.execute(
            "SELECT 1 FROM repost_links WHERE old_posting_id = ? AND new_posting_id = ?",
            (candidate.old_posting_id, candidate.new_posting_id),
        ).fetchone()
        if already_linked is None:
            conn.execute(
                """
                INSERT INTO repost_links (
                    company_id, old_posting_id, new_posting_id,
                    combined_score, component_scores, matched_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.company_id,
                    candidate.old_posting_id,
                    candidate.new_posting_id,
                    candidate.combined,
                    json.dumps(
                        {
                            "scores": candidate.components.as_dict(),
                            "passed": candidate.passed,
                            "gap_days": candidate.gap_days,
                        },
                        sort_keys=True,
                    ),
                    to_utc_z(reference),
                ),
            )
            summary.links_written += 1
        else:
            summary.links_already_present += 1

        if existing["replacement_job_id"] is None:
            conn.execute(
                "UPDATE postings SET replacement_job_id = ?, updated_at = ? WHERE posting_id = ?",
                (candidate.new_posting_id, to_utc_z(reference), candidate.old_posting_id),
            )
            summary.replacements_set += 1
            if existing["reappeared_at"] is None:
                conn.execute(
                    "UPDATE postings SET reappeared_at = ? WHERE posting_id = ?",
                    (to_utc_z(candidate.new_first_observed), candidate.old_posting_id),
                )
                summary.reappeared_at_set += 1
        else:
            summary.replacements_kept += 1

    conn.commit()
    return summary
