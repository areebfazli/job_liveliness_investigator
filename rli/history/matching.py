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
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from pydantic import BaseModel, ConfigDict

from rli.config import Config, Matching
from rli.history.closures import PostingInterval, build_intervals, company_ids_with_captures
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


#: One field of one interval, normalized once: `(normalized, token set)`.
_Prepared = tuple[str, frozenset[str]]


def _prepare_text(value: str | None) -> _Prepared:
    """Normalize and pre-split one field, so scoring a pair re-does neither.

    `normalize_text` runs a regex substitution and a `str.lower()`; before
    this existed it ran four times per candidate pair (title/team/location on
    both sides), i.e. millions of times per corpus rebuild, always over the
    same few thousand distinct strings.
    """
    normalized = normalize_text(value)
    return normalized, frozenset(normalized.split())


def _similarity(left: _Prepared, right: _Prepared) -> float | None:
    """`_combined_similarity` over already-normalized fields."""
    a, tokens_a = left
    b, tokens_b = right
    if not a or not b:
        return None
    if a == b:
        return 1.0

    union = tokens_a | tokens_b
    jaccard = len(tokens_a & tokens_b) / len(union) if union else 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    return (jaccard + ratio) / 2.0


def _combined_similarity(left: str | None, right: str | None) -> float | None:
    """Mean of token-set Jaccard and `SequenceMatcher.ratio()`; None if unknown."""
    return _similarity(_prepare_text(left), _prepare_text(right))


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


@dataclass(slots=True)
class _Scorable:
    """One `PostingInterval` with every per-interval scoring input precomputed.

    Built once per interval per company. Everything on it is a property of
    ONE side of a pair, so hoisting it out of the O(n^2) pair loop cannot
    change which pairs survive the gates or what they score — only how often
    the same work is redone. `interval` is kept so the surviving pairs can
    still be turned into a `MatchCandidate` without a second lookup.
    """

    interval: PostingInterval
    job_id: str
    # `url` stripped of whitespace and a trailing slash, or None when the
    # interval carries no url. Gate (2)'s second test compares these and
    # fires only when BOTH sides have one, exactly as the original
    # `old.url and new.url and <stripped equal>` did.
    url_key: str | None
    first_observed: datetime
    last_seen_open: datetime
    first_seen_absent: datetime | None
    description_hash: str | None
    title: _Prepared
    team: _Prepared
    location: _Prepared
    # Gate (6)'s evidence, as a bitmask: `PostingInterval.present_snapshot_ids`
    # with each snapshot id replaced by its position in the `bits` registry
    # this `_Scorable` was prepared against. ONLY comparable against another
    # `_Scorable` built from the SAME registry — see `_prepare_interval`.
    mask: int


def _prepare_interval(
    interval: PostingInterval, matching: Matching, bits: dict[int, int]
) -> _Scorable | None:
    """Precompute one interval's scoring inputs, or None when gate (7) rejects it.

    Gate (7) ("both sides carry a usable title") is a property of one side,
    so it is settled here — once per interval — rather than twice per pair.

    `bits` maps a `board_snapshots.id` to a bit position and is EXTENDED as
    new snapshot ids are seen. Every interval that will be compared with
    every other must be prepared against the same dict: the resulting masks
    then intersect exactly when `present_snapshot_ids` do, which is gate (6)
    reduced to a single `&` (see `_coexists`).

    Deriving the positions from the intervals in hand, rather than from a
    capture index or the raw snapshot id, is what makes the encoding safe.
    A capture index only means something within one `load_captures` call, so
    two intervals built by separate calls would compare nonsense; raw
    snapshot ids are globally unique but unbounded, so a mask over them
    would grow to kilobytes per interval as the corpus ages. A registry
    scoped to one comparison is both correct and compact — its width is the
    number of distinct captures in that comparison (at most 134 today).
    """
    if is_junk_title(interval.title, min_chars=matching.junk_title_min_chars):
        return None
    url = interval.url

    mask = 0
    for snapshot_id in interval.present_snapshot_ids:
        position = bits.get(snapshot_id)
        if position is None:
            position = len(bits)
            bits[snapshot_id] = position
        mask |= 1 << position

    return _Scorable(
        interval=interval,
        job_id=interval.job_id,
        url_key=url.strip().rstrip("/") if url else None,
        first_observed=interval.first_observed,
        last_seen_open=interval.last_seen_open,
        first_seen_absent=interval.first_seen_absent,
        description_hash=interval.description_hash,
        title=_prepare_text(interval.title),
        team=_prepare_text(interval.team),
        location=_prepare_text(interval.location),
        mask=mask,
    )


