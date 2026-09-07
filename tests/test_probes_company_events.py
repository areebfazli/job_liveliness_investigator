"""`company_events` probe: replay-safe reads of the pre-collected store (spec.md §4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from rli.events.store import (
    CollectionStatus,
    CompanyEvent,
    upsert_event,
    write_collection_status_csv,
)
from rli.models.policy_inputs import UNKNOWN
from rli.probes.base import ProbeContext
from rli.probes.company_events import CompanyEventsArgs, CompanyEventsProbe, company_events

COMPANY = "acme.com"
AS_OF = datetime(2026, 9, 7, tzinfo=UTC)
NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _company(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?, ?, ?, ?)",
        (COMPANY, "Acme", COMPANY, "2026-01-01T00:00:00.000000Z"),
    )
    conn.commit()


def _event(**overrides) -> CompanyEvent:
    fields = dict(
        company_id=COMPANY,
        event_type="layoff",
        event_date=date(2026, 8, 1),
        available_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        source_url="https://news.example.com/layoff",
        headline="Acme cuts 15% of staff",
        raw_excerpt="Acme said it would cut 15% of staff.",
        source_quality="news",
        materiality="material",
        collected_at=AS_OF,
    )
    fields.update(overrides)
    return CompanyEvent(**fields)


def _status_csv(tmp_path: Path, *rows: CollectionStatus) -> str:
    path = tmp_path / "collection_status.csv"
    write_collection_status_csv(list(rows), path)
    return str(path)


def _status(**overrides) -> CollectionStatus:
    fields = dict(company_id=COMPANY, searched_at=AS_OF, queries_run=7, events_found=0)
    fields.update(overrides)
    return CollectionStatus(**fields)


def _ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    return ctx_factory(now=lambda: NOW)


def _args(**overrides) -> CompanyEventsArgs:
    fields = dict(company_id=COMPANY, as_of=AS_OF)
    fields.update(overrides)
    return CompanyEventsArgs(**fields)


# ---------------------------------------------------------------------------
# not-yet-collected == UNKNOWN, never False
# ---------------------------------------------------------------------------


def test_company_absent_from_collection_status_is_unknown(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _company(conn)
    result = company_events(_args(collection_status_csv=_status_csv(tmp_path)), _ctx(ctx_factory))

    assert result.ok is True
    assert result.data is not None
    assert result.data["collected"] is False
    assert result.data["material_negative_event"] is UNKNOWN
    assert result.data["freeze_or_pause"] is UNKNOWN


def test_missing_collection_status_file_is_not_an_error(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _company(conn)
    result = company_events(
        _args(collection_status_csv=str(tmp_path / "never_written.csv")), _ctx(ctx_factory)
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["collected"] is False
    assert result.data["material_negative_event"] is UNKNOWN


def test_searched_with_no_events_is_a_known_negative(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _company(conn)
    csv_path = _status_csv(tmp_path, _status(events_found=0))

    result = company_events(_args(collection_status_csv=csv_path), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["collected"] is True
    assert result.data["material_negative_event"] is False
    assert result.data["freeze_or_pause"] is False
    assert result.data["evidence"] == []


def test_search_recorded_after_as_of_counts_as_not_collected(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _company(conn)
    csv_path = _status_csv(
        tmp_path, _status(searched_at=datetime(2026, 9, 30, tzinfo=UTC), events_found=1)
    )
    upsert_event(conn, _event())

    result = company_events(_args(collection_status_csv=csv_path), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["collected"] is False
    assert result.data["material_negative_event"] is UNKNOWN


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def test_material_layoff_in_window_sets_material_negative_event(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _company(conn)
    upsert_event(conn, _event())
    csv_path = _status_csv(tmp_path, _status(events_found=1))

    result = company_events(_args(collection_status_csv=csv_path), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["material_negative_event"] is True
    assert result.data["freeze_or_pause"] is False

    claims = result.data["evidence"]
    assert len(claims) == 1
    assert claims[0].claim_type == "layoff"
    assert claims[0].value == "Acme cuts 15% of staff"
    assert claims[0].source_quality == "news"
    assert claims[0].source_url == "https://news.example.com/layoff"
    assert claims[0].raw_excerpt == "Acme said it would cut 15% of staff."
    # available_at is the article's own publish time, NOT the probe run time.
    assert claims[0].available_at == datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    assert claims[0].fetched_at == NOW
    assert claims[0].source_event_at == datetime(2026, 8, 1, tzinfo=UTC)


def test_hiring_freeze_sets_freeze_or_pause(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _company(conn)
    upsert_event(
        conn,
        _event(
            event_type="hiring_freeze",
            headline="Acme announces a hiring freeze",
            source_url="https://news.example.com/freeze",
        ),
    )
    csv_path = _status_csv(tmp_path, _status(events_found=1))

    result = company_events(_args(collection_status_csv=csv_path), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["material_negative_event"] is False
    assert result.data["freeze_or_pause"] is True
    assert result.data["evidence"][0].claim_type == "hiring_freeze"


# ---------------------------------------------------------------------------
# point-in-time replay
# ---------------------------------------------------------------------------


def test_event_available_after_as_of_is_invisible(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _company(conn)
    upsert_event(
        conn,
        _event(
            event_date=date(2026, 9, 20),
            available_at=datetime(2026, 9, 20, tzinfo=UTC),
            source_url="https://news.example.com/future",
            headline="Acme cuts another 20% of staff",
        ),
    )
    csv_path = _status_csv(tmp_path, _status(events_found=1))

    result = company_events(_args(collection_status_csv=csv_path), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["events"] == []
    assert result.data["evidence"] == []
    # A future event must not flip the signals either.
    assert result.data["material_negative_event"] is False
    assert result.data["freeze_or_pause"] is False


def test_events_outside_the_signal_window_still_appear_as_evidence(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _company(conn)
    # 2024 layoff: visible at as_of, but far outside negative_event_window_days.
    upsert_event(
        conn,
        _event(
            event_date=date(2024, 1, 15),
            available_at=datetime(2024, 1, 15, tzinfo=UTC),
            source_url="https://news.example.com/old",
            headline="Acme cut 30% of staff in 2024",
        ),
    )
    csv_path = _status_csv(tmp_path, _status(events_found=1))

    result = company_events(_args(collection_status_csv=csv_path), _ctx(ctx_factory))

    assert result.data is not None
    assert result.data["material_negative_event"] is False
    assert [c.claim_type for c in result.data["evidence"]] == ["layoff"]


# ---------------------------------------------------------------------------
# args / metadata
# ---------------------------------------------------------------------------


def test_naive_as_of_is_rejected() -> None:
    try:
        CompanyEventsArgs(company_id=COMPANY, as_of=datetime(2026, 9, 7))
    except Exception as exc:  # pydantic.ValidationError wrapping ValueError
        assert "timezone-aware" in str(exc)
    else:  # pragma: no cover - the validator must reject this
        raise AssertionError("naive as_of must be rejected")


def test_probe_run_delegates_and_declares_its_spec_metadata(
    conn: sqlite3.Connection, ctx_factory, tmp_path: Path
) -> None:
    _company(conn)
    csv_path = _status_csv(tmp_path, _status(events_found=0))

    result = CompanyEventsProbe().run(
        _args(collection_status_csv=csv_path), _ctx(ctx_factory)
    )
    assert result.ok is True
    assert result.data is not None
    assert result.data["as_of"] == "2026-09-07T00:00:00.000000Z"
    assert CompanyEventsProbe.cost_tier == "medium"
    assert CompanyEventsProbe.history_required is False
    assert CompanyEventsProbe.populates == frozenset(
        {"material_negative_event", "freeze_or_pause"}
    )
