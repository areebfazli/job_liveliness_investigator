"""Greenhouse Job Board API adapter (spec.md §3/§10).

GUESSED response shapes (NOT verified against a live API call — built from
the publicly documented field names in
https://developers.greenhouse.io/job-board.html and common knowledge of the
Greenhouse job-board JSON, but not exercised against a real response during
this change):

* job endpoint `GET /v1/boards/{board}/jobs/{id}?content=true` ->
  a single job object with `first_published` / `updated_at` (both
  documented `ats_native` per spec.md §3), `absolute_url`, `title`,
  `content` (HTML), `departments[]`, `offices[]`, `location.name`.
* board endpoint `GET /v1/boards/{board}/jobs?content=true` ->
  `{"jobs": [...]}`, each element shaped like the job endpoint's object
  (assumed NOT to reliably include `first_published`, since Greenhouse's
  own docs only call out `first_published` on the single-job endpoint).
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from rli.net import NetClient
from rli.resolvers.common import FetchResult, content_hash, parse_flexible_datetime

__all__ = ["GreenhouseJob", "fetch_board", "fetch_job", "find_job"]


class GreenhouseJob(BaseModel):
    """One Greenhouse job, from either the job or board-listing endpoint."""

    id: str
    title: str | None = None
    absolute_url: str | None = None
    first_published: str | None = None  # raw ISO string; parsed on demand
    updated_at: str | None = None
    departments: list[str] = []
    location: str | None = None
    content: str | None = None

    @property
    def content_hash(self) -> str | None:
        return content_hash(self.content)

    @property
    def first_published_at(self):  # noqa: ANN201 - datetime | None, kept terse
        return parse_flexible_datetime(self.first_published)

    @property
    def updated_at_dt(self):  # noqa: ANN201 - datetime | None, kept terse
        return parse_flexible_datetime(self.updated_at)


def _parse_job(raw: dict) -> GreenhouseJob | None:
    if not isinstance(raw, dict) or "id" not in raw:
        return None
    departments = [
        d.get("name") for d in raw.get("departments") or [] if isinstance(d, dict) and d.get("name")
    ]
    location = None
    raw_location = raw.get("location")
    if isinstance(raw_location, dict):
        location = raw_location.get("name")
    if location is None:
        offices = raw.get("offices") or []
        if offices and isinstance(offices[0], dict):
            location = offices[0].get("name")
    try:
        job_id = str(raw["id"])
    except (KeyError, TypeError):
        return None
    return GreenhouseJob(
        id=job_id,
        title=raw.get("title"),
        absolute_url=raw.get("absolute_url"),
        first_published=raw.get("first_published"),
        updated_at=raw.get("updated_at"),
        departments=departments,
        location=location,
        content=raw.get("content"),
    )


def fetch_job(net: NetClient, tenant: str, job_id: str) -> FetchResult[GreenhouseJob]:
    """Fetch one job via `GET /v1/boards/{tenant}/jobs/{job_id}?content=true`."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs/{job_id}"
    result = net.get(url, params={"content": "true"})
    if not result.ok:
        return FetchResult.from_failure(result)
    try:
        raw = json.loads(result.body or "")
    except ValueError:
        return FetchResult.from_success(result, None)
    return FetchResult.from_success(result, _parse_job(raw))


def fetch_board(net: NetClient, tenant: str) -> FetchResult[list[GreenhouseJob]]:
    """Fetch the open jobs list via `GET /v1/boards/{tenant}/jobs?content=true`."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs"
    result = net.get(url, params={"content": "true"})
    if not result.ok:
        return FetchResult.from_failure(result)
    try:
        raw = json.loads(result.body or "")
    except ValueError:
        return FetchResult.from_success(result, None)
    jobs_raw = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(jobs_raw, list):
        return FetchResult.from_success(result, None)
    jobs = [job for job in (_parse_job(item) for item in jobs_raw) if job is not None]
    return FetchResult.from_success(result, jobs)


def find_job(jobs: list[GreenhouseJob], job_id: str) -> GreenhouseJob | None:
    for job in jobs:
        if job.id == str(job_id):
            return job
    return None
