"""Temporal and company train/validation/test splits (spec.md §6; PLAN.md M3).

spec.md §6 "Splits" lists exactly two holdouts that policy tuning must
respect:

1. **Temporal holdout:** later postings excluded from training/tuning.
2. **Company holdout:** test companies absent from training.

This module implements both as pure functions over caller-supplied rows —
`(posting_id, company_id, first_observed)` triples — plus a CSV writer for
the resulting assignment. It does not touch the database: the caller is
responsible for querying postings and handing them in as `SplitRow`s, which
keeps this module trivially unit-testable and reusable outside the SQLite
schema (e.g. against a cached replay dataset).

Two independent splits are produced per posting because the two holdouts
answer different questions and a policy-tuning run typically wants both at
once: `temporal_split` answers "would training on this posting see the
future?" and `company_split` answers "would training on this posting leak
information about a company the test set also contains?". `assign_splits`
computes both for the same input and zips them into one row per posting.

Judgment calls made in this implementation, called out explicitly because
each one shapes what "correct" means for a downstream policy-tuning run:

* **Cutoff inclusivity.** `temporal_split` puts a posting with
  `first_observed == cutoff` into `"test"`, not `"dev"`. spec.md §6 frames
  the temporal holdout as excluding "later" postings from training; treating
  the exact cutoff instant as already "later" is the conservative reading —
  it never lets a training run see a posting from the boundary moment
  onward, which is the failure mode (future leakage) spec.md §6 is guarding
  against. The alternative (cutoff exclusive, boundary row goes to "dev")
  would occasionally leak a training example from the same instant used to
  define "the future" for evaluation purposes. `validation_cutoff` follows
  the same inclusive-at-the-later-edge, exclusive-at-cutoff convention:
  `validation_cutoff <= first_observed < cutoff` is `"validation"`.

* **Why a stable hash instead of `random.shuffle` / `hash()`.** Python's
  built-in `hash()` of a `str` is salted per-process by `PYTHONHASHSEED`
  (randomized by default), so two calls in two different processes — or
  even the same process restarted — would group companies differently
  despite an identical `seed` argument. `random.shuffle`/`random.Random`
  have the opposite problem: they are order-sensitive, so the same rows
  presented in a different order (e.g. a different DB query plan) can
  produce a different shuffle and therefore a different split, even though
  nothing about the *data* changed. Neither survives this module's
  determinism requirement (same `seed` + same row set, any order, any
  process -> same assignment). Deriving each company's key from
  `hashlib.blake2b(f"{seed}:{company_id}".encode())` sidesteps both: the key
  is a pure function of `(seed, company_id)` alone, so it is stable across
  processes, across `PYTHONHASHSEED`, and across input ordering.

* **Why largest-group-first.** Company posting counts in this dataset are
  expected to be highly skewed (a handful of large employers post far more
  than most companies). Assigning companies to splits in arbitrary order
  under a target-share heuristic can leave the last, largest company with no
  good split left to balance into. Processing the largest groups first and
  slotting each one into whichever split is currently furthest below its
  target share (ties broken by the stable hash key, not row order) is the
  standard greedy heuristic for balanced multiway partitioning (akin to
  longest-processing-time-first bin packing) and gives much better
  worst-case balance than processing in an arbitrary/insertion order.

* **When exact fractions are unreachable.** A company is never split across
  multiple splits — that would defeat the company holdout entirely — so if
  one company's postings outnumber a split's entire target share (e.g. one
  employer contributes more than 60% of all postings), the realized
  fractions cannot match the requested `fractions` exactly. The algorithm
  still terminates and still guarantees the one invariant that matters (no
  company appears in more than one split); it just may produce a visibly
  lopsided split in that scenario. With very few distinct companies (in the
  extreme, exactly one), some splits can end up with zero companies and
  therefore zero postings.

* **Two company-split methods: `"greedy"` (balanced, but DRIFTS) and
  `"hash"` (stable).** `company_split` (the greedy balancer above) is a
  function of the whole row set: a company's split depends on every other
  company's posting count, so as the collector adds postings day by day a
  company can move between dev, validation and test. That is fine for one
  frozen computation and fatal for anything recomputed later — a company
  tuned on as `dev` could resurface as `test`. `company_split_stable` assigns
  each company from `blake2b(seed:company_id)` alone, mapped onto
  `fractions` as cumulative thresholds: the assignment of a company never
  changes as the corpus grows, at the price of balancing COMPANY counts (in
  expectation) rather than posting counts. spec.md §6 asks only that "test
  companies [be] absent from training", which both satisfy; the stable one is
  what new replay datasets use (`rli.replay.build`), and the greedy one stays
  for reconstructing datasets built before it (`rli.eval.metrics`).

* **Empty input.** All four functions accept an empty `rows` sequence and
  return an empty mapping/list rather than raising; `write_splits_csv`
  still writes the header row so the output file is always valid CSV.

* **Naive datetimes are rejected everywhere**, via
  `rli.models.time.ensure_aware`, consistent with the rest of the codebase's
  point-in-time replay invariant (spec.md §3/§6): a naive datetime cannot be
  safely compared against a cutoff on the `available_at <= T` timeline.
"""

