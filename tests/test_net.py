"""Behavioural tests for `rli.net`.

Four surfaces are covered, each with its own section below:

* `TokenBucket` / `RateLimiter` — refill timing and burst accounting against an
  injected fake clock, so the suite never sleeps on the wall clock.
* `check_allowed` — one test per rejection reason, asserting on
  `DisallowedHostError.reason` wherever the reason distinguishes cases.
* `ToolCache` / `hash_args` — against a real database built by `rli.db.init_db`,
  so the tests break if `tool_cache` drifts from what the cache expects.
* `NetClient` — retries, redirects, structured failure and caching, driven by
  `respx` with every sleep injected.

Nothing here may call `time.sleep`: every component that can block takes an
injectable `sleep`, and every test injects one.
"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from rli.config import load_config
from rli.db import connect, init_db
from rli.models.time import to_utc_z
from rli.net import (
    DisallowedHostError,
    NetClient,
    RateLimiter,
    TokenBucket,
    ToolCache,
    check_allowed,
    hash_args,
)

# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

PROBE = "resolve_posting"
ALLOWED_HOST = "boards.greenhouse.io"
SECOND_ALLOWED_HOST = "jobs.lever.co"
ALLOWLIST = [ALLOWED_HOST, SECOND_ALLOWED_HOST]

ALLOWED_URL = f"https://{ALLOWED_HOST}/acme/jobs/1"
SECOND_ALLOWED_URL = f"https://{SECOND_ALLOWED_HOST}/acme/2"
DISALLOWED_URL = "https://evil.example.com/x"


class FakeClock:
    """A mutable monotonic clock whose `sleep` advances it and records waits.

    An injected `sleep` that does not advance the injected `monotonic` would
    make `TokenBucket.acquire` spin forever, so advancing here is not a
    convenience — it is what makes the fake a faithful stand-in for the real
    clock.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SleepRecorder:
    """A `sleep` callable that records durations without ever blocking."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A connection to a real database created from `rli/db/schema.sql`."""
    db_path = tmp_path / "rli.sqlite3"
    init_db(db_path)
    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def cache(conn: sqlite3.Connection) -> ToolCache:
    return ToolCache(conn)


def tool_cache_row_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM tool_cache").fetchone()[0])


@pytest.fixture
def make_client(
    conn: sqlite3.Connection, sleeps: SleepRecorder
) -> Iterator[Callable[..., NetClient]]:
    """Factory for a fully-injected `NetClient`: no real sleeping, seeded jitter.

    The rate limiter is given a fake clock plus a bucket big enough that it
    never throttles, so rate limiting cannot silently contribute sleeps to the
    `sleeps` recorder that the backoff assertions read.
    """
    created: list[NetClient] = []

    def factory(
        *,
        allowlist: Sequence[str] = ALLOWLIST,
        probe: str | None = PROBE,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
        max_backoff_s: float = 30.0,
        max_redirects: int = 5,
        with_cache: bool = True,
    ) -> NetClient:
        limiter_clock = FakeClock()
        client = NetClient(
            client=httpx.Client(timeout=1.0),
            rate_limiter=RateLimiter(
                default_rps=1_000.0,
                default_burst=1_000,
                monotonic=limiter_clock.monotonic,
                sleep=limiter_clock.sleep,
            ),
            cache=ToolCache(conn) if with_cache else None,
            probe=probe,
            allowlist=allowlist,
            max_retries=max_retries,
            backoff_base_s=backoff_base_s,
            max_backoff_s=max_backoff_s,
            max_redirects=max_redirects,
            sleep=sleeps,
            rng=random.Random(1234),
        )
        created.append(client)
        return client

    try:
        yield factory
    finally:
        for client in created:
            client.close()


# ---------------------------------------------------------------------------
# TokenBucket — burst accounting and refill timing on a fake clock
# ---------------------------------------------------------------------------


def test_token_bucket_allows_burst_acquires_without_sleeping(clock: FakeClock) -> None:
    bucket = TokenBucket(1.0, 3, monotonic=clock.monotonic, sleep=clock.sleep)

    for _ in range(3):
        bucket.acquire()

    assert clock.sleeps == []
    assert bucket.tokens == pytest.approx(0.0)


def test_token_bucket_sleeps_once_the_burst_is_exhausted(clock: FakeClock) -> None:
    bucket = TokenBucket(1.0, 3, monotonic=clock.monotonic, sleep=clock.sleep)

    for _ in range(3):
        bucket.acquire()
    assert clock.sleeps == []

    bucket.acquire()

    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx(1.0)


