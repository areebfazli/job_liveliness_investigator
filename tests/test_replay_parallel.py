"""Sharded, concurrent System C replay (`run_replay(shard=...)`, `replay run --shard`).

N worker PROCESSES replay disjoint shards of one dataset against one SQLite
database. What has to hold:

* the shard assignment is a deterministic partition, identical in every
  process (so no case is run twice and none is dropped);
* concurrent workers finish every case with no lost or duplicated runs and
  no "database is locked" failures, even with the model call slowed down so
  their writes interleave;
* the result is the same as a sequential replay, case for case;
* nothing a worker deletes — a quota-cut run, a stale failed run, a
  lock-failed partial run — can belong to another shard;
* racing identical `llm_cache` inserts cannot fail a case;
* no transaction (and so no lock or read snapshot) is open while the model
  is being waited on.

Every model here is an offline `ScriptedClient`; the workers are real OS
processes running `tests/_replay_parallel_worker.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path

import pytest
import respx
from pydantic import BaseModel
from test_eval_helpers import add_company
from test_replay_helpers import NOW, dev_everything_cutoff, mock_ats, seed_corpus
from typer.testing import CliRunner

import rli.replay.build as replay_build
from rli.agent.explanation import ExplanationOutput
from rli.agent.investigator import InvestigatorOutput
from rli.agent.loop import make_system_c
from rli.cli import app
from rli.config import Config, load_config
from rli.db import connect, init_db
from rli.eval.runner import Run, config_hash
from rli.eval.system_b import run_system_b
from rli.llm.client import CachedClient, Prompt, ScriptedClient
from rli.llm.prompts import TEMPLATE_EXPLANATION, TEMPLATE_INVESTIGATOR
from rli.models.decision import Decision
from rli.replay.build import build_dataset, dataset_case_rows
from rli.replay.run import (
    ShardViolation,
    _assert_deletable,
    dataset_status,
    parse_shard,
    run_replay,
    shard_of,
)

DATASET = "ds-par"
WORKER = Path(__file__).with_name("_replay_parallel_worker.py")
REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_TEXT = "database is locked"


# ---------------------------------------------------------------------------
# Fixtures: one built dataset, copied per test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database holding a ~50-case dataset (grid every 3 days), built once."""
    path = tmp_path_factory.mktemp("par-template") / "template.db"
    init_db(path)
    conn = connect(path)
    try:
        seed_corpus(conn)
        # Module scope runs before conftest's function-scoped switch-off of the
        # stable company holdout, so it is switched off here too.
        with respx.mock, pytest.MonkeyPatch.context() as patch:
            patch.setattr(replay_build, "stable_test_companies", lambda _conn, **_kw: set())
            mock_ats()
            summary = build_dataset(
                conn,
                load_config(),
                dataset_id=DATASET,
                split="dev",
                split_kind="temporal",
                grid_step_days=3,
                now=NOW,
                cutoff=dev_everything_cutoff(),
                use_tool_cache=False,
                collection_status_csv="/nonexistent/collection_status.csv",
            )
        assert summary.postings_failed == 0, summary.failures
        assert summary.cases >= 40
    finally:
        conn.close()  # the last close checkpoints the WAL into the main file
    return path


@pytest.fixture
def db_copy(template_db: Path, tmp_path: Path) -> Path:
    target = tmp_path / "par.db"
    shutil.copy(template_db, target)
    return target


