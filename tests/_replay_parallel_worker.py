"""One sharded System C replay worker, run as its own OS process.

Not a test module (no `test_` prefix, so pytest never collects it). It is
what `tests/test_replay_parallel.py` launches N times concurrently against one
database, and it doubles as the harness for a dry check against a COPY of the
real database — which is why it depends on nothing under `tests/`.

The model is a fixed, offline `ScriptedClient` wrapped in the real
`CachedClient` (so `llm_cache` inserts race exactly as they would live):
every investigator call answers STOP and every explanation call answers `{}`
(the deterministic fallback). `--latency LO,HI` sleeps a seeded random
interval inside each call, standing in for the network wait during which the
sibling workers commit. No network is ever touched.

`--quota-after K` makes the (K+1)-th case's investigator call fail with a
daily-quota `LLMTransportError`, so `stop_on_quota` cuts the walk there —
the path whose cleanup must never reach another shard's runs.

Prints one JSON object on stdout: the summary counts, the per-case errors,
throughput, and `llm_calls_in_transaction` — the number of model calls made
while the replay connection had an open transaction (must be 0: a network
wait inside a transaction would hold a lock or a read snapshot the whole
time).
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import sqlite3
import sys
import time
from typing import Any

from pydantic import BaseModel

from rli.agent.explanation import ExplanationOutput
from rli.agent.investigator import InvestigatorOutput
from rli.agent.loop import make_system_c
from rli.config import load_config
from rli.db import connect
from rli.llm.client import CachedClient, LLMTransportError, Prompt, ScriptedClient
from rli.llm.prompts import TEMPLATE_EXPLANATION, TEMPLATE_INVESTIGATOR
from rli.replay.run import format_shard, parse_shard, run_replay

MODEL_ID = "scripted-fixed-stop"
QUOTA_TEXT = "HTTP 429: Quota exceeded for metric: requests, limit: 500 per day"


def fixed_client(
    conn: sqlite3.Connection,
    *,
    latency: tuple[float, float],
    seed: int,
    quota_after: int | None,
    stats: dict[str, int],
) -> ScriptedClient:
    rng = random.Random(seed)

    def responder(prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
        stats["llm_calls"] += 1
        if conn.in_transaction:
            stats["llm_calls_in_transaction"] += 1
        low, high = latency
        if high > 0:
            time.sleep(rng.uniform(low, high))
        if prompt.template_id == TEMPLATE_INVESTIGATOR:
            stats["investigator_calls"] += 1
            if quota_after is not None and stats["investigator_calls"] > quota_after:
                raise LLMTransportError(QUOTA_TEXT, status_code=429)
            return InvestigatorOutput(stop=True, stop_reason="fixed offline stop")
        if prompt.template_id == TEMPLATE_EXPLANATION:
            return ExplanationOutput()
        raise AssertionError(f"unexpected template {prompt.template_id!r}")

    return ScriptedClient(responder, model_id=MODEL_ID)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--shard", default=None, help="I/N; omit for an unsharded walk")
    parser.add_argument("--latency", default="0,0", help="LO,HI seconds per model call")
    parser.add_argument("--limit-cases", type=int, default=None)
    parser.add_argument("--quota-after", type=int, default=None)
    parser.add_argument("--busy-timeout", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    shard = parse_shard(args.shard) if args.shard else None
    low, high = (float(x) for x in args.latency.split(","))
    stats = {"llm_calls": 0, "llm_calls_in_transaction": 0, "investigator_calls": 0}

    cfg = load_config()
    conn = connect(args.db, busy_timeout_ms=int(args.busy_timeout * 1000))
    try:
        inner = fixed_client(
            conn,
            latency=(low, high),
            seed=args.seed if shard is None else args.seed * 1000 + shard[0],
            quota_after=args.quota_after,
            stats=stats,
        )
        runner = make_system_c(llm_factory=lambda conn_, _cfg: CachedClient(inner, conn_))
        started = time.monotonic()
        summary = run_replay(
            conn,
            cfg,
            dataset_id=args.dataset,
            system="C",
            runner=runner,
            limit_cases=args.limit_cases,
            replace=False,
            resume=True,
            stop_on_quota=True,
            shard=shard,
        )
        elapsed = time.monotonic() - started
    finally:
        conn.close()

    ran = summary.cases - summary.skipped
    report: dict[str, Any] = {
        "shard": None if shard is None else format_shard(shard),
        "cases": summary.cases,
        "completed": summary.completed,
        "skipped": summary.skipped,
        "errors": summary.errors,
        "violations": summary.violations,
        "lock_retries": summary.lock_retries,
        "stopped_reason": summary.stopped_reason,
        "error_texts": [o.error for o in summary.outcomes if o.error is not None],
        "run_ids": [o.run_id for o in summary.outcomes if o.run_id is not None],
        "elapsed_s": round(elapsed, 3),
        "cases_per_min": round(ran / elapsed * 60.0, 1) if elapsed > 0 else None,
        "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1),
        **stats,
    }
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
