"""Synthetic corpus + HTTP mocks shared by the `tests/test_replay_*.py` files.

Named `test_replay_helpers` so it sits inside the `tests/test_replay*.py`
glob that owns `rli/replay`; it contains no tests of its own, exactly like
`tests/test_eval_helpers.py` (whose builders it reuses rather than restates).

The scenario every replay test builds on is deliberately small and has all
three shapes a replay dataset has to handle:

* `OPEN_JOB` — a posting still listed today. Its grid therefore reaches the
  build instant, which is the only `T` at which the LIVE full-probe record
  passes the `available_at <= T` gate.
* `CLOSED_JOB` — a posting that vanished from a complete capture 30 days
  ago. Its grid stops there, so every one of its cases is archive-era and
  its state can only come from board captures.
* `OTHER_COMPANY`'s job — a second company, so round-robin selection and
  the company split have something to spread across.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import respx
from test_eval_helpers import COMPANY, OTHER_COMPANY, add_capture, add_posting, job

NOW = datetime(2026, 9, 7, tzinfo=UTC)

TENANT = "acme"
OTHER_TENANT = "globex"

OPEN_JOB = "6001"
CLOSED_JOB = "6002"
OTHER_JOB = "7001"

OPEN_URL = f"https://boards.greenhouse.io/{TENANT}/jobs/{OPEN_JOB}"
CLOSED_URL = f"https://boards.greenhouse.io/{TENANT}/jobs/{CLOSED_JOB}"
OTHER_URL = f"https://boards.greenhouse.io/{OTHER_TENANT}/jobs/{OTHER_JOB}"

_BOARD_API = "https://boards-api.greenhouse.io/v1/boards"

NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"


def gh_job(job_id: str, url: str, *, published_days_ago: int = 65) -> dict:
    """A Greenhouse job payload. `first_published` defaults to BEFORE the
    seeded postings' `first_observed` (-60 days): a stated first publication
    after our own first sighting is re-labelled by
    `rli.eval.case.guard_first_published` and would not be publish evidence."""
    return {
        "id": int(job_id),
        "title": "Backend Engineer",
        "absolute_url": url,
        "first_published": (NOW - timedelta(days=published_days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "content": "<p>Build things.</p>",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
    }


def seed_corpus(conn: sqlite3.Connection) -> None:
    """Board history for both companies (see the module docstring)."""
    add_posting(
        conn,
        job_id=OPEN_JOB,
        tenant=TENANT,
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=10),
    )
    add_posting(
        conn,
        job_id=CLOSED_JOB,
        tenant=TENANT,
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=45),
        first_seen_absent=NOW - timedelta(days=30),
    )
    add_posting(
        conn,
        job_id=OTHER_JOB,
        company_id=OTHER_COMPANY,
        tenant=OTHER_TENANT,
        canonical_url=OTHER_URL,
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=10),
    )

    # acme: both jobs present at -60/-45; CLOSED_JOB gone from -30 onwards.
    for offset, jobs in (
        (60, [job(OPEN_JOB), job(CLOSED_JOB)]),
        (45, [job(OPEN_JOB), job(CLOSED_JOB)]),
        (30, [job(OPEN_JOB)]),
        (10, [job(OPEN_JOB)]),
    ):
        add_capture(conn, NOW - timedelta(days=offset), jobs, company_id=COMPANY)

    for offset in (60, 45, 30, 10):
        add_capture(
            conn,
            NOW - timedelta(days=offset),
            [job(OTHER_JOB)],
            company_id=OTHER_COMPANY,
        )


def mock_ats() -> None:
    """Register every HTTP route the live build pass needs.

    Call from inside a `@respx.mock`-decorated test. `CLOSED_JOB` answers 404
    on its per-job endpoint (Greenhouse's authoritative "gone"), which is what
    makes the live resolver report `closed` for it.
    """
    open_job = gh_job(OPEN_JOB, OPEN_URL)
    other_job = gh_job(OTHER_JOB, OTHER_URL)

    respx.get(f"{_BOARD_API}/{TENANT}/jobs/{OPEN_JOB}").mock(
        return_value=httpx.Response(200, json=open_job)
    )
    respx.get(f"{_BOARD_API}/{TENANT}/jobs/{CLOSED_JOB}").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    respx.get(f"{_BOARD_API}/{TENANT}/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [open_job]})
    )
    respx.get(f"{_BOARD_API}/{OTHER_TENANT}/jobs/{OTHER_JOB}").mock(
        return_value=httpx.Response(200, json=other_job)
    )
    respx.get(f"{_BOARD_API}/{OTHER_TENANT}/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [other_job]})
    )
    for url in (OPEN_URL, CLOSED_URL, OTHER_URL):
        respx.get(url).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))


def dev_everything_cutoff() -> datetime:
    """A temporal cutoff that puts every seeded posting in the `dev` split.

    `rli.policy.splits.temporal_split` is cutoff-inclusive on the TEST side,
    so a cutoff strictly after every `first_observed` leaves them all in
    `dev` — which is what a replay test wants, since the split assignment is
    `rli.policy.splits`' business and is tested there.
    """
    return NOW + timedelta(days=1)
