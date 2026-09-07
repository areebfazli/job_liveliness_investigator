"""Tests for `rli.events.store` (spec.md §4 `company_events`)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from rli.events.store import (
    CompanyEvent,
    classify_materiality,
    events_for,
    load_events_csv,
    upsert_event,
    write_events_csv,
)

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def _insert_company(conn: sqlite3.Connection, company_id: str = "acme.com") -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?, ?, ?, ?)",
        (company_id, "Acme", company_id, "2026-01-01T00:00:00Z"),
    )
    conn.commit()


def _event(**overrides) -> CompanyEvent:
    fields = dict(
        company_id="acme.com",
        event_type="layoff",
        event_date=date(2026, 6, 1),
        available_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        source_url="https://news.example.com/acme-layoffs",
        headline="Acme lays off 10% of staff",
        raw_excerpt=None,
        source_quality="news",
        materiality="material",
        collected_at=NOW,
    )
    fields.update(overrides)
    return CompanyEvent(**fields)


# ---------------------------------------------------------------------------
# CompanyEvent validation
# ---------------------------------------------------------------------------


def test_company_event_rejects_naive_available_at() -> None:
    with pytest.raises(ValidationError):
        _event(available_at=datetime(2026, 6, 1, 12, 0))


def test_company_event_rejects_naive_collected_at() -> None:
    with pytest.raises(ValidationError):
        _event(collected_at=datetime(2026, 9, 7))


def test_company_event_accepts_tz_aware() -> None:
    event = _event()
    assert event.available_at.tzinfo is not None
    assert event.collected_at.tzinfo is not None


def test_company_event_rejects_bad_event_type() -> None:
    with pytest.raises(ValidationError):
        _event(event_type="not_a_real_type")


# ---------------------------------------------------------------------------
# classify_materiality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["hiring_freeze", "hiring_pause", "shutdown"])
def test_classify_materiality_explicit_types_are_material(event_type: str) -> None:
    assert classify_materiality(event_type, "some headline", None) == "material"


def test_classify_materiality_layoff_percentage_material() -> None:
    assert classify_materiality("layoff", "Acme cuts 10% of staff", None) == "material"


def test_classify_materiality_layoff_percentage_minor() -> None:
    assert classify_materiality("layoff", "Acme cuts 8% of staff", None) == "minor"


def test_classify_materiality_layoff_headcount_material() -> None:
    assert classify_materiality("layoff", "Acme lays off 150 employees", None) == "material"


def test_classify_materiality_layoff_headcount_minor() -> None:
    assert classify_materiality("layoff", "Acme cuts 50 jobs", None) == "minor"


def test_classify_materiality_layoff_no_number_defaults_material() -> None:
    assert classify_materiality("layoff", "Acme announces layoffs", None) == "material"


def test_classify_materiality_layoff_checks_raw_excerpt_too() -> None:
    assert (
        classify_materiality("layoff", "Acme announces layoffs", "Company cut 12% of workers")
        == "material"
    )


@pytest.mark.parametrize("event_type", ["funding", "expansion", "acquisition", "other"])
def test_classify_materiality_non_negative_types_always_minor(event_type: str) -> None:
    assert classify_materiality(event_type, "Acme raises $50M, cuts 500 jobs", None) == "minor"


# ---------------------------------------------------------------------------
# upsert_event + events_for
# ---------------------------------------------------------------------------


def test_events_for_filters_future_available_at(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    as_of = datetime(2026, 6, 15, tzinfo=UTC)

    past_event = _event(
        event_type="funding",
        materiality="minor",
        source_url="https://news.example.com/past",
        available_at=as_of - timedelta(days=1),
    )
    future_event = _event(
        event_type="funding",
        materiality="minor",
        source_url="https://news.example.com/future",
        available_at=as_of + timedelta(days=1),
    )
    upsert_event(conn, past_event)
    upsert_event(conn, future_event)

    results = events_for(conn, "acme.com", as_of)
    assert len(results) == 1
    assert results[0].source_url == "https://news.example.com/past"


def test_upsert_event_dedupes_on_natural_key(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    original = _event(headline="Acme cuts 10% of staff")
    row_id_1 = upsert_event(conn, original)

    updated = _event(headline="Acme cuts 12% of staff (updated)")
    row_id_2 = upsert_event(conn, updated)

    assert row_id_1 == row_id_2
    count = conn.execute("SELECT COUNT(*) AS c FROM company_events").fetchone()["c"]
    assert count == 1

    [event] = events_for(conn, "acme.com", datetime(2026, 12, 31, tzinfo=UTC))
    assert event.headline == "Acme cuts 12% of staff (updated)"


def test_upsert_event_different_source_url_is_a_new_row(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    upsert_event(conn, _event(source_url="https://news.example.com/a"))
    upsert_event(conn, _event(source_url="https://news.example.com/b"))
    count = conn.execute("SELECT COUNT(*) AS c FROM company_events").fetchone()["c"]
    assert count == 2


# ---------------------------------------------------------------------------
# load_events_csv / write_events_csv round-trip
# ---------------------------------------------------------------------------


def test_events_csv_round_trip(conn: sqlite3.Connection, tmp_path) -> None:
    _insert_company(conn)
    events = [
        _event(
            event_type="layoff",
            headline="Acme cuts 10% of staff",
            raw_excerpt="details here",
            source_url="https://news.example.com/1",
        ),
        _event(
            event_type="hiring_freeze",
            headline="Acme announces hiring freeze",
            raw_excerpt=None,
            source_url="https://news.example.com/2",
            event_date=date(2026, 7, 1),
            available_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        ),
        _event(
            event_type="funding",
            headline="Acme raises $50M",
            raw_excerpt=None,
            source_url="https://news.example.com/3",
            materiality="minor",
            event_date=date(2026, 8, 1),
            available_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        ),
    ]
    csv_path = tmp_path / "company_events.csv"
    write_events_csv(events, csv_path)

    loaded_count = load_events_csv(csv_path, conn)
    assert loaded_count == 3

    result = events_for(conn, "acme.com", datetime(2027, 1, 1, tzinfo=UTC))
    assert len(result) == 3

    by_url = {e.source_url: e for e in result}
    assert by_url["https://news.example.com/1"].materiality == "material"
    assert by_url["https://news.example.com/1"].headline == "Acme cuts 10% of staff"
    assert by_url["https://news.example.com/1"].raw_excerpt == "details here"
    assert by_url["https://news.example.com/2"].event_type == "hiring_freeze"
    assert by_url["https://news.example.com/2"].materiality == "material"
    assert by_url["https://news.example.com/3"].materiality == "minor"
    for event in result:
        assert event.company_id == "acme.com"
        assert event.source_quality == "news"
        assert event.collected_at == NOW
