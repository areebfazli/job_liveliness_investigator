"""ATS detection from a job URL (spec.md §3 source policy; PLAN.md M1).

`detect_ats` never fetches the network — it is a pure string/regex
classifier over the URL's host and path, so it is cheap enough to call
before any probe runs. Company identity is deliberately NOT derived here:
per spec.md §3 ("Identity"), `company_id` is the normalized *company
website domain*, resolved later (typically from JSON-LD
`hiringOrganization.sameAs`/`url`), never from the ATS board/tenant slug —
`acme` in `boards.greenhouse.io/acme` need not be `acme.com`.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

__all__ = ["AtsName", "AtsRef", "detect_ats"]

AtsName = Literal["greenhouse", "ashby", "lever", "generic"]

# Both public Greenhouse job-board hosts resolve to the same job-board API
# (spec.md §10): the legacy `boards.greenhouse.io` and the newer
# `job-boards.greenhouse.io`.
_GREENHOUSE_BOARD_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_GREENHOUSE_API_HOST = "boards-api.greenhouse.io"

_SLUG = r"[A-Za-z0-9][\w-]*"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

_GREENHOUSE_BOARD_PATH = re.compile(rf"^/(?P<board>{_SLUG})/jobs/(?P<job_id>\d+)/?$")
_GREENHOUSE_API_PATH = re.compile(rf"^/v1/boards/(?P<board>{_SLUG})/jobs/(?P<job_id>\d+)/?$")
_ASHBY_PATH = re.compile(rf"^/(?P<org>{_SLUG})/(?P<job_id>{_UUID})/?$")
_LEVER_PATH = re.compile(rf"^/(?P<org>{_SLUG})/(?P<job_id>{_UUID})(?:/.*)?$")

_ASHBY_HOST = "jobs.ashbyhq.com"
_LEVER_HOST = "jobs.lever.co"


class AtsRef(BaseModel):
    """Resolved ATS identity for a job URL.

    `tenant` is the ATS-specific board/org slug (kept separate from
    `company_id`, spec.md §3). `canonical_url` is the normalized
    human-facing job-board URL for the detected ATS, or the input URL
    unchanged for `generic`.
    """

    ats: AtsName
    tenant: str | None
    job_id: str | None
    canonical_url: str


def _normalized_https_url(url: str) -> tuple[str, str, str] | None:
    """Return `(scheme, host, path)` for a plausible http(s) URL, else None."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    return parsed.scheme, host, parsed.path or "/"


def detect_ats(url: str) -> AtsRef | None:
    """Classify `url` by ATS, falling back to `generic`.

    Returns `None` only when `url` is not even a well-formed `http(s)://host`
    URL — every syntactically valid job URL classifies as one of
    `greenhouse` / `ashby` / `lever` / `generic`.
    """
    if not url or not isinstance(url, str):
        return None
    normalized = _normalized_https_url(url)
    if normalized is None:
        return None
    _scheme, host, path = normalized

    if host in _GREENHOUSE_BOARD_HOSTS:
        match = _GREENHOUSE_BOARD_PATH.match(path)
        if match:
            board, job_id = match["board"], match["job_id"]
            return AtsRef(
                ats="greenhouse",
                tenant=board,
                job_id=job_id,
                canonical_url=f"https://boards.greenhouse.io/{board}/jobs/{job_id}",
            )
    elif host == _GREENHOUSE_API_HOST:
        match = _GREENHOUSE_API_PATH.match(path)
        if match:
            board, job_id = match["board"], match["job_id"]
            return AtsRef(
                ats="greenhouse",
                tenant=board,
                job_id=job_id,
                canonical_url=f"https://boards.greenhouse.io/{board}/jobs/{job_id}",
            )
    elif host == _ASHBY_HOST:
        match = _ASHBY_PATH.match(path)
        if match:
            org, job_id = match["org"], match["job_id"]
            return AtsRef(
                ats="ashby",
                tenant=org,
                job_id=job_id,
                canonical_url=f"https://jobs.ashbyhq.com/{org}/{job_id}",
            )
    elif host == _LEVER_HOST:
        match = _LEVER_PATH.match(path)
        if match:
            org, job_id = match["org"], match["job_id"]
            return AtsRef(
                ats="lever",
                tenant=org,
                job_id=job_id,
                canonical_url=f"https://jobs.lever.co/{org}/{job_id}",
            )

    return AtsRef(ats="generic", tenant=None, job_id=None, canonical_url=url.strip())
