"""API-only settings, sourced from environment variables (not `rli.config`).

`rli.config.Config` is a frozen Pydantic model with `extra="forbid"` and is
loaded from the project's `config.toml`. Phase 5 (product shell) is not
allowed to touch either `rli/config.py` or `config.toml`, so there is no
`[api]` table anywhere in the real configuration system. Everything this
thin HTTP layer needs that is not already in `Config` (which db file to
open, whether the debug trace route is exposed, which host/port to bind,
where the JSON watch-list file lives, the bearer token that guards the
write/spend endpoints, and the per-client requests-per-minute cap) is
instead read straight from the process environment here, with defaults that
match the conventions used elsewhere in the project (`./data/rli.db`,
`./data/watches.json`, `127.0.0.1:8000`).

Security posture: the API is unauthenticated by default *only* because it
binds loopback by default. `startup_security_error` encodes that rule as a
single pure function — serving is refused when the bind host is not
loopback and `RLI_API_TOKEN` is unset — so the "it's fine, it's local"
assumption can never silently survive an `RLI_API_HOST=0.0.0.0`.

This keeps the split clean: `rli.config.Config` is the frozen, versioned
configuration that all three evaluation systems (A/B/C) run under and that
`config_hash` fingerprints for reproducibility; `ApiSettings` is ordinary
process/deployment configuration for the API process itself, the same way
you would configure any web server's bind address.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _truthy(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def is_loopback_host(host: str) -> bool:
    """True when `host` is one of the loopback names the API may bind unauthenticated."""
    return host.strip().lower() in _LOOPBACK_HOSTS


@dataclass(frozen=True)
class ApiSettings:
    """Resolved API process settings, read once at app-creation time."""

    db_path: str
    debug_routes: bool
    watch_store_path: str
    host: str
    port: int
    api_token: str | None
    rate_limit_rpm: int

    @classmethod
    def from_env(
        cls,
        *,
        db_path: str | None = None,
        watch_store_path: str | None = None,
        debug_routes: bool | None = None,
        api_token: str | None = None,
        rate_limit_rpm: int | None = None,
    ) -> ApiSettings:
        """Build settings from explicit overrides, falling back to env vars.

        Every override exists so tests (and `create_app`) can inject a value
        directly without going through the process environment; `None` means
        "resolve from the environment / the documented default".
        """
        env_token = os.environ.get("RLI_API_TOKEN")
        resolved_token = api_token if api_token is not None else env_token
        return cls(
            db_path=db_path or os.environ.get("RLI_DB_PATH", "./data/rli.db"),
            debug_routes=(
                debug_routes
                if debug_routes is not None
                # Default OFF: `GET /runs/{id}` returns raw `runs`/`run_steps`
                # rows (probe args, prompt hashes, model ids, costs, raw error
                # text) for ANY run id, with no ownership concept.
                else _truthy(os.environ.get("RLI_API_DEBUG_ROUTES"), default=False)
            ),
            watch_store_path=watch_store_path
            or os.environ.get("RLI_WATCH_STORE_PATH", "./data/watches.json"),
            host=os.environ.get("RLI_API_HOST", "127.0.0.1"),
            port=int(os.environ.get("RLI_API_PORT", "8000")),
            # An empty string is "no token configured", never a token that
            # every unauthenticated request would match.
            api_token=resolved_token or None,
            rate_limit_rpm=(
                rate_limit_rpm
                if rate_limit_rpm is not None
                else int(os.environ.get("RLI_API_RPM", "10"))
            ),
        )


def startup_security_error(settings: ApiSettings) -> str | None:
    """Return why it is unsafe to start serving, or `None` when it is safe.

    Pure and side-effect-free so the rule can be unit-tested without binding
    a socket. Serving unauthenticated is allowed only while the bind host is
    loopback; a token makes any bind host acceptable.
    """
    if settings.api_token:
        return None
    if is_loopback_host(settings.host):
        return None
    return (
        f"refusing to start: RLI_API_HOST is {settings.host!r}, which is not a loopback "
        f"address, and RLI_API_TOKEN is not set. Every endpoint that spends LLM quota, "
        f"makes outbound fetches, or writes to the database would be exposed to the "
        f"network with no authentication. Set RLI_API_TOKEN to a secret value, or bind a "
        f"loopback host (one of: {', '.join(sorted(_LOOPBACK_HOSTS))})."
    )