def _coexists(old: _Scorable, new: _Scorable) -> bool:
    """Gate (6) — equivalent to `PostingInterval.coexists_with` for prepared rows.

    Both sides must have been prepared against the same `bits` registry,
    which `_prepare_interval` documents; every caller here does that.
    """
    return old.mask & new.mask != 0


def _gate_pair(old: _Scorable, new: _Scorable, matching: Matching) -> float | None:
    """Hard gates (2)-(6) for a same-company pair; returns `gap_days` or None.

    Gate (1) (same company) is guaranteed by the caller, which only ever
    pairs within one company, and gate (7) by `_prepare_interval`. Every
    gate is a pure predicate over the pair, so where each one is evaluated
    is a performance decision and never a semantic one.

    Nothing here allocates: the point of splitting the gates out of
    `score_pair` is that ~99.6% of admitted pairs are rejected by the
    thresholds, and the vast majority of *considered* pairs are rejected
    right here, so a `MatchCandidate` must not be built to find that out.
    """
    # (2) a version (one job) is not a repost (two jobs). `job_id` equality
    # is the primary test, and is structurally sufficient: `build_intervals`
    # derives exactly one interval per `(company_id, job_id)` however many
    # times the job's content hash changed. Equal canonical URLs are a
    # second, cheap guard for an ATS that re-issues an id while keeping the
    # posting URL.
    if old.job_id == new.job_id:
        return None
    if old.url_key is not None and old.url_key == new.url_key:
        return None
    # (3) the old posting actually disappeared
    absent = old.first_seen_absent
    if absent is None:
        return None
    # (4) temporal ordering
    if new.first_observed <= old.last_seen_open:
        return None
    # (5) ordering relative to the observed absence
    gap_days = (new.first_observed - absent).total_seconds() / 86400.0
    if gap_days <= -matching.pre_absence_tolerance_days:
        return None
    if gap_days > matching.max_gap_days:
        return None
    # (6) never listed together in one capture
    if _coexists(old, new):
        return None
    return gap_days


