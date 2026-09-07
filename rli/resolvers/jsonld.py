"""JSON-LD `JobPosting` extraction from an employer career page (spec.md §3/§10).

Fetches the page over the `json_ld` probe's `"*"` allowlist (any https host,
still subject to `rli.net.check_allowed`'s SSRF hardening) and looks for a
`<script type="application/ld+json">` block describing a schema.org
`JobPosting`, per https://developers.google.com/search/docs/appearance/structured-data/job-posting.

Real career pages vary a lot in how they wrap this: a bare `JobPosting`
object, an array of nodes, or a top-level `@graph` array (JSON-LD's way of
bundling multiple nodes under one context) that may itself contain nested
arrays/`@graph`s. `_iter_nodes` walks all of these. Any single malformed
`<script>` block is skipped (spec.md §2 "never raising for network
problems" extends to untrusted page content in general) — parsing continues
with the next block/node rather than failing the whole fetch.
"""

from __future__ import annotations

import json
from datetime import datetime

from bs4 import BeautifulSoup
from pydantic import BaseModel

from rli.net import NetClient
from rli.resolvers.common import FetchResult, normalize_domain, parse_flexible_datetime

__all__ = ["JsonLdJobPosting", "extract_job_posting", "fetch_job_posting"]


class JsonLdJobPosting(BaseModel):
    """Fields extracted from a `JobPosting` JSON-LD node."""

    title: str | None = None
    date_posted_raw: str | None = None
    date_posted: datetime | None = None
    valid_through_raw: str | None = None
    valid_through: datetime | None = None
    company_domain_candidate: str | None = None


def _iter_nodes(payload: object):
    """Yield every dict-shaped JSON-LD node in `payload`, flattening @graph/arrays."""
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_nodes(item)
    elif isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_nodes(item)
        yield payload


def _is_job_posting(node: object) -> bool:
    if not isinstance(node, dict):
        return False
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return "JobPosting" in node_type
    return node_type == "JobPosting"


def _hiring_org_domain(node: dict) -> str | None:
    org = node.get("hiringOrganization")
    if not isinstance(org, dict):
        return None
    for key in ("sameAs", "url"):
        candidate = normalize_domain(org.get(key) if isinstance(org.get(key), str) else None)
        if candidate:
            return candidate
    return None


def _parse_job_posting_node(node: dict) -> JsonLdJobPosting:
    date_posted_raw = node.get("datePosted") if isinstance(node.get("datePosted"), str) else None
    valid_through_raw = (
        node.get("validThrough") if isinstance(node.get("validThrough"), str) else None
    )
    title = node.get("title") if isinstance(node.get("title"), str) else None
    return JsonLdJobPosting(
        title=title,
        date_posted_raw=date_posted_raw,
        date_posted=parse_flexible_datetime(date_posted_raw),
        valid_through_raw=valid_through_raw,
        valid_through=parse_flexible_datetime(valid_through_raw),
        company_domain_candidate=_hiring_org_domain(node),
    )


def extract_job_posting(html: str) -> JsonLdJobPosting | None:
    """Find the first `JobPosting` node in `html`'s JSON-LD blocks, or None.

    Tolerates malformed JSON in any individual `<script>` block by skipping
    it and continuing to the next.
    """
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string
        if text is None:
            text = script.get_text()
        if not text or not text.strip():
            continue
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            continue
        for node in _iter_nodes(payload):
            if _is_job_posting(node):
                return _parse_job_posting_node(node)
    return None


def fetch_job_posting(net: NetClient, url: str) -> FetchResult[JsonLdJobPosting]:
    """Fetch `url` and extract its `JobPosting` JSON-LD, if any.

    `ok=True, data=None` means the page fetched fine but carried no
    `JobPosting` JSON-LD (or none of its `<script>` blocks parsed) — this is
    NOT a failure; plenty of career pages simply lack structured data.
    """
    result = net.get(url)
    if not result.ok:
        return FetchResult.from_failure(result)
    posting = extract_job_posting(result.body or "")
    return FetchResult.from_success(result, posting)
