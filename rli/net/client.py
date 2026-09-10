"""httpx-based network wrapper: allowlist enforcement, per-host rate limiting,
bounded exponential backoff with jitter, manual allowlist-checked redirects,
and an append-only SQLite-backed tool cache.

Uses a synchronous `httpx.Client` under the hood so it is directly testable
with `respx` (spec.md §2: domain allowlists, structured probe failure, tool
result caching; PLAN.md M0: "net: httpx wrapper, per-host rate limit +
backoff, domain allowlist, tool cache").

Two rules govern the whole module:

* **Untrusted remote input never raises.** spec.md §2 requires structured
  probe failure (`{ok:false,error,retryable}`) rather than exceptions, so
  `NetClient.get` returns a `NetResult` for every network and HTTP outcome,
  including a redirect chain that tries to leave the allowlist.
* **Misconfiguration raises immediately.** A probe asked to fetch a URL its
  own allowlist forbids is a programming error, not remote input, so that
  single case raises `DisallowedHostError`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import httpx

from rli.models.time import now_utc, parse_utc, to_utc_z

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from rli.config import Config

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

# Status codes treated as retryable per spec.md §2 ("Structured probe
# failure ... no uncontrolled retry loops" implies retries are bounded and
# limited to genuinely transient failures). 5xx codes that describe a
# permanent server-side refusal (501, 505, ...) are deliberately absent.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# 3xx codes that carry a `Location` we follow. A 3xx without `Location`
# (notably 304 Not Modified) is a terminal response, not a hop.
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

DEFAULT_TTL_SECONDS = 3600.0

# Host suffixes that are never publicly routable. Checked because the
# json_ld probe runs with the `"*"` allowlist against arbitrary employer
# career pages (spec.md §3), so `"*"` must not become an SSRF primitive.
# This is defence in depth on the URL only: rli.net deliberately does not
# re-check the address a hostname resolves to, because a DNS answer can
# change between the check and the connect (TOCTOU) and httpx exposes no
# hook to validate the socket peer. Treat public-DNS-rebinding as an
# accepted, documented residual risk for the `"*"` allowlist.
NON_PUBLIC_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
    ".in-addr.arpa",
    ".ip6.arpa",
)


class DisallowedHostError(Exception):
    """Raised when a URL fails the pre-request safety checks (spec.md §2).

    Covers every reason a URL may not be fetched — a non-https scheme, embedded
    userinfo, a missing host, an IP literal, a non-public host, or a host that
    is simply absent from the probe's allowlist — because from the caller's
    point of view they are one thing: this URL must not be requested. The
    specific reason is available as `.reason` for logging and tests.
    """

    def __init__(
        self,
        reason: str,
        *,
        url: str = "",
        host: str = "",
        allowlist: Sequence[str] = (),
    ) -> None:
        self.reason = reason
        self.url = url
        self.host = host
        self.allowlist = list(allowlist)
        super().__init__(f"refusing to fetch {url!r}: {reason} (allowlist={self.allowlist!r})")


def _normalize_host(host: str) -> str:
    """Lowercase a host and strip a single trailing dot.

    `boards.greenhouse.io.` is the fully-qualified form of
    `boards.greenhouse.io` and resolves identically, so it must not be a way
    to slip past an exact allowlist match.
    """
    host = host.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    return host


def _response_url(response: httpx.Response, *, fallback: str) -> str:
    """The URL a response actually came from, including any appended `params`.

    `httpx.Response.request` RAISES `RuntimeError` when no request has been
    attached (a hand-constructed `Response`) rather than returning None, so
    this cannot be a plain `is not None` check.
    """
    try:
        return str(response.request.url)
    except RuntimeError:
        return fallback


# Characters allowed in a hostname beyond alphanumerics. Underscore is not
# legal per RFC 1123 but occurs in the wild, so it is tolerated.
_HOST_EXTRA_CHARS = frozenset("-._")


def _is_plausible_hostname(host: str) -> bool:
    """True if `host` could be a real DNS name (length + label + charset).

    Unicode alphanumerics pass, so internationalized domains still work; what
    this rejects is a host containing a space, a quote, a control character or
    any other byte that only appears there because someone put it there —
    the shape of a `Location` header crafted to confuse the URL parser rather
    than to name a server.
    """
    if not 0 < len(host) <= 253:
        return False
    labels = host.split(".")
    if any(not label or len(label) > 63 for label in labels):
        return False
    return all(ch.isalnum() or ch in _HOST_EXTRA_CHARS for ch in host)


def _is_ip_literal(host: str) -> bool:
    """True if `host` is an IPv4/IPv6 literal in any form we can detect.

    `urlparse().hostname` already unwraps the brackets of an IPv6 literal, so
    `[::1]` arrives here as `::1`. Beyond `ipaddress.ip_address`, a host whose
    last label is entirely numeric is rejected too: no public TLD is
    all-numeric (RFC 3696), while `2130706433`, `0177.0.0.1` and `0x7f.0.0.1`
    are all accepted by the platform resolver as 127.0.0.1.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True

    last_label = host.rsplit(".", 1)[-1]
    return last_label.isdigit()


