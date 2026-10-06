"""Point-in-time integrity of `rli replay build` (spec.md §3, §6).

* **Real fetch times.** Every live observation the build makes is dated by
  when it was really made, and the build-time case of a still-open posting is
  placed at the moment that posting's live run finished — so no case can see
  an observation made after its own `T`. (Before: every observation of a
  two-hour build was stamped, and every open posting's last case placed, at
  the build's START.)
* **The leakage checker catches the old stamping**, from `tool_cache`.
* **Frozen splits.** Each case stores its build-time split; evaluation reads
  it back unchanged after the corpus grows, and reconstructs (with a warning)
  for a dataset built before splits were frozen.
* **Company disjointness.** `exclude_companies_from` drops another dataset's
  companies; a company-split build never takes a company holding a `test`
  posting.
"""

from __future__ import annotations

import sqlite3
import warnings
from datetime import datetime, timedelta

import pytest
import respx
from test_eval_helpers import COMPANY, OTHER_COMPANY, add_posting
from test_replay_helpers import (
    CLOSED_JOB,
    NOW,
    OPEN_JOB,
    OTHER_JOB,
    OTHER_TENANT,
    TENANT,
    dev_everything_cutoff,
    mock_ats,
    seed_corpus,
)

from rli.config import Config
from rli.eval.baseline import load_split_map
from rli.eval.metrics import FrozenSplitWarning, split_map_for_dataset
from rli.models.time import parse_utc, to_utc_z
from rli.net import hash_args
from rli.policy.splits import SplitRow, company_split_stable
from rli.replay import build as build_module
from rli.replay.build import build_dataset, case_state_at
from rli.replay.leakage import check_dataset
from rli.replay.mode import ReplayProbeStore
from rli.replay.run import run_replay

OPEN_POSTING = f"greenhouse:{TENANT}:{OPEN_JOB}"
CLOSED_POSTING = f"greenhouse:{TENANT}:{CLOSED_JOB}"
OTHER_POSTING = f"greenhouse:{OTHER_TENANT}:{OTHER_JOB}"
NO_EVENTS_CSV = "/nonexistent/collection_status.csv"


