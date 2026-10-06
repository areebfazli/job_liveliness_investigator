"""Claim-family lexicon: what a reason's TEXT is about vs. what EVIDENCE it may cite.

This is a shared vocabulary, not a policy decision in its own right, and it
lives in `rli.policy` rather than in `rli.eval.metrics` (where it originated)
for one reason: `rli.agent.explanation` — the citation SUPPORT guard applied
to a live model reply, spec.md §9 — needs exactly this classifier, and
`rli.eval.metrics` is a heavy evaluation-reporting module (SQL readers,
report models, split gates) that the agent layer has no business importing
just to reach one dict and one function. `rli.eval.metrics` still uses this
lexicon for its own `CitationSupport` data-quality figures and re-exports
`CLAIM_FAMILIES`/`FAMILY_KEYWORDS` for backward compatibility, but the
lexicon itself is defined here, once, so the two consumers cannot drift.
"""

from __future__ import annotations

__all__ = [
    "CLAIM_FAMILIES",
    "FAMILY_KEYWORDS",
    "classify_reason",
]

#: Claim family -> the `evidence.claim_type` values that belong to it. These
#: are the claim types this system actually emits (see `rli.probes.*`), not
#: an aspirational taxonomy: a family whose claim types never appear simply
#: never matches. `board_listing` and `version_change` deliberately appear in
#: two families each — a board listing supports both "is it still posted" and
#: "was it reposted", and a version change supports both "was it reposted"
#: and "did the requirements move".
CLAIM_FAMILIES: dict[str, frozenset[str]] = {
    "posting_state": frozenset({"posting_state", "board_present", "board_absent", "board_listing"}),
    # `refreshed_at` is `rli.eval.case`'s synthesized claim (an ATS
    # `updated_at` corroborated by an observed content-hash change); it is a
    # publish-family claim because it is what spec.md §5's amended `recent`
    # rule reads alongside `first_published`.
    # `last_published` (Ashby `publishedAt`, "last published") is an ATS
    # update timestamp like `updated_at`, read only as a refresh candidate. A
    # `publish_date_after_first_seen` claim (a stated publish date the
    # first-published guard in `rli.eval.case` refused) is deliberately NOT
    # here: it supports no publish statement.
    "publish": frozenset({"first_published", "updated_at", "refreshed_at", "last_published"}),
    "expiry": frozenset({"declared_expiry"}),
    "repost": frozenset({"disappeared_interval", "reappeared", "version_change", "board_listing"}),
    "requirements": frozenset({"requirements_changed", "requirements_unchanged", "version_change"}),
    "company_event": frozenset(
        {
            "layoff",
            "layoffs",
            "hiring_freeze",
            "freeze",
            "funding",
            "expansion",
            "acquisition",
            "restructuring",
            "shutdown",
            "hiring_pause",
        }
    ),
}

#: Claim family -> lowercase substrings that identify that family in a
#: human-readable `reason.text`. This is a deliberately crude classifier and
#: is treated as one: a reason matching NO keyword is counted as
#: `reasons_unclassified` and is neither "supported" nor "unsupported",
#: because this module cannot tell whether the citation is wrong or the
#: keyword list is incomplete. Guessing would turn a lexicon gap into a
#: quality finding. All matching families are collected, not just the first,
#: so a reason spanning two claims is supported by either.
FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "posting_state": (
        "still listed",
        "no longer listed",
        "job board",
        "board snapshot",
        "removed",
        "closed",
        "open",
        "listed",
    ),
    # "updated" / "refreshed" classify `rli.policy.explain_stub`'s refresh
    # reason ("The posting was updated on ..."), which would otherwise land
    # in `reasons_unclassified` as a lexicon gap rather than a finding.
    "publish": ("published", "posted", "days ago", "first published", "updated", "refreshed"),
    "expiry": ("expire", "expiry", "valid through", "closing"),
    "repost": ("repost", "reappear", "relisted", "disappeared", "previously"),
    "requirements": ("requirement", "description chang", "unchanged", "drift"),
    "company_event": ("layoff", "freeze", "hiring pause", "funding", "expansion", "acquisition"),
}


def classify_reason(text: str) -> set[str]:
    """The `CLAIM_FAMILIES` keys whose keywords appear in `text` (lowercased)."""
    lowered = text.lower()
    return {
        family
        for family, keywords in FAMILY_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    }