def test_token_bucket_refill_wait_is_exactly_one_over_the_rate(clock: FakeClock) -> None:
    bucket = TokenBucket(2.0, 1, monotonic=clock.monotonic, sleep=clock.sleep)

    bucket.acquire()  # spends the single burst token
    bucket.acquire()  # must wait for exactly one token to refill at 2/s

    assert clock.sleeps == [pytest.approx(0.5)]


def test_token_bucket_never_accumulates_more_tokens_than_the_burst(clock: FakeClock) -> None:
    """Idle time must not bank credit: capacity is a hard ceiling, not a rate."""
    bucket = TokenBucket(5.0, 3, monotonic=clock.monotonic, sleep=clock.sleep)

    clock.advance(10_000.0)  # 50,000 tokens' worth of idling at 5/s

    for _ in range(3):
        bucket.acquire()
    assert clock.sleeps == [], "burst-many acquires must still be free after a long idle"

    bucket.acquire()

    assert len(clock.sleeps) == 1, "the burst+1'th acquire must still have to wait"
    assert clock.sleeps[0] == pytest.approx(1.0 / 5.0)


def test_token_bucket_rejects_burst_zero_instead_of_hanging_forever() -> None:
    """burst=0 leaves a bucket that can never yield a token: reject, do not hang."""
    with pytest.raises(ValueError, match="burst must be >= 1"):
        TokenBucket(1.0, 0)


def test_token_bucket_rejects_negative_burst_instead_of_hanging_forever() -> None:
    with pytest.raises(ValueError, match="burst must be >= 1"):
        TokenBucket(1.0, -1)


def test_token_bucket_rejects_zero_rate_instead_of_spinning_forever() -> None:
    """rps=0 makes the refill wait infinite/undefined: reject, do not spin."""
    with pytest.raises(ValueError, match="requests_per_second must be > 0"):
        TokenBucket(0.0, 1)


def test_token_bucket_rejects_negative_rate_instead_of_spinning_forever() -> None:
    with pytest.raises(ValueError, match="requests_per_second must be > 0"):
        TokenBucket(-1.0, 1)


def test_token_bucket_lock_prevents_concurrent_double_spend() -> None:
    """Concurrent probes must not both see the last token and take it.

    Uses the non-blocking `_try_acquire` directly and a rate low enough that
    no measurable refill happens during the test, so the assertion is about
    the lock alone and the test never sleeps. Threads are released together
    from a barrier to maximise the interleaving that a missing lock would
    lose tokens to.
    """
    threads_count = 16
    burst = 4
    bucket = TokenBucket(1e-9, burst)
    barrier = threading.Barrier(threads_count)
    granted: list[bool] = []
    granted_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        got = bucket._try_acquire() == 0.0
        with granted_lock:
            granted.append(got)

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(granted) == burst
    assert len(granted) == threads_count


# ---------------------------------------------------------------------------
# RateLimiter — per-host isolation and overrides
# ---------------------------------------------------------------------------


def test_rate_limiter_gives_each_host_an_independent_bucket(clock: FakeClock) -> None:
    """Throttling one host must never make an unrelated host wait."""
    limiter = RateLimiter(
        default_rps=1.0, default_burst=1, monotonic=clock.monotonic, sleep=clock.sleep
    )

    limiter.acquire("a.example.com")  # spends host A's only token
    limiter.acquire("b.example.com")  # host B has its own, untouched bucket

    assert clock.sleeps == [], "host B must not pay for host A's exhausted bucket"

    limiter.acquire("a.example.com")

    assert clock.sleeps == [pytest.approx(1.0)], "host A alone must be throttled"


def test_rate_limiter_configure_host_overrides_the_default_bucket(clock: FakeClock) -> None:
    limiter = RateLimiter(
        default_rps=1.0, default_burst=1, monotonic=clock.monotonic, sleep=clock.sleep
    )
    limiter.configure_host("fast.example.com", 100.0, 5)

    for _ in range(5):
        limiter.acquire("fast.example.com")
    assert clock.sleeps == [], "the override's burst of 5 must be honoured"

    limiter.acquire("slow.example.com")
    limiter.acquire("slow.example.com")

    assert clock.sleeps == [pytest.approx(1.0)], "the unlisted host keeps the default bucket"


def test_rate_limiter_matches_configured_hosts_case_insensitively(clock: FakeClock) -> None:
    """A host override must key off the same normalization `acquire` uses."""
    limiter = RateLimiter(
        default_rps=1.0, default_burst=1, monotonic=clock.monotonic, sleep=clock.sleep
    )
    limiter.configure_host("FAST.Example.COM.", 100.0, 5)

    for _ in range(5):
        limiter.acquire("fast.example.com")

    assert clock.sleeps == []


# ---------------------------------------------------------------------------
# check_allowed — accepted URLs
# ---------------------------------------------------------------------------


