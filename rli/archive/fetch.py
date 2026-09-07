"""Fetch one Wayback Machine capture body, unmodified (PLAN.md M1 bullet 4).

GUESSED URL shape (NOT verified against a live call at the time this module
was written — the `id_` raw-capture convention below is well known and
publicly documented by the Wayback Machine, but not exercised against a real
response in *this* change; same GUESSED convention as
`rli/resolvers/greenhouse.py` et al.)::

    GET https://web.archive.org/web/{timestamp}id_/{original_url}

The `id_` suffix on the timestamp segment returns the capture's ORIGINAL
bytes with no Wayback toolbar/link-rewriting injected, so an archived JSON
API body parses exactly like the live response would, and an archived HTML
page's `<a href>`s point at the site's own URLs rather than
Wayback-rewritten ones.

`fetch_capture` mirrors `rli.net.NetResult` / `rli.resolvers.common.
FetchResult`'s `ok`/`error`/`retryable` shape and NEVER raises for a
network/HTTP problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rli.net import NetClient, NetResult

__all__ = ["CaptureFetchResult", "capture_url", "fetch_capture"]


def capture_url(timestamp: str, original_url: str) -> str:
    """The raw (`id_`), unrewritten capture URL for one Wayback snapshot."""
    return f"https://web.archive.org/web/{timestamp}id_/{original_url}"


@dataclass(frozen=True, slots=True)
class CaptureFetchResult:
    """Outcome of one `fetch_capture` call. Mirrors `rli.net.NetResult`."""

    ok: bool
    status: int | None
    body: str | None
    url: str
    fetched_at: datetime
    error: str | None = None
    retryable: bool = False

    @classmethod
    def from_net_result(cls, result: NetResult) -> CaptureFetchResult:
        return cls(
            ok=result.ok,
            status=result.status,
            body=result.body,
            url=result.url,
            fetched_at=result.fetched_at,
            error=result.error,
            retryable=result.retryable,
        )


def fetch_capture(net: NetClient, timestamp: str, original_url: str) -> CaptureFetchResult:
    """Fetch one archived capture's raw body via the `id_` URL form.

    `timestamp` is the 14-digit `YYYYMMDDHHMMSS` Wayback timestamp and
    `original_url` is the exact URL that was captured (typically the
    `original` field of a `rli.archive.cdx.CdxCapture`). Never raises for a
    network/HTTP problem.
    """
    url = capture_url(timestamp, original_url)
    result = net.get(url)
    return CaptureFetchResult.from_net_result(result)
