"""Wayback Machine CDX Server API client (PLAN.md M1 bullet 4).

GUESSED response shape (NOT verified against a live call at the time this
module was written — built from the official CDX Server API documentation,
https://github.com/internetarchive/wayback/blob/master/wayback-cdx-server/README.md,
and the informal spot-check in `scripts/wayback_spotcheck.py`, but not
exercised against a real response in *this* change). Tests in
`tests/test_archive_cdx.py` assert against the documented shape below, not
against a live fixture — the same convention `rli/resolvers/greenhouse.py`,
`ashby.py` and `lever.py` already use for their own GUESSED adapters.

Request shape::

    GET https://web.archive.org/cdx/search/cdx
        ?url=<pattern>
        &output=json
        &filter=statuscode:200
        &from=<YYYYMMDD>
        &to=<YYYYMMDD>
        &collapse=digest          (optional — de-duplicates identical content)
        &limit=<N>
        &showResumeKey=true

Response shape: a JSON array of arrays. Row 0 is the field-name header
``["urlkey","timestamp","original","mimetype","statuscode","digest",
"length"]``; every subsequent row is one capture in the same column order.
When ``showResumeKey=true`` and the result was truncated at ``limit``, the
array ends with an empty row ``[]`` followed by a final one-element row
``["<resumeKey>"]`` — that value is passed back as ``resumeKey=<value>`` on
the next request to continue. A response with at most one row (header only,
or completely empty) means there are no more pages.

`list_captures` mirrors `rli.net.NetResult` / `rli.resolvers.common.
FetchResult`'s `ok`/`error`/`retryable` shape and NEVER raises for a
network or parse problem: a transport failure, retry exhaustion, non-2xx
response, or malformed JSON body all come back as
`CdxResult(ok=False, error=..., retryable=...)`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from rli.net import NetClient

__all__ = ["CdxCapture", "CdxResult", "list_captures"]

CDX_URL = "https://web.archive.org/cdx/search/cdx"

# Hard ceiling on pages fetched per `list_captures` call, independent of
# `limit`. A malformed or non-terminating resumeKey sequence from a hostile
# or broken server must not spin this loop forever.
MAX_PAGES = 50


@dataclass(frozen=True, slots=True)
class CdxCapture:
    """One parsed CDX data row."""

    timestamp: str  # 14-digit YYYYMMDDHHMMSS, UTC
    original: str  # the exact URL that was captured
    statuscode: str
    digest: str


@dataclass(frozen=True, slots=True)
class CdxResult:
    """Outcome of `list_captures`. Mirrors `rli.net.NetResult`'s ok/error/retryable shape."""

    ok: bool
    captures: list[CdxCapture]
    pattern: str
    error: str | None = None
    retryable: bool = False


def _date_str(value: str | date | datetime) -> str:
    """Normalize `from_`/`to` into the CDX API's `YYYYMMDD` form."""
    if isinstance(value, str):
        return value
    return value.strftime("%Y%m%d")


def _parse_rows(body: str) -> list[list[str]] | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return data


def _split_resume_key(data_rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Strip a trailing `[[], ["<key>"]]` handoff pair off `data_rows`.

    Returns `(resume_key_or_empty, remaining_rows)`. A lone trailing empty
    row with no following one-element row is also stripped (it signals "no
    more pages" without carrying a key).
    """
    if len(data_rows) >= 2 and data_rows[-2] == [] and len(data_rows[-1]) == 1:
        return [data_rows[-1][0]], data_rows[:-2]
    if data_rows and data_rows[-1] == []:
        return [], data_rows[:-1]
    return [], data_rows


def list_captures(
    net: NetClient,
    url_pattern: str,
    from_: str | date | datetime,
    to: str | date | datetime,
    *,
    filters: list[str] | None = None,
    collapse_digest: bool = True,
    limit: int = 1000,
) -> CdxResult:
    """List every capture of `url_pattern` between `from_` and `to` (inclusive).

    `from_`/`to` are `YYYYMMDD` strings, or `date`/`datetime` values (only
    the calendar date is significant to the CDX API, so a `datetime` is
    reduced via `strftime("%Y%m%d")`). Paginates internally via the
    `showResumeKey`/`resumeKey` handoff, returning the union of every page's
    rows in encounter order, or a structured failure (never a raised
    exception) on the first page that fails.

    `filters` defaults to `["statuscode:200"]` (only "usable" captures —
    every row returned is a 200). `collapse_digest=True` asks the CDX server
    to fold consecutive identical-content captures together, so this does
    not have to be done client-side.
    """
    params_base: dict[str, object] = {
        "url": url_pattern,
        "output": "json",
        "filter": filters if filters is not None else ["statuscode:200"],
        "from": _date_str(from_),
        "to": _date_str(to),
        "limit": str(limit),
        "showResumeKey": "true",
    }
    if collapse_digest:
        params_base["collapse"] = "digest"

    all_captures: list[CdxCapture] = []
    resume_key: str | None = None

    for _ in range(MAX_PAGES):
        params = dict(params_base)
        if resume_key:
            params["resumeKey"] = resume_key

        # httpx tolerates a list value for a repeated query param at runtime
        # even though NetClient.get's declared type is dict[str, str].
        result = net.get(CDX_URL, params=params)  # type: ignore[arg-type]
        if not result.ok:
            return CdxResult(
                ok=False,
                captures=all_captures,
                pattern=url_pattern,
                error=result.error,
                retryable=result.retryable,
            )

        rows = _parse_rows(result.body or "")
        if rows is None:
            return CdxResult(
                ok=False,
                captures=all_captures,
                pattern=url_pattern,
                error="CDX response body was not valid JSON",
                retryable=False,
            )

        if len(rows) <= 1:
            # Header only (or completely empty) — no data, no more pages.
            break

        data_rows = rows[1:]
        resume_keys, data_rows = _split_resume_key(data_rows)
        resume_key = resume_keys[0] if resume_keys else None

        for row in data_rows:
            if len(row) < 6:
                continue
            all_captures.append(
                CdxCapture(timestamp=row[1], original=row[2], statuscode=row[4], digest=row[5])
            )

        if not resume_key:
            break

    return CdxResult(ok=True, captures=all_captures, pattern=url_pattern)
