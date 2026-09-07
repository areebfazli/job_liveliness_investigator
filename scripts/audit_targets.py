"""Coverage audit for candidate target companies (M1).

Hits the live Greenhouse / Ashby / Lever public job-board APIs for each
candidate in a CSV, records whether the board is reachable, how many open
jobs it reports, and writes a full results CSV for traceability.

Usage:
    python scripts/audit_targets.py [--input path/to/candidates.csv] [--output path/to/results.csv]

Stdlib + httpx only. One-off audit script -- kept simple on purpose.
"""

from __future__ import annotations

import argparse
import csv
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "data" / "targets" / "candidates_raw.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "targets" / "audit_results_full.csv"

USER_AGENT = "role-liveness-investigator-coverage-audit/0.1 (contact: uzair.khan@progbid.com)"
REQUEST_TIMEOUT_S = 10.0
RATE_LIMIT_SECONDS = 1.0

FIELDNAMES = [
    "company_name",
    "website_domain",
    "ats",
    "tenant",
    "api_ok",
    "open_job_count",
    "checked_at",
    "note",
]


@dataclass
class AuditResult:
    company_name: str
    website_domain: str
    ats: str
    tenant: str
    api_ok: bool
    open_job_count: int | str
    checked_at: str
    note: str


def build_url(ats: str, tenant: str) -> str:
    if ats == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=false"
    if ats == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{tenant}"
    if ats == "lever":
        return f"https://api.lever.co/v0/postings/{tenant}?mode=json"
    raise ValueError(f"unknown ats: {ats!r}")


def extract_job_count(ats: str, payload: object) -> int | None:
    """Return open job count if the payload has the expected shape, else None."""
    if ats in ("greenhouse", "ashby"):
        if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
            return len(payload["jobs"])
        return None
    if ats == "lever":
        if isinstance(payload, list):
            return len(payload)
        return None
    return None


def fetch_with_retry(client: httpx.Client, url: str) -> tuple[httpx.Response | None, str | None]:
    """GET url, retrying once on timeout/connection error. Returns (response, note)."""
    for attempt in range(2):
        try:
            resp = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_S)
            return resp, None
        except httpx.TimeoutException:
            if attempt == 0:
                time.sleep(RATE_LIMIT_SECONDS)
                continue
            return None, "timeout"
        except httpx.ConnectError as exc:
            if attempt == 0:
                time.sleep(RATE_LIMIT_SECONDS)
                continue
            return None, f"connection error: {exc}"
        except httpx.HTTPError as exc:
            if attempt == 0:
                time.sleep(RATE_LIMIT_SECONDS)
                continue
            return None, f"http error: {exc}"
    return None, "unknown error"


def audit_row(client: httpx.Client, row: dict[str, str]) -> AuditResult:
    company_name = row["company_name"]
    website_domain = row["website_domain"]
    ats = row["ats"]
    tenant = row["tenant"]

    checked_at = datetime.now(timezone.utc).isoformat()
    url = build_url(ats, tenant)

    resp, note = fetch_with_retry(client, url)

    if resp is None:
        return AuditResult(
            company_name, website_domain, ats, tenant, False, "", checked_at, note or "unknown error"
        )

    if resp.status_code != 200:
        return AuditResult(
            company_name,
            website_domain,
            ats,
            tenant,
            False,
            "",
            checked_at,
            str(resp.status_code),
        )

    try:
        payload = resp.json()
    except ValueError:
        return AuditResult(
            company_name, website_domain, ats, tenant, False, "", checked_at, "invalid json"
        )

    count = extract_job_count(ats, payload)
    if count is None:
        return AuditResult(
            company_name,
            website_domain,
            ats,
            tenant,
            False,
            "",
            checked_at,
            "unexpected response shape",
        )

    if count == 0:
        return AuditResult(
            company_name, website_domain, ats, tenant, True, 0, checked_at, "empty jobs array"
        )

    return AuditResult(company_name, website_domain, ats, tenant, True, count, checked_at, "")


def read_candidates(input_path: Path) -> list[dict[str, str]]:
    with input_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_results(results: list[AuditResult], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def print_summary(results: list[AuditResult]) -> None:
    per_ats: dict[str, dict[str, int]] = {}
    total_verified = 0
    total_failed = 0
    for r in results:
        bucket = per_ats.setdefault(r.ats, {"verified": 0, "failed": 0})
        if r.api_ok:
            bucket["verified"] += 1
            total_verified += 1
        else:
            bucket["failed"] += 1
            total_failed += 1

    print("\n=== Coverage audit summary ===")
    for ats in sorted(per_ats):
        b = per_ats[ats]
        print(f"  {ats:10s} verified={b['verified']:3d}  failed={b['failed']:3d}")
    print(f"  {'TOTAL':10s} verified={total_verified:3d}  failed={total_failed:3d}")
    print(f"  candidates checked: {len(results)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Path to candidate CSV (default: data/targets/candidates_raw.csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path to write full audit results CSV (default: data/targets/audit_results_full.csv)",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=RATE_LIMIT_SECONDS,
        help="Seconds to sleep between requests (default: 1.0)",
    )
    args = parser.parse_args()

    input_path = args.input if args.input.is_absolute() else (Path.cwd() / args.input)
    if not input_path.exists():
        # fall back to project-root-relative resolution
        input_path = (PROJECT_ROOT / args.input).resolve()

    output_path = args.output if args.output.is_absolute() else (PROJECT_ROOT / args.output)

    rows = read_candidates(input_path)
    results: list[AuditResult] = []

    with httpx.Client() as client:
        for i, row in enumerate(rows):
            result = audit_row(client, row)
            results.append(result)
            status = "OK " if result.api_ok else "FAIL"
            print(
                f"[{i + 1}/{len(rows)}] {status} {result.company_name:30s} "
                f"({result.ats}/{result.tenant}) -> {result.open_job_count or 0} jobs"
                + (f"  [{result.note}]" if result.note else "")
            )
            if i < len(rows) - 1:
                time.sleep(args.rate_limit)

    write_results(results, output_path)
    print(f"\nWrote {len(results)} rows to {output_path}")
    print_summary(results)


if __name__ == "__main__":
    main()