def test_check_allowed_accepts_an_exact_host_match_and_returns_the_host() -> None:
    assert check_allowed(ALLOWED_URL, [ALLOWED_HOST]) == ALLOWED_HOST


def test_check_allowed_matches_an_uppercase_host_against_a_lowercase_entry() -> None:
    assert check_allowed("https://BOARDS.Greenhouse.IO/x", [ALLOWED_HOST]) == ALLOWED_HOST


def test_check_allowed_matches_a_fully_qualified_trailing_dot_host() -> None:
    """`boards.greenhouse.io.` resolves identically, so it must not bypass the list."""
    assert check_allowed("https://boards.greenhouse.io./x", [ALLOWED_HOST]) == ALLOWED_HOST


def test_check_allowed_accepts_an_ordinary_https_host_under_the_star_allowlist() -> None:
    assert check_allowed("https://careers.example.com/jobs/1", ["*"]) == "careers.example.com"


# ---------------------------------------------------------------------------
# check_allowed — one test per rejection reason
# ---------------------------------------------------------------------------


def test_check_allowed_rejects_a_host_that_only_shares_a_suffix_with_an_entry() -> None:
    """`evilboards.greenhouse.io` is a different registrable host, not a subdomain."""
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://evilboards.greenhouse.io/x", [ALLOWED_HOST])

    assert exc.value.reason == "host is not in the probe's allowlist"
    assert exc.value.host == "evilboards.greenhouse.io"


def test_check_allowed_rejects_an_entry_used_as_a_prefix_of_an_attacker_domain() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://boards.greenhouse.io.attacker.com/x", [ALLOWED_HOST])

    assert exc.value.reason == "host is not in the probe's allowlist"
    assert exc.value.host == "boards.greenhouse.io.attacker.com"


def test_check_allowed_rejects_userinfo_disguising_the_real_host() -> None:
    """The real host here is `evil.example.com`; the allowlisted name is userinfo."""
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://boards.greenhouse.io@evil.example.com/x", [ALLOWED_HOST])

    assert exc.value.reason == "URL carries userinfo"


def test_check_allowed_rejects_userinfo_even_in_front_of_an_allowed_host() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed(f"https://user:pass@{ALLOWED_HOST}/x", [ALLOWED_HOST])

    assert exc.value.reason == "URL carries userinfo"


def test_check_allowed_rejects_plain_http_even_for_an_allowlisted_host() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed(f"http://{ALLOWED_HOST}/x", [ALLOWED_HOST])

    assert exc.value.reason == "scheme 'http' is not https"


def test_check_allowed_rejects_an_ipv4_literal_under_the_star_allowlist() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://10.0.0.5/x", ["*"])

    assert "IP literal" in exc.value.reason


def test_check_allowed_rejects_an_ipv6_literal_under_the_star_allowlist() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://[::1]/x", ["*"])

    assert "IP literal" in exc.value.reason


def test_check_allowed_rejects_the_cloud_metadata_address_under_the_star_allowlist() -> None:
    """`"*"` must not be an SSRF hole: 169.254.169.254 is the canonical target."""
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://169.254.169.254/latest/meta-data/", ["*"])

    assert "IP literal" in exc.value.reason


def test_check_allowed_rejects_an_ip_literal_even_when_it_is_explicitly_listed() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://127.0.0.1/x", ["127.0.0.1"])

    assert "IP literal" in exc.value.reason


def test_check_allowed_rejects_localhost_under_the_star_allowlist() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://localhost/x", ["*"])

    assert exc.value.reason == "host is not publicly routable"


def test_check_allowed_rejects_a_single_label_internal_name_under_the_star_allowlist() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://metadata/computeMetadata/v1/", ["*"])

    assert exc.value.reason == "host is not publicly routable"


def test_check_allowed_rejects_a_non_routable_suffix_under_the_star_allowlist() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://db.internal/x", ["*"])

    assert exc.value.reason == "host is not publicly routable"


def test_check_allowed_rejects_a_url_with_no_host() -> None:
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https:///jobs/1", ["*"])

    assert exc.value.reason == "URL has no host"


def test_check_allowed_rejects_a_syntactically_impossible_hostname() -> None:
    """A host with an empty label only arrives from a hostile `Location` header."""
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed("https://boards..greenhouse.io/x", ["*"])

    assert exc.value.reason == "host is not a syntactically valid hostname"


def test_check_allowed_rejects_an_empty_allowlist_for_an_unimplemented_probe() -> None:
    """An empty list is the fail-closed state: no host at all may be contacted."""
    with pytest.raises(DisallowedHostError) as exc:
        check_allowed(ALLOWED_URL, [])

    assert exc.value.reason == "host is not in the probe's allowlist"


