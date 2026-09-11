"""`rli.replay.run` / `rli.replay.mode`: the four spec.md §6 replay rules.

Every test here builds a real dataset with respx-mocked HTTP and then
replays it with respx OFF, so "no live tool call" is not merely asserted —
any request that escaped would raise `httpx.ConnectError` against a host
that does not exist, and the replay-mode client raises before it can even
try.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from test_eval_helpers import (
    COMPANY,
    add_capture,
    add_company_event,
    add_posting,
    job,
    write_status_csv,
)
from test_replay_helpers import (
    CLOSED_JOB,
    CLOSED_URL,
    NOW,
    OPEN_JOB,
    OPEN_URL,
    TENANT,
    dev_everything_cutoff,
    mock_ats,
    seed_corpus,
)

from rli.config import Config
from rli.eval.runner import Run, RunResult
from rli.eval.system_a import run_system_a
from rli.eval.system_b import run_system_b
from rli.events.policy_signals import CLAIM_EVENTS_SEARCHED
from rli.models.decision import Decision
from rli.models.time import parse_utc, to_utc_z
from rli.policy.inputs import CLAIM_POSTING_STATE
from rli.probes.registry import DYNAMIC_PROBES
from rli.replay.build import archive_state_args_hash, build_dataset, case_state_at
from rli.replay.mode import (
    ARCHIVE_BOARD_STATE_PROBE,
    ReplayContext,
    ReplayNetPool,
    ReplayProbeStore,
    ReplayViolation,
    open_replay_probe_runner,
)
from rli.replay.run import dataset_status, run_replay

DATASET = "ds-run"
OPEN_POSTING = f"greenhouse:{TENANT}:{OPEN_JOB}"
CLOSED_POSTING = f"greenhouse:{TENANT}:{CLOSED_JOB}"
ARCHIVE_T = NOW - timedelta(days=60)


@respx.mock
def _build(conn: sqlite3.Connection, cfg: Config) -> None:
    seed_corpus(conn)
    mock_ats()
    summary = build_dataset(
        conn,
        cfg,
        dataset_id=DATASET,
        split="dev",
        split_kind="temporal",
        grid_step_days=30,
        now=NOW,
        cutoff=dev_everything_cutoff(),
        use_tool_cache=False,
        collection_status_csv="/nonexistent/collection_status.csv",
    )
    assert summary.postings_failed == 0, summary.failures


def _evidence(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM evidence WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()


# ---------------------------------------------------------------------------
# Rule 3: live tool calls are forbidden
# ---------------------------------------------------------------------------


def test_a_live_tool_call_from_replay_mode_raises_and_is_traced(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    with Run(
        conn,
        cfg,
        input_url=OPEN_URL,
        system="A",
        config_hash="cfg:test|dataset:x",
        started_at=NOW,
        mode="replay",
        replay_at=NOW,
    ) as run:
        with open_replay_probe_runner(
            conn,
            cfg,
            run,
            NOW,
            replay=ReplayContext(T=NOW, dataset_id="x"),
            store=ReplayProbeStore(),
            posting_id=OPEN_POSTING,
        ) as probes:
            client = probes.ctx.net_client("resolve_posting")
            with pytest.raises(ReplayViolation, match="spec.md §6"):
                client.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs")

    steps = conn.execute(
        "SELECT decision_type, probe_name, error FROM run_steps WHERE run_id = ?",
        (run.id,),
    ).fetchall()
    net_steps = [s for s in steps if s["decision_type"] == "replay_violation:net_call"]
    assert len(net_steps) == 1
    assert net_steps[0]["probe_name"] == "resolve_posting"
    assert "forbidden live tool call" in net_steps[0]["error"]


def test_the_replay_pool_serves_no_tool_cache_row_either(cfg: Config) -> None:
    """A cached HTTP body is still a tool result; serving one would break the
    same rule by a quieter route."""
    pool = ReplayNetPool(cfg=cfg)
    try:
        client = pool.client_for("board_snapshot")
        assert client._cache is None
    finally:
        pool.close()


def test_a_probe_result_missing_from_the_record_is_a_violation_not_a_failure(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    conn.execute(
        """
        DELETE FROM replay_probe_results
        WHERE dataset_id = ? AND posting_id = ? AND probe_name = 'resolve_posting'
        """,
        (DATASET, OPEN_POSTING),
    )
    conn.commit()

    summary = run_replay(conn, cfg, dataset_id=DATASET, system="A")

    assert summary.violations > 0
    failed = [o for o in summary.outcomes if o.posting_id == OPEN_POSTING]
    assert failed and all("ReplayViolation" in (o.error or "") for o in failed)
    # ... and the other posting's cases still ran.
    assert summary.completed > 0


# ---------------------------------------------------------------------------
# Rule 1: expose only evidence with available_at <= T
# ---------------------------------------------------------------------------


def test_every_stored_evidence_row_of_a_replay_run_predates_its_t(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    summary = run_replay(conn, cfg, dataset_id=DATASET, system="A")
    assert summary.errors == 0, summary.describe()

    rows = conn.execute(
        """
        SELECT e.available_at AS available_at, r.replay_at AS replay_at
        FROM evidence e JOIN runs r ON r.id = e.run_id
        WHERE r.mode = 'replay'
        """
    ).fetchall()
    assert rows
    for row in rows:
        assert parse_utc(row["available_at"]) <= parse_utc(row["replay_at"])


def test_an_archive_era_case_never_sees_the_live_resolver_evidence(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    case = case_state_at(conn, cfg, OPEN_POSTING, ARCHIVE_T, DATASET)

    probes = {item.probe for item in case.evidence}
    assert "resolve_posting" not in probes
    assert "board_snapshot" not in probes
    # The live observation was made at the build instant, which is after T.
    assert all(item.available_at <= ARCHIVE_T for item in case.evidence)


def test_an_archive_era_case_derives_posting_state_from_board_captures(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    case = case_state_at(conn, cfg, OPEN_POSTING, ARCHIVE_T, DATASET)

    state_claims = [i for i in case.evidence if i.claim_type == CLAIM_POSTING_STATE]
    assert len(state_claims) == 1
    assert state_claims[0].probe == ARCHIVE_BOARD_STATE_PROBE
    assert state_claims[0].value == "open"
    assert case.inputs.posting_state == "open"
    # No primary publish date can exist at an archive-era T (spec.md §3
    # forbids backdating the ATS's answer), so the evidence is `weak`.
    assert case.quality is not None
    assert case.quality.quality == "weak"


def test_a_case_at_the_build_instant_does_see_the_live_resolver_evidence(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    case = case_state_at(conn, cfg, OPEN_POSTING, NOW, DATASET)

    probes = {item.probe for item in case.evidence}
    assert "resolve_posting" in probes
    resolver_state = next(
        i
        for i in case.evidence
        if i.claim_type == CLAIM_POSTING_STATE and i.probe == "resolve_posting"
    )
    assert resolver_state.value == "open"
    assert case.inputs.posting_state == "open"
    assert case.inputs.publish_recency == "recent"


def test_an_archive_era_case_of_a_closed_posting_reads_closed(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    closed_t = NOW - timedelta(days=30)
    case = case_state_at(conn, cfg, CLOSED_POSTING, closed_t, DATASET)
    assert case.inputs.posting_state == "closed"


# ---------------------------------------------------------------------------
# Rule 2: expose a dynamic result only if the system selects that probe
# ---------------------------------------------------------------------------


def test_only_the_probes_a_system_selects_are_ever_read_from_the_record(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    store = ReplayProbeStore()

    # `case_state_at` without `include_dynamic` runs the always-run pair only.
    case_state_at(conn, cfg, OPEN_POSTING, NOW, DATASET, store=store)
    exposed = {name for name, _ in store.exposed}
    assert exposed == {ARCHIVE_BOARD_STATE_PROBE, "resolve_posting", "board_snapshot"}

    # The dataset holds MORE than that for this case — the extra records exist
    # and were simply never read, which is exactly spec.md §6's exposure rule.
    stored = {
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT probe_name FROM replay_probe_results
            WHERE dataset_id = ? AND posting_id = ? AND replay_at = ?
            """,
            (DATASET, OPEN_POSTING, to_utc_z(NOW)),
        )
    }
    assert stored - exposed


