"""rli.api end-to-end tests (Phase 5 product shell).

Mirrors the respx-mocked Greenhouse pattern from `tests/test_eval_system_b.py`
against a fake tenant, but drives everything through the HTTP layer
(`fastapi.testclient.TestClient`) instead of calling `run_system_b` directly.

Every test builds its own scratch sqlite file under `tmp_path` and its own
scratch `watches.json` path — `data/rli.db` is never opened.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from test_eval_helpers import add_capture, add_posting, job

from rli.api import app as app_module
from rli.api.app import create_app
from rli.config import Config, load_config
from rli.db import connect, init_db
from rli.models.time import to_utc_z

NOW = datetime(2026, 9, 7, tzinfo=UTC)
NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"


# --------------------------------------------------------------------------- #
# Fixtures (deliberately local to this file: a scratch db_path/watch path per
# test, never the real `data/rli.db`)
# --------------------------------------------------------------------------- #


@pytest.fixture
def cfg() -> Config:
    return load_config()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "rli.sqlite3"
    init_db(path)
    return str(path)


@pytest.fixture
def watch_store_path(tmp_path: Path) -> str:
    return str(tmp_path / "watches.json")


@pytest.fixture
def conn(db_path: str) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(db_path: str, watch_store_path: str) -> Iterator[TestClient]:
    app = create_app(db_path=db_path, watch_store_path=watch_store_path)
    with TestClient(app) as test_client:
        yield test_client


def _gh_job(job_id: str, *, first_published_days_ago: int) -> dict:
    return {
        "id": int(job_id),
        "title": "Backend Engineer",
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "first_published": (NOW - timedelta(days=first_published_days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "content": "<p>Build things.</p>",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
    }


def _seed_history(conn: sqlite3.Connection, job_id: str) -> None:
    """50-day board history: usable (>=30) but not long-lived (<180)."""
    add_posting(
        conn,
        job_id=job_id,
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=10),
    )
    for offset in (60, 45, 30, 10):
        add_capture(conn, NOW - timedelta(days=offset), [job(job_id)])


def _mock_greenhouse(job_id: str, gh_job: dict) -> None:
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/acme/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json=gh_job)
    )
    respx.get(url).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [gh_job]})
    )


SPEC_DECISION_FIELDS = {
    "posting_state",
    "recommended_action",
    "recheck_after_days",
    "evidence_quality",
    "hypotheses",
    "reason",
    "evidence",
}


# --------------------------------------------------------------------------- #
# /investigate
# --------------------------------------------------------------------------- #


@respx.mock
def test_investigate_system_b_returns_spec_fields_and_writes_run(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    job_id = "7001"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    _seed_history(conn, job_id)
    conn.commit()
    _mock_greenhouse(job_id, _gh_job(job_id, first_published_days_ago=5))

    resp = client.post("/investigate", json={"url": url, "system": "B"})
    assert resp.status_code == 200
    data = resp.json()

    assert SPEC_DECISION_FIELDS <= set(data.keys())
    assert data["system_used"] == "B"
    assert data["degraded"] is False
    assert data["degraded_reason"] is None
    assert "run_id" in data

    run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (data["run_id"],)).fetchone()
    assert run_row is not None
    assert run_row["system"] == "B"


@respx.mock
def test_investigate_default_system_without_a_usable_llm_falls_back_to_b(
    client: TestClient, conn: sqlite3.Connection, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No reachable LLM endpoint degrades System C to System B, and says so.

    The endpoint probe is stubbed rather than left to hit the network: the
    default `[llm].base_url` is a local Ollama, so on a developer machine
    that happens to be running one the un-stubbed probe would SUCCEED and
    this test would try to drive a live model.
    """
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)
    monkeypatch.setattr(
        app_module, "endpoint_unavailable_reason", lambda _cfg: "LLM endpoint is not reachable"
    )

    job_id = "7002"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    _seed_history(conn, job_id)
    conn.commit()
    _mock_greenhouse(job_id, _gh_job(job_id, first_published_days_ago=5))

    resp = client.post("/investigate", json={"url": url})
    assert resp.status_code == 200
    data = resp.json()

    assert data["system_used"] == "B"
    assert data["degraded"] is True
    assert data["degraded_reason"] == (
        "LLM endpoint is not reachable; ran System B instead of System C"
    )

    run_row = conn.execute("SELECT system FROM runs WHERE id = ?", (data["run_id"],)).fetchone()
    assert run_row["system"] == "B"


