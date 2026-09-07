"""Repost / version matching for disappeared postings (spec.md §4, PLAN.md M2 #2).

spec.md §4: "Match reposted/versioned roles using title, team, location, and
description similarity. Keep thresholds configurable and validate match
precision on a hand-checked sample of 50 matches." `rli.history.sample`
produces that sample from the ranked output of this module.

CONFIG: every knob lives in the `[matching]` table (`rli.config.Matching`).
That table is authoritative; the older `[thresholds].repost_*` keys it
replaced are no longer read here.

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

--------------------------------------------------------------------------
Hard gates, applied BEFORE scoring
--------------------------------------------------------------------------

A candidate failing any of these is never scored and never appears in the
ranking at all. They encode facts, not tuning:

1. **Same company.** A cross-company pair is not constructed by
   `rank_matches` and is rejected outright by `score_pair`.
2. **Not the same job — the version rule.** spec.md §4 distinguishes a
   *repost* (the role comes back under a NEW ATS job id) from a *version*
   (the SAME ATS job id stays live while its content hash changes). A
   version is a single continuous posting and must never be linked to
   itself as a repost. Two guards enforce it: the pair must have different
   `job_id`s (one `PostingInterval` is derived per `(company_id, job_id)`,
   so a content-hash change under a stable id produces exactly one
   interval, which is never absent and so is never even a candidate `old`),
   and, defensively, the pair must not share a canonical `url`.
3. **The old posting actually disappeared** (`first_seen_absent` is set).
4. **Temporal ordering.** The candidate's `first_observed` must be strictly
   AFTER the old posting's `last_seen_open`: a role cannot be reposted
   before the last time we saw the original open.
5. **Ordering relative to the observed absence.** `gap_days` (the
   candidate's `first_observed` minus the old posting's
   `first_seen_absent`, in days) must satisfy
   `-pre_absence_tolerance_days < gap_days <= max_gap_days`. The upper
   bound is the "how long can a repost gap be" knob. The lower bound is the
   tolerance for a repost that first appeared INSIDE the old posting's
   censoring interval `(last_seen_open, first_seen_absent]` — with sparse
   Wayback captures the very common shape is one capture that shows the old
   job gone and the new one present, which is `gap_days == 0`.
6. **No coexistence.** If any single capture listed BOTH jobs, they are
   coexisting roles, not a role and its repost. This is implied by gate (4)
   for as long as that gate stays strict, but it is enforced independently
   over `PostingInterval.present_snapshot_ids` so that relaxing the
   timestamp comparison can never silently readmit side-by-side postings.
7. **Both sides carry a usable title.** Title is the mandatory component,
   and "usable" means `rli.history.titles.is_junk_title` says no: scraped
   board pages yield link text like `"Apply"` / `"View"` / `""` rather than
   a role name, and a page of those produces N postings that all score 1.0
   against one another. The same module gates the archive HTML extractor,
   so junk is normally never persisted; this gate covers rows written
   before that fix.

--------------------------------------------------------------------------
Match decision
--------------------------------------------------------------------------

A candidate that survives the gates `passes_thresholds` when:

* `title >= matching.title_min`, AND
* `combined >= matching.combined_min`, AND
* **at least one other component corroborates it** — some component among
  team / location / description that is KNOWN for both sides meets its own
  minimum (`team_min` / `location_min` / `description_min`). Corroboration
  is required rather than each component being a veto: requiring an exact
  location match would reject the very common "same role reposted with a
  broadened location", but requiring *something* beyond the title stops two
  unrelated postings from linking on a generic title alone.
* When NO other component is known for both sides, there is nothing to
  corroborate with, so the title bar is raised to `matching.title_only_min`
  instead (and the corroboration requirement is waived rather than made
  unsatisfiable).

`passes_thresholds` is necessary but not sufficient. A final **one-to-one
assignment** is applied over the whole ranking: candidates are considered in
descending combined-score order (ties broken by `(old_job_id, new_job_id)`
for reproducibility) and greedily accepted only while neither side has been
used, so **each disappeared posting links to at most one repost and each
repost is claimed by at most one disappeared posting**. Without it, one
capture that introduces five "Software Engineer" postings after five closed
"Software Engineer" postings produces 25 mutual links. `is_match` reflects
the assignment; `passes_thresholds` reflects only the scores, and
`reject_reason` says which of the two rejected a candidate — all three are
exported to the hand-check CSV.

`link_reposts` write rules:

* Only assigned matches (`is_match`) are written.
* The one-to-one invariant is upheld ACROSS runs as well as within one: a
  `postings` row that already has a `replacement_job_id`, and a posting that
  is already some other posting's `replacement_job_id`, are both treated as
  taken.
* An existing non-NULL `postings.replacement_job_id` is NEVER overwritten.
  Re-running is therefore idempotent and non-destructive; to re-link, clear
  the derived columns first — that is what `rli.history.cli rebuild` does.
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

from rli.config import Config, Matching
from rli.history.closures import PostingInterval, build_intervals
from rli.history.titles import is_junk_title
from rli.models.time import now_utc, to_utc_z

__all__ = [
    "COMPONENT_WEIGHTS",
    "CORROBORATING_COMPONENTS",
    "ComponentScores",
    "MatchCandidate",
    "RepostLinkSummary",
    "assign_one_to_one",
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

# The components that may INDEPENDENTLY corroborate a title match. Title is
# excluded by construction: it cannot corroborate itself.
CORROBORATING_COMPONENTS: tuple[str, ...] = ("description", "team", "location")

# Unicode-aware, and identical to `rli.history.titles`'s pattern by
# construction: the junk-title policy and the similarity scorer must agree
# on what a string is. See that module for why an ASCII-only class is wrong.
_NON_ALNUM = re.compile(r"[\W_]+")


def normalize_text(value: str | None) -> str:
    """Lowercase, replace every non-alphanumeric run with a single space, strip.

    Deliberately crude and dependency-free: it folds punctuation and casing
    ("Sr. Engineer (Remote)" -> "sr engineer remote") without stemming or a
    stopword list, both of which would need tuning data this project does
    not have yet. Identical to `rli.history.titles.normalize_title`, so the
    junk-title policy and the similarity scorer agree on what a string is.
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