# ---------------------------------------------------------------------------
# hash_args — canonical, order-insensitive cache keys
# ---------------------------------------------------------------------------


def test_hash_args_is_insensitive_to_keyword_order() -> None:
    assert hash_args("p", a=1, b=2) == hash_args("p", b=2, a=1)


def test_hash_args_changes_when_any_argument_value_changes() -> None:
    assert hash_args("p", a=1, b=2) != hash_args("p", a=1, b=3)


def test_hash_args_changes_when_the_probe_changes() -> None:
    assert hash_args("p", a=1) != hash_args("q", a=1)


def test_hash_args_distinguishes_a_missing_argument_from_a_null_one() -> None:
    assert hash_args("p", a=1) != hash_args("p", a=1, b=None)


# ---------------------------------------------------------------------------
# ToolCache — against a real database built from schema.sql
# ---------------------------------------------------------------------------


def test_tool_cache_round_trips_a_payload(cache: ToolCache) -> None:
    key = hash_args(PROBE, url=ALLOWED_URL)
    cache.set(PROBE, key, {"status": 200, "body": "hello"}, 3600)

    assert cache.get(PROBE, key) == {"status": 200, "body": "hello"}


def test_tool_cache_miss_for_an_unknown_key(cache: ToolCache) -> None:
    assert cache.get(PROBE, hash_args(PROBE, url="https://nope.example.com/")) is None


def test_tool_cache_treats_an_already_expired_entry_as_a_miss(cache: ToolCache) -> None:
    key = hash_args(PROBE, url=ALLOWED_URL)
    cache.set(PROBE, key, {"status": 200, "body": "stale"}, -1.0)

    assert cache.get(PROBE, key) is None


def test_tool_cache_expiry_is_evaluated_against_the_supplied_now(cache: ToolCache) -> None:
    fetched_at = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)
    key = hash_args(PROBE, url=ALLOWED_URL)
    cache.set(PROBE, key, {"status": 200, "body": "ok"}, 60.0, fetched_at=fetched_at)

    assert cache.get(PROBE, key, now=fetched_at + timedelta(seconds=30)) is not None
    assert cache.get(PROBE, key, now=fetched_at + timedelta(days=365)) is None


def test_tool_cache_treats_a_malformed_expires_at_as_a_miss_not_an_exception(
    conn: sqlite3.Connection, cache: ToolCache
) -> None:
    """A corrupt row must degrade into a re-fetch, never fail the run."""
    key = hash_args(PROBE, url=ALLOWED_URL)
    conn.execute(
        """
        INSERT INTO tool_cache (probe, args_hash, fetched_at, response, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            PROBE,
            key,
            to_utc_z(datetime(2026, 1, 1, tzinfo=UTC)),
            '{"body": "x"}',
            "not-a-timestamp",
        ),
    )
    conn.commit()

    assert cache.get(PROBE, key) is None


def test_tool_cache_treats_a_malformed_response_json_as_a_miss_not_an_exception(
    conn: sqlite3.Connection, cache: ToolCache
) -> None:
    key = hash_args(PROBE, url=ALLOWED_URL)
    conn.execute(
        """
        INSERT INTO tool_cache (probe, args_hash, fetched_at, response, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            PROBE,
            key,
            to_utc_z(datetime(2026, 1, 1, tzinfo=UTC)),
            "{not valid json",
            to_utc_z(datetime(2099, 1, 1, tzinfo=UTC)),
        ),
    )
    conn.commit()

    assert cache.get(PROBE, key) is None