def test_system_b_exposes_a_strict_subset_of_system_a(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    a_summary = run_replay(conn, cfg, dataset_id=DATASET, system="A")
    b_summary = run_replay(conn, cfg, dataset_id=DATASET, system="B")
    assert a_summary.errors == 0 and b_summary.errors == 0

    a_exposed = {(o.posting_id, o.replay_at): set(o.exposed_probes) for o in a_summary.outcomes}
    for outcome in b_summary.outcomes:
        key = (outcome.posting_id, outcome.replay_at)
        assert set(outcome.exposed_probes) <= a_exposed[key]

    # B is the cheaper baseline: it must not run MORE dynamic probes than A.
    assert sum(b_summary.probe_counts.values()) <= sum(a_summary.probe_counts.values())


# ---------------------------------------------------------------------------
# The runs rows a replay produces
# ---------------------------------------------------------------------------


def test_a_replay_run_is_recorded_as_one(conn: sqlite3.Connection, cfg: Config) -> None:
    _build(conn, cfg)
    summary = run_replay(conn, cfg, dataset_id=DATASET, system="A")
    assert summary.completed == summary.cases

    rows = conn.execute("SELECT * FROM runs WHERE mode = 'replay'").fetchall()
    assert len(rows) == summary.cases
    for row in rows:
        assert row["system"] == "A"
        assert row["replay_at"] is not None
        assert str(row["config_hash"]).endswith(f"|dataset:{DATASET}")
        assert row["status"] == "completed"
        # Every trace timestamp is a function of T, not of when we replayed.
        assert row["started_at"] == row["replay_at"]


def test_no_replay_step_is_ever_recorded_as_a_cache_miss(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    run_replay(conn, cfg, dataset_id=DATASET, system="A")
    misses = conn.execute(
        """
        SELECT COUNT(*) FROM run_steps s JOIN runs r ON r.id = s.run_id
        WHERE r.mode = 'replay' AND s.cache_status = 'miss'
        """
    ).fetchone()[0]
    assert misses == 0


def test_replaying_twice_replaces_rather_than_duplicates(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    first = run_replay(conn, cfg, dataset_id=DATASET, system="A")
    second = run_replay(conn, cfg, dataset_id=DATASET, system="A")

    assert second.replaced_runs == first.cases
    assert (
        conn.execute("SELECT COUNT(*) FROM runs WHERE mode = 'replay'").fetchone()[0]
        == second.cases
    )
    # The build's live runs are untouched.
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE mode = 'live'").fetchone()[0] > 0


def test_keeping_previous_runs_leaves_both_sets(conn: sqlite3.Connection, cfg: Config) -> None:
    _build(conn, cfg)
    first = run_replay(conn, cfg, dataset_id=DATASET, system="A")
    run_replay(conn, cfg, dataset_id=DATASET, system="A", replace=False)
    assert (
        conn.execute("SELECT COUNT(*) FROM runs WHERE mode = 'replay'").fetchone()[0]
        == first.cases * 2
    )


def test_a_pluggable_system_c_runner_replays_like_a_shipped_one(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """System C (PLAN.md M5) is replayed by handing in a callable."""
    _build(conn, cfg)
    from rli.eval.system_b import run_system_b

    seen: list[str] = []

    def system_c(conn_, cfg_, url, **kwargs):
        seen.append(url)
        result = run_system_b(conn_, cfg_, url, **kwargs)
        return result.model_copy(update={"system": "C"})

    summary = run_replay(conn, cfg, dataset_id=DATASET, system="C", runner=system_c, limit_cases=2)
    assert summary.cases == 2
    assert summary.errors == 0, summary.describe()
    assert len(seen) == 2


def test_replaying_an_unknown_dataset_is_a_lookup_error(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    with pytest.raises(LookupError, match="no cases"):
        run_replay(conn, cfg, dataset_id="nope", system="A")


def test_replaying_a_system_with_no_runner_is_a_value_error(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    with pytest.raises(ValueError, match="no runner"):
        run_replay(conn, cfg, dataset_id=DATASET, system="C")


def test_an_archive_only_posting_resolves_to_its_collected_row(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`rli.history.closures` keys archive-only postings `archive:{company}:{job}`.

    No resolver can rebuild that id from a job URL, so `rli.eval.case`'s
    identity chain has to reach it through the corpus's own correlation key
    `(company_id, ats_job_id)`. Without that step the posting looks
    uncollected, gets no history features, and both history probes are
    ineligible — which makes System A and System B identical on every
    archive-derived posting in the corpus.
    """
    archive_id = f"archive:{COMPANY}:6100"
    url = f"https://boards.greenhouse.io/{TENANT}/jobs/6100"
    # A posting the collector only ever saw in an archive board capture:
    # no ATS tenant, and an id no URL can reproduce.
    add_posting(
        conn,
        job_id="6100",
        tenant=None,
        posting_id=archive_id,
        canonical_url=url,
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=10),
    )
    # Another posting on the same board, so the tenant resolves to a company.
    seed_corpus(conn)
    for offset in (60, 45, 30, 10):
        add_capture(conn, NOW - timedelta(days=offset), [job("6100")], company_id=COMPANY)

    with respx.mock:
        mock_ats()
        respx.get(f"https://boards-api.greenhouse.io/v1/boards/{TENANT}/jobs/6100").mock(
            return_value=httpx.Response(404, json={"error": "gone"})
        )
        respx.get(url).mock(return_value=httpx.Response(200, text="<html></html>"))
        result = run_system_a(conn, cfg, url, now=NOW, sleep=lambda _s: None, use_tool_cache=False)

    run_row = conn.execute("SELECT posting_id FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run_row["posting_id"] == archive_id
    # ... and the history probes are now eligible, which is the point.
    assert set(result.probes_run) & set(DYNAMIC_PROBES) >= {"repost_history"}


def test_archive_state_record_is_loaded_under_the_agreed_args_hash(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The writer (`build`) and the reader (`run`) must agree on the key."""
    _build(conn, cfg)
    store = ReplayProbeStore()
    claims = store.claims(
        conn,
        dataset_id=DATASET,
        posting_id=OPEN_POSTING,
        replay_at=ARCHIVE_T,
        probe_name=ARCHIVE_BOARD_STATE_PROBE,
        args_hash=archive_state_args_hash(OPEN_POSTING, ARCHIVE_T),
    )
    assert [c.claim_type for c in claims] == [CLAIM_POSTING_STATE, "board_present"]


# ---------------------------------------------------------------------------
# `company_events` in replay: the probe must contribute EVIDENCE, not just data
# ---------------------------------------------------------------------------

EVENTS_DATASET = "ds-run-events"


@respx.mock
def _build_with_events(conn: sqlite3.Connection, cfg: Config, status_csv: str) -> None:
    """A dataset whose company HAS a collected, replay-visible layoff.

    Both the event's `available_at` and the collection `searched_at` are set
    70 days back — before the earliest grid point — so every case in the
    dataset sees them and the assertion below is not about one lucky `T`.
    """
    seed_corpus(conn)
    add_company_event(
        conn,
        event_date=(NOW - timedelta(days=70)).date(),
        available_at=NOW - timedelta(days=70),
    )
    mock_ats()
    summary = build_dataset(
        conn,
        cfg,
        dataset_id=EVENTS_DATASET,
        split="dev",
        split_kind="temporal",
        grid_step_days=30,
        now=NOW,
        cutoff=dev_everything_cutoff(),
        use_tool_cache=False,
        collection_status_csv=status_csv,
    )
    assert summary.postings_failed == 0, summary.failures


def test_company_events_produces_evidence_in_a_replay_run(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """The probe's claims must reach the `evidence` table in replay.

    This is the replay half of the bug the events pipeline was rebuilt for.
    `company_events` used to "produce zero evidence in every run": the three
    policy inputs it answers were pre-populated by `rli.eval.case` from the
    same local store, so nothing downstream read the probe's claims and
    nobody noticed they were doing no work. Now the policy reads ONLY the
    claims, so zero evidence would mean zero answers — and this asserts the
    claims survive the whole build/replay round trip, typed codec included.

    The same `collection_status.csv` is pinned at BUILD and at REPLAY. It has
    to be: the build records what the probe found at `T` under an `args_hash`
    that deliberately excludes the path (`rli.probes.base.ProbeContext`), so
    pinning two different files would silently compare two different worlds.
    """
    status_csv = write_status_csv(tmp_path / "collection_status.csv", NOW - timedelta(days=70))
    _build_with_events(conn, cfg, status_csv)

    summary = run_replay(
        conn,
        cfg,
        dataset_id=EVENTS_DATASET,
        system="A",
        collection_status_csv=status_csv,
    )
    assert summary.errors == 0
    assert summary.violations == 0
    assert summary.probe_counts.get("company_events")

    rows = conn.execute(
        """
        SELECT e.claim_type, COUNT(*) AS n
        FROM evidence e JOIN runs r ON r.id = e.run_id
        WHERE r.mode = 'replay' AND r.config_hash LIKE ? AND e.probe = 'company_events'
        GROUP BY e.claim_type
        """,
        (f"%|dataset:{EVENTS_DATASET}",),
    ).fetchall()
    by_type = {row["claim_type"]: row["n"] for row in rows}

    # Both halves of the contract: the dated event, and the collection-status
    # claim that makes a "checked, none found" answer citable at all.
    assert by_type.get("layoff")
    assert by_type.get(CLAIM_EVENTS_SEARCHED)


# ---------------------------------------------------------------------------
# `resume` / `stop_on_quota` — System C's free-tier daily-quota reality
# (PLAN.md M5): a replay that must survive being cut off mid-quota and
# resumed on a later day without redoing (or losing) completed work.
# ---------------------------------------------------------------------------

# The exact shape `rli.llm.client`'s daily-quota `LLMTransportError` message
# takes, truncated the way `rli.agent.loop`/`rli.agent.explanation` persist
# it into `run_steps.error` (module docstring, "error text shape").
QUOTA_ERROR_TEXT = (
    "LLMTransportError: LLM endpoint returned HTTP 429 after 4 attempt(s) for "
    "model 'x': Quota exceeded for metric: generate_content_free_tier_requests, "
    "limit: 500 per day"
)


def _run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def _replay_run_ids(conn: sqlite3.Connection, *, system: str = "A") -> set[str]:
    return {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM runs WHERE mode = 'replay' AND system = ?", (system,)
        ).fetchall()
    }


def _plant_quota_error(
    conn: sqlite3.Connection, run_id: str, *, decision_type: str = "explanation"
) -> None:
    """Add a `run_steps` row on an existing run carrying a daily-quota error.

    `decision_type` defaults to `'explanation'` but is deliberately
    parameterizable: `_run_quota_detail` must not care which step carried the
    error (module docstring — it scans every `run_steps.error`, not one
    labeled row).
    """
    conn.execute(
        """
        INSERT INTO run_steps
            (run_id, step_index, component, decision_type, error, created_at)
        VALUES (?, 999, 'model', ?, ?, ?)
        """,
        (run_id, decision_type, QUOTA_ERROR_TEXT, to_utc_z(NOW)),
    )
    conn.commit()


def _make_quota_runner(target_call: int, *, decision_type: str = "explanation"):
    """A scripted System-C-shaped runner.

    On its `target_call`-th invocation it opens a REAL `Run` (via
    `rli.eval.runner.Run`, exactly as System C would), writes one
    `run_steps` row carrying the daily-quota error text, finishes the run
    normally with a policy-only decision, and returns a `RunResult` —
    mirroring `run_system_c`'s own contract of never raising for an LLM
    failure (module docstring). Every other invocation delegates to the real
    System B so the surrounding cases in the grid behave like an ordinary
    replay.
    """
    calls = {"n": 0}

    def runner(
        conn_: sqlite3.Connection,
        cfg_: Config,
        url: str,
        *,
        now=None,
        replay=None,
        collection_status_csv=None,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] != target_call:
            return run_system_b(
                conn_,
                cfg_,
                url,
                now=now,
                replay=replay,
                collection_status_csv=collection_status_csv,
            )
        run = Run(
            conn_,
            cfg_,
            input_url=url,
            system="C",
            config_hash="cfg:test" + replay.config_hash_suffix(),
            started_at=now,
            mode="replay",
            replay_at=now,
        )
        run.open()
        run.step(
            component="model",
            decision_type=decision_type,
            error=QUOTA_ERROR_TEXT,
            created_at=now,
        )
        decision = Decision(
            posting_state="open", recommended_action="apply_now", evidence_quality="strong"
        )
        run.finish(decision)
        return RunResult(run_id=run.id, system="C", decision=decision)

    return runner, calls


def test_resume_skips_cases_with_a_prior_completed_non_quota_run(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    first = run_replay(conn, cfg, dataset_id=DATASET, system="A", limit_cases=3)
    assert first.cases == 3
    kept_ids = _replay_run_ids(conn)
    assert len(kept_ids) == 3

    second = run_replay(conn, cfg, dataset_id=DATASET, system="A", resume=True)
    assert second.skipped == 3
    assert second.cases == 8  # the whole grid was visited this call
    assert second.completed == 5  # 8 total cases minus the 3 skipped
    assert second.errors == 0

    after_ids = _replay_run_ids(conn)
    assert len(after_ids) == 8
    # The 3 kept runs are untouched: same ids, still present, not duplicated.
    assert kept_ids <= after_ids


def test_resume_redoes_a_failed_or_quota_affected_case(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    first = run_replay(conn, cfg, dataset_id=DATASET, system="A", limit_cases=2)
    assert first.cases == 2
    # The first two grid positions (both at T1): OPEN_URL and CLOSED_URL.
    failed_case = conn.execute(
        "SELECT id, input_url, replay_at FROM runs WHERE mode = 'replay' AND input_url = ?",
        (OPEN_URL,),
    ).fetchone()
    quota_case = conn.execute(
        "SELECT id, input_url, replay_at FROM runs WHERE mode = 'replay' AND input_url = ?",
        (CLOSED_URL,),
    ).fetchone()
    assert failed_case is not None
    assert quota_case is not None

    conn.execute("UPDATE runs SET status = 'failed' WHERE id = ?", (failed_case["id"],))
    conn.commit()
    _plant_quota_error(conn, quota_case["id"])
    # Sanity: the quota run is otherwise a normal completed run.
    assert _run_row(conn, quota_case["id"])["status"] == "completed"

    second = run_replay(conn, cfg, dataset_id=DATASET, system="A", resume=True)
    assert second.skipped == 0
    assert second.cases == 8
    assert second.completed == 8
    assert second.errors == 0

    # Both stale rows are gone...
    remaining_ids = _replay_run_ids(conn)
    assert failed_case["id"] not in remaining_ids
    assert quota_case["id"] not in remaining_ids
    # ...and fresh, completed runs exist for the same two cases.
    for stale in (failed_case, quota_case):
        fresh = conn.execute(
            "SELECT status FROM runs WHERE mode = 'replay' AND input_url = ? AND replay_at = ?",
            (stale["input_url"], stale["replay_at"]),
        ).fetchone()
        assert fresh is not None
        assert fresh["status"] == "completed"
    assert len(remaining_ids) == 8


def test_stop_on_quota_deletes_the_offending_run_and_returns_immediately(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    runner, calls = _make_quota_runner(target_call=1)

    summary = run_replay(
        conn, cfg, dataset_id=DATASET, system="C", runner=runner, stop_on_quota=True
    )

    assert calls["n"] == 1  # stopped before any later case was even attempted
    assert summary.cases == 1
    assert summary.completed == 0
    assert summary.stopped_reason == "quota_exhausted"
    assert summary.quota_detail is not None
    assert "per day" in summary.quota_detail
    assert summary.resume_after is not None
    reset_at = parse_utc(summary.resume_after)
    assert (reset_at.hour, reset_at.minute, reset_at.second) == (8, 0, 0)

    # The `CaseOutcome` for the stopped case is still reported, even though
    # its run no longer exists in the DB (a snapshot, not a live reference).
    assert len(summary.outcomes) == 1
    deleted_run_id = summary.outcomes[0].run_id
    assert deleted_run_id is not None

    # The offending case's run/run_steps/evidence are gone entirely, and no
    # other System C run exists (the walk stopped before reaching case 2).
    assert _replay_run_ids(conn, system="C") == set()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM run_steps WHERE run_id = ?", (deleted_run_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE run_id = ?", (deleted_run_id,)
        ).fetchone()[0]
        == 0
    )


def test_stop_on_quota_treats_an_investigator_labeled_error_the_same(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`_run_quota_detail` scans every `run_steps.error`, not one labeled row."""
    _build(conn, cfg)
    runner, calls = _make_quota_runner(target_call=1, decision_type="investigator")

    summary = run_replay(
        conn, cfg, dataset_id=DATASET, system="C", runner=runner, stop_on_quota=True
    )

    assert calls["n"] == 1
    assert summary.stopped_reason == "quota_exhausted"
    assert summary.quota_detail is not None
    assert _replay_run_ids(conn, system="C") == set()


def test_summary_describe_reports_skipped_and_a_quota_stop_without_breaking_old_assertions(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The existing substring assertions other tests rely on must still hold."""
    _build(conn, cfg)
    run_replay(conn, cfg, dataset_id=DATASET, system="A", limit_cases=2)
    resumed = run_replay(conn, cfg, dataset_id=DATASET, system="A", resume=True)
    text = resumed.describe()
    assert f"replay {resumed.system} over dataset" in text
    assert "violations=0" in text
    assert "skipped=2" in text

    runner, _ = _make_quota_runner(target_call=1)
    stopped = run_replay(
        conn, cfg, dataset_id=DATASET, system="C", runner=runner, stop_on_quota=True
    )
    stopped_text = stopped.describe()
    assert f"replay {stopped.system} over dataset" in stopped_text
    assert "STOPPED: quota_exhausted" in stopped_text
    assert stopped.quota_detail in stopped_text


def test_dataset_status_reports_per_system_completion(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    before = dataset_status(conn, dataset_id=DATASET)
    assert before.total_cases == 8
    assert {s.system for s in before.by_system} == {"A", "B", "C", "C2"}
    assert all(s.completed == 0 and s.remaining == s.total for s in before.by_system)

    run_replay(conn, cfg, dataset_id=DATASET, system="A", limit_cases=3)
    after = dataset_status(conn, dataset_id=DATASET)
    by_name = {s.system: s for s in after.by_system}
    assert by_name["A"].completed == 3
    assert by_name["A"].remaining == 5
    assert by_name["B"].completed == 0
    assert by_name["B"].remaining == 8


def test_dataset_status_of_an_unbuilt_dataset_is_a_lookup_error(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    with pytest.raises(LookupError, match="no cases"):
        dataset_status(conn, dataset_id="nope")
