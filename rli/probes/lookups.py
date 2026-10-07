"""Read-only lookups shared by the probes (PLAN.md M3).

Two things are needed identically by `rli.probes.repost_history`,
`rli.probes.requirements_drift` and `rli.probes.registry`: the `postings`
row behind a `posting_id`, and the spec.md §4 usable-history test. They live
here rather than being duplicated (or imported across probe modules as
private names) so the SELECT's column list and the history rule each have
exactly one definition. `known_ats_ref` is `rli.probes.resolve_posting`'s
corpus lookup for a job URL that does not name its ATS board itself.

Everything in this module is a pure read. Probes never write (see
`rli.probes.base`).
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlparse

from rli.config import Config
from rli.history.features import coverage_window
from rli.resolvers.detect import AtsRef, greenhouse_job_id_from_query

__all__ = ["has_usable_history", "known_ats_ref", "posting_row"]

# The ATSes with an adapter in `rli.resolvers`; `postings.ats` may also be
# 'other', which no resolver can fetch.
_ADAPTER_ATSES = ("greenhouse", "ashby", "lever")


def posting_row(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    """The `postings` row for `posting_id`, or None if there is no such posting."""
    return conn.execute(
        """
        SELECT posting_id, company_id, ats, ats_tenant_id, ats_job_id, canonical_url,
               title, team, location, first_observed
        FROM postings
        WHERE posting_id = ?
        """,
        (posting_id,),
    ).fetchone()


def has_usable_history(conn: sqlite3.Connection, config: Config, company_id: str) -> bool:
    """Whether `company_id`'s board history is deep enough to reason from.

    spec.md §4: "history probes are ineligible without usable history" and
    "missing history never means flat hiring". The bar is
    `thresholds.min_history_days`, the same one
    `rli.history.features._classify_repost_pattern` uses, so the probe layer
    and the feature layer cannot disagree about what "usable" means.
    """
    return coverage_window(conn, company_id).history_days >= config.thresholds.min_history_days


# ---------------------------------------------------------------------------
# Job URL -> ATS identity, from the corpus
# ---------------------------------------------------------------------------


def known_ats_ref(conn: sqlite3.Connection, url: str, detected: AtsRef | None) -> AtsRef | None:
    """The ATS identity the corpus already knows for `url`, or None to keep `detected`.

    `rli.resolvers.detect.detect_ats` reads an identity off an ATS-hosted URL
    alone. Two kinds of job URL need what the corpus knows on top of that:

    * **A company's own careers page** (`detected.ats == "generic"`). Many
      Greenhouse boards are embedded on the company's site, and the board
      API's `absolute_url` (which the collector stores as
      `postings.canonical_url`) is then e.g.
      `https://careers.airbnb.com/positions/1?gh_jid=1`. The URL carries the
      job id (`gh_jid`) but not the board, so it used to resolve as
      `generic`: no ATS API call, no posting id, and every dynamic probe
      skipped as `identity_unresolved`. Resolved here, in order:

      1. the collected posting whose `canonical_url` is exactly `url`;
      2. for a `gh_jid` URL, the Greenhouse board (tenant) of the collected
         postings published on the same host, or of the company whose
         website domain the host belongs to. When that yields several
         boards, the one under which the corpus has already seen this job id
         wins; otherwise the URL stays unresolved rather than guessing.

      Every answer names a board the corpus already collects, so the only
      new fetch it leads to is the allowlisted Greenhouse job-board API; the
      careers page itself is still read only under the `json_ld` allowlist,
      exactly as before. A `gh_jid` on a host the corpus has never seen
      resolves to nothing: an id alone could point a stranger's URL at
      another company's job.

    * **An ATS URL whose tenant differs only in letter case** from the board
      the collector reads (`jobs.ashbyhq.com/sierra/...` vs the collected
      `Sierra`). The posting ids the collector wrote use its own spelling, so
      that spelling is adopted when it is the only case-insensitive match.

    A read-only lookup; a database error (e.g. a schema without `postings`)
    answers None.
    """
    if detected is None:
        return None
    try:
        if detected.ats == "generic":
            return _careers_url_ref(conn, url)
        return _corpus_tenant_spelling(conn, detected)
    except sqlite3.Error:
        return None


def _corpus_tenant_spelling(conn: sqlite3.Connection, detected: AtsRef) -> AtsRef | None:
    if not detected.tenant:
        return None
    spellings = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT ats_tenant_id FROM postings
            WHERE ats = ? AND ats_tenant_id = ? COLLATE NOCASE
            """,
            (detected.ats, detected.tenant),
        )
    }
    if detected.tenant in spellings or len(spellings) != 1:
        return None
    return detected.model_copy(update={"tenant": spellings.pop()})


def _careers_url_ref(conn: sqlite3.Connection, url: str) -> AtsRef | None:
    url = (url or "").strip()
    if not url:
        return None
    placeholders = ", ".join("?" for _ in _ADAPTER_ATSES)
    row = conn.execute(
        f"""
        SELECT ats, ats_tenant_id, ats_job_id FROM postings
        WHERE canonical_url = ? AND ats IN ({placeholders})
          AND ats_tenant_id IS NOT NULL AND ats_job_id IS NOT NULL
        ORDER BY posting_id
        LIMIT 1
        """,
        (url, *_ADAPTER_ATSES),
    ).fetchone()
    if row is not None:
        return AtsRef(ats=row[0], tenant=str(row[1]), job_id=str(row[2]), canonical_url=url)

    job_id = greenhouse_job_id_from_query(url)
    if job_id is None:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    tenants = _greenhouse_tenants_for_host(conn, host)
    if not tenants:
        return None
    seen_under = {
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT ats_tenant_id FROM postings
            WHERE ats = 'greenhouse' AND ats_job_id = ? AND ats_tenant_id IS NOT NULL
            """,
            (job_id,),
        )
    } & tenants
    if len(seen_under) == 1:
        tenant = seen_under.pop()
    elif len(tenants) == 1:
        tenant = next(iter(tenants))
    else:
        return None
    return AtsRef(ats="greenhouse", tenant=tenant, job_id=job_id, canonical_url=url)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _greenhouse_tenants_for_host(conn: sqlite3.Connection, host: str) -> set[str]:
    """Greenhouse boards the corpus has seen publish on `host`, or of `host`'s company."""
    bare = host[len("www.") :] if host.startswith("www.") else host
    patterns = [
        f"{scheme}://{_like_escape(name)}{tail}"
        for name in sorted({bare, f"www.{bare}"})
        for scheme in ("https", "http")
        for tail in ("/%", "?%")
    ]
    where = " OR ".join("canonical_url LIKE ? ESCAPE '\\'" for _ in patterns)
    tenants = {
        str(r[0])
        for r in conn.execute(
            f"""
            SELECT DISTINCT ats_tenant_id FROM postings
            WHERE ats = 'greenhouse' AND ats_tenant_id IS NOT NULL AND ({where})
            """,
            patterns,
        )
    }
    # The company whose website domain the host belongs to (`careers.acme.com`
    # -> `acme.com`); never a bare public suffix such as `com`.
    labels = bare.split(".")
    domains = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    if domains:
        marks = ", ".join("?" for _ in domains)
        tenants |= {
            str(r[0])
            for r in conn.execute(
                f"""
                SELECT DISTINCT ats_tenant_id FROM postings
                WHERE ats = 'greenhouse' AND ats_tenant_id IS NOT NULL
                  AND company_id IN ({marks})
                """,
                domains,
            )
        }
    return tenants
