"""In-memory per-client token-bucket rate limiter for the API process.

Deliberately not distributed / not persisted: this is a single-process,
single-machine tool (see rli/api/settings.py's docstring for why process
config lives outside rli.config.Config), and the goal is to blunt runaway
or accidental request storms against /investigate and /watch/due (each of
which can trigger outbound network + LLM spend), not to defend a multi-
instance deployment.

Bucket semantics: capacity == requests-per-minute, refilled continuously,
and a key that has never been seen starts full — so the first burst of up
to `requests_per_minute` requests succeeds and the next one is refused.
A `requests_per_minute` of 0 refuses everything, which is the only sane
reading of "zero requests per minute".
"""

from __future__ import annotations

import threading
import time

# A key idle for a full refill window has a full bucket, i.e. it is
# indistinguishable from a key that has never been seen. Dropping such keys
# is therefore behaviour-preserving, and bounds the dict's growth: without
# it, one entry per distinct client host would be retained for the life of
# the process.
_PRUNE_THRESHOLD = 1024


class RateLimiter:
    """Thread-safe token bucket per key (the client host, in practice)."""

    def __init__(self, requests_per_minute: int) -> None:
        self._capacity = float(max(requests_per_minute, 0))
        self._refill_per_second = self._capacity / 60.0
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        """Consume one token for `key`; return whether the request may proceed."""
        if self._capacity <= 0:
            return False
        with self._lock:
            now = time.monotonic()
            if len(self._tokens) >= _PRUNE_THRESHOLD:
                self._prune(now)
            tokens = self._tokens.get(key, self._capacity)
            last = self._last.get(key, now)
            tokens = min(self._capacity, tokens + (now - last) * self._refill_per_second)
            if tokens >= 1.0:
                tokens -= 1.0
                allowed = True
            else:
                allowed = False
            self._tokens[key] = tokens
            self._last[key] = now
            return allowed

    def _prune(self, now: float) -> None:
        """Drop fully-refilled (idle) keys. Caller must hold `self._lock`."""
        stale = [key for key, last in self._last.items() if now - last >= 60.0]
        for key in stale:
            self._tokens.pop(key, None)
            self._last.pop(key, None)
