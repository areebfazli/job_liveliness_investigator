"""`resolve_posting` — the always-run posting-identity probe (spec.md §4).

Steps (spec.md §4 "Always run" table + this task's spec):

1. `detect_ats(url)` to classify the ATS and extract tenant/job id.
2. If the ATS has an adapter (Greenhouse/Ashby/Lever), fetch it and look for
   the target job — its documented `ats_native` dates become evidence:
   Greenhouse `first_published` / `updated_at`, and Ashby `publishedAt` as
   `last_published` (Ashby documents it as "when the job was LAST
   published", so it is never a first-publish claim); Lever's undocumented
   dates never do (spec.md §3).
3. Fetch the canonical job page and look for `JobPosting` JSON-LD regardless
   of ATS, since it can carry `datePosted` (`page_structured`; on an Ashby
   page it is `publishedAt` again, so it is labelled `last_published`),
   `validThrough` (`declared_expiry`), and a company-domain candidate that
   the ATS APIs never provide.
4. Emit `ProbeClaim`s (ids are assigned by the caller, not here) plus the
   resolved posting identity and the `posting_state` observed *now*:
   `"open"` if the job was found on the ATS/board, `"closed"` if the ATS
   authoritatively said so (404, or absent from the board listing),
   `"unknown"` if a fetch failed or nothing could be determined. A failed
   fetch is NEVER reported as `"closed"` (spec.md §4).

`ProbeResult.ok` reflects whether the *authoritative* lookup (the ATS
adapter fetch, when one applies) completed without a transport/HTTP
failure. A JSON-LD-only failure on an ATS-resolved posting does not flip
`ok` to False — the posting identity and any ATS evidence gathered are
still good; only when there was no other decisive signal (the `generic`
ATS, where JSON-LD is the ONLY source of truth) does the JSON-LD outcome
determine `ok`.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel

from rli.models.probe import ProbeResult
from rli.probes.base import Probe, ProbeClaim, ProbeContext
from rli.resolvers import ashby, greenhouse, jsonld, lever
from rli.resolvers.detect import AtsRef, detect_ats

__all__ = ["ResolvePostingArgs", "ResolvePostingProbe", "resolve_posting"]

PostingState = Literal["open", "closed", "unknown"]


class ResolvePostingArgs(BaseModel):
    url: str


def _claim(
    *,
    claim_type: str,
    value: str,
    source_url: str,
    source_quality: Literal["ats_native", "page_structured", "archive", "news", "enrichment"],
    now,
    raw_excerpt: str | None = None,
    source_event_at=None,
) -> ProbeClaim:
    return ProbeClaim(
        claim_type=claim_type,
        value=value,
        source_url=source_url,
        raw_excerpt=raw_excerpt,
        source_quality=source_quality,
        source_event_at=source_event_at,
        available_at=now,
        fetched_at=now,
    )


def _resolve_greenhouse(ref: AtsRef, ctx: ProbeContext):
    net = ctx.net_client("resolve_posting")
    fetch = greenhouse.fetch_job(net, ref.tenant, ref.job_id)  # type: ignore[arg-type]
    # Read the clock AFTER the fetch: a claim is available once we hold it
    # (spec.md §3), never from the moment we set out to fetch it.
    now = ctx.now()
    claims: list[ProbeClaim] = []
    title: str | None = None
    canonical_url = ref.canonical_url
    posting_state: PostingState = "unknown"
    ok, retryable, error = True, False, None

    if fetch.status == 404:
        posting_state = "closed"
    elif fetch.ok and fetch.data is not None:
        job = fetch.data
        posting_state = "open"
        title = job.title
        canonical_url = job.absolute_url or canonical_url
        if job.first_published_at is not None:
            claims.append(
                _claim(
                    claim_type="first_published",
                    value=job.first_published,  # raw string form
                    source_url=canonical_url,
                    source_quality="ats_native",
                    source_event_at=job.first_published_at,
                    now=now,
                )
            )
        if job.updated_at_dt is not None:
            claims.append(
                _claim(
                    claim_type="updated_at",
                    value=job.updated_at,
                    source_url=canonical_url,
                    source_quality="ats_native",
                    source_event_at=job.updated_at_dt,
                    now=now,
                )
            )
    elif not fetch.ok:
        ok, retryable, error = False, fetch.retryable, fetch.error
        posting_state = "unknown"
    # else: ok but data is None (malformed body) -> posting_state stays "unknown"

    return posting_state, title, canonical_url, claims, ok, retryable, error


def _resolve_ashby(ref: AtsRef, ctx: ProbeContext):
    net = ctx.net_client("resolve_posting")
    fetch = ashby.fetch_board(net, ref.tenant)  # type: ignore[arg-type]
    # Read the clock AFTER the fetch: a claim is available once we hold it
    # (spec.md §3), never from the moment we set out to fetch it.
    now = ctx.now()
    claims: list[ProbeClaim] = []
    title: str | None = None
    canonical_url = ref.canonical_url
    posting_state: PostingState = "unknown"
    ok, retryable, error = True, False, None

    if fetch.ok and fetch.data is not None:
        job = ashby.find_job(fetch.data, ref.job_id)  # type: ignore[arg-type]
        if job is None:
            posting_state = "closed"
        else:
            posting_state = "open"
            title = job.title
            canonical_url = job.job_url or canonical_url
            # Ashby documents `publishedAt` as "when the job was LAST
            # published": a re-publish moves it, so it is NOT first-publish
            # evidence. It is emitted as `last_published`, an ATS update
            # timestamp that `rli.eval.case._refresh_claim` may turn into a
            # refresh only when an observed content change corroborates it.
            if job.published_at_dt is not None:
                claims.append(
                    _claim(
                        claim_type="last_published",
                        value=job.published_at,
                        source_url=canonical_url,
                        source_quality="ats_native",
                        source_event_at=job.published_at_dt,
                        now=now,
                    )
                )
    elif not fetch.ok:
        ok, retryable, error = False, fetch.retryable, fetch.error
        posting_state = "unknown"
    # else: ok but data is None -> stays "unknown"

    return posting_state, title, canonical_url, claims, ok, retryable, error


def _resolve_lever(ref: AtsRef, ctx: ProbeContext):
    net = ctx.net_client("resolve_posting")
    fetch = lever.fetch_board(net, ref.tenant)  # type: ignore[arg-type]
    # Read the clock AFTER the fetch: a claim is available once we hold it
    # (spec.md §3), never from the moment we set out to fetch it.
    now = ctx.now()
    claims: list[ProbeClaim] = []
    title: str | None = None
    canonical_url = ref.canonical_url
    posting_state: PostingState = "unknown"
    ok, retryable, error = True, False, None

    if fetch.ok and fetch.data is not None:
        job = lever.find_job(fetch.data, ref.job_id)  # type: ignore[arg-type]
        if job is None:
            posting_state = "closed"
        else:
            posting_state = "open"
            title = job.text
            canonical_url = job.hosted_url or canonical_url
            # spec.md §3: Lever's date fields are undocumented and NOT
            # trusted as ATS-native publish dates. Record existence only,
            # with the raw (untrusted) createdAt kept as raw_excerpt, never
            # as a first_published/ats_native claim.
            claims.append(
                _claim(
                    claim_type="board_listing",
                    value=job.text or job.id,
                    source_url=canonical_url,
                    source_quality="ats_native",
                    raw_excerpt=(
                        f"createdAt(untrusted, epoch ms)={job.created_at_ms}"
                        if job.created_at_ms is not None
                        else None
                    ),
                    now=now,
                )
            )
    elif not fetch.ok:
        ok, retryable, error = False, fetch.retryable, fetch.error
        posting_state = "unknown"
    # else: ok but data is None -> stays "unknown"

    return posting_state, title, canonical_url, claims, ok, retryable, error


def _resolve_jsonld(canonical_url: str, ctx: ProbeContext, *, date_posted_claim: str):
    net = ctx.net_client("json_ld")
    fetch = jsonld.fetch_job_posting(net, canonical_url)
    # Read the clock AFTER the fetch: a claim is available once we hold it
    # (spec.md §3), never from the moment we set out to fetch it.
    now = ctx.now()
    claims: list[ProbeClaim] = []
    company_domain: str | None = None
    title: str | None = None

    if fetch.ok and fetch.data is not None:
        posting = fetch.data
        title = posting.title
        company_domain = posting.company_domain_candidate
        if posting.date_posted is not None:
            claims.append(
                _claim(
                    claim_type=date_posted_claim,
                    value=posting.date_posted_raw or posting.date_posted.isoformat(),
                    source_url=fetch.url,
                    source_quality="page_structured",
                    source_event_at=posting.date_posted,
                    now=now,
                )
            )
        if posting.valid_through is not None:
            claims.append(
                _claim(
                    claim_type="declared_expiry",
                    value=posting.valid_through_raw or posting.valid_through.isoformat(),
                    source_url=fetch.url,
                    source_quality="page_structured",
                    source_event_at=posting.valid_through,
                    now=now,
                )
            )

    return fetch, title, company_domain, claims


def resolve_posting(url: str, ctx: ProbeContext) -> ProbeResult:
    """Pure function backing `ResolvePostingProbe.run` (spec.md §4)."""
    ref = detect_ats(url)
    if ref is None:
        return ProbeResult(
            ok=False,
            error="could not parse a well-formed http(s) job URL",
            retryable=False,
            data={
                "ats": None,
                "tenant": None,
                "job_id": None,
                "canonical_url": url,
                "title": None,
                "company_domain": None,
                "posting_state": "unknown",
                "evidence": [],
            },
        )

    claims: list[ProbeClaim] = []
    title: str | None = None
    canonical_url = ref.canonical_url
    posting_state: PostingState = "unknown"
    ok, retryable, error = True, False, None

    if ref.ats == "greenhouse":
        posting_state, title, canonical_url, ats_claims, ok, retryable, error = _resolve_greenhouse(
            ref, ctx
        )
        claims.extend(ats_claims)
    elif ref.ats == "ashby":
        posting_state, title, canonical_url, ats_claims, ok, retryable, error = _resolve_ashby(
            ref, ctx
        )
        claims.extend(ats_claims)
    elif ref.ats == "lever":
        posting_state, title, canonical_url, ats_claims, ok, retryable, error = _resolve_lever(
            ref, ctx
        )
        claims.extend(ats_claims)
    # generic: no ATS adapter; JSON-LD below is the only signal.

    # An Ashby-hosted job page renders `publishedAt` as its JSON-LD
    # `datePosted` (the same calendar day on every one of 1,160 resolver runs
    # that recorded both), so on an Ashby page it is the LAST publish date
    # too, and is labelled as such. Everywhere else it stays a first-publish
    # claim, still subject to `rli.eval.case.guard_first_published`.
    jsonld_fetch, jsonld_title, company_domain, jsonld_claims = _resolve_jsonld(
        canonical_url,
        ctx,
        date_posted_claim="last_published" if ref.ats == "ashby" else "first_published",
    )
    claims.extend(jsonld_claims)
    if title is None:
        title = jsonld_title

    if ref.ats == "generic":
        if jsonld_fetch.ok and jsonld_fetch.data is not None:
            posting_state = "open"
        elif not jsonld_fetch.ok:
            ok, retryable, error = False, jsonld_fetch.retryable, jsonld_fetch.error
            posting_state = "unknown"
        # else: page fetched fine but no JobPosting JSON-LD -> stays "unknown"

    return ProbeResult(
        ok=ok,
        error=error,
        retryable=retryable,
        data={
            "ats": ref.ats,
            "tenant": ref.tenant,
            "job_id": ref.job_id,
            "canonical_url": canonical_url,
            "title": title,
            "company_domain": company_domain,
            "posting_state": posting_state,
            "evidence": claims,
        },
    )


class ResolvePostingProbe(Probe):
    """Always-run probe: ATS/board resolution + JSON-LD (spec.md §4)."""

    name: ClassVar[str] = "resolve_posting"
    cost_tier: ClassVar[str] = "low"
    history_required: ClassVar[bool] = False
    ArgsModel: ClassVar[type[BaseModel]] = ResolvePostingArgs

    def run(self, args: ResolvePostingArgs, ctx: ProbeContext) -> ProbeResult:
        return resolve_posting(args.url, ctx)