def test_tool_cache_treats_a_non_object_response_as_a_miss(
    conn: sqlite3.Connection, cache: ToolCache
) -> None:
    """Valid JSON that is not an object cannot be a `NetResult` payload."""
    key = hash_args(PROBE, url=ALLOWED_URL)
    conn.execute(
        """
        INSERT INTO tool_cache (probe, args_hash, fetched_at, response, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            PROBE,
            key,
            to_utc_z(datetime(2026, 1, 1, tzinfo=UTC)),
            "[1, 2, 3]",
            to_utc_z(datetime(2099, 1, 1, tzinfo=UTC)),
        ),
    )
    conn.commit()

    assert cache.get(PROBE, key) is None


def test_tool_cache_appends_rather_than_overwriting_and_reads_the_newest(
    conn: sqlite3.Connection, cache: ToolCache
) -> None:
    """Two writes for one key leave two rows; `get` returns the newer payload.

    The row count is the assertion that matters: an in-place overwrite would
    still return the newer payload while destroying the history that
    point-in-time replay depends on (spec.md §3/§6).
    """
    key = hash_args(PROBE, url=ALLOWED_URL)
    older = datetime(2026, 1, 1, 12, 0, 0, 100000, tzinfo=UTC)
    newer = datetime(2026, 1, 1, 12, 0, 30, 200000, tzinfo=UTC)

    cache.set(PROBE, key, {"body": "older"}, 3600.0, fetched_at=older)
    cache.set(PROBE, key, {"body": "newer"}, 3600.0, fetched_at=newer)

    assert tool_cache_row_count(conn) == 2, "the cache must be append-only"
    assert cache.get(PROBE, key, now=newer + timedelta(seconds=1)) == {"body": "newer"}


def test_tool_cache_scans_past_an_expired_newer_row_to_a_live_older_one(
    cache: ToolCache,
) -> None:
    """Per-call TTLs mean an older row can legitimately outlive a newer one."""
    key = hash_args(PROBE, url=ALLOWED_URL)
    older = datetime(2026, 1, 1, 12, 0, 0, 100000, tzinfo=UTC)
    newer = datetime(2026, 1, 1, 12, 0, 30, 200000, tzinfo=UTC)

    cache.set(PROBE, key, {"body": "long-lived"}, 86_400.0, fetched_at=older)
    cache.set(PROBE, key, {"body": "short-lived"}, 1.0, fetched_at=newer)

    assert cache.get(PROBE, key, now=newer + timedelta(seconds=10)) == {"body": "long-lived"}


def test_tool_cache_isolates_entries_by_probe_name(cache: ToolCache) -> None:
    key = hash_args(PROBE, url=ALLOWED_URL)
    cache.set(PROBE, key, {"body": "for-resolve"}, 3600.0)

    assert cache.get("board_snapshot", key) is None


def test_tool_cache_orders_rows_chronologically_when_one_lands_on_a_whole_second(
    cache: ToolCache,
) -> None:
    """Regression: `ORDER BY fetched_at DESC` must not return the older row.

    `datetime.isoformat()` drops the fractional part on a whole second, so a
    naive serializer produces "...T12:00:00Z", which sorts lexically AFTER
    "...T12:00:00.500000Z" because "Z" > ".". `to_utc_z` pins a fixed-width
    6-digit fraction to keep TEXT ordering chronological.
    """
    key = hash_args(PROBE, url=ALLOWED_URL)
    older = datetime(2026, 1, 1, 12, 0, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 1, 1, 12, 0, 0, 500000, tzinfo=UTC)

    cache.set(PROBE, key, {"body": "older"}, 3600.0, fetched_at=older)
    cache.set(PROBE, key, {"body": "newer"}, 3600.0, fetched_at=newer)

    assert cache.get(PROBE, key, now=newer + timedelta(seconds=1)) == {"body": "newer"}


# ---------------------------------------------------------------------------
# NetClient — retries and structured failure
# ---------------------------------------------------------------------------


@respx.mock
def test_net_client_retries_a_429_and_returns_the_following_success(
    make_client: Callable[..., NetClient],
) -> None:
    route = respx.get(ALLOWED_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, text="job body")]
    )
    client = make_client()

    result = client.get(ALLOWED_URL)

    assert route.call_count == 2
    assert result.ok is True
    assert result.status == 200
    assert result.body == "job body"
    assert result.error is None


@respx.mock
def test_net_client_bounds_retries_of_a_persistent_503(
    make_client: Callable[..., NetClient], sleeps: SleepRecorder
) -> None:
    """max_retries=2 means exactly 3 attempts — no uncontrolled retry loop."""
    route = respx.get(ALLOWED_URL).mock(return_value=httpx.Response(503, text="down"))
    client = make_client(max_retries=2)

    result = client.get(ALLOWED_URL)

    assert route.call_count == 3
    assert len(sleeps.calls) == 2, "one backoff per retry, none after the last attempt"
    assert result.ok is False
    assert result.retryable is True
    assert result.status == 503
    assert "503" in (result.error or "")


@respx.mock
def test_net_client_does_not_retry_or_cache_a_404(
    make_client: Callable[..., NetClient], conn: sqlite3.Connection, sleeps: SleepRecorder
) -> None:
    """A 4xx is a definitive answer about this URL: one call, not retryable, not cached."""
    route = respx.get(ALLOWED_URL).mock(return_value=httpx.Response(404, text="gone"))
    client = make_client()

    result = client.get(ALLOWED_URL)

    assert route.call_count == 1
    assert sleeps.calls == []
    assert result.ok is False
    assert result.retryable is False
    assert result.status == 404
    assert tool_cache_row_count(conn) == 0, "a 4xx must never be pinned into the cache"


@respx.mock
def test_net_client_does_not_cache_a_retryable_failure(
    make_client: Callable[..., NetClient], conn: sqlite3.Connection
) -> None:
    """Caching a 503 would pin a host's outage into the evidence record."""
    respx.get(ALLOWED_URL).mock(return_value=httpx.Response(503))
    client = make_client(max_retries=1)

    client.get(ALLOWED_URL)

    assert tool_cache_row_count(conn) == 0


@respx.mock
def test_net_client_returns_structured_failure_for_an_exhausted_transport_error(
    make_client: Callable[..., NetClient],
) -> None:
    """A connect failure must surface as `{ok:false, retryable:true}`, not an exception."""
    route = respx.get(ALLOWED_URL).mock(side_effect=httpx.ConnectError("boom"))
    client = make_client(max_retries=2)

    result = client.get(ALLOWED_URL)

    assert route.call_count == 3
    assert result.ok is False
    assert result.retryable is True
    assert result.status is None
    assert result.body is None
    assert "ConnectError" in (result.error or "")


@respx.mock
def test_net_client_raises_for_a_disallowed_initial_url_without_any_request(
    make_client: Callable[..., NetClient],
) -> None:
    """A probe fetching a host its own allowlist forbids is a programming error."""
    route = respx.get(DISALLOWED_URL).mock(return_value=httpx.Response(200, text="pwned"))
    client = make_client()

    with pytest.raises(DisallowedHostError) as exc:
        client.get(DISALLOWED_URL)

    assert exc.value.reason == "host is not in the probe's allowlist"
    assert route.call_count == 0
    assert respx.calls.call_count == 0


def test_net_client_requires_a_probe_to_be_bound_or_passed(
    make_client: Callable[..., NetClient],
) -> None:
    client = make_client(probe=None)

    with pytest.raises(ValueError, match="no probe bound"):
        client.get(ALLOWED_URL)


# ---------------------------------------------------------------------------
# NetClient — manual, allowlist-checked redirects
# ---------------------------------------------------------------------------


@respx.mock
def test_net_client_follows_a_redirect_to_an_allowed_host_and_reports_the_final_url(
    make_client: Callable[..., NetClient],
) -> None:
    first = respx.get(ALLOWED_URL).mock(
        return_value=httpx.Response(302, headers={"Location": SECOND_ALLOWED_URL})
    )
    second = respx.get(SECOND_ALLOWED_URL).mock(return_value=httpx.Response(200, text="final body"))
    client = make_client()

    result = client.get(ALLOWED_URL)

    assert first.call_count == 1
    assert second.call_count == 1
    assert result.ok is True
    assert result.body == "final body"
    assert result.url == SECOND_ALLOWED_URL, "evidence must cite the URL the body came from"


@respx.mock
def test_net_client_refuses_a_redirect_off_the_allowlist_without_requesting_it(
    make_client: Callable[..., NetClient],
) -> None:
    """A hostile `Location` is remote input: structured failure, and never fetched."""
    first = respx.get(ALLOWED_URL).mock(
        return_value=httpx.Response(302, headers={"Location": DISALLOWED_URL})
    )
    evil = respx.get(DISALLOWED_URL).mock(return_value=httpx.Response(200, text="pwned"))
    client = make_client()

    result = client.get(ALLOWED_URL)

    assert first.call_count == 1
    assert evil.call_count == 0, "the disallowed hop must never reach the network"
    assert result.ok is False
    assert result.retryable is False
    assert "redirect" in (result.error or "").lower()
    assert DISALLOWED_URL in (result.error or "")


@respx.mock
def test_net_client_refuses_a_redirect_downgrading_to_http(
    make_client: Callable[..., NetClient],
) -> None:
    http_url = f"http://{ALLOWED_HOST}/acme/jobs/1"
    respx.get(ALLOWED_URL).mock(return_value=httpx.Response(301, headers={"Location": http_url}))
    downgraded = respx.get(http_url).mock(return_value=httpx.Response(200, text="cleartext"))
    client = make_client()

    result = client.get(ALLOWED_URL)

    assert downgraded.call_count == 0
    assert result.ok is False
    assert result.retryable is False
    assert "not https" in (result.error or "")


@respx.mock
def test_net_client_terminates_a_redirect_loop_instead_of_hanging(
    make_client: Callable[..., NetClient],
) -> None:
    route = respx.get(ALLOWED_URL).mock(
        return_value=httpx.Response(302, headers={"Location": ALLOWED_URL})
    )
    client = make_client(max_redirects=2)

    result = client.get(ALLOWED_URL)

    assert route.call_count == 3, "max_redirects=2 permits the initial hop plus 2 more"
    assert result.ok is False
    assert result.retryable is False
    assert "max_redirects=2" in (result.error or "")


@respx.mock
def test_net_client_resolves_a_relative_redirect_against_the_current_url(
    make_client: Callable[..., NetClient],
) -> None:
    """RFC 9110 permits a relative `Location`; it must resolve on the current hop."""
    respx.get(ALLOWED_URL).mock(return_value=httpx.Response(302, headers={"Location": "/acme/2"}))
    target = respx.get(f"https://{ALLOWED_HOST}/acme/2").mock(
        return_value=httpx.Response(200, text="relative target")
    )
    client = make_client()

    result = client.get(ALLOWED_URL)

    assert target.call_count == 1
    assert result.ok is True
    assert result.url == f"https://{ALLOWED_HOST}/acme/2"


# ---------------------------------------------------------------------------
# NetClient — backoff and Retry-After
# ---------------------------------------------------------------------------


@respx.mock
def test_net_client_honours_a_retry_after_header_verbatim_over_jitter(
    make_client: Callable[..., NetClient], sleeps: SleepRecorder
) -> None:
    respx.get(ALLOWED_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, text="ok"),
        ]
    )
    client = make_client(max_retries=1, backoff_base_s=0.5, max_backoff_s=30.0)

    result = client.get(ALLOWED_URL)

    assert result.ok is True
    assert sleeps.calls == [pytest.approx(2.0)], "the server's delay must win over jitter"


