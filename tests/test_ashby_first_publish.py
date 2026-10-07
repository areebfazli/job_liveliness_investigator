"""Ashby "last published" as first-publish evidence (spec.md §3 amendment 2026-10-07).

An Ashby `publishedAt` value `v` counts as the first publication only when the
last complete OWN capture before the posting's first sighting did not list the
job (`t_absent`), `t_absent < v <= first_observed` (pre-fix run-window slack
as for the first-published guard), and `first_observed - v <=
[thresholds].ashby_first_seen_max_lag_days`. Covered here: the rule itself
(`rli.eval.ashby_first_publish`), the replay builder (`capture_date_claims`),
the live case builder, Q2 / recency, and live == replay.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import respx
from test_eval_helpers import COMPANY, add_capture, add_posting, job

from rli.config import Config
from rli.eval.ashby_first_publish import ASHBY_FIRST_PUBLISH_PROBE, ashby_first_publish_claim
from rli.eval.metrics import data_quality, split_map_for_dataset
from rli.eval.system_a import run_system_a
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import PolicyInputs
from rli.models.time import parse_utc, to_utc_z
from rli.policy.action import PolicyThresholds
from rli.policy.inputs import (
    CLAIM_FIRST_PUBLISHED,
    CLAIM_LAST_PUBLISHED,
    best_publish_claim,
    derive_policy_inputs,
)
from rli.policy.quality import evidence_quality_detail
from rli.probes.board_snapshot import BoardJob
from rli.probes.persist import record_capture_attempt
from rli.replay.build import build_dataset, capture_date_claims, case_state_at
from rli.replay.leakage import check_dataset
from rli.replay.mode import ARCHIVE_BOARD_STATE_PROBE
from rli.replay.run import run_replay
from rli.snapshots.run_windows import RunWindow

JOB = "b6a6d1c0-1234-4abc-8def-0123456789ab"
URL = f"https://jobs.ashbyhq.com/acme/{JOB}"
POSTING = f"ashby:acme:{JOB}"
# First own sighting of the job.
SEEN = datetime(2026, 9, 20, 10, tzinfo=UTC)
# The stated `publishedAt`: 12 hours before the first sighting.
V = SEEN - timedelta(hours=12)


def _ashby(job_id: str = JOB, last_published: datetime | str | None = V) -> BoardJob:
    if isinstance(last_published, datetime):
        last_published = to_utc_z(last_published)
    return BoardJob(
        job_id=job_id,
        title="Designer",
        team="Design",
        location="Remote",
        url=f"https://jobs.ashbyhq.com/acme/{job_id}",
        last_published=last_published,
    )


def _posting(
    conn: sqlite3.Connection, *, ats: str = "ashby", first_observed: datetime = SEEN
) -> str:
    return add_posting(
        conn,
        job_id=JOB,
        ats=ats,
        tenant="acme",
        canonical_url=URL,
        first_observed=first_observed,
        last_seen_open=first_observed,
    )


def _other() -> BoardJob:
    return job("zz-other")


def _claim(
    conn: sqlite3.Connection,
    as_of: datetime,
    *,
    ats: str = "ashby",
    first_observed: datetime | None = SEEN,
    max_lag_days: int = 2,
    windows: tuple[RunWindow, ...] = (),
):
    return ashby_first_publish_claim(
        conn,
        ats=ats,
        company_id=COMPANY,
        job_id=JOB,
        first_observed=first_observed,
        as_of=as_of,
        max_lag_days=max_lag_days,
        windows=windows,
    )


def _standard(conn: sqlite3.Connection) -> str:
    """Absent at SEEN-1d, first seen at SEEN carrying V, still listed at SEEN+3d."""
    posting = _posting(conn)
    add_capture(conn, SEEN - timedelta(days=1), [_other()])
    add_capture(conn, SEEN, [_other(), _ashby()])
    add_capture(conn, SEEN + timedelta(days=3), [_other(), _ashby()])
    return posting


def _as_evidence(claims, probe: str = ARCHIVE_BOARD_STATE_PROBE) -> list[EvidenceItem]:
    return [
        EvidenceItem(id=f"e{i}", run_id="r", probe=probe, **c.model_dump())
        for i, c in enumerate(claims, start=1)
    ]


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_fires_when_absent_earlier_and_first_seen_within_the_lag(
    conn: sqlite3.Connection,
) -> None:
    _standard(conn)
    claim = _claim(conn, SEEN + timedelta(days=5))
    assert claim is not None
    assert claim.claim_type == CLAIM_FIRST_PUBLISHED
    assert claim.source_quality == "ats_native"
    assert claim.source_event_at == V
    assert claim.value == to_utc_z(V)
    # The EARLIEST own capture that carried the value, never earlier.
    assert claim.available_at == SEEN
    assert claim.fetched_at == SEEN
    assert claim.source_url == URL
    excerpt = claim.raw_excerpt or ""
    assert to_utc_z(SEEN - timedelta(days=1)) in excerpt  # absent at
    assert to_utc_z(SEEN) in excerpt  # first seen
    assert to_utc_z(V) in excerpt  # publishedAt
    assert "last published" in excerpt


def test_nothing_before_the_first_sighting_or_the_first_carrying_capture(
    conn: sqlite3.Connection,
) -> None:
    _standard(conn)
    assert _claim(conn, SEEN - timedelta(seconds=1)) is None
    # Through the replay builder too: at T before the first sighting, nothing.
    assert capture_date_claims(conn, POSTING, SEEN - timedelta(seconds=1)) == []


def test_no_earlier_own_capture_means_no_claim(conn: sqlite3.Connection) -> None:
    _posting(conn)
    add_capture(conn, SEEN, [_ashby()])
    assert _claim(conn, SEEN + timedelta(days=1)) is None


def test_an_earlier_capture_that_listed_the_job_means_no_claim(
    conn: sqlite3.Connection,
) -> None:
    # Inconsistent with `first_observed`, so the rule refuses rather than guesses.
    _posting(conn)
    add_capture(conn, SEEN - timedelta(days=1), [_other(), _ashby(last_published=None)])
    add_capture(conn, SEEN, [_other(), _ashby()])
    assert _claim(conn, SEEN + timedelta(days=1)) is None


def test_a_failed_earlier_capture_is_not_an_absence(conn: sqlite3.Connection) -> None:
    _posting(conn)
    record_capture_attempt(
        conn,
        company_id=COMPANY,
        target="ashby:acme",
        attempted_at=SEEN - timedelta(days=1),
        source="own",
        ok=False,
        error="HTTP 503",
        retryable=True,
    )
    conn.commit()
    add_capture(conn, SEEN, [_other(), _ashby()])
    assert _claim(conn, SEEN + timedelta(days=1)) is None


def test_a_partial_earlier_capture_is_not_an_absence(conn: sqlite3.Connection) -> None:
    _posting(conn)
    add_capture(conn, SEEN - timedelta(days=1), [_other()], coverage_status="partial")
    add_capture(conn, SEEN, [_other(), _ashby()])
    assert _claim(conn, SEEN + timedelta(days=1)) is None


def test_a_failed_attempt_after_a_complete_absence_does_not_hide_it(
    conn: sqlite3.Connection,
) -> None:
    _posting(conn)
    add_capture(conn, SEEN - timedelta(days=1, hours=6), [_other()])
    record_capture_attempt(
        conn,
        company_id=COMPANY,
        target="ashby:acme",
        attempted_at=SEEN - timedelta(hours=18),
        source="own",
        ok=False,
        error="timeout",
        retryable=True,
    )
    conn.commit()
    add_capture(conn, SEEN, [_other(), _ashby()])
    claim = _claim(conn, SEEN + timedelta(days=1))
    assert claim is not None and claim.source_event_at == V


def test_archive_only_history_means_no_claim(conn: sqlite3.Connection) -> None:
    _posting(conn)
    add_capture(conn, SEEN - timedelta(days=1), [_other()], source="archive")
    add_capture(conn, SEEN, [_other(), _ashby()])
    assert _claim(conn, SEEN + timedelta(days=1)) is None


def test_lag_over_the_threshold_means_no_claim(conn: sqlite3.Connection) -> None:
    _posting(conn)
    add_capture(conn, SEEN - timedelta(days=5), [_other()])
    add_capture(conn, SEEN, [_other(), _ashby(last_published=SEEN - timedelta(days=2, hours=1))])
    assert _claim(conn, SEEN + timedelta(days=1)) is None
    # The threshold is configuration: a wider one accepts the same history.
    assert _claim(conn, SEEN + timedelta(days=1), max_lag_days=3) is not None


def test_lag_exactly_at_the_threshold_counts(conn: sqlite3.Connection) -> None:
    _posting(conn)
    add_capture(conn, SEEN - timedelta(days=5), [_other()])
    add_capture(conn, SEEN, [_other(), _ashby(last_published=SEEN - timedelta(days=2))])
    claim = _claim(conn, SEEN + timedelta(days=1))
    assert claim is not None and claim.source_event_at == SEEN - timedelta(days=2)


def test_a_value_before_the_absence_means_no_claim(conn: sqlite3.Connection) -> None:
    _posting(conn)
    absent_at = SEEN - timedelta(hours=6)
    add_capture(conn, absent_at, [_other()])
    add_capture(conn, SEEN, [_other(), _ashby(last_published=SEEN - timedelta(hours=12))])
    assert _claim(conn, SEEN + timedelta(days=1)) is None


def test_a_value_equal_to_the_absence_means_no_claim(conn: sqlite3.Connection) -> None:
    _posting(conn)
    absent_at = SEEN - timedelta(hours=6)
    add_capture(conn, absent_at, [_other()])
    add_capture(conn, SEEN, [_other(), _ashby(last_published=absent_at)])
    assert _claim(conn, SEEN + timedelta(days=1)) is None


def test_a_value_after_the_first_sighting_means_no_claim(conn: sqlite3.Connection) -> None:
    _posting(conn)
    add_capture(conn, SEEN - timedelta(days=1), [_other()])
    add_capture(conn, SEEN, [_other(), _ashby(last_published=SEEN + timedelta(hours=1))])
    assert _claim(conn, SEEN + timedelta(days=1)) is None
    # Equal to the first sighting is consistent with it.
    conn.execute("UPDATE board_snapshot_jobs SET last_published = ?", (to_utc_z(SEEN),))
    conn.commit()
    claim = _claim(conn, SEEN + timedelta(days=1))
    assert claim is not None and claim.source_event_at == SEEN


def test_run_window_slack_on_the_first_sighting(conn: sqlite3.Connection) -> None:
    """A first sighting stamped inside a pre-fix run window may really be as
    late as the window's end (`guard_bound`); beyond it, no claim."""
    _posting(conn)
    add_capture(conn, SEEN - timedelta(days=1), [_other()])
    add_capture(conn, SEEN, [_other(), _ashby(last_published=SEEN + timedelta(hours=1))])
    window = (RunWindow(SEEN, SEEN + timedelta(hours=3)),)
    claim = _claim(conn, SEEN + timedelta(days=1), windows=window)
    assert claim is not None and claim.source_event_at == SEEN + timedelta(hours=1)

    conn.execute(
        "UPDATE board_snapshot_jobs SET last_published = ?",
        (to_utc_z(SEEN + timedelta(hours=4)),),
    )
    conn.commit()
    assert _claim(conn, SEEN + timedelta(days=1), windows=window) is None


