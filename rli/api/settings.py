"""API-only settings, sourced from environment variables (not `rli.config`).

`rli.config.Config` is a frozen Pydantic model with `extra="forbid"` and is
loaded from the project's `config.toml`. Phase 5 (product shell) is not
allowed to touch either `rli/config.py` or `config.toml`, so there is no
`[api]` table anywhere in the real configuration system. Everything this
thin HTTP layer needs that is not already in `Config` (which db file to
open, whether the debug trace route is exposed, which host/port to bind,
where the JSON watch-list file lives) is instead read straight from the
process environment here, with defaults that match the conventions used
elsewhere in the project (`./data/rli.db`, `./data/watches.json`,
`127.0.0.1:8000`).

This keeps the split clean: `rli.config.Config` is the frozen, versioned
configuration that all three evaluation systems (A/B/C) run under and that
`config_hash` fingerprints for reproducibility; `ApiSettings` is ordinary
process/deployment configuration for the API process itself, the same way
you would configure any web server's bind address.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ApiSettings:
    """Resolved API process settings, read once at app-creation time."""

    db_path: str
    debug_routes: bool
    watch_store_path: str
    host: str
    port: int

    @classmethod
    def from_env(
        cls,
        *,
        db_path: str | None = None,
        watch_store_path: str | None = None,
    ) -> ApiSettings:
        """Build settings from explicit overrides, falling back to env vars.

        `db_path` / `watch_store_path` overrides exist so tests (and
        `create_app`) can inject a scratch path directly without going
        through the environment.
        """
        return cls(
            db_path=db_path or os.environ.get("RLI_DB_PATH", "./data/rli.db"),
            debug_routes=_truthy(os.environ.get("RLI_API_DEBUG_ROUTES"), default=True),
            watch_store_path=watch_store_path
            or os.environ.get("RLI_WATCH_STORE_PATH", "./data/watches.json"),
            host=os.environ.get("RLI_API_HOST", "127.0.0.1"),
            port=int(os.environ.get("RLI_API_PORT", "8000")),
        )
