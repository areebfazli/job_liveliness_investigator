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


# ---------------------------------------------------------------------------
# The third element: last_material_event_at (spec.md §5, Amendment 2026-09-10)
# ---------------------------------------------------------------------------


def test_last_material_event_at_is_the_max_event_date_when_several_qualify(
    conn: sqlite3.Connection,
) -> None:
    """Several qualifying material events: the MAX `event_date`, not the min or the count.

    The policy asks "has this posting been refreshed SINCE the bad news", so
    the most recent qualifying event is the one that matters — an older
    layoff must not win over a newer one just because it was inserted first
    or has a smaller id.
    """
    _insert_company(conn)
    upsert_event(
        conn,
        _event(
            event_type="layoff",
            materiality="material",
            event_date=date(2026, 6, 1),
            available_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            source_url="https://news.example.com/layoff-june",
            headline="Acme cuts 5% of staff",
        ),
    )
    upsert_event(
        conn,
        _event(
            event_type="shutdown",
            materiality="material",
            event_date=date(2026, 8, 1),  # the later, MAX-qualifying date
            available_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            source_url="https://news.example.com/shutdown-august",
            headline="Acme shuts down a division",
        ),
    )
    # A non-qualifying event with a LATER date than either material one, to
    # prove the max is taken only over the events that made the boolean True.
    upsert_event(
        conn,
        _event(
            event_type="hiring_freeze",
            materiality="material",
            event_date=date(2026, 9, 1),
            available_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            source_url="https://news.example.com/freeze-september",
            headline="Acme announces a hiring freeze",
        ),
    )
    status = {"acme.com": _status(events_found=3)}

    result = derive_policy_signals(conn, "acme.com", AS_OF, status, window_days=WINDOW_DAYS)

    assert result == (True, True, datetime(2026, 8, 1, tzinfo=UTC))


def test_material_negative_event_true_iff_last_material_event_at_is_a_datetime(
    conn: sqlite3.Connection,
) -> None:
    """The documented pairing invariant, checked across every reachable shape.

    `rli.events.policy_signals.EventSignals`: `material_negative_event is
    True` if and only if the third element is a `datetime` — `False` pairs
    with `None` ("checked, nothing found") and `UNKNOWN` pairs with `UNKNOWN`
    ("not yet investigated"). The impossible pairs (`True`/`None`,
    `True`/`UNKNOWN`, `False` or `UNKNOWN` paired with a real date) must never
    occur.
    """
    _insert_company(conn)
    upsert_event(conn, _event(event_type="layoff", materiality="material"))
    scenarios: list[tuple[dict, bool | object]] = [
        ({}, UNKNOWN),  # not searched at all
        ({"acme.com": _status(events_found=1)}, True),  # searched, material event
    ]
    for status, expected_material in scenarios:
        material, _freeze, event_at = derive_policy_signals(
            conn, "acme.com", AS_OF, status, window_days=WINDOW_DAYS
        )
        assert material == expected_material
        assert (material is True) == isinstance(event_at, datetime)

    # Searched, but the only event present does not qualify (outside window).
    conn.execute("DELETE FROM company_events")
    upsert_event(
        conn,
        _event(
            event_type="layoff",
            materiality="material",
            event_date=date(2000, 1, 1),
            available_at=datetime(2000, 1, 1, tzinfo=UTC),
            source_url="https://news.example.com/ancient-layoff",
        ),
    )
    material, _freeze, event_at = derive_policy_signals(
        conn, "acme.com", AS_OF, {"acme.com": _status(events_found=1)}, window_days=WINDOW_DAYS
    )
    assert material is False
    assert (material is True) == isinstance(event_at, datetime)
    assert event_at is None