from __future__ import annotations

import csv
import hashlib
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from rli.models.time import ensure_aware, to_utc_z

__all__ = [
    "COMPANY_SPLIT_METHODS",
    "DEFAULT_SEED",
    "SPLITS_COLUMNS",
    "SPLIT_METHOD_COMPANY_GREEDY",
    "SPLIT_METHOD_COMPANY_HASH",
    "SPLIT_METHOD_TEMPORAL",
    "CompanySplitMethod",
    "Split",
    "SplitAssignment",
    "SplitRow",
    "assign_splits",
    "company_split",
    "company_split_stable",
    "stable_company_split_of",
    "temporal_split",
    "write_splits_csv",
]

Split = Literal["dev", "validation", "test"]

#: Arbitrary fixed default so `company_split`/`assign_splits` are
#: reproducible when a caller does not pass an explicit seed. Any fixed
#: integer works equally well for determinism; this value carries no other
#: meaning. Pass a different `seed` to get a different (still deterministic)
#: company assignment, e.g. for a sensitivity check.
DEFAULT_SEED = 20260607

_SPLIT_NAMES: tuple[Split, Split, Split] = ("dev", "validation", "test")

#: `"greedy"` = `company_split` (balanced by posting count, drifts as the
#: corpus grows); `"hash"` = `company_split_stable` (fixed per company).
CompanySplitMethod = Literal["greedy", "hash"]
COMPANY_SPLIT_METHODS: tuple[str, ...] = ("greedy", "hash")

#: How a replay dataset's split was assigned, as recorded on
#: `replay_datasets.split_method` (schema version 5) by `rli.replay.build`
#: and read back by `rli.eval.metrics.split_map_for_dataset`.
SPLIT_METHOD_TEMPORAL = "temporal"
SPLIT_METHOD_COMPANY_HASH = "company-hash"
SPLIT_METHOD_COMPANY_GREEDY = "company-greedy"

SPLITS_COLUMNS = ["posting_id", "company_id", "first_observed", "temporal_split", "company_split"]


class SplitRow(BaseModel):
    """One posting to be assigned to splits."""

    model_config = ConfigDict(frozen=True)

    posting_id: str
    company_id: str
    first_observed: datetime

    @field_validator("first_observed")
    @classmethod
    def _tz_aware_utc(cls, value: datetime, info: ValidationInfo) -> datetime:
        return ensure_aware(value, info.field_name)


class SplitAssignment(BaseModel):
    """A posting together with its temporal and company split assignment."""

    model_config = ConfigDict(frozen=True)

    posting_id: str
    company_id: str
    first_observed: datetime
    temporal_split: Split
    company_split: Split

    @field_validator("first_observed")
    @classmethod
    def _tz_aware_utc(cls, value: datetime, info: ValidationInfo) -> datetime:
        return ensure_aware(value, info.field_name)