def _launch(db: Path, *args: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env.pop("PYTHONHASHSEED", None)
    return subprocess.Popen(
        [sys.executable, str(WORKER), "--db", str(db), "--dataset", DATASET, *args],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _collect(procs: list[subprocess.Popen[str]]) -> list[dict]:
    reports = []
    for proc in procs:
        out, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, err[-4000:]
        reports.append(json.loads(out.strip().splitlines()[-1]))
    return reports


def _run_workers(db: Path, shards: int, *extra: str) -> list[dict]:
    procs = [_launch(db, "--shard", f"{i}/{shards}", *extra) for i in range(shards)]
    return _collect(procs)


def _c_runs(db: Path) -> list[sqlite3.Row]:
    conn = connect(db)
    try:
        return conn.execute(
            "SELECT id, input_url, replay_at, status, final_decision FROM runs "
            "WHERE mode = 'replay' AND system = 'C'"
        ).fetchall()
    finally:
        conn.close()


def _cases(db: Path) -> list[sqlite3.Row]:
    conn = connect(db)
    try:
        return dataset_case_rows(conn, DATASET)
    finally:
        conn.close()


def _fingerprint(final_decision: str) -> dict:
    """A run's decision with the per-run `evidence[].run_id` dropped."""
    payload = json.loads(final_decision)
    for item in payload.get("evidence", []):
        item.pop("run_id", None)
    return payload


def _decisions_by_case(db: Path) -> dict[tuple[str, str], dict]:
    return {
        (row["input_url"], row["replay_at"]): _fingerprint(row["final_decision"])
        for row in _c_runs(db)
    }


@pytest.fixture(scope="module")
def sequential_decisions(template_db: Path, tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The unsharded, single-process replay every sharded run must reproduce."""
    db = tmp_path_factory.mktemp("par-seq") / "seq.db"
    shutil.copy(template_db, db)
    (report,) = _collect([_launch(db)])
    assert report["errors"] == 0, report
    assert report["completed"] == report["cases"]
    return _decisions_by_case(db)


# ---------------------------------------------------------------------------
# The shard assignment
# ---------------------------------------------------------------------------


def test_shard_assignment_is_a_deterministic_partition() -> None:
    keys = [
        (f"greenhouse:t{i % 7}:{1000 + i}", f"2026-0{1 + i % 9}-01T00:00:00.000000Z")
        for i in range(600)
    ]
    for count in (1, 2, 3, 4, 7):
        owners = [shard_of(posting, at, count) for posting, at in keys]
        assert all(0 <= owner < count for owner in owners)  # exactly one shard each
        assert owners == [shard_of(posting, at, count) for posting, at in keys]
        sizes = Counter(owners)
        assert set(sizes) == set(range(count))
        assert max(sizes.values()) - min(sizes.values()) < 0.25 * len(keys) / count + 10


def test_shard_assignment_does_not_depend_on_the_process_hash_seed() -> None:
    """sha256, not `hash()`: two processes with different seeds must agree."""
    code = (
        "from rli.replay.run import shard_of;"
        "print([shard_of(f'p{i}', f'2026-01-{1 + i % 28:02d}T00:00:00.000000Z', 4) "
        "for i in range(50)])"
    )
    results = set()
    for seed in ("1", "2", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True
        )
        assert out.returncode == 0, out.stderr
        results.add(out.stdout.strip())
    assert len(results) == 1
    local = [shard_of(f"p{i}", f"2026-01-{1 + i % 28:02d}T00:00:00.000000Z", 4) for i in range(50)]
    assert results == {str(local)}


def test_shard_specs_are_validated() -> None:
    assert parse_shard("0/4") == (0, 4)
    assert parse_shard(" 3 / 4 ") == (3, 4)
    for bad in ("4/4", "0/0", "-1/4", "a/b", "1", "1/2/3"):
        with pytest.raises(ValueError):
            parse_shard(bad)


def test_the_dataset_shards_cover_every_case_exactly_once(db_copy: Path) -> None:
    rows = _cases(db_copy)
    conn = connect(db_copy)
    try:
        status = dataset_status(conn, dataset_id=DATASET, shards=4)
    finally:
        conn.close()
    c = next(s for s in status.by_system if s.system == "C")
    assert [part.shard for part in c.shards] == [0, 1, 2, 3]
    assert sum(part.total for part in c.shards) == len(rows) == c.total
    assert "shard 3/4: completed=0" in status.describe()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_sharded_walk_must_resume(conn: sqlite3.Connection, cfg: Config) -> None:
    conn.execute(
        "INSERT INTO replay_datasets (dataset_id, created_at, split_kind, split_name, "
        "grid_step_days, postings, companies, cases, notes) "
        "VALUES ('d', '2026-01-01T00:00:00.000000Z', 'temporal', 'dev', 7, 1, 1, 1, NULL)"
    )
    conn.execute(
        "INSERT INTO replay_cases (dataset_id, posting_id, replay_at, company_id, "
        "canonical_url, built_at) VALUES ('d', 'p', '2026-01-01T00:00:00.000000Z', 'c', 'u', "
        "'2026-01-01T00:00:00.000000Z')"
    )
    conn.commit()
    with pytest.raises(ValueError, match="must resume"):
        run_replay(conn, cfg, dataset_id="d", system="A", shard=(0, 2))
    with pytest.raises(ValueError, match="invalid shard"):
        run_replay(conn, cfg, dataset_id="d", system="A", resume=True, shard=(2, 2))


def test_the_cli_refuses_shard_with_no_resume(tmp_path: Path) -> None:
    db = tmp_path / "rli.db"
    init_db(db)
    result = CliRunner().invoke(
        app,
        [
            "replay", "run", "--system", "C", "--dataset", DATASET,
            "--shard", "0/4", "--no-resume", "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 2
    assert "--no-resume" in result.output

    bad = CliRunner().invoke(
        app,
        ["replay", "run", "--system", "C", "--dataset", DATASET, "--shard", "4/4", "--db", str(db)],
    )
    assert bad.exit_code == 2
    assert "invalid shard" in bad.output


# ---------------------------------------------------------------------------
# Concurrency: real processes, one database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("shards", "latency"), [(2, "0.02,0.1"), (4, "0.05,0.2")])
def test_concurrent_shard_workers_complete_every_case_exactly_once(
    db_copy: Path, shards: int, latency: str, sequential_decisions: dict
) -> None:
    reports = _run_workers(db_copy, shards, "--latency", latency)
    cases = _cases(db_copy)

    for report in reports:
        assert report["errors"] == 0, report["error_texts"]
        assert report["lock_retries"] == 0, report
        assert report["llm_calls_in_transaction"] == 0, report
        assert report["stopped_reason"] is None
        assert not any(LOCK_TEXT in text for text in report["error_texts"])
    assert sum(r["cases"] for r in reports) == len(cases)
    assert sum(r["completed"] for r in reports) == len(cases)
    # The (slowed) model was really called, so writes interleaved with waits.
    assert all(r["llm_calls"] >= r["completed"] > 0 for r in reports)

    runs = _c_runs(db_copy)
    keys = Counter((row["input_url"], row["replay_at"]) for row in runs)
    assert set(keys) == {(row["canonical_url"], row["replay_at"]) for row in cases}  # none lost
    assert max(keys.values()) == 1  # none duplicated
    assert {row["status"] for row in runs} == {"completed"}

    # Case for case, the same decision a single sequential process makes.
    assert _decisions_by_case(db_copy) == sequential_decisions


def test_a_rerun_of_every_shard_skips_everything(db_copy: Path) -> None:
    first = _run_workers(db_copy, 2)
    before = {row["id"] for row in _c_runs(db_copy)}
    second = _run_workers(db_copy, 2)
    assert sum(r["completed"] for r in first) == len(before)
    assert all(r["completed"] == 0 and r["skipped"] == r["cases"] for r in second)
    assert {row["id"] for row in _c_runs(db_copy)} == before


def test_a_quota_stop_never_touches_another_shards_runs(db_copy: Path) -> None:
    """Shard 1 runs to completion WHILE shard 0 hits a daily quota and cleans up."""
    procs = [
        _launch(db_copy, "--shard", "0/2", "--latency", "0.02,0.08", "--quota-after", "3"),
        _launch(db_copy, "--shard", "1/2", "--latency", "0.02,0.08"),
    ]
    stopped, finished = _collect(procs)

    assert stopped["stopped_reason"] == "quota_exhausted"
    assert stopped["completed"] == 3
    assert finished["errors"] == 0 and finished["stopped_reason"] is None

    cases = _cases(db_copy)
    owner = {
        (r["canonical_url"], r["replay_at"]): shard_of(r["posting_id"], r["replay_at"], 2)
        for r in cases
    }
    runs = _c_runs(db_copy)
    by_shard = Counter(owner[(row["input_url"], row["replay_at"])] for row in runs)
    assert by_shard[1] == sum(1 for v in owner.values() if v == 1)  # all of shard 1 kept
    assert by_shard[0] == 3  # the quota-cut 4th run was deleted, nothing else
    assert set(finished["run_ids"]) <= {row["id"] for row in runs}

    # A resume of shard 0 finishes the dataset.
    (resumed,) = _collect([_launch(db_copy, "--shard", "0/2")])
    assert resumed["skipped"] == 3 and resumed["errors"] == 0
    assert len(_c_runs(db_copy)) == len(cases)


def test_a_deletion_outside_the_shard_is_refused(
    db_copy: Path, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a bug that selects the wrong runs cannot delete a sibling's progress."""
    _run_workers(db_copy, 2)
    conn = connect(db_copy)
    try:
        cases = dataset_case_rows(conn, DATASET)
        foreign = next(r for r in cases if shard_of(r["posting_id"], r["replay_at"], 2) == 1)
        foreign_run = conn.execute(
            "SELECT id FROM runs WHERE system = 'C' AND input_url = ? AND replay_at = ?",
            (foreign["canonical_url"], foreign["replay_at"]),
        ).fetchone()["id"]
        # Make it look stale, so a resume that (wrongly) saw it would delete it —
        # and make one of shard 0's own cases stale too, so the walk below has
        # a case to redo and therefore a deletion to compute.
        own = next(r for r in cases if shard_of(r["posting_id"], r["replay_at"], 2) == 0)
        conn.execute(
            "UPDATE runs SET status = 'failed' WHERE system = 'C' AND "
            "((input_url = ? AND replay_at = ?) OR id = ?)",
            (own["canonical_url"], own["replay_at"], foreign_run),
        )
        conn.commit()

        mine = frozenset(
            (r["canonical_url"], r["replay_at"])
            for r in cases
            if shard_of(r["posting_id"], r["replay_at"], 2) == 0
        )
        with pytest.raises(ShardViolation):
            _assert_deletable(conn, [foreign_run], mine)

        # A walk whose case lookup is broken to return EVERY failed run.
        def everything_failed(conn_, **_kwargs):
            return conn_.execute(
                "SELECT id, status, config_hash FROM runs WHERE status = 'failed'"
            ).fetchall()

        monkeypatch.setattr("rli.replay.run._case_run_rows", everything_failed)
        runner = make_system_c(llm=ScriptedClient(lambda p, s: InvestigatorOutput(stop=True)))
        with pytest.raises(ShardViolation):
            run_replay(
                conn, cfg, dataset_id=DATASET, system="C", runner=runner, resume=True, shard=(0, 2)
            )
        assert conn.execute("SELECT 1 FROM runs WHERE id = ?", (foreign_run,)).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Lock failures: retried once, never left half-written
# ---------------------------------------------------------------------------


def _half_written_then_locked(fail_times: int):
    """A System-C-shaped runner whose first `fail_times` calls COMPLETE a run and
    then die on a lock — the explanation-phase failure that would otherwise leave
    a `completed` run `resume` skips forever."""
    calls = {"n": 0}

    def runner(conn_, cfg_, url, *, now=None, replay=None, collection_status_csv=None):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            run = Run(
                conn_,
                cfg_,
                input_url=url,
                system="C",
                config_hash="cfg:x" + replay.config_hash_suffix(),
                started_at=now,
                mode="replay",
                replay_at=now,
            )
            run.open()
            run.finish(
                Decision(
                    posting_state="open", recommended_action="apply_now", evidence_quality="weak"
                )
            )
            raise sqlite3.OperationalError(LOCK_TEXT)
        result = run_system_b(
            conn_, cfg_, url, now=now, replay=replay, collection_status_csv=collection_status_csv
        )
        return result.model_copy(update={"system": "C"})

    return runner, calls


def test_a_lock_failed_case_is_cleaned_up_and_retried_once(
    db_copy: Path, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rli.replay.run._LOCK_RETRY_DELAY_S", 0.0)
    conn = connect(db_copy)
    try:
        runner, calls = _half_written_then_locked(fail_times=1)
        summary = run_replay(
            conn, cfg, dataset_id=DATASET, system="C", runner=runner, limit_cases=2, resume=True
        )
        assert summary.errors == 0
        assert summary.completed == 2
        assert summary.lock_retries == 1
        assert calls["n"] == 3
        rows = conn.execute(
            "SELECT input_url, replay_at, config_hash FROM runs WHERE mode = 'replay'"
        ).fetchall()
        assert len(rows) == 2  # the half-written run is gone, not duplicated
        assert not any(r["config_hash"].startswith("cfg:x") for r in rows)
    finally:
        conn.close()


def test_a_case_that_stays_locked_fails_with_no_run_left_so_resume_redoes_it(
    db_copy: Path, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rli.replay.run._LOCK_RETRY_DELAY_S", 0.0)
    conn = connect(db_copy)
    try:
        runner, calls = _half_written_then_locked(fail_times=2)
        summary = run_replay(
            conn, cfg, dataset_id=DATASET, system="C", runner=runner, limit_cases=1, resume=True
        )
        assert summary.errors == 1 and summary.completed == 0
        assert LOCK_TEXT in summary.outcomes[0].error
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE mode = 'replay'").fetchone()[0] == 0

        again = run_replay(
            conn, cfg, dataset_id=DATASET, system="C", runner=runner, limit_cases=1, resume=True
        )
        assert again.skipped == 0 and again.completed == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# No transaction across the model call
# ---------------------------------------------------------------------------


def test_no_lock_or_read_snapshot_is_held_while_the_model_is_called(
    db_copy: Path, cfg: Config
) -> None:
    """During every model call of a real System C replay, ANOTHER connection can
    write with a zero busy timeout (so no write lock is held) and fully TRUNCATE
    the WAL (so no read snapshot is held — a checkpoint cannot pass a reader)."""
    conn = connect(db_copy)
    probes: list[tuple[bool, int, bool]] = []

    def responder(prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
        other = sqlite3.connect(db_copy, timeout=0)
        try:
            other.execute("PRAGMA busy_timeout = 0")
            wrote = True
            try:
                add_company(other, f"probe-{len(probes)}.example")
            except sqlite3.OperationalError:
                wrote = False
            busy = other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        finally:
            other.close()
        probes.append((conn.in_transaction, busy, wrote))
        if prompt.template_id == TEMPLATE_INVESTIGATOR:
            return InvestigatorOutput(stop=True)
        assert prompt.template_id == TEMPLATE_EXPLANATION
        return ExplanationOutput()

    try:
        inner = ScriptedClient(responder, model_id="probe-model")
        runner = make_system_c(llm_factory=lambda c, _cfg: CachedClient(inner, c))
        summary = run_replay(
            conn, cfg, dataset_id=DATASET, system="C", runner=runner, limit_cases=6, resume=True
        )
    finally:
        conn.close()
    assert summary.completed == 6, summary.describe()
    assert len(probes) >= 12
    assert all(probe == (False, 0, True) for probe in probes), probes


# ---------------------------------------------------------------------------
# llm_cache races
# ---------------------------------------------------------------------------


class _Answer(BaseModel):
    value: str


def _prompt() -> Prompt:
    return Prompt(
        template_id="race", version="1", system="s", instructions="i", structured_input={"k": 1}
    )


@pytest.mark.parametrize("same_answer", [True, False])
def test_concurrent_identical_llm_cache_inserts_do_not_raise(
    tmp_path: Path, same_answer: bool
) -> None:
    """Every thread misses, then all insert the same key at once."""
    db = tmp_path / "race.db"
    init_db(db)
    workers = 6
    barrier = threading.Barrier(workers)
    results: list[str] = []
    failures: list[BaseException] = []

    def work(index: int) -> None:
        conn = connect(db)
        try:

            def responder(prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
                barrier.wait(timeout=10)  # every thread has already missed
                return _Answer(value="same" if same_answer else f"answer-{index}")

            client = CachedClient(ScriptedClient(responder, model_id="m"), conn)
            response = client.complete_structured(_prompt(), _Answer)
            assert response.cache_status == "miss"
            results.append(response.parsed.value)
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=work, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    conn = connect(db)
    try:
        stored = conn.execute("SELECT response FROM llm_cache").fetchall()
    finally:
        conn.close()
    assert len(stored) == 1
    winner = _Answer.model_validate_json(stored[0][0]).value
    # Every caller returns what every later cache hit will return.
    assert results == [winner] * workers


# ---------------------------------------------------------------------------
# CLI: --shard / --rpm / status --shards
# ---------------------------------------------------------------------------


def test_the_cli_runs_one_shard_with_a_per_process_rpm(
    db_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_rpm: list[int | None] = []

    def fake_client(cfg: Config, rpm: int | None):
        seen_rpm.append(rpm)

        def responder(prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
            if prompt.template_id == TEMPLATE_INVESTIGATOR:
                return InvestigatorOutput(stop=True)
            return ExplanationOutput()

        return ScriptedClient(responder, model_id="cli-scripted")

    monkeypatch.setattr("rli.cli._replay_llm_client", fake_client)
    cli = CliRunner()
    result = cli.invoke(
        app,
        [
            "replay", "run", "--system", "C", "--dataset", DATASET, "--shard", "1/3",
            "--rpm", "53", "--db", str(db_copy),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert f"over dataset {DATASET!r} [shard 1/3]" in result.output
    assert seen_rpm == [53]

    status = cli.invoke(
        app, ["replay", "status", "--dataset", DATASET, "--shards", "3", "--db", str(db_copy)]
    )
    assert status.exit_code == 0, status.output
    rows = _cases(db_copy)
    mine = sum(1 for r in rows if shard_of(r["posting_id"], r["replay_at"], 3) == 1)
    assert f"C: completed={mine} remaining={len(rows) - mine}" in status.output
    assert f"shard 1/3: completed={mine} remaining=0 total={mine}" in status.output


def test_rpm_paces_the_client_without_changing_the_config_hash() -> None:
    from rli.cli import _replay_llm_client

    cfg = load_config()
    client = _replay_llm_client(cfg, 40)
    try:
        assert client._throttle._interval_s == pytest.approx(1.5)
    finally:
        client.close()
    # The replay itself still runs under the unmodified config.
    assert config_hash(cfg) == config_hash(load_config())


# ---------------------------------------------------------------------------
# The launcher scripts (syntax and argument validation only: a real launch
# is detached and long-running, and is exercised by hand with an offline
# `RLI=` stand-in — see the script header)
# ---------------------------------------------------------------------------

SCRIPTS = REPO_ROOT / "scripts"


@pytest.mark.parametrize(
    "script", ["replay_c_parallel.sh", "replay_c_parallel_status.sh", "replay_c_loop.sh"]
)
def test_the_replay_scripts_parse(script: str) -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPTS / script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_launcher_validates_before_starting_anything(tmp_path: Path) -> None:
    env = dict(
        os.environ,
        LOG_DIR=str(tmp_path / "logs"),
        RUN_DIR=str(tmp_path / "run"),
        RLI="false",
    )
    usage = subprocess.run(
        ["bash", str(SCRIPTS / "replay_c_parallel.sh")], env=env, capture_output=True, text=True
    )
    assert usage.returncode == 2
    assert "TOTAL_RPM" in usage.stdout

    for bad in ({"WORKERS": "0"}, {"WORKERS": "x"}, {"WORKERS": "4", "TOTAL_RPM": "3"}):
        result = subprocess.run(
            ["bash", str(SCRIPTS / "replay_c_parallel.sh"), "some-dataset"],
            env={**env, **bad},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, (bad, result.stdout, result.stderr)
    assert not (tmp_path / "run" / "replay-c-parallel.pid").exists()
