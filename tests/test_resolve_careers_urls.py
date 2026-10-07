"""Job URLs that do not name their ATS board on their own.

* A Greenhouse board embedded on the company's own careers site: the board
  API's `absolute_url` (stored as `postings.canonical_url`) is a careers-page
  URL carrying `?gh_jid=<id>`, which used to resolve as `generic` and leave
  the case `identity_unresolved`.
* Greenhouse EU boards (`job-boards.eu.greenhouse.io`).
* Ashby org slugs with dots (`checkout.com`), and a tenant spelled in another
  letter case than the collector's (`sierra` vs `Sierra`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx
from test_eval_case import _run_and_probes
from test_eval_helpers import add_posting

from rli.config import Config
from rli.eval.case import build_case_state
from rli.probes.base import ProbeContext
from rli.probes.lookups import known_ats_ref
from rli.probes.resolve_posting import resolve_posting
from rli.resolvers.detect import AtsRef, detect_ats, greenhouse_job_id_from_query

NOW = datetime(2026, 9, 7, tzinfo=UTC)
CAREERS_URL = "https://careers.acme.com/positions/5001?gh_jid=5001"
NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"
ASHBY_UUID = "b6a6d1c0-1234-4abc-8def-0123456789ab"


def _gh_job(job_id: str, absolute_url: str) -> dict:
    return {
        "id": int(job_id),
        "title": "Backend Engineer",
        "absolute_url": absolute_url,
        "first_published": "2026-08-25T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
        "content": "<p>desc</p>",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
    }


def _ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    return ctx_factory(now=lambda: NOW)


# ---------------------------------------------------------------------------
# detect_ats / greenhouse_job_id_from_query (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://job-boards.eu.greenhouse.io/clevr/jobs/4474694101",
        "https://boards.eu.greenhouse.io/clevr/jobs/4474694101/",
    ],
)
def test_detect_greenhouse_eu_board(url: str) -> None:
    assert detect_ats(url) == AtsRef(
        ats="greenhouse",
        tenant="clevr",
        job_id="4474694101",
        canonical_url="https://job-boards.eu.greenhouse.io/clevr/jobs/4474694101",
    )


@pytest.mark.parametrize("org", ["checkout.com", "careers.azx.io", "mistral.ai"])
def test_detect_ashby_and_lever_org_slug_with_dots(org: str) -> None:
    ashby = detect_ats(f"https://jobs.ashbyhq.com/{org}/{ASHBY_UUID}")
    lever = detect_ats(f"https://jobs.lever.co/{org}/{ASHBY_UUID}")
    assert ashby is not None and (ashby.ats, ashby.tenant, ashby.job_id) == (
        "ashby",
        org,
        ASHBY_UUID,
    )
    assert lever is not None and (lever.ats, lever.tenant) == ("lever", org)


@pytest.mark.parametrize("org", [".", "..", ".hidden"])
def test_detect_ashby_org_slug_cannot_be_a_dot_segment(org: str) -> None:
    ref = detect_ats(f"https://jobs.ashbyhq.com/{org}/{ASHBY_UUID}")
    assert ref is not None and ref.ats == "generic"


def test_careers_url_alone_is_still_generic() -> None:
    ref = detect_ats(CAREERS_URL)
    assert ref is not None and ref.ats == "generic"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CAREERS_URL, "5001"),
        ("https://jobs.elastic.co/jobs?gh_jid=6907907&gh_jid=6907907", "6907907"),
        (
            "https://coreweave.com/careers/job?4688540006&board=coreweave&gh_jid=4688540006",
            "4688540006",
        ),
        ("https://acme.com/careers?gh_jid=abc", None),
        ("https://acme.com/careers?gh_jid=12%0A", None),
        ("https://acme.com/careers?gh_jid=", None),
        ("https://acme.com/careers/5001", None),
        ("not a url", None),
    ],
)
def test_greenhouse_job_id_from_query(url: str, expected: str | None) -> None:
    assert greenhouse_job_id_from_query(url) == expected


# ---------------------------------------------------------------------------
# known_ats_ref (corpus lookup)
# ---------------------------------------------------------------------------


def test_exact_canonical_url_of_a_collected_posting(conn: sqlite3.Connection) -> None:
    add_posting(conn, job_id="5001", canonical_url=CAREERS_URL)
    ref = known_ats_ref(conn, CAREERS_URL, detect_ats(CAREERS_URL))
    assert ref == AtsRef(ats="greenhouse", tenant="acme", job_id="5001", canonical_url=CAREERS_URL)


def test_exact_match_ignores_archive_rows_without_a_tenant(conn: sqlite3.Connection) -> None:
    add_posting(
        conn,
        job_id="7",
        tenant=None,
        posting_id="archive:acme.com:7",
        canonical_url="https://careers.acme.com/jobs/7",
    )
    url = "https://careers.acme.com/jobs/7"
    assert known_ats_ref(conn, url, detect_ats(url)) is None


def test_gh_jid_of_a_new_job_on_a_known_careers_host(conn: sqlite3.Connection) -> None:
    """A job the corpus has not collected yet, on a host one board publishes to."""
    add_posting(conn, job_id="5001", canonical_url=CAREERS_URL)
    url = "https://careers.acme.com/positions/9999?gh_jid=9999"
    ref = known_ats_ref(conn, url, detect_ats(url))
    assert ref == AtsRef(ats="greenhouse", tenant="acme", job_id="9999", canonical_url=url)


def test_gh_jid_on_the_company_website_domain(conn: sqlite3.Connection) -> None:
    """No collected posting on `jobs.acme.com`, but the company is `acme.com`."""
    add_posting(conn, job_id="5001")  # boards.greenhouse.io URL, company acme.com
    url = "https://jobs.acme.com/open-roles?gh_jid=42"
    ref = known_ats_ref(conn, url, detect_ats(url))
    assert ref is not None and (ref.ats, ref.tenant, ref.job_id) == ("greenhouse", "acme", "42")


def test_gh_jid_on_an_unknown_host_stays_unresolved(conn: sqlite3.Connection) -> None:
    """An id alone must not point a stranger's URL at a collected company's job."""
    add_posting(conn, job_id="5001", canonical_url=CAREERS_URL)
    url = "https://evil.example/apply?gh_jid=5001"
    assert known_ats_ref(conn, url, detect_ats(url)) is None


def test_gh_jid_like_wildcards_in_host_do_not_match_other_hosts(conn: sqlite3.Connection) -> None:
    add_posting(conn, job_id="5001", canonical_url="https://careersXacme.com/j?gh_jid=5001")
    url = "https://careers_acme.com/j?gh_jid=5001"
    assert known_ats_ref(conn, url, detect_ats(url)) is None


def test_gh_jid_host_with_two_boards_prefers_the_board_that_has_the_job(
    conn: sqlite3.Connection,
) -> None:
    add_posting(conn, job_id="1", tenant="acme", canonical_url="https://acme.com/c?gh_jid=1")
    add_posting(conn, job_id="2", tenant="acmeeu", canonical_url="https://acme.com/c?gh_jid=2")
    known = "https://acme.com/c/other?gh_jid=2"
    ref = known_ats_ref(conn, known, detect_ats(known))
    assert ref is not None and (ref.tenant, ref.job_id) == ("acmeeu", "2")
    # A new id on that host is ambiguous: left unresolved, never guessed.
    new = "https://acme.com/c?gh_jid=3"
    assert known_ats_ref(conn, new, detect_ats(new)) is None


def test_ats_tenant_adopts_the_collector_spelling(conn: sqlite3.Connection) -> None:
    add_posting(
        conn,
        job_id=ASHBY_UUID,
        ats="ashby",
        tenant="Sierra",
        company_id="sierra.ai",
        canonical_url=f"https://jobs.ashbyhq.com/Sierra/{ASHBY_UUID}",
    )
    url = f"https://jobs.ashbyhq.com/sierra/{ASHBY_UUID}"
    ref = known_ats_ref(conn, url, detect_ats(url))
    assert ref is not None and (ref.ats, ref.tenant, ref.job_id) == ("ashby", "Sierra", ASHBY_UUID)
    # Already spelled like the collector: nothing to change.
    same = f"https://jobs.ashbyhq.com/Sierra/{ASHBY_UUID}"
    assert known_ats_ref(conn, same, detect_ats(same)) is None


def test_database_error_answers_none() -> None:
    bare = sqlite3.connect(":memory:")  # no `postings` table
    assert known_ats_ref(bare, CAREERS_URL, detect_ats(CAREERS_URL)) is None
    assert known_ats_ref(bare, CAREERS_URL, None) is None


# ---------------------------------------------------------------------------
# resolve_posting (respx)
# ---------------------------------------------------------------------------


@respx.mock
def test_resolve_careers_url_through_the_greenhouse_api(
    conn: sqlite3.Connection, ctx_factory: Callable[..., ProbeContext]
) -> None:
    add_posting(conn, job_id="5001", canonical_url=CAREERS_URL)
    api = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=_gh_job("5001", CAREERS_URL))
    )
    page = respx.get(CAREERS_URL).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))

    result = resolve_posting(CAREERS_URL, _ctx(ctx_factory))

    assert api.called and page.called
    assert result.ok is True
    data = result.data
    assert (data["ats"], data["tenant"], data["job_id"]) == ("greenhouse", "acme", "5001")
    assert data["identity_source"] == "corpus"
    assert data["posting_state"] == "open"
    assert data["canonical_url"] == CAREERS_URL
    claims = {c.claim_type: c for c in data["evidence"]}
    assert claims["first_published"].source_quality == "ats_native"
    assert claims["updated_at"].source_quality == "ats_native"


@respx.mock
def test_resolve_closed_careers_url_is_closed_by_the_api(
    conn: sqlite3.Connection, ctx_factory: Callable[..., ProbeContext]
) -> None:
    add_posting(conn, job_id="5001", canonical_url=CAREERS_URL)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(404)
    )
    respx.get(CAREERS_URL).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))

    result = resolve_posting(CAREERS_URL, _ctx(ctx_factory))

    assert result.data["posting_state"] == "closed"
    assert result.data["canonical_url"] == CAREERS_URL


@respx.mock
def test_unknown_careers_host_stays_generic_and_never_calls_the_api(
    ctx_factory: Callable[..., ProbeContext],
) -> None:
    api = respx.get(url__startswith="https://boards-api.greenhouse.io/").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(CAREERS_URL).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))

    result = resolve_posting(CAREERS_URL, _ctx(ctx_factory))

    assert not api.called
    assert result.data["ats"] == "generic"
    assert result.data["identity_source"] == "url"


@respx.mock
def test_resolve_greenhouse_eu_url_through_the_job_board_api(
    ctx_factory: Callable[..., ProbeContext],
) -> None:
    eu_url = "https://job-boards.eu.greenhouse.io/clevr/jobs/4474694101"
    respx.get("https://boards-api.greenhouse.io/v1/boards/clevr/jobs/4474694101").mock(
        return_value=httpx.Response(200, json=_gh_job("4474694101", eu_url))
    )
    respx.get(eu_url).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))

    result = resolve_posting(eu_url, _ctx(ctx_factory))

    assert (result.data["ats"], result.data["tenant"]) == ("greenhouse", "clevr")
    assert result.data["posting_state"] == "open"
    assert result.data["identity_source"] == "url"


@respx.mock
def test_resolve_ashby_dotted_org_and_collector_spelling(
    conn: sqlite3.Connection, ctx_factory: Callable[..., ProbeContext]
) -> None:
    add_posting(
        conn,
        job_id=ASHBY_UUID,
        ats="ashby",
        tenant="Checkout.com",
        company_id="checkout.com",
        canonical_url=f"https://jobs.ashbyhq.com/Checkout.com/{ASHBY_UUID}",
    )
    job_url = f"https://jobs.ashbyhq.com/checkout.com/{ASHBY_UUID}"
    board = respx.get("https://api.ashbyhq.com/posting-api/job-board/Checkout.com").mock(
        return_value=httpx.Response(
            200, json={"jobs": [{"id": ASHBY_UUID, "title": "Designer", "jobUrl": job_url}]}
        )
    )
    respx.get(job_url).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))

    result = resolve_posting(job_url, _ctx(ctx_factory))

    assert board.called
    assert (result.data["ats"], result.data["tenant"]) == ("ashby", "Checkout.com")
    assert result.data["posting_state"] == "open"


# ---------------------------------------------------------------------------
# End to end: the case now has an identity
# ---------------------------------------------------------------------------


@respx.mock
def test_build_case_state_resolves_a_careers_url_to_the_collected_posting(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    posting_id = add_posting(conn, job_id="5001", canonical_url=CAREERS_URL)
    gh_job = _gh_job("5001", CAREERS_URL)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001").mock(
        return_value=httpx.Response(200, json=gh_job)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [gh_job]})
    )
    respx.get(CAREERS_URL).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))

    with _run_and_probes(conn, cfg) as (_run, probes):
        case = build_case_state(conn, cfg, url=CAREERS_URL, now=NOW, probes=probes)

    assert case.posting_id == posting_id
    assert case.posting_row_exists is True
    assert case.company_id == "acme.com"
    assert case.case_file() is not None
    assert {e.claim_type for e in case.evidence} >= {"posting_state", "board_present"}
