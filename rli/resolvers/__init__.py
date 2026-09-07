"""rli.resolvers — ATS detection + adapters (spec.md §3; PLAN.md M1).

* `detect_ats` — classify a job URL as `greenhouse` / `ashby` / `lever` /
  `generic` (`AtsRef`), never touching the network.
* `greenhouse`, `ashby`, `lever` — typed adapters over each ATS's public API,
  returning `FetchResult` (never raising for network/parse problems).
* `jsonld` — `JobPosting` structured-data extraction from a career page.
"""

from rli.resolvers.common import FetchResult
from rli.resolvers.detect import AtsName, AtsRef, detect_ats

__all__ = ["AtsName", "AtsRef", "FetchResult", "detect_ats"]
