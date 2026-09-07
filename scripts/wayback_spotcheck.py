"""Rough Wayback Machine capture spot-check for a sample of verified target boards.

Queries the Wayback CDX API at 0.5 req/s (one request every 2 seconds) for a
small, ATS-balanced sample of boards from scripts/targets.csv, and records
how many capture rows (if any) came back for each. This is a spot-check only
-- not a full backfill.

Usage:
    python scripts/wayback_spotcheck.py [--targets T.csv] [--output O.csv] [--sample-size 10]
"""

from __future__ import annotations

import argparse
import csv
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = PROJECT_ROOT / "scripts" / "targets.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "targets" / "wayback_spotcheck_results.csv"

USER_AGENT = "role-liveness-investigator-coverage-audit/0.1 (contact: uzair.khan@progbid.com)"
REQUEST_TIMEOUT_S = 15.0
RATE_LIMIT_SECONDS = 2.0  # 0.5 req/s
SAMPLE_SIZE = 10

CDX_URL_TEMPLATES = {
    "greenhouse": "https://web.archive.org/cdx/search/cdx?url=boards.greenhouse.io/{tenant}*&output=json&limit=5&from=2025",
    "ashby": "https://web.archive.org/cdx/search/cdx?url=jobs.ashbyhq.com/{tenant}*&output=json&limit=5&from=2025",
    "lever": "https://web.archive.org/cdx/search/cdx?url=jobs.lever.co/{tenant}*&output=json&limit=5&from=2025",
}


@dataclass
class WaybackResult:
    company_name: str
    ats: str
    tenant: str
    capture_count: int
    note: str


def read_targets(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pick_sample(rows: list[dict[str, str]], sample_size: int) -> list[dict[str, str]]:
    """Pick a spread across ATS types, roughly proportional to verified counts,
    with at least 2 of each ATS type present."""
    by_ats: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_ats.setdefault(r["ats"], []).append(r)

    ats_types = sorted(by_ats)
    total = len(rows)

    # start each present ATS with a floor of 2 (or all it has, if fewer)
    quota: dict[str, int] = {}
    for ats in ats_types:
        quota[ats] = min(2, len(by_ats[ats]))

    remaining = sample_size - sum(quota.values())

    # distribute remaining proportionally to each ATS's share of total rows
    if remaining > 0:
        # proportional shares based on full population, largest remainder method
        shares = {ats: (len(by_ats[ats]) / total) * sample_size for ats in ats_types}
        # subtract what's already allocated via the floor
        fractional = {ats: shares[ats] - quota[ats] for ats in ats_types}
        # sort by who "deserves" more next, cap at available rows
        order = sorted(ats_types, key=lambda a: fractional[a], reverse=True)
        i = 0
        while remaining > 0 and any(quota[a] < len(by_ats[a]) for a in ats_types):
            ats = order[i % len(order)]
            if quota[ats] < len(by_ats[ats]):
                quota[ats] += 1
                remaining -= 1
            i += 1

    sample: list[dict[str, str]] = []
    for ats in ats_types:
        sample.extend(by_ats[ats][: quota[ats]])
    return sample[:sample_size]


def fetch_capture_count(client: httpx.Client, ats: str, tenant: str) -> tuple[int, str]:
    """Query the CDX API, retrying once on timeout/connection error.

    A throttled or slow CDX response is a coverage *gap*, not evidence of
    absence -- so a single transient failure is retried once (with the same
    politeness delay) before being reported as a note, rather than being
    counted as zero captures.
    """
    url = CDX_URL_TEMPLATES[ats].format(tenant=tenant)
    resp: httpx.Response | None = None
    note: str | None = None

    for attempt in range(2):
        try:
            resp = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_S)
            note = None
            break
        except httpx.TimeoutException:
            note = "timeout"
        except httpx.HTTPError as exc:
            note = f"connection error: {exc}"
        if attempt == 0:
            time.sleep(RATE_LIMIT_SECONDS)

    if resp is None:
        return 0, note or "unknown error"

    if resp.status_code != 200:
        return 0, str(resp.status_code)

    try:
        data = resp.json()
    except ValueError:
        return 0, "invalid json"

    if not isinstance(data, list):
        return 0, "unexpected response shape"

    # CDX JSON format: first row is the field-name header, rest are capture rows
    if len(data) <= 1:
        return 0, "no captures"
    return len(data) - 1, ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample-size", type=int, default=SAMPLE_SIZE)
    args = parser.parse_args()

    targets_path = args.targets if args.targets.is_absolute() else (PROJECT_ROOT / args.targets)
    output_path = args.output if args.output.is_absolute() else (PROJECT_ROOT / args.output)

    rows = read_targets(targets_path)
    sample = pick_sample(rows, args.sample_size)

    results: list[WaybackResult] = []
    with httpx.Client() as client:
        for i, row in enumerate(sample):
            count, note = fetch_capture_count(client, row["ats"], row["tenant"])
            result = WaybackResult(row["company_name"], row["ats"], row["tenant"], count, note)
            results.append(result)
            print(
                f"[{i + 1}/{len(sample)}] {result.company_name:25s} ({result.ats}/{result.tenant}) "
                f"-> {result.capture_count} captures" + (f"  [{note}]" if note else "")
            )
            if i < len(sample) - 1:
                time.sleep(RATE_LIMIT_SECONDS)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["company_name", "ats", "tenant", "capture_count", "note"]
        )
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))

    print(f"\nWrote {len(results)} rows to {output_path}")

    print("\n=== Capture density per ATS ===")
    by_ats: dict[str, list[WaybackResult]] = {}
    for r in results:
        by_ats.setdefault(r.ats, []).append(r)
    for ats in sorted(by_ats):
        sampled = by_ats[ats]
        with_captures = sum(1 for r in sampled if r.capture_count > 0)
        print(f"  {ats:10s} {with_captures}/{len(sampled)} sampled had captures")


if __name__ == "__main__":
    main()