@respx.mock
def test_net_client_clamps_a_retry_after_larger_than_max_backoff(
    make_client: Callable[..., NetClient], sleeps: SleepRecorder
) -> None:
    """A remote host must not be able to park a run past its latency budget."""
    respx.get(ALLOWED_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(200, text="ok"),
        ]
    )
    client = make_client(max_retries=1, backoff_base_s=0.5, max_backoff_s=5.0)

    client.get(ALLOWED_URL)

    assert sleeps.calls == [pytest.approx(5.0)]


@respx.mock
def test_net_client_ignores_a_nonsense_retry_after_and_falls_back_to_jitter(
    make_client: Callable[..., NetClient], sleeps: SleepRecorder
) -> None:
    respx.get(ALLOWED_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}),
            httpx.Response(200, text="ok"),
        ]
    )
    client = make_client(max_retries=1, backoff_base_s=0.5, max_backoff_s=30.0)

    client.get(ALLOWED_URL)

    assert len(sleeps.calls) == 1
    assert 0.0 <= sleeps.calls[0] <= 0.5, "full jitter over the attempt-0 ceiling"


@respx.mock
def test_net_client_backoff_never_exceeds_max_backoff_across_all_retries(
    make_client: Callable[..., NetClient], sleeps: SleepRecorder
) -> None:
    respx.get(ALLOWED_URL).mock(return_value=httpx.Response(500))
    client = make_client(max_retries=6, backoff_base_s=1.0, max_backoff_s=4.0)

    client.get(ALLOWED_URL)

    assert len(sleeps.calls) == 6
    assert all(0.0 <= delay <= 4.0 for delay in sleeps.calls), sleeps.calls