def _same_underlying_job(old: PostingInterval, new: PostingInterval) -> bool:
    """True when the two intervals describe ONE job — a version, not a repost.

    See gate (2) in the module docstring. `job_id` equality is the primary
    test (and is structurally sufficient, since `build_intervals` derives
    exactly one interval per `(company_id, job_id)` however many times the
    job's content hash changed). Equal canonical URLs are a second, cheap
    guard for an ATS that re-issues an id while keeping the posting URL.
    """
    if old.job_id == new.job_id:
        return True
    if old.url and new.url and old.url.strip().rstrip("/") == new.url.strip().rstrip("/"):
        return True
    return False


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
    # match" flags, from the `[matching]` per-component minimums.
    # Components that are unknown are absent from this mapping.
    passed: dict[str, bool]
    # True when some component OTHER than title is known for both sides and
    # meets its own minimum. False both when nothing corroborates and when
    # there was nothing available to corroborate with (see `title_only`).
    corroborated: bool
    # True when title was the only component known for both sides, so the
    # stricter `matching.title_only_min` bar applied.
    title_only: bool
    # Days from the old posting's `first_seen_absent` to the candidate's
    # `first_observed`. May be negative (bounded by
    # `matching.pre_absence_tolerance_days`) when the repost appeared inside
    # the old posting's censoring interval.
    gap_days: float
    # Score-only verdict, before the one-to-one assignment.
    passes_thresholds: bool
    # Final verdict: `passes_thresholds` AND won the one-to-one assignment.
    is_match: bool
    # None when `is_match`; otherwise "thresholds" or "assignment".
    reject_reason: str | None = None


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
    # Assigned matches skipped because the candidate repost was already
    # claimed by a DIFFERENT disappeared posting in an earlier run.
    new_posting_taken: int = 0

    def describe(self) -> str:
        return (
            f"scored={self.candidates_scored} matches={self.matches} "
            f"links_written={self.links_written} "
            f"links_already_present={self.links_already_present} "
            f"replacement_set={self.replacements_set} "
            f"replacement_kept={self.replacements_kept} "
            f"reappeared_at_set={self.reappeared_at_set} "
            f"new_taken={self.new_posting_taken} "
            f"unresolved={self.unresolved_postings}"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _component_minimums(matching: Matching) -> dict[str, float]:
    return {
        "title": matching.title_min,
        "team": matching.team_min,
        "location": matching.location_min,
        "description": matching.description_min,
    }


def score_pair(old: PostingInterval, new: PostingInterval, cfg: Config) -> MatchCandidate | None:
    """Score one candidate pair, or return None if a hard gate rejects it.

    The gates are enumerated in the module docstring; each is applied here
    in the same order, cheapest and most structural first.
    """
    matching = cfg.matching

    # (1) same company
    if old.company_id != new.company_id:
        return None
    # (2) a version (one job) is not a repost (two jobs)
    if _same_underlying_job(old, new):
        return None
    # (3) the old posting actually disappeared
    if old.first_seen_absent is None:
        return None
    # (4) temporal ordering
    if new.first_observed <= old.last_seen_open:
        return None
    # (5) ordering relative to the observed absence
    gap_days = (new.first_observed - old.first_seen_absent).total_seconds() / 86400.0
    if gap_days <= -matching.pre_absence_tolerance_days:
        return None
    if gap_days > matching.max_gap_days:
        return None
    # (6) never listed together in one capture
    if old.coexists_with(new):
        return None
    # (7) both sides carry a usable, non-junk title
    min_chars = matching.junk_title_min_chars
    if is_junk_title(old.title, min_chars=min_chars):
        return None
    if is_junk_title(new.title, min_chars=min_chars):
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

    minimums = _component_minimums(matching)
    passed = {name: value >= minimums[name] for name, value in known.items()}

    available = [name for name in CORROBORATING_COMPONENTS if name in known]
    title_only = not available
    corroborated = any(passed[name] for name in available)

    title_bar = matching.title_only_min if title_only else matching.title_min
    passes_thresholds = (
        known["title"] >= title_bar
        and combined >= matching.combined_min
        and (title_only or corroborated)
    )

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
        corroborated=corroborated,
        title_only=title_only,
        gap_days=gap_days,
        passes_thresholds=passes_thresholds,
        # Provisional; `assign_one_to_one` has the final say.
        is_match=passes_thresholds,
        reject_reason=None if passes_thresholds else "thresholds",
    )


