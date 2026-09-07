"""Ashby Public Job Postings API adapter (spec.md §3/§10).

GUESSED response shape (NOT verified against a live API call — built from
the publicly documented fields at
https://developers.ashbyhq.com/docs/public-job-posting-api, but not
exercised against a real response during this change):

    GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=false
    ->
    {
      "organizationName": "...",
      "jobs": [
        {
          "id": "...",
          "title": "...",
          "department": "...",
          "team": "...",
          "location": "...",
          "isListed": true,
          "isRemote": false,
          "publishedAt": "2026-08-01T00:00:00.000Z",   # documented ats_native (spec.md §3)
          "jobUrl": "https://jobs.ashbyhq.com/{org}/{id}",
          "descriptionHtml": "..."
        }
      ]
    }

There is no documented per-job Ashby endpoint; a single job is resolved by
finding it inside the job-board listing (`find_job`).
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from rli.net import NetClient
from rli.resolvers.common import FetchResult, content_hash, parse_flexible_datetime

__all__ = ["AshbyJob", "fetch_board", "find_job"]


class AshbyJob(BaseModel):
    """One Ashby job posting, from the job-board listing endpoint."""

    id: str
    title: str | None = None
    department: str | None = None
    team: str | None = None
    location: str | None = None
    published_at: str | None = None  # raw ISO string; parsed on demand
    job_url: str | None = None
    description_html: str | None = None

    @property
    def content_hash(self) -> str | None:
        return content_hash(self.description_html)

    @property
    def published_at_dt(self):  # noqa: ANN201 - datetime | None, kept terse
        return parse_flexible_datetime(self.published_at)


def _parse_job(raw: dict) -> AshbyJob | None:
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    return AshbyJob(
        id=str(raw["id"]),
        title=raw.get("title"),
        department=raw.get("department"),
        team=raw.get("team"),
        location=raw.get("location"),
        published_at=raw.get("publishedAt"),
        job_url=raw.get("jobUrl"),
        description_html=raw.get("descriptionHtml"),
    )


def fetch_board(net: NetClient, tenant: str) -> FetchResult[list[AshbyJob]]:
    """Fetch the job-board listing via `GET /posting-api/job-board/{tenant}`."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{tenant}"
    result = net.get(url, params={"includeCompensation": "false"})
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


def find_job(jobs: list[AshbyJob], job_id: str) -> AshbyJob | None:
    for job in jobs:
        if job.id == str(job_id):
            return job
    return None