def _check_unique_posting_ids(rows: Sequence[SplitRow]) -> None:
    """Reject duplicate `posting_id`s.

    Both split functions return a mapping keyed by `posting_id`, so a
    duplicated id would silently collapse into one entry — and in
    `company_split` it would also be counted twice while balancing target
    shares, quietly skewing the split it never appears in. That is a caller
    bug (a bad query, a double-counted archive row), not something to paper
    over: spec.md §6's holdouts are only meaningful if each posting is
    assigned exactly once.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in rows:
        if row.posting_id in seen:
            duplicates.add(row.posting_id)
        seen.add(row.posting_id)
    if duplicates:
        raise ValueError(
            f"duplicate posting_id(s) in rows: {sorted(duplicates)!r}; "
            "each posting must be assigned to a split exactly once"
        )


def _validate_fractions(fractions: tuple[float, float, float]) -> None:
    if len(fractions) != 3:
        raise ValueError("fractions must have exactly three values (dev, validation, test)")
    if any(f <= 0 for f in fractions):
        raise ValueError(f"fractions must all be positive, got {fractions}")
    total = sum(fractions)
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        raise ValueError(f"fractions must sum to ~1.0, got {total} from {fractions}")


def temporal_split(
    rows: Sequence[SplitRow],
    cutoff: datetime,
    *,
    validation_cutoff: datetime | None = None,
) -> dict[str, Split]:
    """Assign each posting to `"dev"`, `"validation"`, or `"test"` by time.

    `first_observed >= cutoff` -> `"test"` (the temporal holdout: later
    postings are excluded from training/tuning per spec.md §6). The cutoff
    boundary is inclusive on the test side — see the module docstring for
    why. When `validation_cutoff` is given, `validation_cutoff <=
    first_observed < cutoff` -> `"validation"`, and everything strictly
    before `validation_cutoff` -> `"dev"`. Without `validation_cutoff`,
    everything before `cutoff` is `"dev"`.

    Raises `ValueError` if `cutoff` or `validation_cutoff` is naive, if
    `validation_cutoff` is after `cutoff`, or if `rows` contains a duplicate
    `posting_id`.

    Returns a mapping keyed by `posting_id`.
    """
    _check_unique_posting_ids(rows)
    cutoff = ensure_aware(cutoff, "cutoff")
    if validation_cutoff is not None:
        validation_cutoff = ensure_aware(validation_cutoff, "validation_cutoff")
        if validation_cutoff > cutoff:
            raise ValueError(
                f"validation_cutoff ({validation_cutoff!r}) must be <= cutoff ({cutoff!r})"
            )

    result: dict[str, Split] = {}
    for row in rows:
        if row.first_observed >= cutoff:
            result[row.posting_id] = "test"
        elif validation_cutoff is not None and row.first_observed >= validation_cutoff:
            result[row.posting_id] = "validation"
        else:
            result[row.posting_id] = "dev"
    return result


def _stable_company_key(seed: int, company_id: str) -> str:
    """Deterministic per-(seed, company) key, independent of process/order.

    See the module docstring's "why a stable hash" judgment call: this must
    NOT depend on Python's randomized `hash()` or on input row order.
    """
    return hashlib.blake2b(f"{seed}:{company_id}".encode(), digest_size=16).hexdigest()


def company_split(
    rows: Sequence[SplitRow],
    *,
    seed: int = DEFAULT_SEED,
    fractions: tuple[float, float, float] = (0.6, 0.2, 0.2),
) -> dict[str, Split]:
    """Assign each posting to a split by grouping on `company_id`.

    Every posting belonging to the same company lands in the same split
    (GroupKFold-style), so test companies are entirely absent from
    dev/validation and vice versa (spec.md §6's company holdout). Companies
    are processed largest-group-first (ties broken by a stable hash of
    `f"{seed}:{company_id}"`, not by row order or Python's randomized
    `hash()`) and each is slotted into whichever split is currently furthest
    below its target share of the total posting count. See the module
    docstring for why this order and this key, and for what happens when one
    company is too large for `fractions` to be hit exactly.

    `fractions` is `(dev, validation, test)` and must be three positive
    floats summing to ~1.0, else `ValueError`. A duplicate `posting_id` in
    `rows` is also a `ValueError`.

    Returns a mapping keyed by `posting_id`. Empty input returns `{}`.
    """
    _check_unique_posting_ids(rows)
    _validate_fractions(fractions)
    if not rows:
        return {}

    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row.company_id, []).append(row.posting_id)

    companies = sorted(
        groups,
        key=lambda company_id: (-len(groups[company_id]), _stable_company_key(seed, company_id)),
    )

    total = sum(len(postings) for postings in groups.values())
    targets = {name: fractions[i] * total for i, name in enumerate(_SPLIT_NAMES)}
    counts: dict[Split, int] = dict.fromkeys(_SPLIT_NAMES, 0)

    company_to_split: dict[str, Split] = {}
    for company_id in companies:
        chosen = max(
            _SPLIT_NAMES,
            key=lambda name: (targets[name] - counts[name], -_SPLIT_NAMES.index(name)),
        )
        company_to_split[company_id] = chosen
        counts[chosen] += len(groups[company_id])

    result: dict[str, Split] = {}
    for company_id, posting_ids in groups.items():
        split = company_to_split[company_id]
        for posting_id in posting_ids:
            result[posting_id] = split
    return result


def company_split_stable(
    rows: Sequence[SplitRow],
    *,
    seed: int = DEFAULT_SEED,
    fractions: tuple[float, float, float] = (0.6, 0.2, 0.2),
) -> dict[str, Split]:
    """Assign each posting to its COMPANY's fixed split (module docstring).

    A company's split is a pure function of `(seed, company_id, fractions)`:
    the first 64 bits of `blake2b(f"{seed}:{company_id}")` as a fraction of
    2**64, compared against the cumulative `fractions` (dev, then validation,
    then test). Adding or removing other rows never moves a company, which is
    what lets a replay dataset be extended or rebuilt later without a company
    changing sides. Same validation as `company_split`.
    """
    _check_unique_posting_ids(rows)
    _validate_fractions(fractions)
    by_company: dict[str, Split] = {}
    result: dict[str, Split] = {}
    for row in rows:
        split = by_company.get(row.company_id)
        if split is None:
            split = stable_company_split_of(row.company_id, seed=seed, fractions=fractions)
            by_company[row.company_id] = split
        result[row.posting_id] = split
    return result


def stable_company_split_of(
    company_id: str,
    *,
    seed: int = DEFAULT_SEED,
    fractions: tuple[float, float, float] = (0.6, 0.2, 0.2),
) -> Split:
    """The split `company_split_stable` gives every posting of `company_id`.

    A pure function of `(seed, company_id, fractions)`, so a company's side is
    known without loading any posting — which is what lets every non-test
    replay build exclude the stable company holdout up front.
    """
    _validate_fractions(fractions)
    digest = hashlib.blake2b(f"{seed}:{company_id}".encode(), digest_size=8).digest()
    position = int.from_bytes(digest, "big") / 2.0**64
    if position < fractions[0]:
        return "dev"
    if position < fractions[0] + fractions[1]:
        return "validation"
    return "test"


def assign_splits(
    rows: Sequence[SplitRow],
    *,
    cutoff: datetime,
    validation_cutoff: datetime | None = None,
    seed: int = DEFAULT_SEED,
    fractions: tuple[float, float, float] = (0.6, 0.2, 0.2),
    company_method: CompanySplitMethod = "greedy",
) -> list[SplitAssignment]:
    """Compute both splits for `rows` and combine them, preserving order.

    One `SplitAssignment` is produced per input row, in the same order as
    `rows`. See `temporal_split` and `company_split` / `company_split_stable`
    (`company_method`) for the individual semantics and their `ValueError`
    conditions.
    """
    if company_method not in COMPANY_SPLIT_METHODS:
        raise ValueError(
            f"company_method must be one of {COMPANY_SPLIT_METHODS}, got {company_method!r}"
        )
    temporal = temporal_split(rows, cutoff, validation_cutoff=validation_cutoff)
    if company_method == "hash":
        company = company_split_stable(rows, seed=seed, fractions=fractions)
    else:
        company = company_split(rows, seed=seed, fractions=fractions)
    return [
        SplitAssignment(
            posting_id=row.posting_id,
            company_id=row.company_id,
            first_observed=row.first_observed,
            temporal_split=temporal[row.posting_id],
            company_split=company[row.posting_id],
        )
        for row in rows
    ]


def write_splits_csv(path: str | Path, assignments: Sequence[SplitAssignment]) -> int:
    """Write `assignments` to `path` as CSV with header `SPLITS_COLUMNS`.

    Returns the number of data rows written. The header is always written,
    so zero assignments still produce a valid (header-only) file rather than
    a missing one. Parent directories are created if missing, matching
    `rli.history.sample.export_match_sample`.
    """
    destination = Path(path)
    if destination.parent and not destination.parent.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(SPLITS_COLUMNS)
        for assignment in assignments:
            writer.writerow(
                [
                    assignment.posting_id,
                    assignment.company_id,
                    to_utc_z(assignment.first_observed),
                    assignment.temporal_split,
                    assignment.company_split,
                ]
            )

    return len(assignments)