@pytest.mark.parametrize(
    "url",
    [
        "http://boards.greenhouse.io/acme/jobs/1",  # not https
        "https://127.0.0.1/acme/jobs/1",  # IP literal
        "https://localhost/acme/jobs/1",  # single-label / loopback host
    ],
)
def test_investigate_rejects_disallowed_urls(client: TestClient, url: str) -> None:
    resp = client.post("/investigate", json={"url": url})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# /runs/{run_id}
# --------------------------------------------------------------------------- #


@respx.mock
def test_get_run_requires_debug_header(client: TestClient, conn: sqlite3.Connection) -> None:
    job_id = "7003"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    _seed_history(conn, job_id)
    conn.commit()
    _mock_greenhouse(job_id, _gh_job(job_id, first_published_days_ago=5))

    resp = client.post("/investigate", json={"url": url, "system": "B"})
    run_id = resp.json()["run_id"]

    no_header = client.get(f"/runs/{run_id}")
    assert no_header.status_code == 404

    with_header = client.get(f"/runs/{run_id}", headers={"X-RLI-Debug": "1"})
    assert with_header.status_code == 200
    body = with_header.json()
    assert body["run"]["id"] == run_id
    assert isinstance(body["steps"], list)
    assert len(body["steps"]) > 0
    assert all(step["run_id"] == run_id for step in body["steps"])