def assign_one_to_one(candidates: list[MatchCandidate]) -> list[MatchCandidate]:
    """Resolve `passes_thresholds` candidates into a one-to-one assignment.

    `candidates` must already be in descending combined-score order with a
    deterministic tiebreak (`rank_matches` sorts them). Candidates are taken
    greedily in that order and accepted only while NEITHER side has been
    used, so the best-scoring pairing wins and every posting appears on at
    most one accepted link — as an old posting or as a new one, never both
    ways round.

    Returns a NEW list in the same order with `is_match` / `reject_reason`
    finalized. Candidates that never passed the thresholds keep
    `reject_reason="thresholds"`; those that passed but lost the assignment
    get `reject_reason="assignment"`.

    Greedy (rather than an optimal assignment) is a deliberate choice: it is
    O(n) after the sort, it is stable and explainable ("the best available
    pairing was taken first"), and with a hard `title_min` the score matrix
    is far too sparse for the optimal solution to differ often. Postings are
    keyed by `(company_id, job_id)` so identical job ids under different
    companies cannot collide.
    """
    used_old: set[tuple[str, str]] = set()
    used_new: set[tuple[str, str]] = set()
    resolved: list[MatchCandidate] = []

    for candidate in candidates:
        if not candidate.passes_thresholds:
            resolved.append(
                candidate.model_copy(update={"is_match": False, "reject_reason": "thresholds"})
            )
            continue

        old_key = (candidate.company_id, candidate.old_job_id)
        new_key = (candidate.company_id, candidate.new_job_id)
        if old_key in used_old or new_key in used_new:
            resolved.append(
                candidate.model_copy(update={"is_match": False, "reject_reason": "assignment"})
            )
            continue

        used_old.add(old_key)
        used_new.add(new_key)
        resolved.append(candidate.model_copy(update={"is_match": True, "reject_reason": None}))

    return resolved


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

    The returned list is the ASSIGNED ranking: `is_match` is true only for
    candidates that both pass the thresholds and win the one-to-one
    assignment (see `assign_one_to_one`). `matches_only=True` filters to
    those.

    `intervals` may be supplied to avoid recomputing closures when the
    caller already has them; otherwise they are derived here.
    """
    all_intervals = intervals if intervals is not None else build_intervals(conn, company_id)

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
                candidates.append(scored)

    candidates.sort(key=lambda c: (-c.combined, c.old_job_id, c.new_job_id))
    assigned = assign_one_to_one(candidates)
    if matches_only:
        return [c for c in assigned if c.is_match]
    return assigned


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _claimed_replacements(
    conn: sqlite3.Connection, company_id: str | None
) -> tuple[dict[str, str], set[str]]:
    """`(old_posting_id -> replacement_job_id, {claimed new posting ids})`.

    Read once per run so the cross-run half of the one-to-one invariant can
    be enforced without a query per candidate.
    """
    sql = "SELECT posting_id, replacement_job_id FROM postings WHERE replacement_job_id IS NOT NULL"
    params: tuple[str, ...] = ()
    if company_id is not None:
        sql += " AND company_id = ?"
        params = (company_id,)

    existing: dict[str, str] = {}
    for row in conn.execute(sql, params):
        existing[row["posting_id"]] = row["replacement_job_id"]
    return existing, set(existing.values())


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

    Idempotent: a second run over unchanged data writes nothing (every link
    is already present and every `replacement_job_id` is already set).
    """
    reference = now or now_utc()
    summary = RepostLinkSummary()

    ranked = rank_matches(conn, cfg, company_id)
    summary.candidates_scored = len(ranked)

    replaced_by, claimed_new = _claimed_replacements(conn, company_id)

    for candidate in ranked:
        if not candidate.is_match:
            continue
        summary.matches += 1

        if candidate.old_posting_id is None or candidate.new_posting_id is None:
            summary.unresolved_postings += 1
            continue
        if candidate.old_posting_id == candidate.new_posting_id:
            continue

        old_id = candidate.old_posting_id
        new_id = candidate.new_posting_id

        # Cross-run half of the one-to-one invariant: a repost already
        # claimed by a DIFFERENT disappeared posting is not available.
        if new_id in claimed_new and replaced_by.get(old_id) != new_id:
            summary.new_posting_taken += 1
            continue

        existing = conn.execute(
            "SELECT replacement_job_id, reappeared_at FROM postings WHERE posting_id = ?",
            (old_id,),
        ).fetchone()
        if existing is None:  # pragma: no cover - resolved ids always exist
            summary.unresolved_postings += 1
            continue

        already_linked = conn.execute(
            "SELECT 1 FROM repost_links WHERE old_posting_id = ? AND new_posting_id = ?",
            (old_id, new_id),
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
                    old_id,
                    new_id,
                    candidate.combined,
                    json.dumps(
                        {
                            "scores": candidate.components.as_dict(),
                            "passed": candidate.passed,
                            "corroborated": candidate.corroborated,
                            "title_only": candidate.title_only,
                            "gap_days": candidate.gap_days,
                            "new_first_observed": to_utc_z(candidate.new_first_observed),
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
                (new_id, to_utc_z(reference), old_id),
            )
            summary.replacements_set += 1
            replaced_by[old_id] = new_id
            claimed_new.add(new_id)
            if existing["reappeared_at"] is None:
                conn.execute(
                    "UPDATE postings SET reappeared_at = ? WHERE posting_id = ?",
                    (to_utc_z(candidate.new_first_observed), old_id),
                )
                summary.reappeared_at_set += 1
        else:
            summary.replacements_kept += 1

    conn.commit()
    return summary
