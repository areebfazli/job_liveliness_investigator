"""Lever Postings API adapter (spec.md §3/§10).

GUESSED response shape (NOT verified against a live API call — built from
the public schema documented at https://github.com/lever/postings-api, but
not exercised against a real response during this change):

    GET https://api.lever.co/v0/postings/{org}?mode=json
    ->
    [
      {
        "id": "...",
        "text": "...",                 # job title
        "categories": {"team": "...", "location": "...", "commitment": "..."},
        "hostedUrl": "https://jobs.lever.co/{org}/{id}",
        "createdAt": 1692000000000,    # epoch ms — UNDOCUMENTED, untrusted (spec.md §3)
        "descriptionPlain": "..."
      }
    ]

spec.md §3 is explicit that Lever's date fields are undocumented and must
NOT be trusted as ATS-native publish dates, so this adapter deliberately
exposes `created_at_ms` only as a raw, unparsed value for a `board_listing`
evidence item's `raw_excerpt` — never as a `first_published`/`ats_native`
claim (enforced in `rli.probes.resolve_posting`, not here).
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from rli.net import NetClient
from rli.resolvers.common import FetchResult, content_hash

__all__ = ["LeverJob", "fetch_board", "find_job"]


class LeverJob(BaseModel):
    """One Lever posting, from the public postings listing endpoint."""

    id: str
    text: str | None = None  # job title
    team: str | None = None
    location: str | None = None
    hosted_url: str | None = None
    created_at_ms: int | None = None  # UNTRUSTED (spec.md §3) — raw only
    description_plain: str | None = None

    @property
    def content_hash(self) -> str | None:
        return content_hash(self.description_plain)


def _parse_job(raw: dict) -> LeverJob | None:
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    categories = raw.get("categories")
    team = location = None
    if isinstance(categories, dict):
        team = categories.get("team")
        location = categories.get("location")
    created_at_ms = raw.get("createdAt")
    if not isinstance(created_at_ms, int):
        created_at_ms = None
    return LeverJob(
        id=str(raw["id"]),
        text=raw.get("text"),
        team=team,
        location=location,
        hosted_url=raw.get("hostedUrl"),
        created_at_ms=created_at_ms,
        description_plain=raw.get("descriptionPlain"),
    )


def fetch_board(net: NetClient, tenant: str) -> FetchResult[list[LeverJob]]:
    """Fetch the current postings list via `GET /v0/postings/{tenant}?mode=json`."""
    url = f"https://api.lever.co/v0/postings/{tenant}"
    result = net.get(url, params={"mode": "json"})
    if not result.ok:
        return FetchResult.from_failure(result)
    try:
        raw = json.loads(result.body or "")
    except ValueError:
        return FetchResult.from_success(result, None)
    if not isinstance(raw, list):
        return FetchResult.from_success(result, None)
    jobs = [job for job in (_parse_job(item) for item in raw) if job is not None]
    return FetchResult.from_success(result, jobs)


def find_job(jobs: list[LeverJob], job_id: str) -> LeverJob | None:
    for job in jobs:
        if job.id == str(job_id):
            return job
    return None