def test_an_absence_stamped_at_a_pre_fix_run_start_is_held_at_the_window_end(
    conn: sqlite3.Connection,
) -> None:
    """The absence capture may really have been fetched up to its window's end,
    so a value inside that window is not provably after the absence."""
    _posting(conn)
    absent_at = SEEN - timedelta(days=1)
    add_capture(conn, absent_at, [_other()])
    add_capture(conn, SEEN, [_other(), _ashby(last_published=SEEN - timedelta(hours=20))])
    assert _claim(conn, SEEN + timedelta(days=1)) is not None
    window = (RunWindow(absent_at, absent_at + timedelta(hours=6)),)
    assert _claim(conn, SEEN + timedelta(days=1), windows=window) is None


def test_greenhouse_and_lever_never_qualify(conn: sqlite3.Connection) -> None:
    _standard(conn)
    for ats in ("greenhouse", "lever"):
        assert _claim(conn, SEEN + timedelta(days=5), ats=ats) is None


def test_a_greenhouse_posting_gets_no_inferred_claim_from_the_builder(
    conn: sqlite3.Connection,
) -> None:
    posting = _posting(conn, ats="greenhouse")
    add_capture(conn, SEEN - timedelta(days=1), [_other()])
    add_capture(conn, SEEN, [_other(), _ashby()])
    claims = capture_date_claims(conn, posting, SEEN + timedelta(days=1))
    assert [c.claim_type for c in claims] == [CLAIM_LAST_PUBLISHED]