class _TickingClock:
    """A real-looking clock: each read is one minute after the previous one."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


def _build(conn: sqlite3.Connection, cfg: Config, dataset_id: str, **kwargs: object):
    options: dict[str, object] = {
        "dataset_id": dataset_id,
        "split": "dev",
        "split_kind": "temporal",
        "grid_step_days": 30,
        "now": NOW,
        "cutoff": dev_everything_cutoff(),
        "use_tool_cache": False,
        "collection_status_csv": NO_EVENTS_CSV,
    }
    options.update(kwargs)
    summary = build_dataset(conn, cfg, **options)  # type: ignore[arg-type]
    assert summary.postings_failed == 0, summary.failures
    return summary


def _case_times(conn: sqlite3.Connection, dataset_id: str, posting_id: str) -> list[datetime]:
    return sorted(
        parse_utc(row[0])
        for row in conn.execute(
            "SELECT replay_at FROM replay_cases WHERE dataset_id = ? AND posting_id = ?",
            (dataset_id, posting_id),
        )
    )


# ---------------------------------------------------------------------------
# Real fetch times
# ---------------------------------------------------------------------------


@respx.mock
def test_build_time_evidence_carries_real_fetch_times_and_its_own_t(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    mock_ats()
    _build(conn, cfg, "ds-clock", clock=_TickingClock(NOW))

    store = ReplayProbeStore()
    open_times = _case_times(conn, "ds-clock", OPEN_POSTING)
    other_times = _case_times(conn, "ds-clock", OTHER_POSTING)
    build_t = open_times[-1]
    # The build-time grid point is this posting's own observation time, after
    # the build started, and differs per posting (they were observed in turn).
    assert build_t > NOW
    assert other_times[-1] > NOW and other_times[-1] != build_t
    assert NOW not in open_times
    # A closed posting's grid still ends at its own first absence.
    assert _case_times(conn, "ds-clock", CLOSED_POSTING)[-1] == NOW - timedelta(days=30)

    record = store.get(
        conn,
        dataset_id="ds-clock",
        posting_id=OPEN_POSTING,
        replay_at=build_t,
        probe_name="resolve_posting",
        args_hash=hash_args(
            "resolve_posting", url=f"https://boards.greenhouse.io/acme/jobs/{OPEN_JOB}"
        ),
    )
    assert record is not None
    assert NOW < record.observed_at <= build_t
    claims = list(record.result.data["evidence"])
    assert claims, "the resolver produced no publish claim to check"
    for claim in claims:
        # Stamped when the fetch returned: after the build started, never
        # after the case that cites it, and invisible at the build's start.
        assert NOW < claim.available_at <= build_t

    # At its own T the case sees the live observation; at the build's start,
    # the replay gate would have dropped all of it.
    case = case_state_at(conn, cfg, OPEN_POSTING, build_t, "ds-clock")
    resolver = [e for e in case.evidence if e.probe == "resolve_posting"]
    assert resolver and all(e.available_at <= build_t for e in resolver)
    assert all(claim.available_at > NOW for claim in claims)

    # The build run's own (live) trace is dated the same way.
    live = conn.execute(
        """
        SELECT e.available_at FROM evidence AS e JOIN runs AS r ON r.id = e.run_id
        WHERE r.mode = 'live' AND r.config_hash LIKE '%|replay_build:ds-clock'
          AND e.probe IN ('resolve_posting', 'board_snapshot')
        """
    ).fetchall()
    assert live and all(parse_utc(row[0]) > NOW for row in live)

    result = run_replay(
        conn, cfg, dataset_id="ds-clock", system="A", collection_status_csv=NO_EVENTS_CSV
    )
    assert result.errors == 0, result.describe()
    report = check_dataset(conn, "ds-clock")
    assert report.clean, report.describe()


# ---------------------------------------------------------------------------
# The leakage checker catches the old (build-start) stamping
# ---------------------------------------------------------------------------


def _cache_row(
    conn: sqlite3.Connection, probe: str, url: str, params: dict, fetched_at: datetime
) -> None:
    conn.execute(
        "INSERT INTO tool_cache (probe, args_hash, fetched_at, response, expires_at) "
        "VALUES (?, ?, ?, '{}', ?)",
        (
            probe,
            hash_args(probe, url=url, params=params),
            to_utc_z(fetched_at),
            to_utc_z(fetched_at + timedelta(hours=1)),
        ),
    )
    conn.commit()


@respx.mock
def test_evidence_stamped_before_its_real_fetch_is_a_violation(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A frozen build clock reproduces the old stamping: every claim at T = build start.

    `tool_cache` holds the truth — the resolver's fetch really completed 30
    minutes later — so the case at the build start was shown the future.
    """
    seed_corpus(conn)
    mock_ats()
    _build(conn, cfg, "ds-old")  # clock frozen at NOW: the pre-fix stamping
    _cache_row(
        conn,
        "resolve_posting",
        f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs/{OPEN_JOB}",
        {"content": "true"},
        NOW + timedelta(minutes=30),
    )
    result = run_replay(
        conn, cfg, dataset_id="ds-old", system="A", collection_status_csv=NO_EVENTS_CSV
    )
    assert result.errors == 0, result.describe()

    report = check_dataset(conn, "ds-old")
    assert report.counts == {"evidence_fetched_after_t": 1}, report.describe()
    (violation,) = report.violations
    assert violation.replay_at == to_utc_z(NOW)
    assert "resolve_posting" in violation.detail
    assert to_utc_z(NOW + timedelta(minutes=30)) in violation.detail


