"""Per-company capture stamps in `rli.snapshots.daily` (spec.md §3: never backdate).

A daily run over ~350 boards has taken from minutes to 18 hours. Stamping
every company's capture — and the posting lifecycle fields derived from it —
with the run-start instant dated captures up to 18 hours before their data
was fetched. Each company is now stamped with the clock read right after its
own board request returned, and "already captured today" is judged per
company, against the day the company is reached in.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from rli.config import load_config
from rli.db import connect, init_db
from rli.models.time import to_utc_z
from rli.snapshots.daily import run_daily_snapshot
from rli.snapshots.targets import Target, upsert_companies

DAY1 = datetime(2026, 1, 1, tzinfo=UTC)
DAY2 = datetime(2026, 1, 2, tzinfo=UTC)

ACME_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
GLOBEX_URL = "https://boards-api.greenhouse.io/v1/boards/globex/jobs"
INITECH_URL = "https://boards-api.greenhouse.io/v1/boards/initech/jobs"

ACME = Target(company_name="Acme", website_domain="acme.com", ats="greenhouse", tenant="acme")
GLOBEX = Target(
    company_name="Globex", website_domain="globex.com", ats="greenhouse", tenant="globex"
)
INITECH = Target(
    company_name="Initech", website_domain="initech.com", ats="greenhouse", tenant="initech"
)


def _gh_job(job_id: str, tenant: str) -> dict:
    return {
        "id": int(job_id),
        "title": "Engineer",
        "absolute_url": f"https://boards.greenhouse.io/{tenant}/jobs/{job_id}",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
        "content": f"desc-{job_id}",
    }


def _board(url: str, tenant: str, *job_ids: str) -> None:
    respx.get(url).mock(
        return_value=httpx.Response(200, json={"jobs": [_gh_job(j, tenant) for j in job_ids]})
    )


def _no_sleep(_seconds: float) -> None:
    return None


class _SlowClock:
    """Returns the given instants in order, then the last one forever.

    `run_daily_snapshot` reads the clock twice per company it does not skip
    (before the fetch, for the "already captured today" check; after it, for
    the capture stamp), once per company it skips, and once for the
    page-date pass.
    """

    def __init__(self, *instants: datetime) -> None:
        self._instants = list(instants)

    def __call__(self) -> datetime:
        if len(self._instants) > 1:
            return self._instants.pop(0)
        return self._instants[0]


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    path = tmp_path / "rli.sqlite3"
    init_db(path)
    connection = connect(path)
    upsert_companies(connection, [ACME, GLOBEX, INITECH], now=DAY1)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def cfg():
    return load_config()


def test_each_company_is_stamped_when_its_own_board_was_fetched(conn, cfg) -> None:
    """A slow run: three companies fetched hours apart, none stamped with the start."""
    start = DAY1.replace(hour=1)
    clock = _SlowClock(
        start,  # acme: check
        start + timedelta(hours=2),  # acme: fetched
        start + timedelta(hours=2),  # globex: check
        start + timedelta(hours=7),  # globex: fetched
        start + timedelta(hours=7),  # initech: check
        start + timedelta(hours=9),  # initech: fetch failed
    )
    with respx.mock:
        _board(ACME_URL, "acme", "1")
        _board(GLOBEX_URL, "globex", "2")
        respx.get(INITECH_URL).mock(side_effect=httpx.ConnectError("boom"))
        summary = run_daily_snapshot(
            conn, cfg, [ACME, GLOBEX, INITECH], start, sleep=_no_sleep, clock=clock
        )
    assert (summary.companies_ok, summary.companies_failed) == (2, 1)

    captured = {
        row["company_id"]: row["captured_at"]
        for row in conn.execute("SELECT company_id, captured_at FROM board_snapshots")
    }
    assert captured == {
        "acme.com": to_utc_z(start + timedelta(hours=2)),
        "globex.com": to_utc_z(start + timedelta(hours=7)),
    }
    # Lifecycle fields derive from the company's own stamp, not the run start.
    lifecycle = {
        row["posting_id"]: (row["first_observed"], row["last_seen_open"])
        for row in conn.execute("SELECT posting_id, first_observed, last_seen_open FROM postings")
    }
    assert lifecycle == {
        "greenhouse:acme:1": (captured["acme.com"], captured["acme.com"]),
        "greenhouse:globex:2": (captured["globex.com"], captured["globex.com"]),
    }
    snapshots = {
        row["posting_id"]: row["captured_at"]
        for row in conn.execute("SELECT posting_id, captured_at FROM posting_snapshots")
    }
    assert snapshots == {
        "greenhouse:acme:1": captured["acme.com"],
        "greenhouse:globex:2": captured["globex.com"],
    }
    # A failed fetch's coverage gap is dated by that attempt, too.
    attempt = conn.execute(
        "SELECT attempted_at FROM capture_attempts WHERE company_id = 'initech.com'"
    ).fetchone()
    assert attempt["attempted_at"] == to_utc_z(start + timedelta(hours=9))
    assert to_utc_z(start) not in set(captured.values())


def test_absence_and_reappearance_are_dated_by_the_capture_that_saw_them(conn, cfg) -> None:
    with respx.mock:
        _board(ACME_URL, "acme", "1")
        run_daily_snapshot(conn, cfg, [ACME], DAY1, sleep=_no_sleep)
    gone_at = DAY2.replace(hour=3)
    with respx.mock:
        _board(ACME_URL, "acme")
        run_daily_snapshot(
            conn, cfg, [ACME], DAY2, sleep=_no_sleep, clock=_SlowClock(DAY2, gone_at)
        )
    back_at = DAY2 + timedelta(days=1, hours=5)
    with respx.mock:
        _board(ACME_URL, "acme", "1")
        run_daily_snapshot(
            conn,
            cfg,
            [ACME],
            DAY2 + timedelta(days=1),
            sleep=_no_sleep,
            clock=_SlowClock(DAY2 + timedelta(days=1), back_at),
        )
    row = conn.execute(
        "SELECT first_seen_absent, reappeared_at FROM postings "
        "WHERE posting_id = 'greenhouse:acme:1'"
    ).fetchone()
    assert (row["first_seen_absent"], row["reappeared_at"]) == (
        to_utc_z(gone_at),
        to_utc_z(back_at),
    )


def test_a_run_crossing_utc_midnight_checks_each_company_against_its_own_day(conn, cfg) -> None:
    """Started on day 1, the run reaches globex after midnight: day 2 is what counts.

    globex was already captured at noon on day 1. Judged against the RUN's
    start day it would be skipped and day 2 would get no capture from this
    run; judged against the moment it is reached, it is due, and is captured
    for day 2.
    """
    with respx.mock:
        _board(GLOBEX_URL, "globex", "2")
        run_daily_snapshot(conn, cfg, [GLOBEX], DAY1.replace(hour=12), sleep=_no_sleep)

    start = DAY1.replace(hour=23)
    clock = _SlowClock(
        start,  # acme: check (day 1, not captured yet)
        start + timedelta(minutes=30),  # acme: fetched, day 1
        DAY2 + timedelta(minutes=30),  # globex: check -> day 2
        DAY2 + timedelta(minutes=40),  # globex: fetched, day 2
    )
    with respx.mock:
        _board(ACME_URL, "acme", "1")
        _board(GLOBEX_URL, "globex", "2")
        summary = run_daily_snapshot(conn, cfg, [ACME, GLOBEX], start, sleep=_no_sleep, clock=clock)
    assert (summary.companies_ok, summary.companies_skipped) == (2, 0)
    globex = [
        row["captured_at"]
        for row in conn.execute(
            "SELECT captured_at FROM board_snapshots WHERE company_id = 'globex.com' "
            "ORDER BY captured_at"
        )
    ]
    assert globex == [to_utc_z(DAY1.replace(hour=12)), to_utc_z(DAY2 + timedelta(minutes=40))]


def test_a_fetch_straddling_midnight_into_a_captured_day_writes_nothing(conn, cfg) -> None:
    """One capture per company per UTC day holds on the capture's own stamp."""
    with respx.mock:
        _board(ACME_URL, "acme", "1")
        run_daily_snapshot(conn, cfg, [ACME], DAY2.replace(minute=5), sleep=_no_sleep)

    # Checked just before midnight on day 1 (no day-1 capture: due), but the
    # fetch only returned on day 2, which already has its capture.
    clock = _SlowClock(DAY2 - timedelta(seconds=1), DAY2 + timedelta(seconds=30))
    with respx.mock:
        _board(ACME_URL, "acme", "1")
        summary = run_daily_snapshot(
            conn, cfg, [ACME], DAY2 - timedelta(seconds=1), sleep=_no_sleep, clock=clock
        )
    assert (summary.companies_ok, summary.companies_skipped) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM board_snapshots").fetchone()[0] == 1
