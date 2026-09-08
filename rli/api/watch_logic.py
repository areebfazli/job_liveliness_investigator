"""Role-watch business logic: add a watch, check due watches.

Kept separate from `rli/api/app.py`'s route handlers so this reads like a
plain `rli watch check`-style function even though Phase 5 is not allowed
to touch `rli/cli.py` to actually wire up such a subcommand — it is only
exposed over HTTP here (`GET /watch/due`), but nothing about it depends on
FastAPI.

Both functions always run System B (never C): a watch's whole point is a
cheap, deterministic recheck, and spec.md's System C is the expensive
LLM-driven path this shell explicitly avoids for background rechecks.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from rli.api.watch_store import load_watches, normalize_watch_key, update_watches, upsert_watch
from rli.config import Config
from rli.eval.system_b import run_system_b
from rli.models.time import now_utc, parse_utc, to_utc_z


def add_watch(conn: sqlite3.Connection, cfg: Config, url: str, store_path: str) -> dict:
    """Run a fresh System B investigation of `url` and store/refresh its watch entry."""
    result = run_system_b(conn, cfg, url)
    decision = result.decision
    now = now_utc()

    posting_id = conn.execute(
        "SELECT posting_id FROM runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    posting_id_value = posting_id["posting_id"] if posting_id is not None else None

    key = normalize_watch_key(url)
    entry = {
        "url": key,
        "posting_id": posting_id_value,
        "added_at": to_utc_z(now),
        "last_checked_at": to_utc_z(now),
        "recheck_after_days": decision.recheck_after_days,
        "last_posting_state": decision.posting_state,
        "last_recommended_action": decision.recommended_action,
        "last_run_id": result.run_id,
    }
    return upsert_watch(store_path, key, entry)


def _is_due(entry: dict, now: datetime) -> bool:
    recheck_after_days = entry.get("recheck_after_days")
    last_checked_at = entry.get("last_checked_at")
    if recheck_after_days is None or last_checked_at is None:
        return False
    due_at = parse_utc(last_checked_at) + timedelta(days=recheck_after_days)
    return due_at <= now


def check_due_watches(
    conn: sqlite3.Connection,
    cfg: Config,
    store_path: str,
    now: datetime | None = None,
) -> list[dict]:
    """Re-run System B for every due watch entry; return a change summary per entry.

    A watch entry is "due" when `last_checked_at + recheck_after_days days
    <= now`; a null `recheck_after_days` means the entry is never
    automatically due. Entries that are not due are left untouched and
    excluded from the returned list.
    """
    moment = now if now is not None else now_utc()
    entries = load_watches(store_path)
    updates: dict[str, dict] = {}
    results: list[dict] = []

    for entry in entries:
        if not _is_due(entry, moment):
            continue

        url = entry["url"]
        result = run_system_b(conn, cfg, url, now=moment)
        decision = result.decision

        previous_state = entry.get("last_posting_state")
        previous_action = entry.get("last_recommended_action")
        new_state = decision.posting_state
        new_action = decision.recommended_action

        posting_row = conn.execute(
            "SELECT posting_id FROM runs WHERE id = ?", (result.run_id,)
        ).fetchone()
        posting_id_value = posting_row["posting_id"] if posting_row is not None else None

        checked_at = to_utc_z(moment)
        updated_entry = {
            **entry,
            "posting_id": posting_id_value or entry.get("posting_id"),
            "last_checked_at": checked_at,
            "recheck_after_days": decision.recheck_after_days,
            "last_posting_state": new_state,
            "last_recommended_action": new_action,
            "last_run_id": result.run_id,
        }
        updates[url] = updated_entry

        results.append(
            {
                "url": url,
                "previous_state": previous_state,
                "previous_action": previous_action,
                "new_state": new_state,
                "new_action": new_action,
                "changed": (previous_state, previous_action) != (new_state, new_action),
                "checked_at": checked_at,
            }
        )

    update_watches(store_path, updates)
    return results