def check_allowed(url: str, allowlist: Sequence[str]) -> str:
    """Validate `url` against `allowlist`; return the normalized host.

    Enforces spec.md §2 ("Enforce per-probe network-domain allowlists") plus
    the hardening that makes such an allowlist actually meaningful. Rejects,
    before any network call:

    * a non-`https` scheme (`http:`, `file:`, `gopher:`, ...) — evidence
      provenance (spec.md §3) is worthless over a channel an on-path
      attacker can rewrite;
    * embedded userinfo. `https://boards.greenhouse.io@evil.example.com/x`
      has hostname `evil.example.com`, so the host check already catches it,
      but userinfo in a job URL is a phishing smell in its own right and is
      refused even when the real host is allowed;
    * an empty host;
    * an IP literal (v4/v6, bracketed or not) — this covers private,
      loopback, link-local and reserved ranges including the cloud metadata
      address, and also non-dotted integer/octal/hex spellings of them;
    * a host that is not a syntactically valid DNS name (spaces, quotes,
      control characters, empty or over-long labels) — such a host reaches
      us only from a `Location` header written to confuse a URL parser;
    * a non-public host: a single-label host (`localhost`, `metadata`) or one
      under a non-routable suffix (see `NON_PUBLIC_HOST_SUFFIXES`).

    It is a URL-level guard only, and deliberately does not resolve the host:
    a DNS answer can change between the check and the connect, so a
    resolution-time check would be security theatre without a hook to
    validate the actual socket peer (which httpx does not offer). DNS
    rebinding against a public name is an accepted, documented residual risk
    of the `"*"` allowlist.

    Matching rule: the entry `"*"` allows any host that passed the checks
    above (used by the `json_ld` probe, which reads arbitrary employer career
    pages). Otherwise the normalized host must equal an allowlist entry
    exactly, case-insensitively. A suffix match is NOT enough:
    `evilboards.greenhouse.io` and `boards.greenhouse.io.attacker.com` are
    both rejected by the entry `boards.greenhouse.io`. Wildcard subdomain
    entries (`*.greenhouse.io`) are deliberately unsupported for now — every
    ATS host this project needs is a fixed, enumerable name, and a
    subdomain wildcard is exactly the construct that turns one compromised
    or user-controlled subdomain into an allowlist bypass.

    Raises `DisallowedHostError` (with a `.reason`) on any failure.
    """
    entries = list(allowlist)

    try:
        parsed = urlparse(url)
        host_raw = parsed.hostname or ""
        username = parsed.username
        password = parsed.password
    except ValueError as exc:
        # e.g. an unparseable port or a malformed IPv6 bracket group.
        raise DisallowedHostError(f"malformed URL ({exc})", url=url, allowlist=entries) from exc

    if parsed.scheme.lower() != "https":
        raise DisallowedHostError(
            f"scheme {parsed.scheme!r} is not https", url=url, allowlist=entries
        )

    if username is not None or password is not None:
        raise DisallowedHostError("URL carries userinfo", url=url, allowlist=entries)

    host = _normalize_host(host_raw)
    if not host:
        raise DisallowedHostError("URL has no host", url=url, allowlist=entries)

    if _is_ip_literal(host):
        raise DisallowedHostError(
            "host is an IP literal (only named public hosts are allowed)",
            url=url,
            host=host,
            allowlist=entries,
        )

    if not _is_plausible_hostname(host):
        raise DisallowedHostError(
            "host is not a syntactically valid hostname",
            url=url,
            host=host,
            allowlist=entries,
        )

    if "." not in host or host.endswith(NON_PUBLIC_HOST_SUFFIXES):
        raise DisallowedHostError(
            "host is not publicly routable", url=url, host=host, allowlist=entries
        )

    if "*" in entries:
        return host

    if host in {_normalize_host(entry) for entry in entries}:
        return host

    raise DisallowedHostError(
        "host is not in the probe's allowlist", url=url, host=host, allowlist=entries
    )