def test_available_from_the_first_capture_that_carried_the_value(
    conn: sqlite3.Connection,
) -> None:
    """A first sighting from before dates were stored carries no value; the
    claim is available only from the first capture that did."""
    _posting(conn)
    add_capture(conn, SEEN - timedelta(days=1), [_other()])
    add_capture(conn, SEEN, [_other(), _ashby(last_published=None)])
    add_capture(conn, SEEN + timedelta(days=2), [_other(), _ashby()])
    add_capture(conn, SEEN + timedelta(days=4), [_other(), _ashby()])
    assert _claim(conn, SEEN + timedelta(days=1)) is None
    claim = _claim(conn, SEEN + timedelta(days=5))
    assert claim is not None
    assert claim.available_at == SEEN + timedelta(days=2)


def test_a_later_republish_changes_nothing_at_an_earlier_t(conn: sqlite3.Connection) -> None:
    _standard(conn)
    t = SEEN + timedelta(days=5)
    before = _claim(conn, t)
    before_builder = capture_date_claims(conn, POSTING, t)
    assert before is not None

    # Re-published at SEEN+9d; our capture at SEEN+10d carries the new value.
    republished = SEEN + timedelta(days=9)
    add_capture(conn, SEEN + timedelta(days=10), [_other(), _ashby(last_published=republished)])

    assert _claim(conn, t) == before
    assert capture_date_claims(conn, POSTING, t) == before_builder
    # And later: the early value still proves the first publication; the new
    # value is only a `last_published` (refresh candidate).
    later = capture_date_claims(conn, POSTING, SEEN + timedelta(days=11))
    by_type = {c.claim_type: c for c in later}
    assert by_type[CLAIM_FIRST_PUBLISHED] == before
    assert by_type[CLAIM_LAST_PUBLISHED].source_event_at == republished


