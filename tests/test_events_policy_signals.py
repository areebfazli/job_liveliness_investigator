"""Tests for `rli.events.policy_signals.derive_policy_signals`."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

from rli.events.policy_signals import derive_policy_signals
from rli.events.store import CollectionStatus, CompanyEvent, upsert_event
from rli.models.policy_inputs import UNKNOWN

WINDOW_DAYS = 180
AS_OF = datetime(2026, 9, 7, tzinfo=UTC)
# Midnight UTC on `_event`'s default `event_date` — the third element of the
# `EventSignals` triple when a material negative event is found.
EVENT_MIDNIGHT = datetime(2026, 8, 1, tzinfo=UTC)


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
        event_date=date(2026, 8, 1),
        available_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        source_url="https://news.example.com/x",
        headline="Acme cuts 10% of staff",
        raw_excerpt=None,
        source_quality="news",
        materiality="material",
        collected_at=AS_OF,
    )
    fields.update(overrides)
    return CompanyEvent(**fields)


def _status(**overrides) -> CollectionStatus:
    fields = dict(
        company_id="acme.com",
        searched_at=AS_OF,
        queries_run=7,
        events_found=0,
    )
    fields.update(overrides)
    return CollectionStatus(**fields)


def test_company_absent_from_collection_status_is_unknown(conn: sqlite3.Connection) -> None:
    result = derive_policy_signals(conn, "acme.com", AS_OF, {}, window_days=WINDOW_DAYS)
    assert result == (UNKNOWN, UNKNOWN, UNKNOWN)


def test_searched_no_events_resolves_to_false_false(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    status = {"acme.com": _status(events_found=0)}
    result = derive_policy_signals(conn, "acme.com", AS_OF, status, window_days=WINDOW_DAYS)
    assert result == (False, False, None)


def test_material_layoff_in_window_sets_material_negative_event(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    upsert_event(conn, _event(event_type="layoff", materiality="material"))
    status = {"acme.com": _status(events_found=1)}
    result = derive_policy_signals(conn, "acme.com", AS_OF, status, window_days=WINDOW_DAYS)
    assert result == (True, False, EVENT_MIDNIGHT)


def test_hiring_freeze_in_window_sets_freeze_or_pause(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    upsert_event(
        conn,
        _event(
            event_type="hiring_freeze",
            materiality="material",
            headline="Acme announces hiring freeze",
            source_url="https://news.example.com/freeze",
        ),
    )
    status = {"acme.com": _status(events_found=1)}
    result = derive_policy_signals(conn, "acme.com", AS_OF, status, window_days=WINDOW_DAYS)
    assert result == (False, True, None)


def test_events_outside_window_are_ignored(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    old_event = _event(
        event_type="layoff",
        materiality="material",
        event_date=date(2020, 1, 1),
        available_at=datetime(2020, 1, 1, 12, 0, tzinfo=UTC),
        source_url="https://news.example.com/old-layoff",
    )
    upsert_event(conn, old_event)
    status = {"acme.com": _status(events_found=1)}
    result = derive_policy_signals(conn, "acme.com", AS_OF, status, window_days=WINDOW_DAYS)
    assert result == (False, False, None)


def test_search_after_as_of_is_treated_as_unknown(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    upsert_event(conn, _event(event_type="layoff", materiality="material"))
    future_search = _status(searched_at=AS_OF + timedelta(days=1))
    status = {"acme.com": future_search}
    result = derive_policy_signals(conn, "acme.com", AS_OF, status, window_days=WINDOW_DAYS)
    assert result == (UNKNOWN, UNKNOWN, UNKNOWN)


def test_minor_layoff_does_not_set_material_negative_event(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    upsert_event(conn, _event(event_type="layoff", materiality="minor"))
    status = {"acme.com": _status(events_found=1)}
    result = derive_policy_signals(conn, "acme.com", AS_OF, status, window_days=WINDOW_DAYS)
    assert result == (False, False, None)


def test_shutdown_in_window_sets_material_negative_event(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    upsert_event(
        conn,
        _event(
            event_type="shutdown",
            materiality="material",
            headline="Acme shuts down",
            source_url="https://news.example.com/shutdown",
        ),
    )
    status = {"acme.com": _status(events_found=1)}
    result = derive_policy_signals(conn, "acme.com", AS_OF, status, window_days=WINDOW_DAYS)
    assert result == (True, False, EVENT_MIDNIGHT)