def test_get_run_404_for_nonexistent_run_id_even_with_header(client: TestClient) -> None:
    resp = client.get("/runs/does-not-exist", headers={"X-RLI-Debug": "1"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# /outcomes
# --------------------------------------------------------------------------- #


@respx.mock
def test_outcomes_valid_insert_via_posting_id(client: TestClient, conn: sqlite3.Connection) -> None:
    job_id = "7004"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    _seed_history(conn, job_id)
    conn.commit()
    _mock_greenhouse(job_id, _gh_job(job_id, first_published_days_ago=5))

    investigate_resp = client.post("/investigate", json={"url": url, "system": "B"})
    run_id = investigate_resp.json()["run_id"]
    run_row = conn.execute("SELECT posting_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    posting_id = run_row["posting_id"]
    assert posting_id is not None

    resp = client.post(
        "/outcomes",
        json={"posting_id": posting_id, "outcome": "applied", "note": "via referral"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["posting_id"] == posting_id
    assert body["outcome"] == "applied"

    row = conn.execute("SELECT * FROM outcomes WHERE id = ?", (body["id"],)).fetchone()
    assert row is not None
    assert row["outcome_type"] == "applied"
    assert row["notes"] == "via referral"


@respx.mock
def test_outcomes_valid_insert_via_run_id(client: TestClient, conn: sqlite3.Connection) -> None:
    job_id = "7005"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    _seed_history(conn, job_id)
    conn.commit()
    _mock_greenhouse(job_id, _gh_job(job_id, first_published_days_ago=5))

    investigate_resp = client.post("/investigate", json={"url": url, "system": "B"})
    run_id = investigate_resp.json()["run_id"]

    resp = client.post("/outcomes", json={"run_id": run_id, "outcome": "interview"})
    assert resp.status_code == 201
    assert resp.json()["outcome"] == "interview"


def test_outcomes_invalid_outcome_literal_is_422(client: TestClient) -> None:
    resp = client.post(
        "/outcomes", json={"posting_id": "whatever", "outcome": "not_a_real_outcome"}
    )
    assert resp.status_code == 422


def test_outcomes_missing_both_ids_is_400(client: TestClient) -> None:
    resp = client.post("/outcomes", json={"outcome": "applied"})
    assert resp.status_code == 400


def test_outcomes_unknown_posting_id_is_400(client: TestClient) -> None:
    resp = client.post("/outcomes", json={"posting_id": "no-such-posting", "outcome": "applied"})
    assert resp.status_code == 400


@respx.mock
def test_outcomes_run_with_null_posting_id_is_400(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    # Mirrors tests/test_eval_system_b.py's resolver-failure scenario: every
    # ATS call fails, so the resolver never establishes a posting and
    # `runs.posting_id` stays NULL.
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/9999").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/9999").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    resp = client.post(
        "/investigate",
        json={"url": "https://boards.greenhouse.io/acme/jobs/9999", "system": "B"},
    )
    run_id = resp.json()["run_id"]
    run_row = conn.execute("SELECT posting_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run_row["posting_id"] is None

    outcome_resp = client.post("/outcomes", json={"run_id": run_id, "outcome": "silence"})
    assert outcome_resp.status_code == 400


# --------------------------------------------------------------------------- #
# /watch, /watch/due
# --------------------------------------------------------------------------- #


@respx.mock
def test_watch_post_then_get(client: TestClient, conn: sqlite3.Connection) -> None:
    job_id = "7006"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    _seed_history(conn, job_id)
    conn.commit()
    _mock_greenhouse(job_id, _gh_job(job_id, first_published_days_ago=5))

    resp = client.post("/watch", json={"url": url})
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["url"] == url
    assert "last_posting_state" in entry
    assert "last_run_id" in entry

    list_resp = client.get("/watch")
    assert list_resp.status_code == 200
    watches = list_resp.json()
    assert any(w["url"] == url for w in watches)


def test_watch_due_includes_due_and_excludes_not_due(
    client: TestClient, watch_store_path: str
) -> None:
    due_url = "https://boards.greenhouse.io/acme/jobs/8001"
    not_due_url = "https://boards.greenhouse.io/acme/jobs/8002"

    watches = {
        due_url: {
            "url": due_url,
            "posting_id": None,
            "added_at": to_utc_z(NOW - timedelta(days=10)),
            "last_checked_at": to_utc_z(NOW - timedelta(days=10)),
            "recheck_after_days": 3,
            "last_posting_state": "open",
            "last_recommended_action": "apply_now",
            "last_run_id": "run-due",
        },
        not_due_url: {
            "url": not_due_url,
            "posting_id": None,
            "added_at": to_utc_z(NOW),
            "last_checked_at": to_utc_z(NOW),
            "recheck_after_days": 7,
            "last_posting_state": "open",
            "last_recommended_action": "apply_now",
            "last_run_id": "run-not-due",
        },
    }
    Path(watch_store_path).write_text(json.dumps(watches), encoding="utf-8")

    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/8001").mock(
            side_effect=httpx.ConnectError("boom")
        )
        respx.get(due_url).mock(side_effect=httpx.ConnectError("boom"))
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(200, json={"jobs": []})
        )

        resp = client.get("/watch/due")
        assert resp.status_code == 200
        results = resp.json()

    urls_checked = {r["url"] for r in results}
    assert due_url in urls_checked
    assert not_due_url not in urls_checked


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #


def test_health_reports_the_configured_llm_without_probing_it(
    client: TestClient, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/health answers from config only — no socket, so it is always fast.

    No respx mock is installed here on purpose: any HTTP call this route
    made would raise, so a passing test is itself the proof that /health
    does not probe the endpoint.
    """
    resp = client.get("/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["llm_base_url"] == cfg.llm.base_url
    assert payload["llm_model_id"] == cfg.llm.model_id
    # The shipped default is a local endpoint, which needs no credential.
    assert payload["llm_configured"] is True

    remote = "https://generativelanguage.googleapis.com/v1beta/openai"
    monkeypatch.setattr(
        app_module,
        "load_config",
        lambda: cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"base_url": remote})}),
    )
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)
    assert client.get("/health").json()["llm_configured"] is False

    monkeypatch.setenv(cfg.llm.api_key_env, "a-key")
    assert client.get("/health").json()["llm_configured"] is True


def test_root_serves_ui(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Role-Liveness Investigator" in resp.text
