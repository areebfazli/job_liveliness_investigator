"""rli.net — allowlisted, rate-limited, cached HTTP access (spec.md §2/§4).

Public surface:

* `NetClient` — the only supported way to make an outbound HTTP request.
  Build it with `NetClient.from_config(cfg, probe=..., conn=...)` so the
  probe's allowlist, the `[net]` retry/backoff knobs and the per-host rate
  limits all come from `config.toml`. `NetClient.get` returns a `NetResult`
  and never raises for a network/HTTP condition.
* `NetResult` — the spec.md §2 structured outcome
  (`ok` / `error` / `retryable`, plus `status`, `body`, final `url`,
  `fetched_at`, `from_cache`).
* `DisallowedHostError` — the one exception `NetClient.get` still raises,
  and only for an initial URL the calling probe is not allowed to fetch.
* `check_allowed` — the pre-request URL guard (https-only, no userinfo, no
  IP literals, no non-public hosts, exact allowlist match).
* `TokenBucket` / `RateLimiter` — per-host token-bucket rate limiting.
* `ToolCache` — the append-only `tool_cache` reader/writer.
* `hash_args` — canonical cache/repeat-detection key for a probe call.
* `RETRYABLE_STATUS_CODES` / `REDIRECT_STATUS_CODES` / `DEFAULT_TTL_SECONDS`
  — the module's policy constants, exported so tests and the controller can
  assert against them rather than restate them.
"""

from rli.net.client import (
    DEFAULT_TTL_SECONDS,
    REDIRECT_STATUS_CODES,
    RETRYABLE_STATUS_CODES,
    DisallowedHostError,
    NetClient,
    NetResult,
    RateLimiter,
    TokenBucket,
    ToolCache,
    check_allowed,
    hash_args,
)

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "REDIRECT_STATUS_CODES",
    "RETRYABLE_STATUS_CODES",
    "DisallowedHostError",
    "NetClient",
    "NetResult",
    "RateLimiter",
    "TokenBucket",
    "ToolCache",
    "check_allowed",
    "hash_args",
]
