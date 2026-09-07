"""`rli.eval.case`: the always-run pair, identity resolution, and the
evidence-claim contract it owes (spec.md §2/§4; module docstring of
`rli.eval.case`).

Every test builds its own `Run` + `ProbeRunner` (via `open_probe_runner`)
rather than going through `run_system_a`/`run_system_b`, so `build_case_state`
and `extend_case_state` are exercised directly and in isolation from the
routing/policy layers those two systems add on top.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import respx
from test_eval_helpers import COMPANY, add_posting

from rli.config import Config
from rli.eval.case import (
    BOARD_SNAPSHOT_URL_PLACEHOLDER,
    CLAIM_BOARD_PRESENT,
    build_case_state,
    extend_case_state,
)
from rli.eval.runner import STEP_PROBE_SKIPPED, ProbeRunner, Run
from rli.events.store import CompanyEvent, upsert_event
from rli.models.policy_inputs import UNKNOWN
from rli.policy.inputs import CLAIM_BOARD_ABSENT, CLAIM_POSTING_STATE
from rli.probes.company_events import CompanyEventsProbe

NOW = datetime(2026, 9, 7, tzinfo=UTC)

GH_JOB = {
    "id": 5001,
    "title": "Backend Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/5001",
    "first_published": "2026-08-25T00:00:00Z",
    "content": "<p>desc</p>",
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "Remote"}],
}

NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"

GH_BOARD_WITH_JOB = {"jobs": [GH_JOB]}
GH_BOARD_EMPTY = {"jobs": []}


@contextmanager
def _run_and_probes(
    conn: sqlite3.Connection, cfg: Config, *, system: str = "A"
) -> Iterator[tuple[Run, ProbeRunner]]:
    """Build a `Run` + `ProbeRunner` pair the way `run_system_a`/`b` do.

    `use_tool_cache=False` so a respx-mocked fetch can never be answered
    from a stored `tool_cache` row (see the eval runner's docstring); `sleep`
    is a no-op so a retry path never actually waits.
    """
    from rli.eval.runner import open_probe_runner

    with Run(
        conn,
        cfg,
        input_url="test://case",
        system=system,  # type: ignore[arg-type]
        config_hash="cfg:test",
        started_at=NOW,
    ) as run:
        with open_probe_runner(
            conn, cfg, run, NOW, sleep=lambda _s: None, use_tool_cache=False
        ) as probes:
            yield run, probes


@respx.mock
def test_build_case_state_emits_posting_state_and_board_present_claims(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="5001", first_observed=NOW, last_seen_open=NOW)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5001").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD_WITH_JOB)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )

    assert case.posting_id == "greenhouse:acme:5001"
    assert case.posting_row_exists is True
    assert case.company_id == COMPANY

    # The contract in rli.eval.case's module docstring: these two claim
    # types are synthesized by THIS module, not by any probe.
    state_claims = [c for c in case.evidence if c.claim_type == CLAIM_POSTING_STATE]
    assert len(state_claims) == 1
    assert state_claims[0].probe == "resolve_posting"
    assert state_claims[0].value == "open"
    assert state_claims[0].source_quality == "ats_native"

    present_claims = [c for c in case.evidence if c.claim_type == CLAIM_BOARD_PRESENT]
    assert len(present_claims) == 1
    assert present_claims[0].probe == "board_snapshot"
    assert present_claims[0].source_quality == "ats_native"


@respx.mock
def test_build_case_state_emits_board_absent_when_job_missing_from_listing(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="5001", first_observed=NOW, last_seen_open=NOW)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5001").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    # The board listing does NOT include job 5001.
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD_EMPTY)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )

    absent_claims = [c for c in case.evidence if c.claim_type == CLAIM_BOARD_ABSENT]
    assert len(absent_claims) == 1
    absent = absent_claims[0]
    assert absent.probe == "board_snapshot"
    assert absent.source_quality == "ats_native"
    # An absence has no fetchable per-job URL: the self-describing placeholder.
    assert absent.source_url == BOARD_SNAPSHOT_URL_PLACEHOLDER.format(
        ats="greenhouse", tenant="acme"
    )


@respx.mock
def test_build_case_state_prefers_collected_row_team_and_location(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """Team/location come from the collected `postings` row, not the resolver."""
    add_posting(
        conn,
        job_id="5001",
        team="Platform",
        location="NYC",
        first_observed=NOW,
        last_seen_open=NOW,
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5001").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD_WITH_JOB)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )

    assert case.team == "Platform"
    assert case.location == "NYC"
    # Title prefers the freshly-fetched resolver value.
    assert case.title == "Backend Engineer"


@respx.mock
def test_generic_ats_unresolved_identity_yields_no_case_file(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A `generic` posting with no JSON-LD company domain resolves no identity."""
    respx.get("https://careers.acme.com/jobs/1").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://careers.acme.com/jobs/1", now=NOW, probes=probes
        )

    assert case.posting_id is None
    assert case.company_id is None
    assert case.case_file() is None

    # board_snapshot has no endpoint for a generic ATS: recorded, not run.
    skipped = [
        step
        for step in conn.execute(
            "SELECT * FROM run_steps WHERE run_id = ? AND component = 'controller'", (run.id,)
        ).fetchall()
        if step["decision_type"].startswith(STEP_PROBE_SKIPPED)
    ]
    assert any(s["probe_name"] == "board_snapshot" for s in skipped)


@respx.mock
def test_extend_case_state_appends_company_events_evidence_and_preserves_signals(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`company_events` evidence is appended; pre-populated UNKNOWN signals hold.

    Neither `acme.com` nor `globex.com` appears in the real
    `data/events/collection_status.csv`, so `derive_policy_signals` reports
    both booleans UNKNOWN both before and after the probe runs — the case
    state's docstring says the two must "agree by construction".
    """
    add_posting(conn, job_id="5001", first_observed=NOW, last_seen_open=NOW)
    # A real event so the probe has at least one claim to append.
    upsert_event(
        conn,
        CompanyEvent(
            company_id=COMPANY,
            event_type="funding",
            event_date=NOW.date(),
            available_at=NOW,
            source_url="https://news.example.com/acme-funding",
            headline="Acme raises a round",
            materiality="minor",
            collected_at=NOW,
        ),
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/5001").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GH_BOARD_WITH_JOB)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://boards.greenhouse.io/acme/jobs/5001", now=NOW, probes=probes
        )
        assert case.event_signals == (UNKNOWN, UNKNOWN)
        before = len(case.evidence)

        extend_case_state(case, [CompanyEventsProbe], probes=probes)

    assert len(case.evidence) == before + 1
    added = case.evidence[-1]
    assert added.probe == "company_events"
    assert added.claim_type == "funding"
    # The probe's own answer replaces the pre-populated one; they agree.
    assert case.event_signals == (UNKNOWN, UNKNOWN)
    assert case.inputs.material_negative_event == UNKNOWN
    assert case.inputs.freeze_or_pause == UNKNOWN


@respx.mock
def test_extend_case_state_is_a_noop_without_a_case_file(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """No posting_id/company_id -> nothing runs, `case` is returned unchanged."""
    respx.get("https://careers.acme.com/jobs/1").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )

    with _run_and_probes(conn, cfg) as (run, probes):
        case = build_case_state(
            conn, cfg, url="https://careers.acme.com/jobs/1", now=NOW, probes=probes
        )
        before = len(case.evidence)
        result = extend_case_state(case, [CompanyEventsProbe], probes=probes)

    assert result is case
    assert len(case.evidence) == before