# ---------------------------------------------------------------------------
# NetClient — tool cache integration
# ---------------------------------------------------------------------------


@respx.mock
def test_net_client_second_get_is_served_from_cache_without_a_network_call(
    make_client: Callable[..., NetClient],
) -> None:
    route = respx.get(ALLOWED_URL).mock(return_value=httpx.Response(200, text="cached body"))
    client = make_client()

    first = client.get(ALLOWED_URL)
    second = client.get(ALLOWED_URL)

    assert route.call_count == 1, "the second get must not touch the network"
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.body == "cached body"
    assert second.status == 200


@respx.mock
def test_net_client_cache_hit_preserves_the_original_fetched_at(
    make_client: Callable[..., NetClient],
) -> None:
    """Caching must not move evidence on the replay timeline (spec.md §3)."""
    respx.get(ALLOWED_URL).mock(return_value=httpx.Response(200, text="body"))
    client = make_client()

    first = client.get(ALLOWED_URL)
    second = client.get(ALLOWED_URL)

    assert second.fetched_at == first.fetched_at


@respx.mock
def test_net_client_cache_hit_preserves_the_final_redirected_url(
    make_client: Callable[..., NetClient],
) -> None:
    respx.get(ALLOWED_URL).mock(
        return_value=httpx.Response(302, headers={"Location": SECOND_ALLOWED_URL})
    )
    respx.get(SECOND_ALLOWED_URL).mock(return_value=httpx.Response(200, text="final"))
    client = make_client()

    first = client.get(ALLOWED_URL)
    second = client.get(ALLOWED_URL)

    assert second.from_cache is True
    assert second.url == first.url == SECOND_ALLOWED_URL


