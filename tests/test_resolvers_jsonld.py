"""`rli.resolvers.jsonld` — JSON-LD `JobPosting` extraction (spec.md §3/§10)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import respx

from rli.net import NetClient
from rli.probes.base import ProbeContext
from rli.resolvers import jsonld

SIMPLE_PAGE = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org/",
  "@type": "JobPosting",
  "title": "Senior Backend Engineer",
  "datePosted": "2026-08-20",
  "validThrough": "2026-09-20T00:00:00Z",
  "hiringOrganization": {
    "@type": "Organization",
    "name": "Acme",
    "sameAs": "https://www.acme.com"
  }
}
</script>
</head><body>hello</body></html>
"""

GRAPH_PAGE = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {"@type": "Organization", "name": "Acme", "url": "https://acme.com"},
    {
      "@type": ["JobPosting"],
      "title": "Staff Engineer",
      "datePosted": "2026-07-01T08:00:00-04:00",
      "hiringOrganization": {"@type": "Organization", "url": "https://acme.com"}
    }
  ]
}
</script>
</head><body>hello</body></html>
"""

MALFORMED_THEN_VALID_PAGE = """
<html><head>
<script type="application/ld+json">
{ this is not valid json ]]
</script>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Recovers After Bad Block", "datePosted": "2026-06-01"}
</script>
</head><body>hello</body></html>
"""

NO_JOB_POSTING_PAGE = """
<html><head>
<script type="application/ld+json">
{"@type": "WebSite", "name": "Acme Careers"}
</script>
</head><body>no jobs here</body></html>
"""

NO_SCRIPT_PAGE = "<html><body>plain page, no structured data</body></html>"


def _net(ctx_factory: Callable[..., ProbeContext]) -> NetClient:
    return ctx_factory().net_client("json_ld")


def test_extract_job_posting_simple() -> None:
    posting = jsonld.extract_job_posting(SIMPLE_PAGE)
    assert posting is not None
    assert posting.title == "Senior Backend Engineer"
    assert posting.date_posted == datetime(2026, 8, 20, tzinfo=UTC)
    assert posting.valid_through == datetime(2026, 9, 20, tzinfo=UTC)
    assert posting.company_domain_candidate == "acme.com"


def test_extract_job_posting_handles_at_graph_array() -> None:
    posting = jsonld.extract_job_posting(GRAPH_PAGE)
    assert posting is not None
    assert posting.title == "Staff Engineer"
    assert posting.date_posted == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert posting.company_domain_candidate == "acme.com"


def test_extract_job_posting_skips_malformed_script_block() -> None:
    posting = jsonld.extract_job_posting(MALFORMED_THEN_VALID_PAGE)
    assert posting is not None
    assert posting.title == "Recovers After Bad Block"


def test_extract_job_posting_returns_none_when_no_job_posting_type() -> None:
    assert jsonld.extract_job_posting(NO_JOB_POSTING_PAGE) is None


def test_extract_job_posting_returns_none_when_no_script_tags() -> None:
    assert jsonld.extract_job_posting(NO_SCRIPT_PAGE) is None


@respx.mock
def test_fetch_job_posting_success(ctx_factory) -> None:
    respx.get("https://careers.acme.com/jobs/backend-engineer").mock(
        return_value=httpx.Response(200, text=SIMPLE_PAGE)
    )
    net = _net(ctx_factory)
    result = jsonld.fetch_job_posting(net, "https://careers.acme.com/jobs/backend-engineer")

    assert result.ok
    assert result.data is not None
    assert result.data.title == "Senior Backend Engineer"


@respx.mock
def test_fetch_job_posting_404_is_structured_failure(ctx_factory) -> None:
    respx.get("https://careers.acme.com/jobs/gone").mock(
        return_value=httpx.Response(404, text="not found")
    )
    net = _net(ctx_factory)
    result = jsonld.fetch_job_posting(net, "https://careers.acme.com/jobs/gone")

    assert result.ok is False
    assert result.status == 404
    assert result.data is None


@respx.mock
def test_fetch_job_posting_ok_with_no_data_when_page_lacks_structured_data(ctx_factory) -> None:
    respx.get("https://careers.acme.com/jobs/plain").mock(
        return_value=httpx.Response(200, text=NO_SCRIPT_PAGE)
    )
    net = _net(ctx_factory)
    result = jsonld.fetch_job_posting(net, "https://careers.acme.com/jobs/plain")

    assert result.ok is True
    assert result.data is None
