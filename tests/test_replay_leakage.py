"""`rli.replay.leakage`: spec.md §6's "future-leakage violations (0 target)".

The clean case is a real end-to-end replay (build with respx, replay without
it). Each violation kind is then PLANTED into that same trace, one at a time,
so the checker is shown to catch a thing that is genuinely there rather than
to agree with itself.

`blob_input_exposures` is the one figure the fixture produces WITHOUT
planting: it reproduces security review H4's dataset hazard for real.
`team_signal`'s args carry no `as_of`, so one build-time record serves every
`T`; its claims are stamped at the build instant and the point-in-time gate
correctly drops them at an archive-era `T`; and its `data` blob goes on
carrying the boolean for the `corroborating_hiring_signal` policy input.
`_unexpose_team_signal` blanks that key the way the security review's own
control did, so the counter can be shown to go to zero on a dataset that no
longer carries the hazard.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import respx
from test_replay_helpers import (
    NOW,
    OPEN_JOB,
    OPEN_URL,
    TENANT,
    dev_everything_cutoff,
    mock_ats,
    seed_corpus,
)

from rli.config import Config
from rli.models.policy_inputs import PolicyInputs
from rli.models.time import to_utc_z
from rli.policy.action import PRECEDENCE
from rli.replay.build import build_dataset
from rli.replay.leakage import (
    _BRANCH_REQUIREMENTS,
    _INPUT_EXPOSURES,
    VIOLATION_KINDS,
    check_dataset,
    net_call_count,
)
from rli.replay.mode import ReplayNetPool, ReplayViolation
from rli.replay.run import run_replay

DATASET = "ds-leak"
OPEN_POSTING = f"greenhouse:{TENANT}:{OPEN_JOB}"
TEAM_SIGNAL_KEY = "corroborating_hiring_signal"


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


def _replay_both(conn: sqlite3.Connection, cfg: Config) -> None:
    for system in ("A", "B"):
        summary = run_replay(conn, cfg, dataset_id=DATASET, system=system)
        assert summary.errors == 0, summary.describe()


def _a_replay_run(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM runs WHERE mode = 'replay' ORDER BY replay_at, id LIMIT 1"
    ).fetchone()
    assert row is not None
    return row


def _unexpose_team_signal(conn: sqlite3.Connection) -> None:
    """Blank the `team_signal` blob's policy-input key in every stored record.

    `None` is the probe's OWN encoding of Unknown (`rli.probes.team_signal`
    leaves the key at `None` so that a data-blob reader "cannot read it as a
    negative either"), so this is a legal payload, not a mangled one. It is
    the same control the security review used to prove the blob was H4's
    source, and it is what makes "the rest of this fixture is clean" a true
    statement rather than a wish.
    """
    rows = conn.execute(
        "SELECT rowid, data FROM replay_probe_results WHERE probe_name = 'team_signal'"
    ).fetchall()
    assert rows, "fixture no longer stores team_signal records"
    for row in rows:
        payload = json.loads(row["data"])
        payload[TEAM_SIGNAL_KEY] = None
        conn.execute(
            "UPDATE replay_probe_results SET data = ? WHERE rowid = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), row["rowid"]),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# The clean case
# ---------------------------------------------------------------------------


def test_a_clean_replay_reports_zero_violations(conn: sqlite3.Connection, cfg: Config) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)

    report = check_dataset(conn, DATASET)
    assert report.clean, report.describe()
    assert report.total == 0
    assert report.counts == {}
    assert report.runs_checked > 0
    assert report.evidence_checked > 0
    assert report.steps_checked > 0
    assert report.failed_runs == 0
    assert report.systems == ("A", "B")
    assert "CLEAN (0 violations)" in report.describe()
    # Clean is not silent: the dataset hazard is still on the report.
    assert report.model_cache_misses == 0
    assert report.blob_input_exposures > 0
    assert "blob input exposures=" in report.describe()


def test_the_checker_writes_nothing(conn: sqlite3.Connection, cfg: Config) -> None:
    """The module's first stated property, asserted rather than asserted-in-prose.

    `PRAGMA query_only` makes SQLite refuse every write on this connection, so
    a checker that inserted, updated or created so much as a temp table (the
    way `rli.replay.pit.point_in_time` legitimately does) would raise here.
    """
    _build(conn, cfg)
    _replay_both(conn, cfg)

    conn.execute("PRAGMA query_only = ON")
    try:
        report = check_dataset(conn, DATASET)
    finally:
        conn.execute("PRAGMA query_only = OFF")
    assert report.runs_checked > 0


def test_an_unbuilt_dataset_is_vacuously_clean(conn: sqlite3.Connection) -> None:
    report = check_dataset(conn, "never-built")
    assert report.clean
    assert report.runs_checked == 0


def test_the_checker_ignores_live_runs_and_other_datasets(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    # The build wrote live System A runs with evidence stamped at the build
    # instant; none of it may be counted against a replay dataset.
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE mode = 'live'").fetchone()[0] > 0
    assert check_dataset(conn, DATASET).clean
    assert check_dataset(conn, "some-other-dataset").runs_checked == 0


# ---------------------------------------------------------------------------
# One planted violation per kind
# ---------------------------------------------------------------------------


def test_evidence_dated_after_t_is_caught(conn: sqlite3.Connection, cfg: Config) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    leaked_at = to_utc_z(NOW + timedelta(days=365))
    conn.execute(
        """
        INSERT INTO evidence
            (id, run_id, posting_id, probe, claim_type, value, source_url,
             source_quality, available_at, fetched_at)
        VALUES ('e999', ?, NULL, 'resolve_posting', 'first_published', '2027-01-01',
                ?, 'ats_native', ?, ?)
        """,
        (run["id"], OPEN_URL, leaked_at, leaked_at),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert not report.clean
    assert report.counts == {"evidence_after_t": 1}
    assert report.violations[0].kind == "evidence_after_t"
    assert report.violations[0].run_id == run["id"]
    assert "e999" in report.violations[0].detail


def test_a_probe_cache_miss_in_a_replay_run_is_caught(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`'miss'` means "at least one call reached the network" (rli.eval.runner)."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    conn.execute(
        """
        UPDATE run_steps SET cache_status = 'miss'
        WHERE run_id = ? AND probe_name = 'resolve_posting'
        """,
        (run["id"],),
    )
    conn.commit()
    assert (
        conn.execute(
            "SELECT DISTINCT component FROM run_steps WHERE run_id = ? AND cache_status = 'miss'",
            (run["id"],),
        ).fetchone()["component"]
        == "probe"
    )

    report = check_dataset(conn, DATASET)
    assert report.counts.get("cache_miss") == 1
    assert report.model_cache_misses == 0
    assert not report.clean


def test_a_model_cache_miss_is_counted_but_is_not_a_violation(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §6: "live **LLM** calls are allowed on cache miss ... and are recorded".

    This is the `dev-300-v2` regression: 199 System C investigator steps were
    reported as leakage for doing exactly what the spec permits.
    """
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    conn.execute(
        """
        INSERT INTO run_steps
            (run_id, step_index, component, decision_type, model_id, cache_status, created_at)
        VALUES (?, 998, 'model', 'investigator:tokens=100/20', 'some-model', 'miss', ?)
        """,
        (run["id"], to_utc_z(NOW)),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert "cache_miss" not in report.counts
    assert report.total == 0
    assert report.clean, report.describe()
    assert report.model_cache_misses == 1
    assert "model cache misses=1" in report.describe()


def test_a_controller_cache_miss_is_still_a_violation(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """No writer sets `cache_status` on a controller row, so a `'miss'` there is
    network traffic attributed to a decision — reported, not excused."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    conn.execute(
        """
        INSERT INTO run_steps
            (run_id, step_index, component, decision_type, cache_status, created_at)
        VALUES (?, 997, 'controller', 'probe_selected', 'miss', ?)
        """,
        (run["id"], to_utc_z(NOW)),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.counts.get("cache_miss") == 1
    assert report.model_cache_misses == 0


def test_a_recorded_net_call_attempt_is_caught(conn: sqlite3.Connection, cfg: Config) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    conn.execute(
        """
        INSERT INTO run_steps
            (run_id, step_index, component, decision_type, probe_name, error, created_at)
        VALUES (?, 999, 'controller', 'replay_violation:net_call', 'board_snapshot',
                'forbidden live tool call', ?)
        """,
        (run["id"], to_utc_z(NOW)),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.counts.get("net_call") == 1


def test_a_dataset_gap_is_reported_as_a_missing_probe_result(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    conn.execute(
        """
        DELETE FROM replay_probe_results
        WHERE dataset_id = ? AND posting_id = ? AND probe_name = 'board_snapshot'
        """,
        (DATASET, OPEN_POSTING),
    )
    conn.commit()
    summary = run_replay(conn, cfg, dataset_id=DATASET, system="A")
    assert summary.violations > 0

    report = check_dataset(conn, DATASET)
    assert report.counts.get("missing_probe_result", 0) > 0
    assert report.failed_runs > 0
    assert not report.clean


def test_a_replay_run_without_a_t_cannot_be_audited_and_says_so(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)
    conn.execute("UPDATE runs SET replay_at = NULL WHERE id = ?", (run["id"],))
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.counts.get("missing_replay_at") == 1


def test_the_itemized_list_is_capped_but_the_counts_are_not(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    leaked_at = to_utc_z(NOW + timedelta(days=365))
    for index in range(6):
        conn.execute(
            """
            INSERT INTO evidence
                (id, run_id, posting_id, probe, claim_type, value, source_url,
                 source_quality, available_at, fetched_at)
            VALUES (?, ?, NULL, 'resolve_posting', 'first_published', 'x',
                    ?, 'ats_native', ?, ?)
            """,
            (f"z{index}", run["id"], OPEN_URL, leaked_at, leaked_at),
        )
    conn.commit()

    report = check_dataset(conn, DATASET, max_items=2)
    assert report.counts["evidence_after_t"] == 6
    assert len(report.violations) == 2
    assert report.truncated == 4
    assert "and 4 more" in report.describe()


# ---------------------------------------------------------------------------
# `input_without_evidence` — the H4 class of bug, proved from the branch taken
# ---------------------------------------------------------------------------


def _a_run_with_no_team_signal_claim(conn: sqlite3.Connection) -> sqlite3.Row:
    """A replay run whose gated evidence cannot decide `corroborating_hiring_signal`.

    That is the H4 state: at an archive-era `T` the `team_signal` claims are
    stamped at the build instant and the gate drops them all.
    """
    row = conn.execute(
        """
        SELECT r.* FROM runs AS r
        WHERE r.mode = 'replay' AND r.replay_at IS NOT NULL
          AND NOT EXISTS (
                SELECT 1 FROM evidence AS e
                WHERE e.run_id = r.id AND e.claim_type = ?
                  AND e.available_at <= r.replay_at
          )
        ORDER BY r.replay_at, r.id LIMIT 1
        """,
        (TEAM_SIGNAL_KEY,),
    ).fetchone()
    assert row is not None
    return row


def _plant_branch(conn: sqlite3.Connection, run_id: str, branch: str) -> None:
    """The `policy_decision:<branch>:<rule>` step `rli.eval.runner` writes."""
    conn.execute(
        """
        INSERT INTO run_steps
            (run_id, step_index, component, decision_type, created_at)
        VALUES (?, 996, 'controller', ?, ?)
        """,
        (run_id, f"policy_decision:{branch}:strong", to_utc_z(NOW)),
    )
    conn.commit()


def test_a_branch_that_proves_a_decided_input_without_a_claim_is_caught(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """Security review H4, caught where it actually does harm.

    `rli.policy.action` reaches `P4_repeated_repost` only when
    `corroborating_hiring_signal is False`, so the branch is durable proof the
    input was decided. No `team_signal` claim survives the gate at this `T`,
    so it cannot have been decided from evidence.

    Note what this does NOT claim: H4's own runs took `P6`/`P1`/`P3c` with a
    properly backed `posting_state`, and `P4` fires zero times in any real
    dataset, so this rule would not by itself have caught them —
    `blob_input_exposures` is what surfaces those. See the module docstring.
    """
    _build(conn, cfg)
    _replay_both(conn, cfg)
    assert check_dataset(conn, DATASET).clean

    run = _a_run_with_no_team_signal_claim(conn)
    _plant_branch(conn, run["id"], "P4_repeated_repost")

    report = check_dataset(conn, DATASET)
    assert report.counts == {"input_without_evidence": 1}
    assert not report.clean
    violation = report.violations[0]
    assert violation.run_id == run["id"]
    assert "P4_repeated_repost" in violation.detail
    assert TEAM_SIGNAL_KEY in violation.detail


def test_a_supporting_claim_at_t_clears_the_branch_requirement(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`available_at == T` is inside the window, so it backs the input."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_run_with_no_team_signal_claim(conn)
    _plant_branch(conn, run["id"], "P4_repeated_repost")
    assert not check_dataset(conn, DATASET).clean

    conn.execute(
        """
        INSERT INTO evidence
            (id, run_id, posting_id, probe, claim_type, value, source_url,
             source_quality, available_at, fetched_at)
        VALUES ('ts1', ?, NULL, 'team_signal', ?, 'false', ?, 'enrichment', ?, ?)
        """,
        (run["id"], TEAM_SIGNAL_KEY, OPEN_URL, run["replay_at"], run["replay_at"]),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.clean, report.describe()


def test_a_claim_dated_after_t_does_not_back_the_branch_requirement(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The gate is the point: a claim the run was not allowed to see cannot be
    the thing that decided the input."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_run_with_no_team_signal_claim(conn)
    _plant_branch(conn, run["id"], "P4_repeated_repost")

    after_t = to_utc_z(NOW + timedelta(days=365))
    conn.execute(
        """
        INSERT INTO evidence
            (id, run_id, posting_id, probe, claim_type, value, source_url,
             source_quality, available_at, fetched_at)
        VALUES ('ts2', ?, NULL, 'team_signal', ?, 'false', ?, 'enrichment', ?, ?)
        """,
        (run["id"], TEAM_SIGNAL_KEY, OPEN_URL, after_t, after_t),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.counts.get("input_without_evidence") == 1
    # ... and the same row is independently caught by the first rule.
    assert report.counts.get("evidence_after_t") == 1


def test_only_the_unresolved_state_branch_carries_no_obligation(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`P2_state_unresolved` is the branch that FIRES on an unknown
    `posting_state`, so it is the only one that proves nothing — and because
    `_branch` is first-match-wins, every later branch proves the opposite."""
    assert "P2_state_unresolved" not in _BRANCH_REQUIREMENTS
    assert set(_BRANCH_REQUIREMENTS) == {b for b, _ in PRECEDENCE} - {"P2_state_unresolved"}

    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_run_with_no_team_signal_claim(conn)
    conn.execute(
        "DELETE FROM evidence WHERE run_id = ? AND claim_type = 'posting_state'", (run["id"],)
    )
    # ... and drop the branch the replay really took, so P2 is the only one.
    conn.execute(
        "DELETE FROM run_steps WHERE run_id = ? AND substr(decision_type, 1, 16) = ?",
        (run["id"], "policy_decision:"),
    )
    _plant_branch(conn, run["id"], "P2_state_unresolved")

    assert check_dataset(conn, DATASET).clean, check_dataset(conn, DATASET).describe()

    # The very same run, on any later branch, IS flagged.
    _plant_branch(conn, run["id"], "P6_active_mixed_or_weak")
    report = check_dataset(conn, DATASET)
    assert report.counts.get("input_without_evidence") == 1
    assert "posting_state" in report.violations[0].detail


def test_a_run_without_a_t_is_not_flagged_by_the_branch_rule(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """No `T`, no window: "no backing claim at <= T" would be true by vacuum,
    so the run is reported once as unauditable and not twice."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_run_with_no_team_signal_claim(conn)
    _plant_branch(conn, run["id"], "P4_repeated_repost")
    conn.execute("UPDATE runs SET replay_at = NULL WHERE id = ?", (run["id"],))
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.counts == {"missing_replay_at": 1}


def test_a_multi_requirement_branch_reports_each_unbacked_input_once(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`P5_open_recent_strong` proves `posting_state` AND `publish_recency`;
    two branch steps on one run are still one finding per input."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_run_with_no_team_signal_claim(conn)
    conn.execute(
        "DELETE FROM evidence WHERE run_id = ? AND claim_type IN "
        "('posting_state', 'first_published', 'refreshed_at')",
        (run["id"],),
    )
    _plant_branch(conn, run["id"], "P5_open_recent_strong")
    conn.execute(
        """
        INSERT INTO run_steps (run_id, step_index, component, decision_type, created_at)
        VALUES (?, 995, 'controller', 'policy_decision:P5_open_recent_strong:mixed', ?)
        """,
        (run["id"], to_utc_z(NOW)),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.counts.get("input_without_evidence") == 2
    assert {v.detail.split("policy input ")[1].split("'")[1] for v in report.violations} == {
        "posting_state",
        "publish_recency",
    }


def test_the_company_events_branches_are_backed_by_an_events_claim(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`P3a`/`P3c` need `freeze_or_pause is True` / `material_negative_event is
    True`, which only `company_events` claims can populate."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_run_with_no_team_signal_claim(conn)
    _plant_branch(conn, run["id"], "P3c_material_event_unrefreshed")

    report = check_dataset(conn, DATASET)
    assert report.counts.get("input_without_evidence") == 1
    assert "material_negative_event" in report.violations[0].detail

    conn.execute(
        """
        INSERT INTO evidence
            (id, run_id, posting_id, probe, claim_type, value, source_url,
             source_quality, available_at, fetched_at)
        VALUES ('ce1', ?, NULL, 'company_events', 'company_events_searched', 'x', ?,
                'enrichment', ?, ?)
        """,
        (run["id"], OPEN_URL, run["replay_at"], run["replay_at"]),
    )
    conn.commit()
    assert check_dataset(conn, DATASET).clean


def test_every_branch_requirement_is_a_real_branch_and_a_real_policy_input() -> None:
    """The table is keyed by `rli.policy.action` branch ids and
    `PolicyInputs` field names, never by a string this module made up."""
    assert "input_without_evidence" in VIOLATION_KINDS
    branches = {branch for branch, _ in PRECEDENCE}
    assert _BRANCH_REQUIREMENTS
    for branch, requirements in _BRANCH_REQUIREMENTS.items():
        assert branch in branches, branch
        assert requirements
        for requirement in requirements:
            assert requirement.input_name in PolicyInputs.model_fields, requirement.input_name
            assert requirement.backed_by
            assert requirement.backing_label


# ---------------------------------------------------------------------------
# `blob_input_exposures` — the dataset hazard, counted but never a violation
# ---------------------------------------------------------------------------


def test_the_team_signal_blob_exposure_is_counted_without_failing_the_check(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """H4's DATASET half, reproduced by an ordinary build + replay.

    Nothing is planted. `team_signal`'s record is built once and served at
    every `T`; at an archive-era `T` the gate correctly drops its claims and
    the blob keeps answering `corroborating_hiring_signal` anyway. No code
    change can clear this — only a rebuild with `as_of` on `TeamSignalArgs` —
    so it is a number, not a gate.
    """
    _build(conn, cfg)
    _replay_both(conn, cfg)

    report = check_dataset(conn, DATASET)
    assert report.blob_input_exposures > 0
    assert report.counts == {}
    assert report.total == 0
    assert report.clean, report.describe()
    assert f"blob input exposures={report.blob_input_exposures}" in report.describe()


def test_an_exposure_a_run_never_executed_is_not_counted(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The dataset stores a record for every probe at every `T`, including ones
    a system never selects (`rli.replay.build`). Only a SERVED record counts."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    assert check_dataset(conn, DATASET).blob_input_exposures > 0

    conn.execute("DELETE FROM run_steps WHERE component = 'probe' AND probe_name = 'team_signal'")
    conn.commit()
    assert check_dataset(conn, DATASET).blob_input_exposures == 0


def test_a_blank_blob_value_is_not_a_decided_input(conn: sqlite3.Connection, cfg: Config) -> None:
    """`None` is the probe's own "Unknown"; a blob that declines to answer is
    not an exposure."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    assert check_dataset(conn, DATASET).blob_input_exposures > 0

    _unexpose_team_signal(conn)
    assert check_dataset(conn, DATASET).blob_input_exposures == 0


def test_a_missing_blob_record_is_not_counted(conn: sqlite3.Connection, cfg: Config) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    conn.execute("DELETE FROM replay_probe_results WHERE probe_name = 'team_signal'")
    conn.commit()

    assert check_dataset(conn, DATASET).blob_input_exposures == 0


def test_a_malformed_blob_is_skipped_rather_than_crashing_the_checker(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A payload this corrupt raises in `decode_probe_data` at SERVE time, so
    it can never have exposed anything; the checker must not die on it."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    conn.execute(
        "UPDATE replay_probe_results SET data = '{not json' WHERE probe_name = 'team_signal'"
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.blob_input_exposures == 0
    assert report.clean, report.describe()


def test_a_supporting_claim_at_t_clears_the_exposure(conn: sqlite3.Connection, cfg: Config) -> None:
    """A run whose `team_signal` claim survives the gate is not exposed: the
    blob agrees with evidence it was allowed to see."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    before = check_dataset(conn, DATASET)
    assert before.blob_input_exposures > 0

    for index, run in enumerate(
        conn.execute(
            "SELECT id, replay_at FROM runs WHERE mode = 'replay' AND replay_at IS NOT NULL"
        ).fetchall()
    ):
        conn.execute(
            """
            INSERT INTO evidence
                (id, run_id, posting_id, probe, claim_type, value, source_url,
                 source_quality, available_at, fetched_at)
            VALUES (?, ?, NULL, 'team_signal', ?, 'true', ?, 'enrichment', ?, ?)
            """,
            (
                f"tsx{index}",
                run["id"],
                TEAM_SIGNAL_KEY,
                OPEN_URL,
                run["replay_at"],
                run["replay_at"],
            ),
        )
    conn.commit()

    after = check_dataset(conn, DATASET)
    assert after.blob_input_exposures == 0
    assert after.clean, after.describe()


def test_every_exposure_key_names_a_real_policy_input() -> None:
    assert _INPUT_EXPOSURES
    for exposure in _INPUT_EXPOSURES:
        assert exposure.key in PolicyInputs.model_fields, exposure.key
        assert exposure.backed_by
        assert exposure.backing_label


# ---------------------------------------------------------------------------
# The in-process counterpart
# ---------------------------------------------------------------------------


def test_net_call_count_counts_refused_attempts(cfg: Config) -> None:
    pool = ReplayNetPool(cfg=cfg)
    try:
        assert net_call_count(pool) == 0
        client = pool.client_for("board_snapshot")
        for _ in range(2):
            try:
                client.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
            except ReplayViolation:
                pass
        assert net_call_count(pool) == 2
    finally:
        pool.close()
