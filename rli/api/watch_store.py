"""The "role watch" list: a small JSON file store, not a schema table.

Phase 5 may not add a table to `rli/db/schema.sql`, so the watch list lives
in a plain JSON file (default `./data/watches.json`, overridable via
`RLI_WATCH_STORE_PATH` or `create_app(watch_store_path=...)`). This is a
runtime *data* file the API manages, exactly like `data/rli.db` is a
runtime data file the collector manages — it is not a source file, and it
never touches `data/rli.db` itself.

Concurrency: FastAPI (via Starlette) runs sync route handlers in a
threadpool, so two requests can race on a read-modify-write of the JSON
file within one process. `_LOCK` is a module-scope `threading.Lock` that
every read-modify-write in this module takes, which is sufficient for a
single-process deployment (this is a local single-user tool, per the
project brief) but not for multiple processes sharing one file — that is an
explicit, acceptable simplification here.

A watch entry is keyed by its normalized URL (`AtsRef.canonical_url` when
the URL detects as a known ATS, else the raw URL) and shaped as::

    {
        "url": str,
        "posting_id": str | None,
        "added_at": str,               # to_utc_z
        "last_checked_at": str,        # to_utc_z
        "recheck_after_days": int | None,
        "last_posting_state": str,
        "last_recommended_action": str,
        "last_run_id": str,
    }
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_LOCK = threading.Lock()


def normalize_watch_key(url: str) -> str:
    """Normalize a URL to the key watch entries are stored/looked-up under.

    Uses `rli.resolvers.detect.detect_ats`'s `canonical_url` when the URL
    detects as a known ATS (so `.../jobs/123` and `.../jobs/123/` collapse
    to the same watch), and falls back to the raw URL for a `generic` https
    posting page, which has no canonicalization rule of its own.
    """
    from rli.resolvers.detect import detect_ats

    ref = detect_ats(url)
    if ref is not None and ref.canonical_url:
        return ref.canonical_url
    return url


def _read_all(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def _write_all(path: Path, entries: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def load_watches(store_path: str) -> list[dict]:
    """Return all stored watch entries, in stored (insertion) order."""
    path = Path(store_path)
    with _LOCK:
        entries = _read_all(path)
    return list(entries.values())


def upsert_watch(store_path: str, key: str, entry: dict) -> dict:
    """Insert or replace the watch entry stored under `key`; return it."""
    path = Path(store_path)
    with _LOCK:
        entries = _read_all(path)
        entries[key] = entry
        _write_all(path, entries)
    return entry


def update_watches(store_path: str, updates: dict[str, dict]) -> None:
    """Merge `updates` (key -> full replacement entry) into the store."""
    if not updates:
        return
    path = Path(store_path)
    with _LOCK:
        entries = _read_all(path)
        entries.update(updates)
        _write_all(path, entries)