def hash_args(probe: str, **args: object) -> str:
    """Stable cache key for one probe call, over canonical JSON.

    `sort_keys=True` means keyword order can never produce two cache keys for
    the same call, which matters both for the tool cache and for the
    controller's "the same probe+arguments would repeat" hard stop
    (spec.md §4). `default=str` keeps non-JSON values (notably datetimes)
    hashable rather than raising; callers should still pass pre-serialized
    values where the exact form matters.
    """
    payload = json.dumps(
        {"probe": probe, "args": args},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TokenBucket:
    """An in-process token-bucket rate limiter for one host (spec.md §4).

    Thread-safe: token accounting happens under a lock so two concurrent
    probes cannot both see the last token and double-spend it. The lock is
    never held across the sleep — that would serialize waiters into a queue
    whose total wait is the sum rather than the max of their deficits.

    `monotonic` and `sleep` are injectable so tests can exercise refill
    timing without wall-clock delays. An injected `sleep` MUST advance the
    injected `monotonic`, otherwise `acquire` spins forever — which is
    exactly what a real clock does for free.
    """

    def __init__(
        self,
        requests_per_second: float,
        burst: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Config validation (rli.config.RateLimit / Net) makes these
        # impossible, but a bad literal at a call site would otherwise hang
        # forever (burst=0 with rps<=0) or spin (rps<=0), so fail here.
        if not requests_per_second > 0:
            raise ValueError(f"requests_per_second must be > 0, got {requests_per_second!r}")
        if not burst >= 1:
            raise ValueError(f"burst must be >= 1, got {burst!r}")

        self.rate = float(requests_per_second)
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()
        self._lock = threading.Lock()

    @property
    def tokens(self) -> float:
        """Current token count, refill not applied (diagnostics/tests only)."""
        with self._lock:
            return self._tokens

    def _try_acquire(self) -> float:
        """Consume a token if available; else return the seconds to wait."""
        with self._lock:
            now = self._monotonic()
            elapsed = max(0.0, now - self._last)
            self._last = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            return (1.0 - self._tokens) / self.rate

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            wait_s = self._try_acquire()
            if wait_s <= 0.0:
                return
            # Sleep the exact deficit, then re-check under the lock: another
            # thread may have taken the token this sleep was waiting for.
            self._sleep(wait_s)


class RateLimiter:
    """Per-host token-bucket rate limiting, with per-host overrides.

    Buckets are created lazily per host and cached, so an unlisted host still
    gets the configured default rather than unlimited access (spec.md §4).
    """

    def __init__(
        self,
        default_rps: float = 1.0,
        default_burst: int = 3,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._default_rps = default_rps
        self._default_burst = default_burst
        self._overrides: dict[str, tuple[float, int]] = {}
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> RateLimiter:
        """Build a limiter from `cfg.rate_limits`, defaulting from `cfg.net`."""
        limiter = cls(
            default_rps=cfg.net.default_requests_per_second,
            default_burst=cfg.net.default_burst,
            monotonic=monotonic,
            sleep=sleep,
        )
        for host, limit in cfg.rate_limits.items():
            limiter.configure_host(host, limit.requests_per_second, limit.burst)
        return limiter

    def configure_host(self, host: str, requests_per_second: float, burst: int) -> None:
        """Override the bucket parameters for one host, discarding its bucket."""
        key = _normalize_host(host)
        with self._lock:
            self._overrides[key] = (requests_per_second, burst)
            self._buckets.pop(key, None)

    def acquire(self, host: str) -> None:
        """Block until `host`'s bucket yields a token."""
        key = _normalize_host(host)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                rps, burst = self._overrides.get(key, (self._default_rps, self._default_burst))
                bucket = TokenBucket(rps, burst, monotonic=self._monotonic, sleep=self._sleep)
                self._buckets[key] = bucket
        # Released before blocking: one throttled host must not stall others.
        bucket.acquire()


class ToolCache:
    """Append-only SQLite cache for probe/tool HTTP results (spec.md §2/§6).

    Rows are never updated in place. Each `set` appends a
    `(probe, args_hash, fetched_at)` row, so the history of what a probe saw
    is preserved and point-in-time replay can resolve the result that was in
    effect at a given time rather than only the newest one. `get` therefore
    means "latest non-expired row", not "the row".

    Timestamps are written with `to_utc_z`, so the TEXT column's lexical
    ordering is chronological ordering and `ORDER BY fetched_at DESC` is
    correct without parsing.

    Connection ownership: this class calls `commit()` on the connection it is
    given, and therefore TAKES OWNERSHIP OF WRITE-COMMIT on it. Do not hand
    it a connection with an in-flight caller transaction — a cache write
    would commit that caller's partial work as a side effect. Give it its own
    connection (or one used only for cache access). This is a documented
    contract rather than an enforced one; a connection pool is out of scope
    for M0.
    """

    # Upper bound on rows examined per lookup. Bounded because a malformed
    # or long-lived key could otherwise make a cache read scan an unbounded
    # slice of an append-only table. More than a handful of consecutive
    # unparseable/expired rows for one key is a corruption signal, not a
    # case worth paging through.
    MAX_CANDIDATE_ROWS = 8

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, probe: str, args_hash: str, *, now: datetime | None = None) -> dict | None:
        """Return the latest non-expired cached response, or None.

        A row that cannot be parsed — a malformed `expires_at`, a `response`
        that is not a JSON object — is a cache MISS, never an exception: a
        corrupt cache row must degrade into a re-fetch, not fail the run
        (spec.md §2, structured failure). Rows are read newest-first and
        scanning continues past a bad or expired row, because per-call TTLs
        mean an older row can outlive a newer one.

        Column access is positional so this works whether or not the caller's
        connection sets `row_factory = sqlite3.Row`.
        """
        moment = now if now is not None else now_utc()
        rows = self._conn.execute(
            """
            SELECT response, expires_at
            FROM tool_cache
            WHERE probe = ? AND args_hash = ?
            ORDER BY fetched_at DESC
            LIMIT ?
            """,
            (probe, args_hash, self.MAX_CANDIDATE_ROWS),
        ).fetchall()

        for row in rows:
            response_text, expires_at_text = row[0], row[1]
            try:
                expires_at = parse_utc(expires_at_text)
            except (ValueError, TypeError, AttributeError):
                continue
            if expires_at <= moment:
                continue
            try:
                payload = json.loads(response_text)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def set(
        self,
        probe: str,
        args_hash: str,
        response: dict,
        ttl_seconds: float,
        *,
        fetched_at: datetime | None = None,
    ) -> None:
        """Append a cache row valid for `ttl_seconds` from `fetched_at`.

        `INSERT OR REPLACE` guards the one collision the append-only primary
        key `(probe, args_hash, fetched_at)` still permits: two fetches of
        the same call inside the same microsecond. Replacing is right here —
        the two rows describe the same observation at the same instant, so
        keeping the newer body loses nothing, whereas nudging `fetched_at`
        forward to dodge the collision would falsify a recorded fetch time
        that replay reads as fact (spec.md §3).
        """
        moment = fetched_at if fetched_at is not None else now_utc()
        expires_at = moment + timedelta(seconds=ttl_seconds)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO tool_cache
                (probe, args_hash, fetched_at, response, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                probe,
                args_hash,
                to_utc_z(moment),
                json.dumps(response, sort_keys=True, separators=(",", ":")),
                to_utc_z(expires_at),
            ),
        )
        self._conn.commit()


@dataclass(frozen=True, slots=True)
class NetResult:
    """Outcome of one `NetClient.get`, in the spec.md §2 probe-failure shape.

    `ok=False` with `retryable` set is the structured failure the spec
    requires (`{ok:false,error,retryable}`); `NetClient.get` never raises for
    a network or HTTP condition, so every caller can branch on these fields
    instead of on exception types.

    * `ok` — the request reached a terminal response with status < 400.
    * `status` — final HTTP status, or None if no response was received.
    * `body` — response text, or None if no response was received.
    * `url` — the FINAL url after redirects, i.e. what `body` actually came
      from. Evidence must cite this, not the requested URL (spec.md §3).
    * `fetched_at` — tz-aware UTC; for a cache hit, the ORIGINAL fetch time,
      so `available_at` accounting is not backdated by caching (spec.md §3).
    * `error` / `retryable` — populated only when `ok` is False.
    * `from_cache` — served from `tool_cache` without a network call.
    """

    ok: bool
    status: int | None
    body: str | None
    url: str
    fetched_at: datetime
    error: str | None = None
    retryable: bool = False
    from_cache: bool = False


class NetClient:
    """Allowlisted, rate-limited, retrying, cache-aware HTTP GET wrapper.

    Uses `httpx.Client` (sync, not async) so it is directly mockable with
    `respx` in tests.

    Redirects are followed manually (`follow_redirects=False` on the
    underlying client) so that every hop is `check_allowed`-validated before
    it is fetched. httpx's built-in following would resolve the whole chain
    inside one call, which would mean the allowlist only ever constrained the
    first request — a redirect from an allowed board to an arbitrary host is
    trivially attacker-controllable and would defeat spec.md §2.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        cache: ToolCache | None = None,
        probe: str | None = None,
        allowlist: Sequence[str] | None = None,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
        max_backoff_s: float = 30.0,
        timeout_s: float = 20.0,
        max_redirects: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=False)
        # Forced even for an injected client: the per-hop allowlist check is
        # only reachable if httpx does not follow redirects itself.
        self._client.follow_redirects = False
        self._rate_limiter = rate_limiter or RateLimiter()
        self._cache = cache
        self._probe = probe
        self._allowlist = list(allowlist) if allowlist is not None else None
        self._max_retries = max(0, int(max_retries))
        self._backoff_base_s = float(backoff_base_s)
        self._max_backoff_s = float(max_backoff_s)
        self._max_redirects = max(0, int(max_redirects))
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        probe: str,
        conn: sqlite3.Connection | None = None,
        client: httpx.Client | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> NetClient:
        """Build a client bound to one probe's allowlist and the `[net]` knobs.

        Binding the probe here is what makes spec.md §2's "per-probe network
        domain allowlists" a property of the client rather than of each call
        site remembering to pass the right list.

        `monotonic`/`sleep`/`rng` are injected into BOTH the retry backoff and
        the per-host rate limiter, so a client built here can be driven by a
        fake clock end to end.
        """
        valid_probes = tuple(type(cfg.allowlists).model_fields)
        if probe not in valid_probes:
            raise ValueError(
                f"unknown probe {probe!r}; valid probes are: {', '.join(valid_probes)}"
            )

        return cls(
            client=client,
            # `monotonic`/`sleep` must reach the limiter too, not just the
            # backoff path: a caller that injected them still blocks on the
            # wall clock the first time a bucket runs dry otherwise.
            rate_limiter=RateLimiter.from_config(cfg, monotonic=monotonic, sleep=sleep),
            cache=ToolCache(conn) if conn is not None else None,
            probe=probe,
            allowlist=getattr(cfg.allowlists, probe),
            max_retries=cfg.net.max_retries,
            backoff_base_s=cfg.net.backoff_base_s,
            max_backoff_s=cfg.net.max_backoff_s,
            timeout_s=cfg.net.timeout_s,
            max_redirects=cfg.net.max_redirects,
            sleep=sleep,
            rng=rng,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NetClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- backoff -----------------------------------------------------------

    def _retry_after_seconds(self, header_value: str | None) -> float | None:
        """Parse a `Retry-After` header in delta-seconds form.

        Only the integer/decimal seconds form is honoured. The HTTP-date form
        is deliberately ignored (returns None, so the computed backoff
        applies): parsing it correctly means trusting the remote clock
        against ours, and the value is clamped to `max_backoff_s` anyway, so
        the extra fidelity buys nothing. Negative, NaN and infinite values are
        ignored rather than clamped to 0, since they signal a broken server
        rather than "retry immediately".
        """
        if not header_value:
            return None
        try:
            seconds = float(header_value.strip())
        except (ValueError, AttributeError):
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return min(seconds, self._max_backoff_s)

    def _backoff_delay(self, attempt: int, retry_after: str | None = None) -> float:
        """Seconds to sleep before retry `attempt` (0-based).

        A server-supplied `Retry-After` wins, clamped to `max_backoff_s` so a
        remote host can never park a run past its latency budget. Otherwise:
        FULL jitter, `uniform(0, min(max_backoff_s, base * 2**attempt))`.
        Full rather than equal jitter because the snapshot jobs (spec.md §4)
        fan out across many postings on the same host and retry in lockstep
        after a 429; full jitter is what actually decorrelates them.
        """
        override = self._retry_after_seconds(retry_after)
        if override is not None:
            return override
        # min() on the exponent keeps `2.0 ** attempt` from overflowing if a
        # caller ever passes an absurd max_retries.
        ceiling = min(self._max_backoff_s, self._backoff_base_s * 2.0 ** min(attempt, 32))
        return self._rng.uniform(0.0, ceiling)

    # -- fetch -------------------------------------------------------------

    def _failure(
        self,
        url: str,
        status: int | None,
        body: str | None,
        error: str,
        *,
        retryable: bool,
    ) -> NetResult:
        return NetResult(
            ok=False,
            status=status,
            body=body,
            url=url,
            fetched_at=now_utc(),
            error=error,
            retryable=retryable,
        )

    def _fetch_one_hop(self, url: str, params: dict[str, str] | None) -> httpx.Response | NetResult:
        """Fetch one URL with bounded retries; never raise for network errors.

        Returns the `httpx.Response` on any terminal outcome (including 4xx,
        which the caller turns into a non-retryable failure), or a `NetResult`
        describing an exhausted-retry failure.
        """
        host = _normalize_host(urlparse(url).hostname or "")
        last_error = "no attempt was made"

        for attempt in range(self._max_retries + 1):
            self._rate_limiter.acquire(host)
            try:
                response = self._client.get(url, params=params)
            except (httpx.InvalidURL, UnicodeError) as exc:
                # Neither of these is an `httpx.RequestError`: `InvalidURL`
                # derives straight from Exception, and a host that fails IDNA
                # encoding (`https://xn--.com/`) surfaces as `idna.IDNAError`,
                # a `UnicodeError`. A redirect `Location` is remote-controlled,
                # so without this clause any origin server could crash a probe
                # by answering with a syntactically hostile URL. Not retryable:
                # a URL that cannot be encoded will never become valid.
                return self._failure(
                    url, None, None, f"invalid URL: {type(exc).__name__}: {exc}", retryable=False
                )
            except httpx.RequestError as exc:
                # RequestError, not TransportError: it is the common base of
                # transport failures (connect/read/timeout/protocol) as well
                # as of the request-level errors httpx raises before sending.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._max_retries:
                    self._sleep(self._backoff_delay(attempt))
                    continue
                return self._failure(url, None, None, last_error, retryable=True)

            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = f"HTTP {response.status_code}"
                if attempt < self._max_retries:
                    self._sleep(self._backoff_delay(attempt, response.headers.get("retry-after")))
                    continue
                return self._failure(
                    url,
                    response.status_code,
                    response.text,
                    f"{last_error} after {self._max_retries} retries",
                    retryable=True,
                )

            return response

        # Unreachable: the loop returns on its final iteration either way.
        return self._failure(url, None, None, last_error, retryable=True)

    def _fetch_following_redirects(
        self,
        url: str,
        *,
        allowlist: Sequence[str],
        params: dict[str, str] | None,
    ) -> NetResult:
        """Fetch `url`, following at most `max_redirects` allowlisted hops.

        A redirect target is remote-controlled input, so a hop to a
        disallowed host/scheme and an over-long chain are STRUCTURED failures
        (`ok=False, retryable=False`) rather than exceptions — spec.md §2
        requires probes to fail with `{ok:false,error,retryable}`, and an
        exception here would let any origin server crash a probe by
        answering `302 Location: http://127.0.0.1/`. Contrast with the
        initial URL, which the probe itself chose: that raises.

        `params` are sent only on the first hop; a `Location` carries its own
        query string, and re-appending ours would corrupt it.
        """
        current = url
        sent_params = params

        for _ in range(self._max_redirects + 1):
            outcome = self._fetch_one_hop(current, sent_params)
            if isinstance(outcome, NetResult):
                return outcome

            location = outcome.headers.get("location")
            if outcome.status_code in REDIRECT_STATUS_CODES and location:
                # Relative Locations are legal (RFC 9110 §10.2.2) and must be
                # resolved against the CURRENT url, not the original.
                next_url = urljoin(current, location)
                try:
                    check_allowed(next_url, allowlist)
                except DisallowedHostError as exc:
                    return self._failure(
                        current,
                        outcome.status_code,
                        None,
                        f"redirect to disallowed target {next_url!r}: {exc.reason}",
                        retryable=False,
                    )
                current = next_url
                sent_params = None
                continue

            # Terminal response: 2xx, a 3xx with no Location (e.g. 304), or
            # any 4xx/5xx that survived the retry loop.
            status = outcome.status_code
            ok = status < 400
            return NetResult(
                ok=ok,
                status=status,
                body=outcome.text,
                url=_response_url(outcome, fallback=current),
                fetched_at=now_utc(),
                error=None if ok else f"HTTP {status}",
                # 4xx is a definitive answer about this URL (410 Gone is how
                # a closed posting often reads); retrying cannot change it.
                retryable=False,
            )

        return self._failure(
            current,
            None,
            None,
            f"exceeded max_redirects={self._max_redirects}",
            retryable=False,
        )

    # -- public API --------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        probe: str | None = None,
        allowlist: Sequence[str] | None = None,
        params: dict[str, str] | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        use_cache: bool = True,
    ) -> NetResult:
        """Fetch `url`, returning a structured `NetResult`.

        `probe` and `allowlist` default to those bound by `from_config`; pass
        them to override (or when the client was built directly).

        This method NEVER raises for a network or HTTP condition — timeouts,
        DNS failures, 5xx, 4xx, an attacker-supplied redirect chain — because
        spec.md §2 requires structured probe failure. The single exception it
        still raises is `DisallowedHostError` for the INITIAL url: a probe
        asking for a host its own allowlist forbids is a programming error in
        this codebase, and turning it into `{ok:false}` would let a
        misconfigured probe look like a merely unreachable one.

        Only `ok` results are cached. A 4xx is not cached (it is cheap to
        re-ask and may be transient authorization state) and a retryable
        failure is certainly not (caching a 503 would pin a host's outage
        into the evidence record for the whole TTL).
        """
        probe_name = probe if probe is not None else self._probe
        if probe_name is None:
            raise ValueError(
                "no probe bound to this NetClient; pass probe=... or build it "
                "with NetClient.from_config(cfg, probe=...)"
            )

        effective_allowlist = allowlist if allowlist is not None else self._allowlist
        if effective_allowlist is None:
            raise ValueError(
                "no allowlist bound to this NetClient; pass allowlist=... or "
                "build it with NetClient.from_config(cfg, probe=...)"
            )

        # Raises: the probe chose this URL (spec.md §2 misconfiguration).
        check_allowed(url, effective_allowlist)

        args_hash = hash_args(probe_name, url=url, params=params or {})

        if use_cache and self._cache is not None:
            cached = self._cache.get(probe_name, args_hash)
            result = _result_from_cache_payload(cached, requested_url=url)
            if result is not None:
                return result

        result = self._fetch_following_redirects(url, allowlist=effective_allowlist, params=params)

        if result.ok and use_cache and self._cache is not None:
            self._cache.set(
                probe_name,
                args_hash,
                {
                    "status": result.status,
                    "body": result.body,
                    "url": result.url,
                    "fetched_at": to_utc_z(result.fetched_at),
                },
                ttl_seconds,
                fetched_at=result.fetched_at,
            )
        return result


def _result_from_cache_payload(payload: dict | None, *, requested_url: str) -> NetResult | None:
    """Rebuild a `NetResult` from a cached payload, or None if unusable.

    A payload written by an older/other version of this module may not have
    the fields we now expect. Like a malformed row in `ToolCache.get`, that is
    a cache miss rather than a crash. `fetched_at` is restored from the
    payload — NOT set to now — so cached evidence keeps its original fetch
    time and cannot be backdated onto the replay timeline (spec.md §3).
    """
    if not payload:
        return None
    status = payload.get("status")
    body = payload.get("body")
    if not isinstance(status, int) or not isinstance(body, str):
        return None
    try:
        fetched_at = parse_utc(payload["fetched_at"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    final_url = payload.get("url")
    return NetResult(
        ok=True,
        status=status,
        body=body,
        url=final_url if isinstance(final_url, str) and final_url else requested_url,
        fetched_at=fetched_at,
        error=None,
        retryable=False,
        from_cache=True,
    )
