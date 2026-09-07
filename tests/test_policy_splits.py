"""Tests for `rli.policy.splits` (spec.md §6; PLAN.md M3 "Temporal + company splits")."""

from __future__ import annotations

import csv
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rli.policy.splits import (
    SPLITS_COLUMNS,
    SplitRow,
    assign_splits,
    company_split,
    temporal_split,
    write_splits_csv,
)

CUTOFF = datetime(2026, 6, 1, tzinfo=UTC)
VALIDATION_CUTOFF = datetime(2026, 3, 1, tzinfo=UTC)


def _row(posting_id: str, company_id: str, first_observed: datetime) -> SplitRow:
    return SplitRow(posting_id=posting_id, company_id=company_id, first_observed=first_observed)


# ---------------------------------------------------------------------------
# temporal_split
# ---------------------------------------------------------------------------


def test_temporal_split_boundary_exactly_at_cutoff_is_test():
    rows = [_row("p1", "c1", CUTOFF)]
    assert temporal_split(rows, CUTOFF) == {"p1": "test"}


def test_temporal_split_before_cutoff_is_dev():
    rows = [_row("p1", "c1", CUTOFF - timedelta(seconds=1))]
    assert temporal_split(rows, CUTOFF) == {"p1": "dev"}


def test_temporal_split_after_cutoff_is_test():
    rows = [_row("p1", "c1", CUTOFF + timedelta(days=1))]
    assert temporal_split(rows, CUTOFF) == {"p1": "test"}


def test_temporal_split_validation_window():
    rows = [
        _row("before", "c1", VALIDATION_CUTOFF - timedelta(seconds=1)),
        _row("at_validation_cutoff", "c1", VALIDATION_CUTOFF),
        _row("mid_validation", "c1", VALIDATION_CUTOFF + timedelta(days=10)),
        _row("at_cutoff", "c1", CUTOFF),
        _row("after_cutoff", "c1", CUTOFF + timedelta(days=1)),
    ]
    result = temporal_split(rows, CUTOFF, validation_cutoff=VALIDATION_CUTOFF)
    assert result == {
        "before": "dev",
        "at_validation_cutoff": "validation",
        "mid_validation": "validation",
        "at_cutoff": "test",
        "after_cutoff": "test",
    }


def test_temporal_split_rejects_naive_cutoff():
    rows = [_row("p1", "c1", CUTOFF)]
    with pytest.raises(ValueError):
        temporal_split(rows, datetime(2026, 6, 1))


def test_temporal_split_rejects_naive_validation_cutoff():
    rows = [_row("p1", "c1", CUTOFF)]
    with pytest.raises(ValueError):
        temporal_split(rows, CUTOFF, validation_cutoff=datetime(2026, 3, 1))


def test_temporal_split_validation_cutoff_after_cutoff_raises():
    rows = [_row("p1", "c1", CUTOFF)]
    with pytest.raises(ValueError):
        temporal_split(rows, CUTOFF, validation_cutoff=CUTOFF + timedelta(days=1))


def test_temporal_split_empty_input():
    assert temporal_split([], CUTOFF) == {}


def test_split_row_rejects_naive_first_observed():
    with pytest.raises(ValueError):
        SplitRow(posting_id="p1", company_id="c1", first_observed=datetime(2026, 1, 1))


# ---------------------------------------------------------------------------
# company_split
# ---------------------------------------------------------------------------


def _synthetic_rows(n_companies: int = 40, seed: int = 7) -> list[SplitRow]:
    rng = random.Random(seed)
    rows: list[SplitRow] = []
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n_companies):
        company_id = f"company-{i}"
        n_postings = rng.randint(1, 20)
        for j in range(n_postings):
            rows.append(_row(f"{company_id}-post-{j}", company_id, base + timedelta(days=j)))
    return rows


def test_company_split_no_company_in_more_than_one_split():
    rows = _synthetic_rows()
    result = company_split(rows)
    company_to_splits: dict[str, set[str]] = {}
    for row in rows:
        company_to_splits.setdefault(row.company_id, set()).add(result[row.posting_id])
    for company_id, splits in company_to_splits.items():
        assert len(splits) == 1, f"{company_id} spans multiple splits: {splits}"


def test_company_split_deterministic_across_calls():
    rows = _synthetic_rows()
    assert company_split(rows) == company_split(rows)


def test_company_split_deterministic_across_shuffled_order():
    rows = _synthetic_rows()
    shuffled = list(rows)
    random.Random(99).shuffle(shuffled)
    assert company_split(rows) == company_split(shuffled)


def test_company_split_different_seed_can_differ():
    rows = _synthetic_rows()
    result_a = company_split(rows, seed=1)
    result_b = company_split(rows, seed=2)
    assert result_a != result_b


def test_company_split_all_postings_of_a_company_share_split():
    rows = _synthetic_rows()
    result = company_split(rows)
    by_company: dict[str, set[str]] = {}
    for row in rows:
        by_company.setdefault(row.company_id, set()).add(result[row.posting_id])
    assert all(len(splits) == 1 for splits in by_company.values())


