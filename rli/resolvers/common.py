"""Shared plumbing for ATS/JSON-LD adapters (rli.resolvers.*).

Every adapter (`greenhouse`, `ashby`, `lever`, `jsonld`) fetches one URL via
`NetClient.get` and parses the body into an adapter-specific model. Both
steps can fail independently — the network call for the usual reasons
(`NetResult`), the parse because a remote API changed shape or a page's
JSON-LD is malformed — and neither failure may ever raise (spec.md §2:
"never raising for network problems"; adapters extend that to parsing too,
since a malformed remote body is exactly as untrusted as a failed request).
`FetchResult` gives both failure modes one structured shape so callers
(the probes) branch on `.ok` / `.data` rather than on exception types.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from rli.net import NetResult

__all__ = ["FetchResult", "content_hash", "normalize_domain", "parse_flexible_datetime"]


@dataclass(frozen=True, slots=True)
class FetchResult[T]:
    """Outcome of one adapter fetch+parse.

    Mirrors `NetResult`'s `ok` / `error` / `retryable` contract (spec.md §2)
    but carries parsed `data` instead of a raw body.

    * `ok=False` means the HTTP fetch itself failed (network error, or a
      terminal HTTP status such as 404/5xx) — `status`/`error`/`retryable`
      come straight from the underlying `NetResult`.
    * `ok=True, data=None` means the fetch succeeded (2xx) but the body could
      not be parsed into the expected shape. This is deliberately distinct
      from a transport/HTTP failure: a 200 with a malformed body is not
      retryable-the-same-way, and callers (resolve_posting) treat it as an
      inconclusive observation rather than a definitive "closed".
    """

    ok: bool
    status: int | None
    error: str | None
    retryable: bool
    url: str
    fetched_at: datetime
    data: T | None

    @classmethod
    def from_failure(cls, net_result: NetResult) -> FetchResult[T]:
        return cls(
            ok=False,
            status=net_result.status,
            error=net_result.error,
            retryable=net_result.retryable,
            url=net_result.url,
            fetched_at=net_result.fetched_at,
            data=None,
        )

    @classmethod
    def from_success(cls, net_result: NetResult, data: T | None) -> FetchResult[T]:
        return cls(
            ok=True,
            status=net_result.status,
            error=None,
            retryable=False,
            url=net_result.url,
            fetched_at=net_result.fetched_at,
            data=data,
        )


def content_hash(text: str | None) -> str | None:
    """Stable content hash for repost/version diffing (spec.md §4), or None."""
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_domain(url_or_domain: str | None) -> str | None:
    """Best-effort normalized website domain from a URL or bare domain.

    Used for the JSON-LD `hiringOrganization.sameAs`/`url` -> company website
    domain candidate (spec.md §3 "Identity": company_id is the normalized
    company website domain, resolved separately from the ATS tenant). Lower-
    cases the host and strips a leading `www.`; returns None for anything
    that does not look like a hostname.
    """
    if not url_or_domain:
        return None
    text = url_or_domain.strip()
    if "//" not in text:
        text = f"//{text}"
    host = urlparse(text).hostname
    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[len("www.") :]
    return host or None


def parse_flexible_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-ish timestamp (or bare date) into an aware UTC datetime.

    Handles the two shapes seen in the wild for adapter/JSON-LD dates:
    a full timestamp with a `Z` or numeric offset, and a bare calendar date
    (`"2026-08-20"`, as `JobPosting.datePosted` commonly is). A bare date
    carries no timezone information at all; treating it as UTC midnight is a
    documented simplification (GUESSED — not verified against a live API),
    not a claim that the underlying event happened at that exact instant.
    Returns None rather than raising for anything else.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None

    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
