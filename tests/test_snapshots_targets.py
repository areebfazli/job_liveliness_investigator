"""`rli.snapshots.targets` — verified ATS target CSV loading + upsert (PLAN.md M1)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rli.snapshots.targets import Target, load_targets, upsert_companies

CSV_HEADER = "company_name,website_domain,ats,tenant,open_job_count,checked_at\n"


def _write_csv(path: Path, body: str) -> Path:
    path.write_text(CSV_HEADER + body, encoding="utf-8")
    return path


def test_load_targets_parses_csv_and_normalizes_company_id(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "targets.csv",
        "Acme Inc,https://www.Acme.COM,greenhouse,acme,10,2026-01-01T00:00:00Z\n"
        "Beta,beta.io,ashby,beta,5,2026-01-01T00:00:00Z\n"
        "Gamma,gamma.co,lever,gamma,3,2026-01-01T00:00:00Z\n",
    )

    targets = load_targets(csv_path)

    assert len(targets) == 3
    assert targets[0].company_name == "Acme Inc"
    assert targets[0].ats == "greenhouse"
    assert targets[0].tenant == "acme"
    # normalize_domain: lowercased, strips scheme and leading www.
    assert targets[0].company_id == "acme.com"
    assert targets[1].company_id == "beta.io"
    assert targets[2].ats == "lever"


def test_target_company_id_falls_back_to_lowered_domain_when_normalize_fails() -> None:
    # normalize_domain returns None for input with no parseable hostname.
    target = Target(company_name="Weird", website_domain="???", ats="greenhouse", tenant="weird")
    assert target.company_id == "???"


def test_load_targets_raises_value_error_on_bad_ats(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "targets.csv",
        "Acme,acme.com,greenhouse,acme,10,2026-01-01T00:00:00Z\n"
        "Bogus,bogus.com,workday,bogus,1,2026-01-01T00:00:00Z\n",
    )

    with pytest.raises(ValueError, match="Bogus"):
        load_targets(csv_path)


def test_load_targets_raises_file_not_found_for_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.csv"
    with pytest.raises(FileNotFoundError):
        load_targets(missing)


def test_upsert_companies_inserts_new_rows(conn: sqlite3.Connection) -> None:
    targets = [
        Target(company_name="Acme", website_domain="acme.com", ats="greenhouse", tenant="acme"),
        Target(company_name="Beta", website_domain="beta.io", ats="ashby", tenant="beta"),
    ]

    count = upsert_companies(conn, targets, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert count == 2
    rows = conn.execute("SELECT company_id, name FROM companies ORDER BY company_id").fetchall()
    assert [r["company_id"] for r in rows] == ["acme.com", "beta.io"]
    assert rows[0]["name"] == "Acme"


def test_upsert_companies_updates_name_without_touching_created_at(
    conn: sqlite3.Connection,
) -> None:
    target_v1 = Target(
        company_name="Acme Old Name", website_domain="acme.com", ats="greenhouse", tenant="acme"
    )
    upsert_companies(conn, [target_v1], now=datetime(2026, 1, 1, tzinfo=UTC))
    first_created_at = conn.execute(
        "SELECT created_at FROM companies WHERE company_id = 'acme.com'"
    ).fetchone()["created_at"]

    target_v2 = Target(
        company_name="Acme New Name", website_domain="acme.com", ats="greenhouse", tenant="acme"
    )
    upsert_companies(conn, [target_v2], now=datetime(2026, 6, 1, tzinfo=UTC))

    row = conn.execute(
        "SELECT name, created_at FROM companies WHERE company_id = 'acme.com'"
    ).fetchone()
    assert row["name"] == "Acme New Name"
    assert row["created_at"] == first_created_at


def test_upsert_companies_dedupes_same_company_id_last_wins(conn: sqlite3.Connection) -> None:
    targets = [
        Target(
            company_name="Acme First",
            website_domain="acme.com",
            ats="greenhouse",
            tenant="acme",
        ),
        Target(
            company_name="Acme Second",
            website_domain="acme.com",
            ats="greenhouse",
            tenant="acme2",
        ),
    ]

    upsert_companies(conn, targets, now=datetime(2026, 1, 1, tzinfo=UTC))

    row = conn.execute("SELECT name FROM companies WHERE company_id = 'acme.com'").fetchone()
    assert row["name"] == "Acme Second"
