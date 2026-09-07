"""Pre-collect dated `company_events` for target companies (PLAN.md M2 bullet 4).

spec.md §4: "For historical `company_events`, pre-collect dated events and
replay by `available_at`; do not live-search during benchmark replay."
i.e. this script (or, more precisely, an agent driving `run_collection`
with a real search backend) is how the `company_events.csv` /
`collection_status.csv` fixtures used by replay get built — replay itself
never calls a search backend.

This module holds the CSV-writing loop and the `SearchBackend` schema only.
It does NOT implement real web search: `_no_backend` raises
`NotImplementedError` on purpose (see its docstring) so that running this
script with no backend wired in fails loudly instead of silently writing
empty CSVs that look like a completed collection run.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rli.events.store import (
    CollectionStatus,
    CompanyEvent,
    classify_materiality,
    write_collection_status_csv,
    write_events_csv,
)
from rli.models.time import ensure_aware, parse_utc

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS_CSV = PROJECT_ROOT / "scripts" / "targets.csv"
DEFAULT_OUT_EVENTS_CSV = PROJECT_ROOT / "data" / "events" / "company_events.csv"
DEFAULT_OUT_STATUS_CSV = PROJECT_ROOT / "data" / "events" / "collection_status.csv"

# Query templates run per target company. Dates span 2025-01-01 through
# "today" per the task; the two-year (2025/2026) spread in the templates
# below covers that span without hardcoding a moving "today".
QUERY_TEMPLATES = [
    "{company} layoffs 2025",
    "{company} layoffs 2026",
    "{company} hiring freeze",
    "{company} funding 2025",
    "{company} funding 2026",
    "{company} acquisition 2025 2026",
    "{company} expansion 2025 2026",
]

# A raw search hit: {"title": str, "url": str, "snippet": str, "date": "YYYY-MM-DD" | None}
SearchHit = dict
# (company_name, query) -> list of raw hits.
SearchBackend = Callable[[str, str], list[SearchHit]]


def _no_backend(company_name: str, query: str) -> list[SearchHit]:
    """Default `SearchBackend`: always raises.

    Real event collection is done by an agent that runs actual web searches
    (e.g. via its own web-search tool) and either calls
    `rli.events.store.write_events_csv` / `write_collection_status_csv`
    directly, or injects a real `SearchBackend` implementation into
    `run_collection`. This script is not meant to be run standalone against
    the live internet — it only holds the CSV-writing plumbing and the
    query-template/schema contract that a real backend must satisfy.
    """
    raise NotImplementedError(
        "no SearchBackend configured — collect_events.py only defines the "
        "CSV-writing plumbing; real collection requires an agent running "
        "live web searches and injecting a SearchBackend into run_collection "
        "(or writing company_events.csv / collection_status.csv directly)"
    )


# Best-effort keyword guess at event_type from the query template used to
# find a hit. This is a starting point only, NOT a claim of accuracy — a
# real collection pass (an agent reading each article) should assign
# event_type from the article's actual content, not from which query
# surfaced it.
_QUERY_KEYWORD_TO_EVENT_TYPE = [
    ("layoffs", "layoff"),
    ("hiring freeze", "hiring_freeze"),
    ("funding", "funding"),
    ("acquisition", "acquisition"),
    ("expansion", "expansion"),
]


def _guess_event_type(query: str) -> str:
    lowered = query.lower()
    for keyword, event_type in _QUERY_KEYWORD_TO_EVENT_TYPE:
        if keyword in lowered:
            return event_type
    return "other"


def _read_targets(targets_csv: Path) -> list[dict[str, str]]:
    with targets_csv.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_collection(
    targets_csv: Path,
    out_events_csv: Path,
    out_status_csv: Path,
    search_backend: SearchBackend,
    *,
    now: datetime,
) -> None:
    """Run `QUERY_TEMPLATES` through `search_backend` for every target company.

    For each target row in `targets_csv` (`company_id` = `website_domain`),
    every query template is run through `search_backend`. Hits carrying a
    concrete `date` become `CompanyEvent`s (`available_at` = that hit's
    date, at midnight UTC, since a raw search hit only carries a date, not a
    time; `event_date` = the same date, since this best-effort loop has no
    way to distinguish "when the event happened" from "when the article
    reporting it was published" without an agent actually reading the
    article). Hits with no date are dropped (unusable for point-in-time
    replay: spec.md §3 requires evidence to carry `available_at`).

    Per-company `queries_run` / `events_found` tallies are written to
    `out_status_csv`, and all collected events to `out_events_csv`, via
    `rli.events.store`'s writer functions.
    """
    now = ensure_aware(now, "now")
    targets = _read_targets(targets_csv)

    all_events: list[CompanyEvent] = []
    status_rows: list[CollectionStatus] = []

    for target in targets:
        company_id = target["website_domain"]
        company_name = target["company_name"]
        queries_run = 0
        found_for_company: list[CompanyEvent] = []

        for template in QUERY_TEMPLATES:
            query = template.format(company=company_name)
            hits = search_backend(company_name, query)
            queries_run += 1

            for hit in hits:
                raw_date = hit.get("date")
                if not raw_date:
                    continue
                event_date = datetime.fromisoformat(raw_date).date()
                available_at = parse_utc(f"{raw_date}T00:00:00Z")
                event_type = _guess_event_type(query)
                headline = hit.get("title", "")
                raw_excerpt = hit.get("snippet")
                found_for_company.append(
                    CompanyEvent(
                        company_id=company_id,
                        event_type=event_type,  # type: ignore[arg-type]
                        event_date=event_date,
                        available_at=available_at,
                        source_url=hit.get("url", ""),
                        headline=headline,
                        raw_excerpt=raw_excerpt,
                        source_quality="news",
                        materiality=classify_materiality(event_type, headline, raw_excerpt),  # type: ignore[arg-type]
                        collected_at=now,
                    )
                )

        all_events.extend(found_for_company)
        status_rows.append(
            CollectionStatus(
                company_id=company_id,
                searched_at=now,
                queries_run=queries_run,
                events_found=len(found_for_company),
            )
        )

    write_events_csv(all_events, out_events_csv)
    write_collection_status_csv(status_rows, out_status_csv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS_CSV)
    parser.add_argument("--out-events", type=Path, default=DEFAULT_OUT_EVENTS_CSV)
    parser.add_argument("--out-status", type=Path, default=DEFAULT_OUT_STATUS_CSV)
    args = parser.parse_args(argv)

    print(
        "collect_events.py has no real SearchBackend wired in standalone — "
        f"would have targeted {args.targets} -> {args.out_events}, {args.out_status}, "
        "but real company_events collection is done by an agent running live "
        "web searches and injecting a SearchBackend into run_collection() "
        "(see the module docstring). Refusing to write empty/no-op CSVs.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
