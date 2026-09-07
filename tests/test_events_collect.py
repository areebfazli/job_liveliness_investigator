"""Tests for `scripts/collect_events.py`."""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from collect_events import QUERY_TEMPLATES, SearchHit, run_collection  # noqa: E402

from rli.events.store import load_events_csv

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def _insert_company(conn: sqlite3.Connection, company_id: str) -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?, ?, ?, ?)",
        (company_id, company_id, company_id, "2026-01-01T00:00:00Z"),
    )
    conn.commit()


def _write_targets_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = ["company_name", "website_domain", "ats", "tenant", "open_job_count", "checked_at"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _fake_backend_factory(hits_by_exact_query: dict[str, list[SearchHit]]):
    """Build a `SearchBackend` keyed by the *exact* formatted query string.

    Exact-match (not substring) keying matters here: several
    `QUERY_TEMPLATES` share substrings across companies (e.g. every company's
    "{company} layoffs 2025" query contains "layoffs 2025"), so a
    substring-matching fake would leak one company's canned hits into
    another company's results.
    """

    def backend(company_name: str, query: str) -> list[SearchHit]:
        return hits_by_exact_query.get(query, [])

    return backend


def test_run_collection_writes_expected_csvs(tmp_path: Path) -> None:
    targets_csv = tmp_path / "targets.csv"
    _write_targets_csv(
        targets_csv,
        [
            {
                "company_name": "Acme",
                "website_domain": "acme.com",
                "ats": "greenhouse",
                "tenant": "acme",
                "open_job_count": "10",
                "checked_at": "2026-09-07T00:00:00Z",
            },
            {
                "company_name": "Globex",
                "website_domain": "globex.com",
                "ats": "lever",
                "tenant": "globex",
                "open_job_count": "5",
                "checked_at": "2026-09-07T00:00:00Z",
            },
        ],
    )

    backend = _fake_backend_factory(
        {
            "Acme layoffs 2025": [
                {
                    "title": "Acme lays off 10% of staff",
                    "url": "https://news.example.com/acme-layoffs",
                    "snippet": "Acme announced layoffs.",
                    "date": "2025-03-01",
                }
            ],
            "Globex hiring freeze": [
                {
                    "title": "Globex freezes hiring",
                    "url": "https://news.example.com/globex-freeze",
                    "snippet": None,
                    "date": "2026-01-15",
                }
            ],
            "Acme funding 2025": [
                {
                    # No date -> should be dropped.
                    "title": "Acme in funding talks",
                    "url": "https://news.example.com/acme-rumor",
                    "snippet": None,
                    "date": None,
                }
            ],
        }
    )

    out_events = tmp_path / "company_events.csv"
    out_status = tmp_path / "collection_status.csv"

    run_collection(targets_csv, out_events, out_status, backend, now=NOW)

    assert out_events.exists()
    assert out_status.exists()

    with out_events.open(newline="", encoding="utf-8") as f:
        event_rows = list(csv.DictReader(f))
    # 1 layoff hit for acme (via "layoffs 2025" template) + 1 freeze hit for globex.
    # The "funding 2025" hit with no date is dropped.
    assert len(event_rows) == 2
    urls = {row["source_url"] for row in event_rows}
    assert "https://news.example.com/acme-layoffs" in urls
    assert "https://news.example.com/globex-freeze" in urls
    assert "https://news.example.com/acme-rumor" not in urls

    with out_status.open(newline="", encoding="utf-8") as f:
        status_rows = list(csv.DictReader(f))
    assert len(status_rows) == 2
    by_company = {row["company_id"]: row for row in status_rows}
    assert int(by_company["acme.com"]["queries_run"]) == len(QUERY_TEMPLATES)
    assert int(by_company["acme.com"]["events_found"]) == 1
    assert int(by_company["globex.com"]["queries_run"]) == len(QUERY_TEMPLATES)
    assert int(by_company["globex.com"]["events_found"]) == 1


def test_run_collection_output_is_loadable_by_load_events_csv(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _insert_company(conn, "acme.com")

    targets_csv = tmp_path / "targets.csv"
    _write_targets_csv(
        targets_csv,
        [
            {
                "company_name": "Acme",
                "website_domain": "acme.com",
                "ats": "greenhouse",
                "tenant": "acme",
                "open_job_count": "10",
                "checked_at": "2026-09-07T00:00:00Z",
            }
        ],
    )

    backend = _fake_backend_factory(
        {
            "Acme layoffs 2025": [
                {
                    "title": "Acme lays off 150 employees",
                    "url": "https://news.example.com/acme-layoffs-2",
                    "snippet": "Big cuts.",
                    "date": "2025-05-01",
                }
            ]
        }
    )

    out_events = tmp_path / "company_events.csv"
    out_status = tmp_path / "collection_status.csv"
    run_collection(targets_csv, out_events, out_status, backend, now=NOW)

    loaded = load_events_csv(out_events, conn)
    assert loaded == 1


def test_run_collection_with_no_backend_raises(tmp_path: Path) -> None:
    from collect_events import _no_backend

    targets_csv = tmp_path / "targets.csv"
    _write_targets_csv(
        targets_csv,
        [
            {
                "company_name": "Acme",
                "website_domain": "acme.com",
                "ats": "greenhouse",
                "tenant": "acme",
                "open_job_count": "10",
                "checked_at": "2026-09-07T00:00:00Z",
            }
        ],
    )
    out_events = tmp_path / "company_events.csv"
    out_status = tmp_path / "collection_status.csv"

    with pytest.raises(NotImplementedError):
        run_collection(targets_csv, out_events, out_status, _no_backend, now=NOW)
