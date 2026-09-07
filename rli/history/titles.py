"""Job-title quality policy — the single source of truth for "is this a title?".

spec.md §4 requires repost/version matches to be made on *title*, team,
location and description similarity. That is only meaningful if the stored
title is actually a job title. An archived board page scraped for `<a href>`
job links very often yields the LINK TEXT rather than the role name —
``"Apply"``, ``"Apply now"``, ``"View"``, ``"Learn more"``, or an empty
string — and a whole page of such links produces N postings that all share
one title and therefore score 1.0 against each other. That is the exact
false-positive family observed in `data/match_sample.csv` before this module
existed (gohighlevel.com: ``old_title="Apply"`` -> ``new_title="Apply"``,
``combined=1.0``).

The policy is enforced in TWO places, which is why it lives in its own
module rather than inside either caller:

* `rli.archive.backfill` — at EXTRACTION time, so a junk title is never
  persisted to `board_snapshot_jobs` in the first place. A capture whose
  jobs cannot be given plausible titles is recorded as
  ``coverage_status='partial'`` (a coverage gap) with zero job rows, never
  as a complete capture full of ``"Apply"``.
* `rli.history.matching` — at MATCH time, as a belt-and-braces gate over
  rows that predate the extractor fix (a database backfilled before this
  change still holds junk titles until it is re-backfilled).

Both callers share this module so the two definitions cannot drift. It is
deliberately dependency-free (stdlib only) and holds no I/O.

Design notes / judgment calls:

* Matching is done on the NORMALIZED form (lowercased, punctuation folded to
  single spaces), so ``"APPLY!"``, ``"Apply »"`` and ``"apply"`` are one
  case. Comparison against `GENERIC_TITLES` is EXACT on that normalized
  form — never a substring test — because a substring test would reject the
  real titles ``"Application Security Engineer"`` or ``"Head of Careers"``.
* Two prefix rules supplement the exact set, because they generalize safely:
  a real job title essentially never STARTS with ``"apply"``, and a short
  phrase starting with ``view/see/read/learn/browse`` is a call to action,
  not a role. The ``view``-family rule is bounded to <= 4 tokens so
  ``"Learning Experience Designer"`` (which does not match the word
  boundary anyway) and similar long titles are safe.
* A title that is only digits/punctuation, or shorter than
  `[matching].junk_title_min_chars` normalized characters, is junk.
* PAGE-LEVEL degeneracy is separate from per-title junk: a scrape can
  produce N syntactically fine but identical titles (every card's first
  heading being the company name, say). `dominant_title_fraction` measures
  that, and `is_degenerate_page` applies the configured ceiling. It is only
  applied from `page_shared_title_min_jobs` jobs upward, because "2 of 2
  jobs share a title" is 100% by arithmetic and says nothing.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

__all__ = [
    "GENERIC_TITLES",
    "dominant_title_fraction",
    "is_degenerate_page",
    "is_junk_title",
    "junk_title_reason",
    "normalize_title",
]

# Unicode-aware: `\W` is "not a word character" under the default `str`
# semantics, so accented Latin, CJK, Cyrillic and the rest survive
# normalization. An ASCII-only class here would fold a real title such as
# "アカウント営業/リテール" to the empty string and this module would then
# reject it as junk — that exact false positive was found in the live
# `board_snapshot_jobs` table. `_` is stripped explicitly because `\w`
# counts it as a word character while no job title means it as one.
_NON_ALNUM = re.compile(r"[\W_]+")

# A real job title never starts with "apply".
_APPLY_PREFIX = re.compile(r"^apply\b")

# Call-to-action link text. Bounded by a token count at the call site so a
# genuine title beginning with one of these words is not swept up.
_CTA_PREFIX = re.compile(r"^(view|see|read|learn|browse|explore|show|discover|check)\b")
_CTA_MAX_TOKENS = 4

# Exact (normalized) strings that are never a job title. Kept explicit
# rather than pattern-based so every entry is auditable and so that adding
# one can never accidentally reject a real role.
GENERIC_TITLES: frozenset[str] = frozenset(
    {
        # apply / view call-to-action variants (also covered by the prefix
        # rules; listed for documentation value and for exact-match speed)
        "apply",
        "apply now",
        "apply here",
        "apply today",
        "apply for this job",
        "apply to this job",
        "view",
        "view job",
        "view jobs",
        "view role",
        "view posting",
        "view details",
        "view opening",
        "see job",
        "see details",
        "see more",
        "learn more",
        "read more",
        "more",
        "more info",
        "more information",
        "details",
        "click here",
        "here",
        "link",
        "submit",
        "submit application",
        "start application",
        # generic nouns a card wrapper often contributes
        "job",
        "jobs",
        "role",
        "roles",
        "position",
        "positions",
        "opening",
        "openings",
        "open role",
        "open roles",
        "open position",
        "open positions",
        "current openings",
        "all openings",
        "all jobs",
        "all roles",
        "career",
        "careers",
        "vacancy",
        "vacancies",
        "listing",
        "listings",
        "posting",
        "postings",
        "department",
        "departments",
        "team",
        "teams",
        "location",
        "locations",
        # site chrome that can be picked up by a loose selector
        "home",
        "menu",
        "search",
        "back",
        "next",
        "previous",
        "sign in",
        "log in",
        "login",
        "share",
        "print",
        "email",
        "contact",
        "contact us",
        "about",
        "about us",
        "join us",
        "join our team",
        "we are hiring",
        "hiring",
        "work with us",
        # attribute values that describe a job WITHOUT naming the role
        "remote",
        "onsite",
        "on site",
        "hybrid",
        "full time",
        "part time",
        "fulltime",
        "parttime",
        "contract",
        "temporary",
        "permanent",
        "internship",
        "intern",
        "n a",
        "na",
        "tbd",
        "untitled",
        "unknown",
    }
)


def normalize_title(value: str | None) -> str:
    """Lowercase, fold every non-alphanumeric run to one space, strip.

    The same normalization `rli.history.matching.normalize_text` applies
    before scoring, so "is this junk" and "how similar is this" agree on
    what the string is.
    """
    if value is None:
        return ""
    return _NON_ALNUM.sub(" ", value.lower()).strip()


def junk_title_reason(value: str | None, *, min_chars: int = 3) -> str | None:
    """Why `value` is not a usable job title, or `None` when it is usable.

    Returning the reason rather than a bare bool is what lets the archive
    extractor record WHY a capture was downgraded to ``'partial'`` instead
    of silently dropping rows.
    """
    if value is None or not value.strip():
        return "title is empty"

    normalized = normalize_title(value)
    if not normalized:
        return f"title {value!r} contains no alphanumeric characters"
    if len(normalized) < min_chars:
        return f"title {value!r} is shorter than {min_chars} characters"
    if normalized.isdigit():
        return f"title {value!r} is only digits"
    if normalized in GENERIC_TITLES:
        return f"title {value!r} is a generic call-to-action / chrome string"
    if _APPLY_PREFIX.match(normalized):
        return f"title {value!r} starts with 'apply' (link text, not a role)"
    if _CTA_PREFIX.match(normalized) and len(normalized.split()) <= _CTA_MAX_TOKENS:
        return f"title {value!r} is a short call-to-action phrase"
    return None


def is_junk_title(value: str | None, *, min_chars: int = 3) -> bool:
    """True when `value` must not be persisted or matched on as a job title."""
    return junk_title_reason(value, min_chars=min_chars) is not None


def dominant_title_fraction(titles: Iterable[str | None]) -> float:
    """Fraction of `titles` taken by the single most common normalized title.

    Empty input scores 0.0 ("nothing is dominated"), so a caller need not
    special-case it before comparing against a ceiling.
    """
    normalized = [normalize_title(t) for t in titles]
    if not normalized:
        return 0.0
    _, count = Counter(normalized).most_common(1)[0]
    return count / len(normalized)


def is_degenerate_page(
    titles: list[str | None],
    *,
    max_fraction: float = 0.60,
    min_jobs: int = 3,
) -> bool:
    """True when one title dominates a scraped page beyond `max_fraction`.

    Below `min_jobs` the measure is meaningless (1 of 1 job is always 100%),
    so it is not applied at all — per-title junk detection still is.
    """
    if len(titles) < min_jobs:
        return False
    return dominant_title_fraction(titles) > max_fraction
