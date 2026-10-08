"""Pilot (archived per-job pages): parsers, URL normalisation, selection, client."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx

from rli.pilot import analysis
from rli.pilot import wayback_pages as wp

FIX = Path(__file__).parent / "fixtures" / "pilot_wayback"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# -- parsers -----------------------------------------------------------------


def test_greenhouse_open_page_yields_published_at() -> None:
    p = wp.parse_greenhouse_page(200, None, _read("gh_open.html"))
    assert p.state == "open"
    assert p.parser == "gh_published_at"
    assert p.published_raw == "2026-10-02T17:55:47-04:00"
    # Same instant as the board API's first_published for this job (21:55:47Z).
    assert p.published_at == datetime(2026, 10, 2, 21, 55, 47, tzinfo=UTC)
    assert p.title == "Software Engineer (Machine Learning) Intern (Summer 2027)"


def test_greenhouse_redirect_to_error_is_closed_but_host_move_is_not() -> None:
    closed = wp.parse_greenhouse_page(
        302, "/web/20260220145324id_/https://job-boards.greenhouse.io/affirm?error=true", ""
    )
    assert closed.state == "closed"
    moved = wp.parse_greenhouse_page(
        301, "/web/20260101000000id_/https://job-boards.greenhouse.io/affirm/jobs/123456", ""
    )
    assert moved.state == "unknown"
    careers = wp.parse_greenhouse_page(
        302, "/web/20260101000000id_/https://careers.example.com/x?gh_jid=123456", ""
    )
    assert careers.state == "unknown"


def test_greenhouse_200_without_job_post_is_unknown() -> None:
    p = wp.parse_greenhouse_page(200, None, "<html><title>Jobs</title></html>")
    assert p.state == "unknown"


def test_ashby_full_page_is_open_with_last_published_date() -> None:
    p = wp.parse_ashby_page(200, None, _read("ashby_open.html"))
    assert p.state == "open"
    assert p.parser == "ashby_posting"
    assert p.published_raw == "2025-11-17"
    assert p.updated_raw == "2026-05-23T03:21:09.902Z"


def test_ashby_empty_shell_is_unknown_never_closed() -> None:
    p = wp.parse_ashby_page(200, None, _read("ashby_shell.html"))
    assert p.state == "unknown"
    assert p.parser == "ashby_shell"
    assert "org_null" in p.notes


def test_ashby_unlisted_posting_is_not_open() -> None:
    body = _read("ashby_open.html").replace('"isListed":true', '"isListed":false')
    p = wp.parse_ashby_page(200, None, body)
    assert p.state == "unknown"
    assert p.parser == "ashby_unlisted"


def test_lever_pages() -> None:
    open_ = wp.parse_lever_page(200, None, _read("lever_open.html"))
    assert open_.state == "open" and open_.published_raw == "2026-04-02"
    gone = wp.parse_lever_page(404, None, _read("lever_404.html"))
    assert gone.state == "closed"
    unknown = wp.parse_lever_page(200, None, "<html>nothing here</html>")
    assert unknown.state == "unknown"


def test_cdx_inference_never_closes_on_ashby_200() -> None:
    assert wp.infer_from_cdx("ashby", "jobs.ashbyhq.com", "job", "200")[0] == "unknown"
    # Large server-rendered page -> open; ~3 KB shell -> unknown, never closed.
    assert wp.infer_from_cdx("ashby", "jobs.ashbyhq.com", "job", "200", 14000)[0] == "open"
    assert wp.infer_from_cdx("ashby", "jobs.ashbyhq.com", "job", "200", 3160)[0] == "unknown"
    assert wp.infer_from_cdx("greenhouse", "job-boards.greenhouse.io", "job", "302")[0] == "closed"
    assert wp.infer_from_cdx("greenhouse", "boards.greenhouse.io", "job", "301")[0] == "unknown"
    assert wp.infer_from_cdx("lever", "jobs.lever.co", "job", "404")[0] == "closed"
    assert wp.infer_from_cdx("lever", "jobs.lever.co", "job", "-")[0] == "unknown"


# -- normalisation / selection -----------------------------------------------


def test_normalize_job_url_strips_tracking_and_rejects_non_job_pages() -> None:
    ju = wp.normalize_job_url(
        "http://job-boards.greenhouse.io/affirm/jobs/8008645003?gh_src=Simplify",
        "greenhouse",
        "affirm",
    )
    assert ju is not None and ju.job_id == "8008645003" and ju.page_kind == "job"
    assert ju.canonical == "https://job-boards.greenhouse.io/affirm/jobs/8008645003"
    assert (
        wp.normalize_job_url(
            "https://job-boards.greenhouse.io/affirm/jobs/8008645003/confirmation",
            "greenhouse",
            "affirm",
        )
        is None
    )
    assert (
        wp.normalize_job_url(
            "https://job-boards.greenhouse.io/affirmx/jobs/8008645003", "greenhouse", "affirm"
        )
        is None
    )
    ash = wp.normalize_job_url(
        "https://jobs.ashbyhq.com/Cursor/D0E5B41D-84ab-4887-bd3a-55589b11dd7b/application?embed=true",
        "ashby",
        "cursor",
    )
    assert ash is not None and ash.page_kind == "application"
    assert ash.job_id == "d0e5b41d-84ab-4887-bd3a-55589b11dd7b"
    assert wp.normalize_job_url("https://jobs.lever.co/binance", "lever", "binance") is None


def test_select_captures_keeps_earliest_latest_and_month_boundaries() -> None:
    ts = [f"2026{m:02d}{d:02d}000000" for m in range(1, 13) for d in (3, 17)]
    picked = wp.select_captures(ts, max_n=8)
    assert len(picked) == 8
    stamps = [t for t, _ in picked]
    assert ts[0] in stamps and ts[-1] in stamps
    assert dict(picked)[ts[0]] == 0 and dict(picked)[ts[-1]] == 1
    assert all(t[6:8] == "03" for t, rank in picked if rank == 2)
    assert wp.select_captures([]) == []


# -- client --------------------------------------------------------------------


@respx.mock
def test_client_paginates_cdx_caches_and_backs_off_on_429(tmp_path: Path) -> None:
    sleeps: list[float] = []
    header = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
    row1 = ["k", "20260101000000", "https://jobs.lever.co/x/a", "text/html", "200", "D", "10"]
    row2 = ["k", "20260201000000", "https://jobs.lever.co/x/a", "text/html", "404", "E", "9"]
    route = respx.get(url__startswith=wp.CDX_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}),
            httpx.Response(200, json=[header, row1, [], ["RESUME"]]),
            httpx.Response(200, json=[header, row2]),
        ]
    )
    client = wp.WaybackClient(cache_dir=tmp_path, sleep=sleeps.append, log=lambda _m: None)
    rows, error = client.cdx("jobs.lever.co/x/")
    assert error is None
    assert [r[4] for r in rows] == ["200", "404"]
    assert route.call_count == 3
    assert any(s >= 60 for s in sleeps)  # 429 -> at least a minute
    assert "resumeKey=RESUME" in str(route.calls[2].request.url)
    # Second run: served from the disk cache, no new request.
    rows2, _ = client.cdx("jobs.lever.co/x/")
    assert rows2 == rows and route.call_count == 3


@respx.mock
def test_client_failure_is_a_gap_not_a_cached_answer(tmp_path: Path) -> None:
    respx.get(url__startswith="https://web.archive.org/web/").mock(return_value=httpx.Response(503))
    client = wp.WaybackClient(
        cache_dir=tmp_path, sleep=lambda _s: None, max_retries=1, log=lambda _m: None
    )
    resp = client.fetch_capture("20260101000000", "https://jobs.lever.co/x/a")
    assert not resp.ok and resp.error == "HTTP 503"
    assert client.cached(resp.url) is None


@respx.mock
def test_fetch_capture_does_not_follow_archived_redirect(tmp_path: Path) -> None:
    respx.get(url__startswith="https://web.archive.org/web/").mock(
        return_value=httpx.Response(
            302,
            headers={
                "location": "/web/20260220145324id_/https://job-boards.greenhouse.io/a?error=true",
                "memento-datetime": "Fri, 20 Feb 2026 14:53:24 GMT",
            },
        )
    )
    client = wp.WaybackClient(cache_dir=tmp_path, sleep=lambda _s: None, log=lambda _m: None)
    resp = client.fetch_capture("20260220145324", "https://job-boards.greenhouse.io/a/jobs/1")
    assert resp.status == 302 and resp.memento
    parsed = wp.parse_capture("greenhouse", resp.status, resp.location, resp.body)
    assert parsed.state == "closed"


# -- point-in-time labels ------------------------------------------------------


def _d(m: int, d: int) -> datetime:
    return datetime(2026, m, d, tzinfo=UTC)


def test_known_open_and_label_are_point_in_time() -> None:
    v = analysis.View(obs=[(_d(1, 20), True), (_d(2, 15), True), (_d(3, 10), False)], pubs=[])
    cut = _d(2, 1)
    assert analysis._known_open(v, cut)  # open on 01-20, within lookback
    assert analysis._label(v, cut) == 1  # closed observed 03-10 <= cut + 60d
    stale = analysis.View(obs=[(_d(1, 1), True), (_d(6, 1), True)], pubs=[])
    assert not analysis._known_open(stale, _d(3, 1))  # last sighting older than lookback
    straddle = analysis.View(obs=[(_d(1, 25), True), (_d(2, 20), True), (_d(5, 1), False)], pubs=[])
    assert analysis._label(straddle, cut) is None  # closed somewhere in (02-20, 05-01]
    still = analysis.View(obs=[(_d(1, 25), True), (_d(4, 15), True)], pubs=[])
    assert analysis._label(still, cut) == 0


def test_start_uses_only_dates_available_at_cut() -> None:
    v = analysis.View(
        obs=[(_d(3, 1), True)],
        pubs=[(_d(4, 1), _d(1, 1), "page_gh_first_published")],  # captured after the cut
    )
    assert analysis._start(v, _d(3, 15)) == _d(3, 1)
    assert analysis._start(v, _d(4, 2)) == _d(1, 1)


def test_pilot_db_schema_roundtrip(tmp_path: Path) -> None:
    conn = wp.open_pilot_db(tmp_path / "p.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"targets", "captures", "fetches", "page_obs", "cdx_queries"} <= tables
    assert json.dumps(sorted(tables))
