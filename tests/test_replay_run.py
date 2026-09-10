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

import httpx
import pytest
import respx
from test_eval_helpers import COMPANY, add_capture, add_posting, job
from test_replay_helpers import (
    CLOSED_JOB,
    NOW,
    OPEN_JOB,
    OPEN_URL,
    TENANT,
    dev_everything_cutoff,
    mock_ats,
    seed_corpus,
)

from rli.config import Config
from rli.eval.runner import Run
from rli.eval.system_a import run_system_a
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
from rli.replay.run import run_replay

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
