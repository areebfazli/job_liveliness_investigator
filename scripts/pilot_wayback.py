"""Pilot driver: archived per-job ATS pages (see rli/pilot/wayback_pages.py).

    uv run python scripts/pilot_wayback.py cdx      # CDX listings -> pilot DB
    uv run python scripts/pilot_wayback.py fetch --cap 3000   # resumable
    uv run python scripts/pilot_wayback.py infer    # CDX-inferred states for unfetched
    uv run python scripts/pilot_wayback.py analyze --out reports/pilot_wayback_job_pages_data.json

Reads data/rli.db READ-ONLY. Writes only data/pilot/ (pilot DB + raw cache).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rli.config import load_config
from rli.pilot import wayback_pages as wp

REPO = Path(__file__).resolve().parents[1]


def _default_data_dir() -> Path:
    # The pilot data lives with the main checkout's data/ (gitignored), also
    # when this script runs from a git worktree.
    for parent in [REPO, *REPO.parents]:
        if (parent / "data" / "rli.db").exists():
            return parent / "data"
    return REPO / "data"


def main(argv: list[str] | None = None) -> int:
    data = _default_data_dir()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["cdx", "plan", "fetch", "infer", "analyze", "cc"])
    ap.add_argument("--main-db", default=str(data / "rli.db"))
    ap.add_argument("--pilot-db", default=str(data / "pilot" / "wayback_job_pages.db"))
    ap.add_argument("--cache", default=str(data / "pilot" / "raw" / "http"))
    ap.add_argument("--cap", type=int, default=3000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    pilot = wp.open_pilot_db(args.pilot_db)
    main_db = wp.open_main_db_readonly(args.main_db)

    def log(msg: str) -> None:
        print(msg, flush=True)

    if args.cmd in ("cdx", "fetch"):
        client = wp.WaybackClient(cache_dir=Path(args.cache), cfg=load_config(), log=log)
    if args.cmd == "cdx":
        wp.ingest_cdx(pilot, main_db, client, log=log)
        log(f"stats {dict(client.stats)}")
    elif args.cmd == "plan":
        plan = wp.plan_fetches(pilot, cap=args.cap)
        from collections import Counter

        log(f"plan: {len(plan)} fetches {Counter(r for _, r in plan)}")
    elif args.cmd == "fetch":
        plan = wp.plan_fetches(pilot, cap=args.cap)
        log(f"plan: {len(plan)} fetches")
        counts = wp.run_fetches(pilot, client, plan, log=log)
        log(f"done {counts} stats {dict(client.stats)}")
    elif args.cmd == "infer":
        log(f"inferred {wp.infer_unfetched(pilot)} captures")
    elif args.cmd == "cc":
        wp.ingest_cc_index(pilot, main_db, Path(args.cache).parent / "cc", log=log)
    elif args.cmd == "analyze":
        from rli.pilot import analysis

        result = analysis.run_all(pilot, main_db, log=log)
        if args.out:
            Path(args.out).write_text(json.dumps(result, indent=1, default=str))
            log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