def _build_candidate(
    old: _Scorable, new: _Scorable, gap_days: float, matching: Matching
) -> MatchCandidate | None:
    """Score a pair that has already passed every hard gate."""
    components = ComponentScores(
        title=_similarity(old.title, new.title),
        team=_similarity(old.team, new.team),
        location=_similarity(old.location, new.location),
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

    old_interval, new_interval = old.interval, new.interval
    return MatchCandidate(
        company_id=old_interval.company_id,
        old_job_id=old.job_id,
        old_posting_id=old_interval.posting_id,
        old_title=old_interval.title,
        old_first_seen_absent=old.first_seen_absent,
        old_last_seen_open=old.last_seen_open,
        new_job_id=new.job_id,
        new_posting_id=new_interval.posting_id,
        new_title=new_interval.title,
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


def score_pair(old: PostingInterval, new: PostingInterval, cfg: Config) -> MatchCandidate | None:
    """Score one candidate pair, or return None if a hard gate rejects it.

    The gates are enumerated in the module docstring. This is the
    single-pair entry point (tests, ad-hoc inspection); the bulk path is
    `_score_company`, which applies exactly the same gates and the same
    scoring through the same helpers but hoists the per-interval work out of
    the pair loop.
    """
    matching = cfg.matching

    # (1) same company
    if old.company_id != new.company_id:
        return None
    # (7) both sides carry a usable, non-junk title. One shared `bits`
    # registry, so the two masks gate (6) compares are commensurable.
    bits: dict[int, int] = {}
    prepared_old = _prepare_interval(old, matching, bits)
    if prepared_old is None:
        return None
    prepared_new = _prepare_interval(new, matching, bits)
    if prepared_new is None:
        return None
    # (2)-(6)
    gap_days = _gate_pair(prepared_old, prepared_new, matching)
    if gap_days is None:
        return None
    return _build_candidate(prepared_old, prepared_new, gap_days, matching)


def _rank_key(candidate: MatchCandidate) -> tuple[float, str, str]:
    """Descending combined score, ties broken reproducibly.

    `(old_job_id, new_job_id)` is unique per candidate within a company, so
    this is a TOTAL order over one company's candidates — which is what
    makes the one-to-one assignment independent of whether candidates are
    ranked company by company or all at once (see `_score_company`).
    """
    return (-candidate.combined, candidate.old_job_id, candidate.new_job_id)


def _score_company(
    intervals: Sequence[PostingInterval],
    cfg: Config,
    *,
    passing_only: bool = False,
    top_n: int | None = None,
) -> tuple[list[MatchCandidate], int]:
    """Score every surviving pair within ONE company.

    Returns `(candidates, scored)`, where `scored` counts every pair the
    hard gates admitted whether or not it was retained, so a run summary
    reports the same number it always did.

    RETENTION (this is the memory fix). On the expanded corpus the gates
    admit ~1.6M pairs but only ~0.4% of them pass the thresholds; holding
    all 1.6M `MatchCandidate` objects (~3.6 kB each) is what OOM-killed the
    2026-09-21 rebuild. So:

    * `passing_only=True` — keep only candidates that `passes_thresholds`.
      That is everything `link_reposts` can possibly write, and it is exact:
      `assign_one_to_one` never touches `used_old` / `used_new` for a
      candidate that failed the thresholds, so dropping those cannot change
      any other candidate's verdict.
    * `top_n=k` — additionally keep at most `k` of the rejected candidates,
      the best `k` by `_rank_key`, trimming as it goes. The union of "all
      passing" and "this company's best k" always contains the global best
      k, so a caller that only wants a top-k ranking (the hand-check sample)
      gets exactly the rows it would have got from the full list.
    * neither — keep everything, the historical behaviour.

    PAIR SEARCH. Gates (4) and (5) confine a candidate's `first_observed` to
    `(max(last_seen_open, first_seen_absent - tolerance),
    first_seen_absent + max_gap]`, so the candidates for one disappeared
    posting are a contiguous run of the intervals sorted by
    `first_observed` and are found by binary search instead of by scanning
    the whole board. The window is widened by a day at each end and the
    exact comparisons are still applied inside it, so no pair that
    `_gate_pair` would have admitted can be skipped by a floating-point
    edge: this is a search optimisation, not a change of gate.
    """
    matching = cfg.matching
    # One registry for the whole company, so every pair's gate-(6) masks
    # index the same captures.
    bits: dict[int, int] = {}
    rows = [
        prepared
        for prepared in (_prepare_interval(interval, matching, bits) for interval in intervals)
        if prepared is not None
    ]
    if not rows:
        return [], 0

    by_first_observed = sorted(rows, key=lambda r: (r.first_observed, r.job_id))
    starts = [r.first_observed for r in by_first_observed]
    slack = timedelta(days=1)
    max_gap = timedelta(days=matching.max_gap_days)
    tolerance = timedelta(days=matching.pre_absence_tolerance_days)

    passing: list[MatchCandidate] = []
    rejected: list[MatchCandidate] = []
    # Trim the rejected buffer in batches rather than on every append.
    trim_at = None if top_n is None else max(4 * top_n, 64)
    scored = 0

    for old in rows:
        absent = old.first_seen_absent
        if absent is None:  # gate (3): only a disappeared posting can be `old`
            continue
        low = max(old.last_seen_open, absent - tolerance) - slack
        high = absent + max_gap + slack
        for index in range(bisect_left(starts, low), bisect_right(starts, high)):
            new = by_first_observed[index]
            gap_days = _gate_pair(old, new, matching)
            if gap_days is None:
                continue
            candidate = _build_candidate(old, new, gap_days, matching)
            if candidate is None:  # pragma: no cover - defensive, see _build_candidate
                continue
            scored += 1
            if candidate.passes_thresholds:
                passing.append(candidate)
            elif not passing_only:
                rejected.append(candidate)
                if trim_at is not None and len(rejected) >= trim_at:
                    rejected.sort(key=_rank_key)
                    del rejected[top_n:]

    if top_n is not None and len(rejected) > top_n:
        rejected.sort(key=_rank_key)
        del rejected[top_n:]
    return passing + rejected, scored


def _assign_one(
    candidate: MatchCandidate,
    used_old: set[tuple[str, str]],
    used_new: set[tuple[str, str]],
) -> MatchCandidate:
    """One greedy step of the one-to-one assignment (see `assign_one_to_one`)."""
    if not candidate.passes_thresholds:
        return candidate.model_copy(update={"is_match": False, "reject_reason": "thresholds"})

    old_key = (candidate.company_id, candidate.old_job_id)
    new_key = (candidate.company_id, candidate.new_job_id)
    if old_key in used_old or new_key in used_new:
        return candidate.model_copy(update={"is_match": False, "reject_reason": "assignment"})

    used_old.add(old_key)
    used_new.add(new_key)
    return candidate.model_copy(update={"is_match": True, "reject_reason": None})


def assign_one_to_one(candidates: list[MatchCandidate]) -> list[MatchCandidate]:
    """Resolve `passes_thresholds` candidates into a one-to-one assignment.

    `candidates` must already be in descending combined-score order with a
    deterministic tiebreak (`rank_matches` sorts them). Candidates are taken
    greedily in that order and accepted only while NEITHER side has been
    used, so the best-scoring pairing wins and every posting appears on at
    most one accepted link — as an old posting or as a new one, never both
    ways round.

    Returns a NEW list in the same order with `is_match` / `reject_reason`
    finalized, leaving `candidates` untouched. Candidates that never passed
    the thresholds keep `reject_reason="thresholds"`; those that passed but
    lost the assignment get `reject_reason="assignment"`.

    Greedy (rather than an optimal assignment) is a deliberate choice: it is
    O(n) after the sort, it is stable and explainable ("the best available
    pairing was taken first"), and with a hard `title_min` the score matrix
    is far too sparse for the optimal solution to differ often. Postings are
    keyed by `(company_id, job_id)` so identical job ids under different
    companies cannot collide.

    Because those keys carry the company, a candidate's verdict depends only
    on the candidates of its OWN company that precede it. Ranking company by
    company therefore yields the same assignment as ranking the whole corpus
    at once, which is what lets `link_reposts` bound its memory.
    """
    used_old: set[tuple[str, str]] = set()
    used_new: set[tuple[str, str]] = set()
    return [_assign_one(candidate, used_old, used_new) for candidate in candidates]


def _assign_consuming(candidates: list[MatchCandidate]) -> list[MatchCandidate]:
    """`assign_one_to_one`, but EMPTYING `candidates` as it goes.

    `assign_one_to_one` holds a full second list of `model_copy` results
    while the caller still holds the originals. At corpus scale that
    doubling was ~2.9 GB of the 7.1 GB peak that OOM-killed the 2026-09-21
    rebuild, so the bulk paths use this variant instead: same order, same
    decisions, one live copy of each candidate.
    """
    used_old: set[tuple[str, str]] = set()
    used_new: set[tuple[str, str]] = set()
    resolved: list[MatchCandidate] = []
    candidates.reverse()  # so `pop()` yields them in rank order
    while candidates:
        resolved.append(_assign_one(candidates.pop(), used_old, used_new))
    return resolved


def rank_matches(
    conn: sqlite3.Connection,
    cfg: Config,
    company_id: str | None = None,
    *,
    matches_only: bool = False,
    intervals: list[PostingInterval] | None = None,
    top_n: int | None = None,
) -> list[MatchCandidate]:
    """Score every surviving candidate pair, best combined score first.

    Pairs are formed only WITHIN a company: intervals are grouped by
    `company_id` and one company is loaded, scored and released at a time,
    so a cross-company pair is never even constructed, let alone scored.
    Ties are broken by `(old_job_id, new_job_id)` so the ranking is
    reproducible for the hand-checked sample.

    The returned list is the ASSIGNED ranking: `is_match` is true only for
    candidates that both pass the thresholds and win the one-to-one
    assignment (see `assign_one_to_one`). `matches_only=True` filters to
    those — and, because a threshold-rejected candidate can never affect
    another candidate's verdict, lets the scorer discard them as it goes
    instead of materialising every scored pair.

    `top_n` returns at most that many rows, identically to slicing the full
    ranking, but bounds memory while doing so (see `_score_company`). Use it
    rather than `rank_matches(...)[:n]` on a corpus-sized database.

    `intervals` may be supplied to avoid recomputing closures when the
    caller already has them; otherwise they are derived per company here.
    """
    grouped: dict[str, list[PostingInterval]] = {}
    if intervals is None:
        company_ids = [company_id] if company_id is not None else company_ids_with_captures(conn)
    else:
        for interval in intervals:
            if company_id is not None and interval.company_id != company_id:
                continue
            grouped.setdefault(interval.company_id, []).append(interval)
        company_ids = list(grouped)

    candidates: list[MatchCandidate] = []
    for cid in company_ids:
        company_intervals = grouped[cid] if intervals is not None else build_intervals(conn, cid)
        scored, _ = _score_company(company_intervals, cfg, passing_only=matches_only, top_n=top_n)
        candidates.extend(scored)
        del company_intervals, scored

    candidates.sort(key=_rank_key)
    assigned = _assign_consuming(candidates)
    if matches_only:
        assigned = [c for c in assigned if c.is_match]
    if top_n is not None:
        del assigned[top_n:]
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
    commit: bool = True,
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

    MEMORY: one company is loaded, scored, written and released before the
    next is touched, and within a company only the candidates that pass the
    thresholds are retained. Peak usage therefore tracks the largest single
    board, not the corpus. Doing this company by company gives exactly the
    corpus-wide result — see `assign_one_to_one` for why the assignment does
    not couple companies, and `_write_company_links` for why the cross-run
    claim check does not either.

    `commit=False` leaves the writes in the caller's open transaction (see
    `rli.history.cli.rebuild`).
    """
    reference = now or now_utc()
    summary = RepostLinkSummary()

    company_ids = [company_id] if company_id is not None else company_ids_with_captures(conn)
    for cid in company_ids:
        intervals = build_intervals(conn, cid)
        candidates, scored = _score_company(intervals, cfg, passing_only=True)
        del intervals
        summary.candidates_scored += scored
        candidates.sort(key=_rank_key)
        assigned = _assign_consuming(candidates)
        _write_company_links(conn, cid, assigned, summary, reference)
        del assigned

    if commit:
        conn.commit()
    return summary


def _write_company_links(
    conn: sqlite3.Connection,
    company_id: str,
    ranked: list[MatchCandidate],
    summary: RepostLinkSummary,
    reference: datetime,
) -> None:
    """Persist one company's assigned matches (the write half of `link_reposts`).

    `_claimed_replacements` is read per company rather than once per run.
    That is equivalent, not merely cheaper: a candidate's `new_posting_id`
    always belongs to `company_id` — `closures._existing_postings` filters
    by company and an archive-only posting is minted as
    `archive:{company_id}:{job_id}` — so a claim by some OTHER company's
    posting can never apply to it, and scoping the read cannot hide one.
    """
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