@respx.mock
def test_evidence_a_cached_row_could_have_served_is_not_a_violation(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    mock_ats()
    _build(conn, cfg, "ds-cached")
    # Fetched 10 minutes BEFORE the stamp and still valid at it: the build
    # could have been served this row, so the data was held at the stamp.
    _cache_row(
        conn,
        "resolve_posting",
        f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs/{OPEN_JOB}",
        {"content": "true"},
        NOW - timedelta(minutes=10),
    )
    run_replay(conn, cfg, dataset_id="ds-cached", system="A", collection_status_csv=NO_EVENTS_CSV)
    report = check_dataset(conn, "ds-cached")
    assert report.clean, report.describe()


# ---------------------------------------------------------------------------
# Frozen splits
# ---------------------------------------------------------------------------


def _grow_corpus_so_the_greedy_split_moves_acme(conn: sqlite3.Connection) -> None:
    """Ten postings of a new, larger company, created after the build.

    The greedy company split slots the largest company first, so `big.com`
    takes `dev` and pushes `acme` (2 postings) out of it — the drift a
    recomputed split suffers as the collector adds postings.
    """
    for index in range(10):
        add_posting(
            conn,
            job_id=f"b{index}",
            company_id="big.com",
            tenant="big",
            created_at=NOW + timedelta(days=1),
            first_observed=NOW + timedelta(days=1),
        )


@respx.mock
def test_frozen_splits_read_back_unchanged_after_the_corpus_grows(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    mock_ats()
    _build(conn, cfg, "ds-company", split_kind="company", company_split_method="greedy")
    stored = {
        row["posting_id"]: row["split"]
        for row in conn.execute(
            "SELECT DISTINCT posting_id, split FROM replay_cases WHERE dataset_id = 'ds-company'"
        )
    }
    assert stored == {OPEN_POSTING: "dev", CLOSED_POSTING: "dev"}
    header = conn.execute(
        "SELECT split_method, split_seed FROM replay_datasets WHERE dataset_id = 'ds-company'"
    ).fetchone()
    assert header["split_method"] == "company-greedy"

    with warnings.catch_warnings():
        warnings.simplefilter("error", FrozenSplitWarning)
        before, kind = split_map_for_dataset(conn, dataset_id="ds-company")
    assert kind == "company"

    _grow_corpus_so_the_greedy_split_moves_acme(conn)
    recomputed = load_split_map(conn, cutoff=NOW, split_kind="company")
    assert recomputed[OPEN_POSTING] != "dev", "fixture failed to make the greedy split drift"

    with warnings.catch_warnings():
        warnings.simplefilter("error", FrozenSplitWarning)
        after, _ = split_map_for_dataset(conn, dataset_id="ds-company")
    assert {p: after[p] for p in stored} == stored == {p: before[p] for p in stored}


@respx.mock
def test_a_dataset_built_before_frozen_splits_is_reconstructed_as_of_its_build(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    mock_ats()
    _build(
        conn,
        cfg,
        "ds-legacy",
        split_kind="company",
        company_split_method="greedy",
        notes="legacy build",
    )
    # What a pre-version-5 dataset looks like: no stored assignment.
    conn.execute("UPDATE replay_cases SET split = NULL WHERE dataset_id = 'ds-legacy'")
    conn.execute(
        "UPDATE replay_datasets SET split_method = NULL, split_seed = NULL, "
        "split_cutoff = NULL WHERE dataset_id = 'ds-legacy'"
    )
    conn.commit()
    _grow_corpus_so_the_greedy_split_moves_acme(conn)

    with pytest.warns(FrozenSplitWarning, match="RECONSTRUCTED"):
        mapping, _ = split_map_for_dataset(conn, dataset_id="ds-legacy")
    assert mapping[OPEN_POSTING] == "dev"
    assert mapping[CLOSED_POSTING] == "dev"


def test_old_notes_cutoffs_are_used_to_reconstruct_a_temporal_split(
    conn: sqlite3.Connection,
) -> None:
    add_posting(
        conn, job_id="1", first_observed=datetime.fromisoformat("2026-09-01T00:00:00+00:00")
    )
    add_posting(
        conn, job_id="2", first_observed=datetime.fromisoformat("2026-09-10T00:00:00+00:00")
    )
    conn.execute(
        "INSERT INTO replay_datasets (dataset_id, created_at, split_kind, split_name, "
        "grid_step_days, postings, companies, cases, notes) VALUES ('old', "
        "'2026-09-29T00:00:00.000000Z', 'temporal', 'dev', 7, 2, 1, 2, "
        "'7-day grid, test cutoff 2026-09-15, validation cutoff 2026-09-07')"
    )
    conn.commit()
    with pytest.warns(FrozenSplitWarning, match="from its notes"):
        mapping, _ = split_map_for_dataset(conn, dataset_id="old")
    # Not `created_at` as the cutoff (which would call both `dev`).
    assert mapping["greenhouse:acme:1"] == "dev"
    assert mapping["greenhouse:acme:2"] == "validation"


# ---------------------------------------------------------------------------
# Company disjointness
# ---------------------------------------------------------------------------


def _seed_with_both_companies_in_dev() -> int:
    """A seed under which the stable hash puts both seeded companies in `dev`."""
    for seed in range(1, 500):
        rows = [
            SplitRow(posting_id=company, company_id=company, first_observed=NOW)
            for company in (COMPANY, OTHER_COMPANY)
        ]
        if set(company_split_stable(rows, seed=seed).values()) == {"dev"}:
            return seed
    raise AssertionError("no seed puts both companies in dev")  # pragma: no cover


@respx.mock
def test_a_company_split_build_excludes_another_datasets_companies(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    mock_ats()
    # The temporal dataset takes the largest company first: acme only.
    first = _build(conn, cfg, "ds-temporal", limit_postings=1)
    assert first.companies == 1
    temporal_companies = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT company_id FROM replay_cases WHERE dataset_id = 'ds-temporal'"
        )
    }
    assert temporal_companies == {COMPANY}

    summary = _build(
        conn,
        cfg,
        "ds-company-holdout",
        split_kind="company",
        seed=_seed_with_both_companies_in_dev(),
        exclude_companies_from=("ds-temporal",),
    )
    companies = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT company_id FROM replay_cases WHERE dataset_id = 'ds-company-holdout'"
        )
    }
    assert companies == {OTHER_COMPANY}
    assert companies.isdisjoint(temporal_companies)
    assert summary.companies_excluded == 1
    header = conn.execute(
        "SELECT split_method, exclude_companies_from FROM replay_datasets "
        "WHERE dataset_id = 'ds-company-holdout'"
    ).fetchone()
    assert header["split_method"] == "company-hash"
    assert header["exclude_companies_from"] == '["ds-temporal"]'


def test_excluding_a_dataset_with_no_cases_is_an_error(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    with pytest.raises(ValueError, match="no cases"):
        build_dataset(
            conn,
            cfg,
            dataset_id="ds-x",
            split="dev",
            split_kind="company",
            now=NOW,
            exclude_companies_from=("no-such-dataset",),
            use_tool_cache=False,
        )


@respx.mock
def test_a_company_split_build_never_takes_a_company_holding_a_test_posting(
    conn: sqlite3.Connection, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even from a map that splits a company (which a company split never does)."""
    seed_corpus(conn)
    mock_ats()
    mixed = {OPEN_POSTING: "dev", CLOSED_POSTING: "test", OTHER_POSTING: "dev"}
    monkeypatch.setattr(build_module, "load_split_map", lambda *_a, **_k: dict(mixed))
    _build(conn, cfg, "ds-guarded", split_kind="company")
    companies = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT company_id FROM replay_cases WHERE dataset_id = 'ds-guarded'"
        )
    }
    assert companies == {OTHER_COMPANY}