def test_the_builder_emits_both_last_and_first_published(conn: sqlite3.Connection) -> None:
    _standard(conn)
    claims = capture_date_claims(conn, POSTING, SEEN + timedelta(days=5))
    by_type = {c.claim_type: c for c in claims}
    assert set(by_type) == {CLAIM_LAST_PUBLISHED, CLAIM_FIRST_PUBLISHED}
    assert by_type[CLAIM_FIRST_PUBLISHED].source_event_at == V
    assert by_type[CLAIM_FIRST_PUBLISHED].available_at == SEEN


def test_q2_turns_strong_and_recency_reads_the_inferred_date(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _standard(conn)
    state = EvidenceItem(
        id="s1",
        run_id="r",
        probe=ARCHIVE_BOARD_STATE_PROBE,
        claim_type="posting_state",
        value="open",
        source_url=URL,
        source_quality="ats_native",
        available_at=SEEN,
        fetched_at=SEEN,
    )
    thresholds = PolicyThresholds.coerce(cfg)

    t = SEEN + timedelta(days=5)
    evidence = [state, *_as_evidence(capture_date_claims(conn, POSTING, t, cfg=cfg))]
    verdict = evidence_quality_detail(evidence, PolicyInputs(posting_state="open"), (), cfg)
    assert verdict.quality == "strong", verdict.detail
    assert best_publish_claim(evidence).source_event_at == V  # type: ignore[union-attr]
    inputs = derive_policy_inputs(evidence, None, t, cfg=cfg)
    assert inputs.publish_recency == "recent"

    late = SEEN + timedelta(days=thresholds.recent_publish_days + 1)
    evidence = [state, *_as_evidence(capture_date_claims(conn, POSTING, late, cfg=cfg))]
    assert derive_policy_inputs(evidence, None, late, cfg=cfg).publish_recency == "not_recent"

    # Without the rule (a lag the history cannot meet) Q2 stays weak.
    strict = cfg.model_copy(
        update={
            "thresholds": cfg.thresholds.model_copy(update={"ashby_first_seen_max_lag_days": 0})
        }
    )
    evidence = [state, *_as_evidence(capture_date_claims(conn, POSTING, t, cfg=strict))]
    verdict = evidence_quality_detail(evidence, PolicyInputs(posting_state="open"), (), strict)
    assert verdict.rule == "no_primary_publish_evidence"


def test_the_threshold_is_part_of_the_policy_fingerprint(cfg: Config) -> None:
    thresholds = PolicyThresholds.coerce(cfg)
    assert thresholds.ashby_first_seen_max_lag_days == 2
    assert "ashby_first_seen_max_lag_days=2" in thresholds.fingerprint()


# ---------------------------------------------------------------------------
# Live path, and live == replay
# ---------------------------------------------------------------------------

LIVE_NOW = SEEN + timedelta(days=5)


def _mock_ashby(published: datetime = V) -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": JOB,
                        "title": "Designer",
                        "publishedAt": published.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "jobUrl": URL,
                    }
                ]
            },
        )
    )
    respx.get(URL).mock(
        return_value=httpx.Response(200, text="<html><body>no structured data</body></html>")
    )