def test_company_split_approximate_fraction_balance():
    rows = _synthetic_rows(n_companies=40)
    result = company_split(rows, fractions=(0.6, 0.2, 0.2))
    total = len(rows)
    counts = {"dev": 0, "validation": 0, "test": 0}
    for split in result.values():
        counts[split] += 1
    assert counts["dev"] / total == pytest.approx(0.6, abs=0.1)
    assert counts["validation"] / total == pytest.approx(0.2, abs=0.1)
    assert counts["test"] / total == pytest.approx(0.2, abs=0.1)


def test_company_split_single_company():
    rows = [_row(f"p{i}", "only-co", datetime(2026, 1, 1, tzinfo=UTC)) for i in range(5)]
    result = company_split(rows)
    assert len(set(result.values())) == 1
    assert set(result.keys()) == {f"p{i}" for i in range(5)}


def test_company_split_empty_input():
    assert company_split([]) == {}


def test_company_split_rejects_bad_fractions_not_summing_to_one():
    rows = _synthetic_rows()
    with pytest.raises(ValueError):
        company_split(rows, fractions=(0.5, 0.5, 0.5))


def test_company_split_rejects_non_positive_fraction():
    rows = _synthetic_rows()
    with pytest.raises(ValueError):
        company_split(rows, fractions=(1.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# assign_splits
# ---------------------------------------------------------------------------


def test_assign_splits_preserves_order_and_count():
    rows = _synthetic_rows()
    assignments = assign_splits(rows, cutoff=datetime(2026, 6, 1, tzinfo=UTC))
    assert len(assignments) == len(rows)
    assert [a.posting_id for a in assignments] == [r.posting_id for r in rows]


def test_assign_splits_combines_temporal_and_company():
    rows = [
        _row("early", "c1", CUTOFF - timedelta(days=100)),
        _row("late", "c2", CUTOFF + timedelta(days=1)),
    ]
    assignments = assign_splits(rows, cutoff=CUTOFF)
    by_id = {a.posting_id: a for a in assignments}
    assert by_id["early"].temporal_split == "dev"
    assert by_id["late"].temporal_split == "test"
    # company_split is still computed and present for every row
    assert by_id["early"].company_split in ("dev", "validation", "test")
    assert by_id["late"].company_split in ("dev", "validation", "test")


def test_assign_splits_empty_input():
    assert assign_splits([], cutoff=CUTOFF) == []


# ---------------------------------------------------------------------------
# write_splits_csv
# ---------------------------------------------------------------------------


def test_write_splits_csv_header_only_for_empty_input(tmp_path: Path):
    out = tmp_path / "splits.csv"
    n = write_splits_csv(out, [])
    assert n == 0
    with out.open(newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    assert rows == [SPLITS_COLUMNS]


def test_write_splits_csv_round_trip(tmp_path: Path):
    rows = _synthetic_rows(n_companies=5)
    assignments = assign_splits(rows, cutoff=CUTOFF, validation_cutoff=VALIDATION_CUTOFF)
    out = tmp_path / "splits.csv"
    n = write_splits_csv(out, assignments)
    assert n == len(assignments)

    with out.open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == SPLITS_COLUMNS
        parsed = list(reader)

    assert len(parsed) == len(assignments)
    for record, assignment in zip(parsed, assignments, strict=True):
        assert record["posting_id"] == assignment.posting_id
        assert record["company_id"] == assignment.company_id
        assert record["temporal_split"] == assignment.temporal_split
        assert record["company_split"] == assignment.company_split
        assert record["first_observed"].endswith("Z")


def test_write_splits_csv_creates_missing_parent_dirs(tmp_path: Path):
    out = tmp_path / "nested" / "dir" / "splits.csv"
    assert not out.parent.exists()
    n = write_splits_csv(out, [])
    assert n == 0
    assert out.exists()


# ---------------------------------------------------------------------------
# Duplicate posting ids
# ---------------------------------------------------------------------------


DUPLICATE_ROWS = [
    _row("p1", "c1", CUTOFF - timedelta(days=1)),
    _row("p1", "c2", CUTOFF + timedelta(days=1)),
]


def test_temporal_split_rejects_duplicate_posting_ids():
    """A duplicated id would silently collapse in the returned mapping."""
    with pytest.raises(ValueError, match="duplicate posting_id"):
        temporal_split(DUPLICATE_ROWS, CUTOFF)


def test_company_split_rejects_duplicate_posting_ids():
    """It would also be double-counted while balancing target shares."""
    with pytest.raises(ValueError, match="duplicate posting_id"):
        company_split(DUPLICATE_ROWS)


def test_assign_splits_rejects_duplicate_posting_ids():
    with pytest.raises(ValueError, match="duplicate posting_id"):
        assign_splits(DUPLICATE_ROWS, cutoff=CUTOFF)