@respx.mock
def test_net_client_use_cache_false_bypasses_both_read_and_write(
    make_client: Callable[..., NetClient], conn: sqlite3.Connection
) -> None:
    route = respx.get(ALLOWED_URL).mock(return_value=httpx.Response(200, text="body"))
    client = make_client()

    client.get(ALLOWED_URL, use_cache=False)
    client.get(ALLOWED_URL, use_cache=False)

    assert route.call_count == 2
    assert tool_cache_row_count(conn) == 0


@respx.mock
def test_net_client_cache_key_distinguishes_different_query_params(
    make_client: Callable[..., NetClient],
) -> None:
    """Two calls differing only in `params` are different cache entries."""
    route = respx.get(ALLOWED_URL).mock(return_value=httpx.Response(200, text="body"))
    client = make_client()

    client.get(ALLOWED_URL, params={"page": "1"})
    client.get(ALLOWED_URL, params={"page": "2"})

    assert route.call_count == 2


@respx.mock
def test_net_client_expired_cache_entry_triggers_a_refetch(
    make_client: Callable[..., NetClient],
) -> None:
    route = respx.get(ALLOWED_URL).mock(return_value=httpx.Response(200, text="body"))
    client = make_client()

    client.get(ALLOWED_URL, ttl_seconds=-1.0)
    second = client.get(ALLOWED_URL, ttl_seconds=-1.0)

    assert route.call_count == 2
    assert second.from_cache is False


@respx.mock
def test_net_client_writes_one_append_only_row_per_successful_fetch(
    make_client: Callable[..., NetClient], conn: sqlite3.Connection
) -> None:
    respx.get(ALLOWED_URL).mock(return_value=httpx.Response(200, text="body"))
    client = make_client()

    client.get(ALLOWED_URL, ttl_seconds=-1.0)
    client.get(ALLOWED_URL, ttl_seconds=-1.0)

    assert tool_cache_row_count(conn) == 2
    stored = conn.execute("SELECT response FROM tool_cache ORDER BY fetched_at").fetchall()
    assert json.loads(stored[0][0])["body"] == "body"


# ---------------------------------------------------------------------------
# NetClient.from_config — binding a probe to the real config.toml allowlist
# ---------------------------------------------------------------------------


@respx.mock
def test_from_config_binds_the_named_probes_allowlist(sleeps: SleepRecorder) -> None:
    api_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"
    route = respx.get(api_url).mock(return_value=httpx.Response(200, text="{}"))
    cfg = load_config()

    with NetClient.from_config(
        cfg, probe=PROBE, client=httpx.Client(timeout=1.0), sleep=sleeps
    ) as client:
        result = client.get(api_url)
        assert result.ok is True
        assert route.call_count == 1

        with pytest.raises(DisallowedHostError) as exc:
            client.get("https://unrelated-host.example.com/jobs/1")

    assert exc.value.reason == "host is not in the probe's allowlist"


def test_from_config_rejects_an_unknown_probe_name() -> None:
    cfg = load_config()

    with pytest.raises(ValueError, match="unknown probe 'nope'"):
        NetClient.from_config(cfg, probe="nope")


def test_from_config_uses_the_configured_net_knobs() -> None:
    cfg = load_config()

    client = NetClient.from_config(cfg, probe=PROBE, client=httpx.Client(timeout=1.0))
    try:
        assert client._max_retries == cfg.net.max_retries
        assert client._max_backoff_s == cfg.net.max_backoff_s
        assert client._max_redirects == cfg.net.max_redirects
    finally:
        client.close()


def test_from_config_threads_the_injected_clock_into_the_rate_limiter(
    sleeps: SleepRecorder,
) -> None:
    """Regression: an injected clock must reach the limiter, not just backoff.

    `from_config` previously built `RateLimiter.from_config(cfg)` with no
    clock, so a "fully injected" client still blocked on the real
    `time.sleep` the first time a host bucket ran dry (web.archive.org is
    configured at 0.5 rps / burst 2, i.e. a ~2s real wait on the third
    request). Asserting on the wiring is deliberate: the behavioural
    alternative is to actually exhaust a bucket, which is the multi-second
    wall-clock wait this test exists to prevent.
    """
    cfg = load_config()
    clock = FakeClock(start=4321.0)

    client = NetClient.from_config(
        cfg,
        probe=PROBE,
        client=httpx.Client(timeout=1.0),
        monotonic=clock.monotonic,
        sleep=sleeps,
    )
    try:
        assert client._rate_limiter._sleep is sleeps
        # A bound method is a fresh object per attribute access, so compare
        # behaviour rather than identity.
        assert client._rate_limiter._monotonic() == 4321.0
    finally:
        client.close()