@respx.mock
def test_live_run_cites_the_same_claim_as_the_replay_builder(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _standard(conn)
    _mock_ashby()
    result = run_system_a(conn, cfg, URL, now=LIVE_NOW, sleep=lambda _s: None, use_tool_cache=False)

    rows = conn.execute(
        "SELECT * FROM evidence WHERE run_id = ? AND claim_type = ?",
        (result.run_id, CLAIM_FIRST_PUBLISHED),
    ).fetchall()
    assert len(rows) == 1
    (row,) = rows
    assert row["probe"] == ASHBY_FIRST_PUBLISH_PROBE
    (replayed,) = [
        c
        for c in capture_date_claims(conn, POSTING, LIVE_NOW, cfg=cfg)
        if c.claim_type == CLAIM_FIRST_PUBLISHED
    ]
    assert row["value"] == replayed.value
    assert row["source_quality"] == replayed.source_quality
    assert parse_utc(row["source_event_at"]) == replayed.source_event_at
    assert parse_utc(row["available_at"]) == replayed.available_at
    assert row["source_url"] == replayed.source_url
    assert result.decision.evidence_quality == "strong"


@respx.mock
def test_live_run_without_an_earlier_absence_stays_last_published_only(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _posting(conn)
    add_capture(conn, SEEN, [_other(), _ashby()])
    _mock_ashby()
    result = run_system_a(conn, cfg, URL, now=LIVE_NOW, sleep=lambda _s: None, use_tool_cache=False)
    types = {
        r["claim_type"]
        for r in conn.execute("SELECT claim_type FROM evidence WHERE run_id = ?", (result.run_id,))
    }
    assert CLAIM_FIRST_PUBLISHED not in types
    assert CLAIM_LAST_PUBLISHED in types
    assert result.decision.evidence_quality == "weak"


DATASET = "ds-ashby-first"


@respx.mock
def test_replay_case_state_equals_live_and_is_leakage_clean(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _standard(conn)
    _mock_ashby()
    summary = build_dataset(
        conn,
        cfg,
        dataset_id=DATASET,
        split="dev",
        split_kind="temporal",
        grid_step_days=2,
        now=LIVE_NOW,
        # Strictly after every first sighting: the posting is in `dev`.
        cutoff=LIVE_NOW + timedelta(days=1),
        use_tool_cache=False,
        collection_status_csv="/nonexistent/collection_status.csv",
    )
    assert summary.postings_failed == 0, summary.failures
    assert summary.cases_with_capture_publish_date >= 1

    # The live (build-time) System A run cited the claim under its own name.
    live = conn.execute(
        """
        SELECT e.* FROM evidence AS e JOIN runs AS r ON r.id = e.run_id
        WHERE r.mode = 'live' AND e.claim_type = ?
        """,
        (CLAIM_FIRST_PUBLISHED,),
    ).fetchall()
    assert [r["probe"] for r in live] == [ASHBY_FIRST_PUBLISH_PROBE]

    for t in (SEEN, SEEN + timedelta(days=2), LIVE_NOW):
        case = case_state_at(conn, cfg, POSTING, t, DATASET)
        published = [e for e in case.evidence if e.claim_type == CLAIM_FIRST_PUBLISHED]
        # Exactly once (the replay record's copy; not cited twice).
        assert [(e.probe, e.available_at, e.source_event_at) for e in published] == [
            (ARCHIVE_BOARD_STATE_PROBE, SEEN, V)
        ], t
        assert published[0].value == live[0]["value"]
        verdict = evidence_quality_detail(case.evidence, case.inputs, case.failures, cfg)
        assert verdict.quality == "strong", (t, verdict.detail)
        assert case.inputs.publish_recency == "recent"
        # Long-lived age is measured from the earliest origin: the inferred
        # publication, 12 hours before the first sighting.
        assert case.features is not None
        assert case.features.age_days == (t - V).total_seconds() / 86400.0

    for system in ("A", "B"):
        replayed = run_replay(conn, cfg, dataset_id=DATASET, system=system)
        assert replayed.errors == 0, replayed.describe()
    report = check_dataset(conn, DATASET)
    assert report.clean, report.describe()

    # The report's first-publish coverage counts it (every case is at or
    # after the first carrying capture).
    splits, _kind = split_map_for_dataset(conn, dataset_id=DATASET)
    dq = data_quality(conn, cfg, dataset_id=DATASET, splits=splits)
    assert dq.runs_checked >= 3
    assert dq.first_publish_runs == dq.runs_checked
    assert dq.first_publish_by_source_quality == {"ats_native": dq.runs_checked}
