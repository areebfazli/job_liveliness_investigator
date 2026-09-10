"""Build the case state every system starts from (spec.md §2/§4; PLAN.md M3).

spec.md §2's pipeline begins:

```text
Job URL
  ↓
Resolver + current board snapshot                 deterministic
  ↓
Case file
```

and spec.md §4's "Always run" table names exactly those two components:
`resolve_posting` (ATS, canonical job ID/URL, publish evidence) and
`board_snapshot` (current company/team openings). This module is that step,
shared verbatim by System A and System B so the only difference between them
is which DYNAMIC probes they go on to run (spec.md §6).

Everything here is a read of the collection corpus plus at most two network
probes; nothing in this module writes to `companies`, `postings` or any
capture table. See `rli.eval.runner`'s write invariant.

--------------------------------------------------------------------------
The evidence-claim contract (rli.policy.inputs)
--------------------------------------------------------------------------

`rli.policy.inputs` states that two claim types are the RUNNER's
responsibility, not any probe's, because policy inputs are derived from
`EvidenceItem`s and never from a `ProbeResult.data` blob (spec.md §9: every
user-facing statement maps to evidence). This module satisfies that contract:

* **`posting_state`** — from `resolve_posting`'s `data["posting_state"]`
  (`open | closed | reposted | unknown`), `source_url` = the canonical URL,
  `available_at = fetched_at = now`. `source_quality` is `"ats_native"` when
  an ATS adapter decided the state (Greenhouse/Ashby/Lever: a job fetched, a
  404, or a board listing that did or did not contain the job) and
  `"page_structured"` when JSON-LD was the only signal — the `generic` ATS
  path in `rli.probes.resolve_posting`, where "open" means "the page carried
  a `JobPosting`". An unparseable URL (no `AtsRef` at all) also records
  `"page_structured"`: no adapter spoke, so claiming `ats_native` would
  overstate a claim whose value is `"unknown"` anyway. This mirrors spec.md
  §3's source ranking, where a page's own structured data ranks below the
  ATS API.
* **`board_absent` / `board_present`** — from `board_snapshot`: whether the
  resolved ATS job id appears among the jobs the board listing returned.
  `source_quality="ats_native"` for both (a board listing IS the ATS's own
  answer). `board_present` cites the matched job's `url` when it has one;
  `board_absent` has no fetchable URL by construction — the fact is the
  ABSENCE of a job from a listing — so it cites the self-describing,
  deliberately non-fetchable `BOARD_SNAPSHOT_URL_PLACEHOLDER`, in the
  established style of `rli.probes.repost_history.BOARD_HISTORY_URL_PLACEHOLDER`
  and `rli.history.closures.ARCHIVE_ONLY_URL_PLACEHOLDER`. A reader must be
  able to tell at a glance that the claim cites a listing we fetched, not a
  page that can be opened.

  Neither claim is emitted when the resolver produced no job id: "is job X
  on the board?" is not a question that can be asked without an X, and
  inventing `board_absent` from a missing id would fabricate a
  contradiction. The skip is recorded as a controller step instead.
* **`refreshed_at`** — an ATS `updated_at` timestamp that COINCIDES with a
  content-hash change we actually observed. See "The refresh match" below.

**Why a same-instant `posting_state` + `board_absent` pair is NOT a
contradiction, and why that is correct.** `rli.policy.quality`'s C2 rule
fires only when a `board_absent` claim's `available_at` is *strictly later*
than the `posting_state` claim's. Both claims made here carry the same `now`,
so C2 never fires from one run's own always-run pair — by design.

C2 exists to catch STALE optimism: the resolver said open at time T, and a
board capture taken *after* T did not list the job. Within a single run there
is no such staleness; both observations are of the same instant, and if they
genuinely disagree the resolver has already reported that disagreement
through `posting_state` itself. Consider what the alternative would mean:
`resolve_posting` for Greenhouse fetches the per-job endpoint, so a job that
is `"open"` there but missing from the board listing is a real disagreement —
but calling it a "contradiction" would mark the evidence `mixed` on the
strength of a single ambiguous instant, which is precisely the judgment
spec.md §1 reserves for conflicting sources rather than for one source's
internal timing. And treating the pair as fresher-than-itself would make
C2 fire on essentially every closed posting (state `closed` + absent), which
is not a conflict at all — it is agreement. Cross-run staleness is exactly
what C2 is for, and it will fire the moment a later board capture (or a
stored `board_absent` claim from a previous run, once replay assembles
evidence across runs) postdates the resolver's answer.

--------------------------------------------------------------------------
The refresh match (spec.md §5, Amendment 2026-09-10)
--------------------------------------------------------------------------

The amendment redefines `recent` as "latest of first publish **or** an ATS
`updated_at` that **coincides with an observed content-hash change**". The
second half is the point: a bare `updated_at` proves nothing (ATSes bump it
for a typo fix, a department rename, a re-index), so it counts only when our
own or archived captures independently show the posting's content actually
moving at around the same time. That corroboration is computed here, and it
is emitted as a `refreshed_at` claim rather than plumbed to the policy as a
boolean, because `rli.policy.inputs` derives every input from evidence and
spec.md §9 requires every user-facing statement to map to an evidence id.

Why THIS module: the match needs the corpus connection (`posting_snapshots`
and `board_snapshot_jobs`), which the policy layer deliberately does not
have, and it needs the resolver's `updated_at` claims, which only exist once
the always-run pair has run. It is the same shape as the `board_absent`
claim above.

Attribution: the claim is attributed to `REFRESH_MATCH_PROBE`
(`"refresh_match"`), a name no probe answers to. **No probe fetched it.** It
is a re-reading of captures we already hold, exactly like the archive-state
claims contributed through `probes.always_run_extra()`
(`rli.replay.mode.ARCHIVE_BOARD_STATE_PROBE`), and attributing it to
`resolve_posting` would make the `evidence` table say the resolver observed
something it did not. `source_quality` is `"ats_native"` because the
TIMESTAMP is the ATS's own; the captures only corroborate it.

Three details that are judgment calls, and are load-bearing:

* **The change is dated to an INTERVAL, not to an instant.** Captures
  BRACKET a change: we saw hash A at capture N and hash B at capture N+1, so
  all we know is that the content moved somewhere in `[N, N+1]`. The
  `updated_at` timestamp matches when it falls inside that interval widened
  by `refresh_match_days` at each end (inclusive: exactly at the limit
  matches). This is the same interval-censoring discipline the rest of the
  codebase applies to closures (spec.md §4: "Do not invent an exact
  `closed_at` from sparse captures") — pinning the change to the detecting
  capture alone would make the tolerance silently one-sided and would reject
  real refreshes whenever captures are sparse.
* **`available_at` is `max(the updated_at claim's available_at, the
  DETECTING capture's captured_at)`.** We could not know the content had
  changed before the capture that revealed it, and spec.md §3 forbids
  backdating a current discovery onto the moment the underlying event
  happened. Getting this wrong would let an archive-era replay (spec.md §6)
  read a refresh out of a capture taken months after `T`.
* **When one `updated_at` matches SEVERAL changes, the EARLIEST change's
  detecting capture wins** (earliest by detecting capture, which is what
  `available_at` is measured on) — the earliest moment we could verifiably
  have known. Taking the latest would postpone the claim past the point it
  became knowable, i.e. hide it from replays that should see it.

When several `updated_at` claims match, ONE claim is emitted, for the LATEST
of them: the question the policy asks is "when did this posting last show a
sign of life". When nothing matches, NOTHING is emitted — silence, never a
manufactured claim, and `publish_recency` falls back to the publish date
alone exactly as before.

--------------------------------------------------------------------------
Replay seams (spec.md §6) — two, and only two
--------------------------------------------------------------------------

This module is shared verbatim by every system in live mode AND in replay
mode, so the point-in-time rules of spec.md §6 must reach it without it
growing a `replay` branch. They do, through the `ProbeRunner` it is already
handed:

1. **Every claim is persisted through `probes.save_evidence`**, never through
   `probes.run.save_evidence`. That method is the one choke point
   `rli.replay.mode.ReplayProbeRunner` overrides to drop every claim whose
   `available_at` is after `T` (spec.md §6: "expose only evidence with
   `available_at <= T`"). Live mode delegates it straight to the `Run`, so
   the two paths are identical outside replay.
2. **`probes.always_run_extra()` is appended to the always-run evidence.**
   It is empty in live mode; in replay it carries the archive-derived board
   state at `T`. See the block where it is called for why that evidence
   cannot come from a probe.

Related, and easy to get wrong: the two claims this module SYNTHESIZES
(`posting_state` and `board_present`/`board_absent`) are stamped with
`observed_at(result.data, now)` — the instant the underlying observation was
really made — not with the run clock. In live mode those are the same value.
In replay they are not, and using the run clock would stamp `available_at ==
T` on an observation made months later, which is both spec.md §3's forbidden
backdating and a gate that can never fire.

--------------------------------------------------------------------------
Identity resolution, and why it never creates a row
--------------------------------------------------------------------------

`posting_id` is `f"{ats}:{tenant}:{job_id}"` — the same convention
`rli.snapshots.daily` writes into `postings`, so a run against a URL we
already collect lines up with the collected row.

`company_id` (spec.md §3: "the normalized company website domain") is
resolved by this fallback chain, most trustworthy first:

1. the `postings` row for `posting_id` — the identity the collector already
   committed to;
2. any `postings` row with the same `(ats, ats_tenant_id)` — a job we have
   not collected on a board we have. The tenant is the company's board, so
   the company is the same; only the job is new. Deterministically ordered
   by `posting_id` so a tenant that (wrongly) spans two companies resolves
   the same way every time rather than by table order;
   **and, having learned the company that way, the `postings` row under the
   archive correlation key `(company_id, ats_job_id)`** — see
   `_posting_by_correlation_key`;
3. `rli.resolvers.common.normalize_domain` of the JSON-LD
   `hiringOrganization` domain the resolver extracted — a real observation
   about the posting, but from the page rather than from our own corpus;
4. `None` — unknown. Every downstream consumer treats that as "no history,
   no events", never as a default company.

**Why step 2's second half exists, and what its absence cost.** A posting
that this system only ever saw inside a Wayback board capture gets a
`postings` row from `rli.history.closures` keyed
`"archive:{company_id}:{job_id}"`, with `ats_tenant_id` NULL, because a board
capture carries no ATS or tenant. `f"{ats}:{tenant}:{job_id}"` can therefore
NEVER match it — and without this step the whole archive-collected half of
the corpus resolves to `posting_row_exists=False`, which means no history
features, no `evidence.posting_id`, and — because `RepostHistoryProbe.
eligible` / `RequirementsDriftProbe.eligible` both begin with "is there a
`postings` row?" — no history probe is ever eligible for it. On the real
corpus that silently made System A and System B identical on every
archive-derived posting: the exact comparison spec.md §6 exists to make. The
lookup is the corpus's own documented correlation key, so this step resolves
the posting the collector actually committed to rather than inventing one.

At no point does this module INSERT a `companies` or `postings` row. Without
that rule a single evaluation run could invent a company, and the next run's
history features would be computed against a corpus the evaluation itself
manufactured (see `rli.eval.runner`'s write invariant). The cost is that
steps 3 and 4 leave `posting_row_exists` False, which in turn means no
history features and a NULL `evidence.posting_id` — the honest state for a
posting nobody has been snapshotting.

--------------------------------------------------------------------------
JUDGMENT CALL: the three event inputs stay UNKNOWN until the probe runs
--------------------------------------------------------------------------

`build_case_state` does **not** read the `company_events` store, and leaves
`material_negative_event`, `freeze_or_pause` and `last_material_event_at`
UNKNOWN. They are populated only when the `company_events` PROBE runs and
its claims land in the evidence list, from which `rli.policy.inputs` derives
them like every other input.

This module used to do the opposite: it called
`rli.events.policy_signals.derive_policy_signals` over the local store and
handed the resulting triple to `derive_policy_inputs`, so all three inputs
could arrive already answered — with **no evidence item behind them**. The
argument for it was that the read is free (spec.md §4 forbids live search
here, so the probe is a local read too) and that modelling a cost that does
not exist would flatter any system that "discovered" what it already had.

That argument was wrong, and the ways it was wrong were observed on real
runs rather than reasoned about:

* **spec.md §5 sources these inputs from the `company_events` probe**, and
  spec.md §9/§1 require every user-facing reason to map to evidence. A
  populated-but-uncited input is a policy input the explanation cannot
  legitimately talk about.
* **A populated input made the probe ineligible.** spec.md §4's rule —
  "a candidate probe is eligible only if it can populate at least one [of
  the unpopulated policy inputs]", implemented as `populates & unpopulated`
  in `rli.probes.registry` — dropped `CompanyEventsProbe` for every company
  whose events had been collected, which is most of the corpus.
* **So System C stopped before it started.** With the event questions
  already "answered", the controller's pre-flight hit
  `no_unresolved_question`, the investigator was never called, and
  `company_events` never ran. C then returned `wait` via the P3c
  `material_event_unrefreshed` branch holding four evidence items, none of
  them a layoff, and the LLM explanation — asked to cite something for a
  layoff nobody had produced — cited an unrelated evidence id.
* **And System B's probe produced nothing.** B routes `company_events` by
  name (rules R1/R3) rather than through the eligibility gate, so it did run
  it — but in replay its claims were the only thing the probe contributed,
  and the recorded outcome was that `company_events` "produced zero evidence
  in every run": the answer had already been supplied by this function, so
  nothing downstream needed the claims and nothing noticed they were absent.

**The accepted cost, stated honestly.** `company_events` is now eligible on
every case, so it is counted in spec.md §6's medium/high-cost probe use for
every system that runs it — System A always did, System B routes it by name,
and System C now actually reaches it. The reported ABSOLUTE cost figure
therefore goes up. That figure is now the true one: the probe really is run,
and a spec.md §6 cost metric that hides a step the system took is worse than
one that reports a local read as costing something. A-vs-B-vs-C stay
comparable because all three pay the same price on the same cases, which is
what the ratio gates (e.g. "C medium/high-cost probe use <= 70% of B")
actually measure.

The pinned collection-status file that used to be threaded into this
function is now threaded into the `ProbeContext` instead
(`rli.eval.runner.open_system_runner`), because the PROBE is what reads it.

--------------------------------------------------------------------------
Other judgment calls
--------------------------------------------------------------------------

* **`board_snapshot` runs only for an ATS it can query.** Its `ArgsModel`
  accepts `ats` in `{greenhouse, ashby, lever}` only; a `generic` or
  unresolved posting has no board endpoint. Rather than fabricate a result
  or silently do nothing, the skip is recorded as a
  `probe_skipped:<reason>` controller step, so a trace never leaves the
  reader guessing whether the always-run pair really ran.
* **History features are only read when the `postings` row exists.**
  `rli.history.features.posting_features` raises `KeyError` otherwise (by
  design: "a missing posting is a caller bug, not a thin-history case"), so
  the guard is the row check, not a `try/except`. `features=None` then flows
  through `derive_policy_inputs` as UNKNOWN `repost_pattern` and through
  `decide(long_lived=UNKNOWN)` — spec.md §4: "missing history never means
  flat hiring."
* **`CaseState` carries the evidence-quality verdict of the always-run
  evidence.** System B's routing reads it (rule R2), and computing it here —
  once, from the always-run evidence only — is what makes B's reads
  honest: B must never see the output of a probe it did not run. System A
  ignores this field and recomputes quality over the full evidence set after
  its probes finish.
* **`CaseState` is a mutable snapshot** (`frozen=False`) holding live
  `ProbeResult`/`EvidenceItem` objects. It is an internal working record,
  not a stored artifact; the stored artifacts are the `evidence` rows and
  the `runs.final_decision` JSON.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.runner import STEP_PROBE_SKIPPED, ProbeRunner
from rli.history.features import PostingHistoryFeatures, posting_features
from rli.models.case_file import CaseFile
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN, PolicyInputs, Unknown
from rli.models.probe import ProbeResult
from rli.models.time import ensure_aware, parse_utc, to_utc_z
from rli.policy.action import PolicyThresholds
from rli.policy.inputs import (
    CLAIM_BOARD_ABSENT,
    CLAIM_POSTING_STATE,
    CLAIM_REFRESHED_AT,
    best_publish_claim,
    derive_policy_inputs,
)
from rli.policy.quality import QualityVerdict, evidence_quality_detail
from rli.probes.base import Probe, ProbeClaim
from rli.probes.board_snapshot import BoardJob, BoardSnapshotArgs, BoardSnapshotProbe
from rli.probes.lookups import posting_row
from rli.probes.registry import build_args
from rli.probes.resolve_posting import ResolvePostingArgs, ResolvePostingProbe
from rli.probes.team_signal import TeamSignalProbe
from rli.resolvers.common import normalize_domain

__all__ = [
    "BOARD_SNAPSHOT_URL_PLACEHOLDER",
    "CLAIM_BOARD_PRESENT",
    "REFRESH_MATCH_PROBE",
    "CaseState",
    "build_case_state",
    "extend_case_state",
    "observed_at",
]

# Self-describing, deliberately non-fetchable `source_url` for a fact whose
# subject is the ABSENCE of a job from a board listing. Mirrors
# `rli.probes.repost_history.BOARD_HISTORY_URL_PLACEHOLDER`.
BOARD_SNAPSHOT_URL_PLACEHOLDER = "board-snapshot:{ats}/{tenant}"

# The positive counterpart of `rli.policy.inputs.CLAIM_BOARD_ABSENT`. It is
# defined here rather than there because no policy input reads it: it exists
# so the trace and the user-facing evidence list record that the board WAS
# checked and did list the job, instead of leaving "we looked" implicit.
CLAIM_BOARD_PRESENT = "board_present"

# ATSes with a board-listing endpoint (`rli.probes.board_snapshot.AtsName`).
_BOARD_ATSES = frozenset({"greenhouse", "ashby", "lever"})

# The `evidence.probe` attribution for the `refreshed_at` claim. NOT a probe:
# no network call is made and no `Probe` subclass answers to this name. It
# exists so the evidence table never says `resolve_posting` observed a
# content-hash change it never looked at — the same reasoning that gives
# `rli.replay.mode.ARCHIVE_BOARD_STATE_PROBE` its own name. See the module
# docstring's "The refresh match" section.
REFRESH_MATCH_PROBE = "refresh_match"

# The claim type the resolver emits for an ATS-reported last-modified time.
# Only `rli.probes.resolve_posting` emits it, and only for Greenhouse today.
_CLAIM_UPDATED_AT = "updated_at"


class CaseState(BaseModel):
    """Everything the always-run pair established, plus what it implies.

    This is the internal superset of `rli.models.case_file.CaseFile`: the
    `CaseFile` is the identity-complete subset handed to the controller /
    investigator, and `case_file()` returns `None` when the identity is not
    complete enough to build one.
    """

    # Mutable on purpose: a working snapshot that probes grow (see the
    # module docstring). The stored artifacts are the `evidence` rows and
    # the `runs.final_decision` JSON, not this object.
    model_config = ConfigDict(frozen=False)

    input_url: str
    canonical_url: str

    ats: str | None = None
    tenant: str | None = None
    job_id: str | None = None
    posting_id: str | None = None
    posting_row_exists: bool = False
    company_id: str | None = None

    title: str | None = None
    team: str | None = None
    location: str | None = None

    evidence: list[EvidenceItem] = []
    inputs: PolicyInputs = PolicyInputs()
    quality: QualityVerdict | None = None
    features: PostingHistoryFeatures | None = None

    # ALWAYS-RUN probe failures only — the set `rli.policy.quality` documents
    # as the correct `failures=` argument. Dynamic failures are traced in
    # `run_steps`, never folded in here.
    failures: list[ProbeResult] = []
    resolver_ok: bool = False
    unpopulated: set[str] = set()
    next_evidence_index: int = 1
    probe_results: dict[str, ProbeResult] = {}

    def case_file(self) -> CaseFile | None:
        """The `CaseFile` for this case, or `None` when the identity is incomplete.

        `CaseFile` requires a `posting_id` AND a `company_id`; both are
        mandatory because every dynamic probe's arguments are built from them
        (`rli.probes.registry.build_args`). Returning `None` rather than a
        `CaseFile` with placeholder strings is what stops a system from
        running `repost_history` against a posting id nobody can resolve.
        """
        if self.posting_id is None or self.company_id is None:
            return None
        return CaseFile(
            posting_id=self.posting_id,
            company_id=self.company_id,
            canonical_url=self.canonical_url,
            title=self.title,
            team=self.team,
            location=self.location,
            evidence=list(self.evidence),
            policy_inputs=self.inputs,
        )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def _company_id_for_tenant(
    conn: sqlite3.Connection, ats: str | None, tenant: str | None
) -> str | None:
    """Fallback 2: the company of any collected posting on the same ATS tenant."""
    if not ats or not tenant:
        return None
    row = conn.execute(
        """
        SELECT company_id FROM postings
        WHERE ats = ? AND ats_tenant_id = ?
        ORDER BY posting_id
        LIMIT 1
        """,
        (ats, tenant),
    ).fetchone()
    return None if row is None else row["company_id"]


def _posting_by_correlation_key(
    conn: sqlite3.Connection, company_id: str | None, job_id: str | None
) -> sqlite3.Row | None:
    """Fallback 1b: the collected posting for `(company_id, ats_job_id)`.

    `rli.history.closures` documents `(company_id, ats_job_id)` as THE
    correlation key between a board capture and a `postings` row, because
    `board_snapshots` carries no ATS or tenant column. It is also the key
    under which that module creates archive-only postings, whose ids are
    `"archive:{company_id}:{job_id}"` and whose `ats_tenant_id` is NULL —
    ids no resolver can ever reconstruct from a job URL.

    Deterministic tiebreak (lowest `posting_id`) and the accepted collision
    limitation are `rli.history.closures`' own, restated in one place rather
    than diverging.
    """
    if not company_id or not job_id:
        return None
    row = conn.execute(
        """
        SELECT posting_id FROM postings
        WHERE company_id = ? AND ats_job_id = ?
        ORDER BY posting_id
        LIMIT 1
        """,
        (company_id, job_id),
    ).fetchone()
    return None if row is None else posting_row(conn, row["posting_id"])


def _resolve_identity(
    conn: sqlite3.Connection, data: dict[str, Any]
) -> tuple[str | None, sqlite3.Row | None, str | None]:
    """Return `(posting_id, postings row or None, company_id)` — see the docstring.

    The `postings` row is returned rather than a bare boolean because it also
    carries the collected `title`/`team`/`location`, which are better than
    the resolver's (a board listing names the team; a job page usually does
    not) and which `rli.models.case_file.CaseFile` wants.
    """
    ats, tenant, job_id = data.get("ats"), data.get("tenant"), data.get("job_id")

    posting_id: str | None = None
    if ats and tenant and job_id:
        # Same convention as rli.snapshots.daily, so a run against a
        # collected posting matches the collected row exactly.
        posting_id = f"{ats}:{tenant}:{job_id}"

    row = posting_row(conn, posting_id) if posting_id is not None else None
    if row is not None:
        return posting_id, row, row["company_id"]

    company_id = _company_id_for_tenant(conn, ats, tenant)

    # Fallback 1b: the collected row under the ARCHIVE correlation key. See
    # `_posting_by_correlation_key`, and the module docstring for why this
    # step is not optional.
    collected = _posting_by_correlation_key(conn, company_id, job_id)
    if collected is not None:
        return collected["posting_id"], collected, collected["company_id"]

    if company_id is None:
        company_id = normalize_domain(data.get("company_domain"))
    return posting_id, None, company_id


# ---------------------------------------------------------------------------
# Claims the runner owes (see the module docstring's contract section)
# ---------------------------------------------------------------------------


def observed_at(data: dict[str, Any] | None, now: datetime) -> datetime:
    """When the observation behind `data` was REALLY made (spec.md §3).

    Live, that is the run clock: the probe just ran. In replay it is not —
    `rli.replay.mode.ReplayProbeStore.save` stamps every cached record with
    `data["observed_at"]`, the build-time instant the live observation was
    actually made, precisely so the two claims this module synthesizes
    (`posting_state`, `board_present`/`board_absent`) can carry that instant
    instead of `T`.

    Stamping `T` would backdate a current discovery onto the replay
    timeline, which spec.md §3 forbids in as many words ("Do not backdate
    current discoveries merely because the underlying event happened
    earlier") — and it would silently defeat the whole point-in-time gate:
    every synthesized claim would be `available_at == T`, therefore always
    exposed, and an archive-era replay would decide from an observation made
    a year in its future.

    A malformed or missing stamp degrades to `now`, the live meaning. It
    cannot degrade to "expose anyway" by accident: a live run has no stamp
    and must use `now`, and a replay record always has one.
    """
    stamped = (data or {}).get("observed_at")
    if isinstance(stamped, datetime):
        return ensure_aware(stamped, "observed_at")
    if isinstance(stamped, str):
        try:
            return parse_utc(stamped)
        except ValueError:
            return now
    return now


def _posting_state_claim(data: dict[str, Any], canonical_url: str, now: datetime) -> ProbeClaim:
    state = str(data.get("posting_state") or "unknown")
    ats = data.get("ats")
    seen_at = observed_at(data, now)
    return ProbeClaim(
        claim_type=CLAIM_POSTING_STATE,
        value=state,
        source_url=canonical_url,
        # An ATS adapter decided the state; JSON-LD alone is page-structured.
        source_quality="ats_native" if ats in _BOARD_ATSES else "page_structured",
        available_at=seen_at,
        fetched_at=seen_at,
    )


def _board_claim(
    *, jobs: list[BoardJob], ats: str, tenant: str, job_id: str, now: datetime
) -> ProbeClaim:
    match = next((job for job in jobs if job.job_id == job_id), None)
    if match is not None:
        return ProbeClaim(
            claim_type=CLAIM_BOARD_PRESENT,
            value=job_id,
            source_url=match.url or BOARD_SNAPSHOT_URL_PLACEHOLDER.format(ats=ats, tenant=tenant),
            raw_excerpt=match.title,
            source_quality="ats_native",
            available_at=now,
            fetched_at=now,
        )
    return ProbeClaim(
        claim_type=CLAIM_BOARD_ABSENT,
        value=job_id,
        # No fetchable URL exists for an absence; see the module docstring.
        source_url=BOARD_SNAPSHOT_URL_PLACEHOLDER.format(ats=ats, tenant=tenant),
        raw_excerpt=f"{len(jobs)} job(s) listed, none with id {job_id}",
        source_quality="ats_native",
        available_at=now,
        fetched_at=now,
    )


# ---------------------------------------------------------------------------
# The refresh match (see the module docstring)
# ---------------------------------------------------------------------------


class _HashChange(NamedTuple):
    """One OBSERVED content change, interval-censored between two captures.

    `start` is the capture that last showed the OLD hash and `end` the
    capture that first showed the NEW one; the change happened somewhere in
    between and we cannot say where (spec.md §4). `end` is therefore also the
    DETECTING capture — the first moment the change was knowable to us — and
    is what `available_at` is measured on.
    """

    start: datetime
    end: datetime
    source: str
    old_hash: str
    new_hash: str


def _changes_from_rows(rows: Sequence[tuple[str, str]], source: str) -> list[_HashChange]:
    """Consecutive rows with DIFFERING hashes, as interval-censored changes.

    `rows` must already be ordered by capture time, and the callers' SQL has
    already dropped rows with a NULL hash — a capture that stored no hash is
    not evidence that the content did or did not move. Pairing the SURVIVING
    rows deliberately bridges across such a gap, WIDENING the interval to
    cover it: if we saw hash A, then a hashless capture, then hash B, the
    change could have happened at either step and the honest bracket is the
    outer one. Narrowing it to the last two hashed captures would assert a
    precision the captures do not have (spec.md §4's interval censoring),
    and would be the direction that manufactures matches.
    """
    changes: list[_HashChange] = []
    for (previous_at, previous_hash), (current_at, current_hash) in zip(
        rows, rows[1:], strict=False
    ):
        if previous_hash != current_hash:
            changes.append(
                _HashChange(
                    start=parse_utc(previous_at),
                    end=parse_utc(current_at),
                    source=source,
                    old_hash=previous_hash,
                    new_hash=current_hash,
                )
            )
    return changes


def _observed_hash_changes(
    conn: sqlite3.Connection, posting_id: str | None, company_id: str | None, ats_job_id: str | None
) -> list[_HashChange]:
    """Every observed content change for this posting, from both capture tables.

    Two independent sources, because the corpus holds two kinds of capture
    and a posting may appear in either or both:

    * `posting_snapshots.content_hash` — per-posting captures, own + archive.
      Keyed by `posting_id`.
    * `board_snapshot_jobs.description_hash` — the per-job rows of a
      company-wide board capture. `board_snapshots` carries no ATS or tenant
      column, so the key is `(company_id, ats_job_id)`, which
      `rli.history.closures` documents as THE correlation key between a board
      capture and a `postings` row.

    A source whose key we do not have is SKIPPED, never guessed at: an
    unresolved `ats_job_id` would otherwise match every job on the board.
    """
    changes: list[_HashChange] = []

    if posting_id is not None:
        rows = conn.execute(
            """
            SELECT captured_at, content_hash FROM posting_snapshots
            WHERE posting_id = ? AND source IN ('own', 'archive')
                  AND content_hash IS NOT NULL
            ORDER BY captured_at, id
            """,
            (posting_id,),
        ).fetchall()
        changes += _changes_from_rows(
            [(row["captured_at"], row["content_hash"]) for row in rows], "posting_snapshots"
        )

    if company_id is not None and ats_job_id is not None:
        rows = conn.execute(
            """
            SELECT s.captured_at AS captured_at, j.description_hash AS description_hash
            FROM board_snapshot_jobs AS j
            JOIN board_snapshots AS s ON s.id = j.board_snapshot_id
            WHERE s.company_id = ? AND j.job_id = ? AND j.description_hash IS NOT NULL
            ORDER BY s.captured_at, j.id
            """,
            (company_id, ats_job_id),
        ).fetchall()
        changes += _changes_from_rows(
            [(row["captured_at"], row["description_hash"]) for row in rows],
            "board_snapshot_jobs",
        )

    return changes


def _refresh_claim(
    conn: sqlite3.Connection,
    *,
    evidence: Sequence[EvidenceItem],
    posting_id: str | None,
    company_id: str | None,
    ats_job_id: str | None,
    refresh_match_days: int,
    now: datetime,
) -> ProbeClaim | None:
    """The `refreshed_at` claim, or `None` when nothing corroborates a refresh.

    See the module docstring's "The refresh match" section for every judgment
    call here. Returning `None` — silence — is the common case and the
    correct one: a manufactured claim would flatter `publish_recency` on
    exactly the postings we hold the least capture history for.
    """
    candidates = [
        item
        for item in evidence
        if item.claim_type == _CLAIM_UPDATED_AT
        # "an ATS `updated_at` claim" (spec.md §5 amendment): a page-scraped
        # or archive-derived modification time is not the ATS's own answer,
        # and an unparsed timestamp cannot be placed against an interval.
        and item.source_quality == "ats_native"
        and item.source_event_at is not None
    ]
    if not candidates:
        return None

    changes = _observed_hash_changes(conn, posting_id, company_id, ats_job_id)
    if not changes:
        return None

    tolerance = timedelta(days=refresh_match_days)
    # Latest matching `updated_at` wins: the policy asks when the posting
    # last showed a sign of life. Ties broken by evidence id so the choice is
    # total and therefore reproducible.
    matched: tuple[EvidenceItem, list[_HashChange]] | None = None
    for claim in sorted(candidates, key=lambda c: (c.source_event_at, c.id)):
        stamp = claim.source_event_at
        assert stamp is not None  # filtered above; narrows the type
        hits = [c for c in changes if c.start - tolerance <= stamp <= c.end + tolerance]
        if hits:
            matched = (claim, hits)
    if matched is None:
        return None

    claim, hits = matched
    # EARLIEST detecting capture among the matching changes — the earliest
    # moment the refresh was verifiably knowable (module docstring).
    change = min(hits, key=lambda c: (c.end, c.start, c.source))
    stamp = claim.source_event_at
    assert stamp is not None

    return ProbeClaim(
        claim_type=CLAIM_REFRESHED_AT,
        # Normalized rather than the ATS's raw string (which `claim.value`
        # still holds), so `value` and `source_event_at` cannot disagree.
        value=to_utc_z(stamp),
        # The ATS record the timestamp came from — a real, fetchable URL.
        source_url=claim.source_url,
        raw_excerpt=(
            f"ATS updated_at {to_utc_z(stamp)} coincides (within {refresh_match_days}d) "
            f"with a content change observed in {change.source} between "
            f"{to_utc_z(change.start)} (hash {change.old_hash[:12]}) and "
            f"{to_utc_z(change.end)} (hash {change.new_hash[:12]})"
        ),
        # The timestamp is the ATS's own; the captures corroborate it.
        source_quality="ats_native",
        source_event_at=stamp,
        # We could not know about the change before the capture that revealed
        # it (spec.md §3 forbids backdating a discovery).
        available_at=max(claim.available_at, change.end),
        fetched_at=now,
    )


# ---------------------------------------------------------------------------
# build_case_state
# ---------------------------------------------------------------------------


def build_case_state(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    url: str,
    now: datetime,
    probes: ProbeRunner,
) -> CaseState:
    """Run the always-run pair and derive everything it implies (spec.md §4).

    Both probes execute through `probes` — i.e. through the run's
    `ProbeContext` (so per-probe allowlists and per-host rate limits apply)
    and through the run's tracing (so each execution appears in `run_steps`
    with its cost, latency, cache status and any structured failure).

    Arguments:
        conn: the corpus connection. Read-only in this module.
        cfg: loaded configuration.
        url: the user-supplied job URL.
        now: the run clock; every claim's `available_at`/`fetched_at`.
        probes: the run's `rli.eval.runner.ProbeRunner`. It bundles the
            `Run` and the `ProbeContext`, which is why this signature takes
            it rather than the two separately — an untraced probe call would
            corrupt spec.md §6's probe-count metric, and there is no way to
            make one through this object.
    """
    resolver = probes.execute(ResolvePostingProbe, ResolvePostingArgs(url=url))
    data: dict[str, Any] = resolver.data or {}
    canonical_url = str(data.get("canonical_url") or url)

    posting_id, collected_row, company_id = _resolve_identity(conn, data)
    posting_row_exists = collected_row is not None

    failures: list[ProbeResult] = []
    if not resolver.ok:
        failures.append(resolver)

    evidence_posting_id = posting_id if posting_row_exists else None
    # `probes.save_evidence`, never `probes.run.save_evidence`: the runner's
    # method is the single choke point the point-in-time gate hangs off
    # (`rli.eval.runner.ProbeRunner.save_evidence`, overridden by
    # `rli.replay.mode.ReplayProbeRunner` to drop every claim with
    # `available_at > T`). Going straight to the `Run` would bypass spec.md
    # §6's first replay rule for the always-run evidence — the evidence that
    # decides `posting_state` — which is the one place it matters most.
    evidence = probes.save_evidence(
        probe=ResolvePostingProbe.name,
        # The synthesized `posting_state` claim leads: it is the primary
        # observation, and the contract in `rli.policy.inputs` makes it the
        # one claim without which the observed state is not evidence-backed.
        claims=[
            _posting_state_claim(data, canonical_url, now),
            *list(data.get("evidence") or []),
        ],
        posting_id=evidence_posting_id,
    )

    # -- board_snapshot ---------------------------------------------------
    ats, tenant, job_id = data.get("ats"), data.get("tenant"), data.get("job_id")
    board: ProbeResult | None = None
    if ats in _BOARD_ATSES and tenant:
        board = probes.execute(BoardSnapshotProbe, BoardSnapshotArgs(ats=ats, tenant=tenant))
        if not board.ok:
            failures.append(board)
        elif job_id:
            jobs: list[BoardJob] = list((board.data or {}).get("jobs") or [])
            evidence += probes.save_evidence(
                probe=BoardSnapshotProbe.name,
                claims=[
                    _board_claim(
                        jobs=jobs,
                        ats=str(ats),
                        tenant=str(tenant),
                        job_id=str(job_id),
                        now=observed_at(board.data, now),
                    )
                ],
                posting_id=evidence_posting_id,
            )
        else:
            probes.note(
                f"{STEP_PROBE_SKIPPED}:no_ats_job_id",
                probe_name=BoardSnapshotProbe.name,
                error="board listing fetched, but the resolver produced no ATS job id to "
                "look for; emitting neither board_present nor board_absent",
            )
    else:
        probes.note(
            f"{STEP_PROBE_SKIPPED}:no_board_endpoint",
            probe_name=BoardSnapshotProbe.name,
            error=f"no board-listing endpoint for ats={ats!r} tenant={tenant!r}",
        )

    # -- evidence contributed by the RUN ENVIRONMENT ----------------------
    # Empty in live mode. In replay this is the archive-derived board state
    # at `T` (`rli.replay.build.archive_state_claims`): at an archive-era `T`
    # the live resolver's answer is not available (its `available_at` is the
    # build instant, so the gate above dropped it) and the only honest
    # observable state comes from the board captures that existed at `T`.
    # It is contributed here rather than by a probe because no probe observes
    # it — it is a re-reading of captures the collector already took — and it
    # is attributed to its own probe name so the `evidence` table never
    # claims `resolve_posting` said something it did not.
    for extra_probe, extra_claims in probes.always_run_extra():
        evidence += probes.save_evidence(
            probe=extra_probe, claims=extra_claims, posting_id=evidence_posting_id
        )

    # -- the refresh match (module docstring: "The refresh match") --------
    # Computed ONCE, here, because only the always-run `resolve_posting`
    # emits `updated_at` — no dynamic probe does — so the candidate set this
    # reads is complete the moment the resolver's evidence is saved, and the
    # capture tables it reads are a corpus this module never writes to. See
    # `extend_case_state` for why it is not recomputed there.
    #
    # `ats_job_id` prefers the COLLECTED row's value over the resolver's:
    # both name the same job, but the collected one is the id the board
    # captures were actually indexed under (an archive-only posting has a
    # `postings` row whose id no resolver can reconstruct — see
    # `_posting_by_correlation_key`), and matching against captures is the
    # entire purpose here.
    ats_job_id = (
        str(collected_row["ats_job_id"])
        if collected_row is not None and collected_row["ats_job_id"] is not None
        else (str(job_id) if job_id else None)
    )
    refresh = _refresh_claim(
        conn,
        evidence=evidence,
        # `posting_snapshots` rows can only exist for a collected posting.
        posting_id=posting_id if posting_row_exists else None,
        company_id=company_id,
        ats_job_id=ats_job_id,
        refresh_match_days=PolicyThresholds.coerce(cfg).refresh_match_days,
        now=now,
    )
    if refresh is not None:
        # Through `probes.save_evidence` like every other always-run claim,
        # so the replay point-in-time gate applies to it too.
        evidence += probes.save_evidence(
            probe=REFRESH_MATCH_PROBE, claims=[refresh], posting_id=evidence_posting_id
        )

    # -- derived state ----------------------------------------------------
    # The publish date reaches the history layer from HERE rather than being
    # re-derived there: `rli.policy.inputs` owns spec.md §3's source ranking
    # and the primary-quality rule, and `rli.history.features` holds no
    # evidence at all, so re-deriving it there would duplicate that ranking
    # in a module with no way to apply it.
    publish = best_publish_claim(evidence)
    features = (
        posting_features(
            conn,
            cfg,
            posting_id,
            now=now,
            first_published=publish.source_event_at if publish is not None else None,
        )
        if posting_row_exists and posting_id is not None
        else None
    )
    inputs = derive_policy_inputs(
        evidence,
        features,
        now,
        cfg=cfg,
        resolver_ok=resolver.ok,
    )
    quality = evidence_quality_detail(evidence, inputs, failures, cfg)

    probe_results: dict[str, ProbeResult] = {ResolvePostingProbe.name: resolver}
    if board is not None:
        probe_results[BoardSnapshotProbe.name] = board

    return CaseState(
        input_url=url,
        canonical_url=canonical_url,
        ats=ats,
        tenant=tenant,
        job_id=job_id,
        posting_id=posting_id,
        posting_row_exists=posting_row_exists,
        company_id=company_id,
        # The collected row wins over the resolver for team/location: a board
        # listing carries them, a job page usually does not. Title prefers
        # what we just fetched, which is fresher than the stored row.
        title=data.get("title") or (collected_row["title"] if collected_row else None),
        team=collected_row["team"] if collected_row else None,
        location=collected_row["location"] if collected_row else None,
        evidence=list(evidence),
        inputs=inputs,
        quality=quality,
        features=features,
        failures=failures,
        resolver_ok=resolver.ok,
        unpopulated=inputs.unpopulated(),
        next_evidence_index=probes.run.next_evidence_index,
        probe_results=probe_results,
    )


def extend_case_state(
    case: CaseState, probe_classes: Sequence[type[Probe]], *, probes: ProbeRunner
) -> CaseState:
    """Execute dynamic probes against `case`, merge their evidence, re-derive inputs.

    This is the "one probe -> append evidence -> repeat" leg of spec.md §2's
    pipeline, minus the investigator: WHICH probes to run is the systems'
    only degree of freedom (spec.md §6), so choosing them is System A's and
    System B's job and running them is this function's. Sharing it is what
    guarantees that a probe behaves identically under A and under B — the
    same arguments (`rli.probes.registry.build_args`, never LLM-proposed
    values), the same evidence numbering, the same trace, and one re-derivation
    of the policy inputs at the end.

    Mutates and returns `case` (a `CaseState` is a mutable working snapshot;
    see the module docstring). With an incomplete identity — `case_file()` is
    `None` — it runs nothing and returns unchanged: every dynamic probe's
    arguments are built from `posting_id`/`company_id`, so there is nothing
    to run and nothing to guess. The caller records that as a controller step.

    Inputs are re-derived ONCE, from the full evidence set, rather than after
    each probe. Deriving per probe would be the same answer computed N times
    (`derive_policy_inputs` is a pure function of the whole evidence list),
    and re-deriving mid-loop would invite a future caller to use a
    half-updated input set to decide what to run next — which is exactly the
    controller's job in M5, not this function's.

    **The refresh match is NOT recomputed here**, and does not need to be.
    `build_case_state` derives the `refreshed_at` claim from the `updated_at`
    claims in hand, and `rli.probes.resolve_posting` — an ALWAYS-RUN probe
    that has therefore already finished — is the only probe in this codebase
    that emits `updated_at`. No dynamic probe can add one, so the candidate
    set cannot grow; the capture tables the match reads are a corpus no probe
    writes to (`rli.eval.runner`'s write invariant), so the observed changes
    cannot grow either. Recomputing would spend two queries per dynamic step
    to reproduce a claim already in `case.evidence` — and, worse, would
    invite a second `refreshed_at` claim for the same refresh. If a future
    probe ever emits `updated_at`, this is the function that must change.

    ONE input cannot be read from evidence alone and is threaded through
    explicitly:

    * `team_signal` — spec.md §4 makes `team_signal` the only source for
      `corroborating_hiring_signal`. The documented path is a
      `corroborating_hiring_signal` CLAIM (`rli.policy.inputs.CLAIM_TEAM_SIGNAL`),
      which `derive_policy_inputs` reads from the evidence and which always
      wins. The `team_signal=` keyword here is a narrow fallback for a
      licensed adapter that reports the boolean in `data` without emitting a
      claim; it is only used when the probe succeeded AND the value is
      genuinely a `bool`. `NullTeamSignalSource` returns `ok=False`, so an
      unlicensed run never populates the input — which is the whole point of
      that class (see `rli.probes.team_signal`).

    The company-event signals used to be threaded the same way, from
    `company_events`' `ProbeResult.data`, on top of a triple this module had
    pre-populated from the local store. Both halves of that are gone:
    `derive_policy_inputs` reads `material_negative_event`,
    `freeze_or_pause` and `last_material_event_at` out of the probe's own
    claims. The probe's `data` still reports all three (the replay codec in
    `rli.replay.mode` decodes them, and a trace reader wants to see them),
    but nothing in the decision path reads them any more — which is what
    makes it impossible for the trace and the policy to disagree about
    whether there was a layoff. See this module's docstring for the bug that
    made the difference matter.
    """
    case_file = case.case_file()
    if case_file is None:
        return case

    evidence_posting_id = case.posting_id if case.posting_row_exists else None

    for probe_cls in probe_classes:
        args = build_args(probe_cls, case_file, probes.ctx)
        result = probes.execute(probe_cls, args)
        case.probe_results[probe_cls.name] = result

        claims = list((result.data or {}).get("evidence") or [])
        if claims:
            # Through `probes`, not `probes.run` — see `build_case_state`.
            case.evidence += probes.save_evidence(
                probe=probe_cls.name, claims=claims, posting_id=evidence_posting_id
            )

    team = case.probe_results.get(TeamSignalProbe.name)
    team_value: bool | Unknown = UNKNOWN
    if team is not None and team.ok and team.data is not None:
        candidate = team.data.get("corroborating_hiring_signal")
        if isinstance(candidate, bool):
            team_value = candidate

    case.inputs = derive_policy_inputs(
        case.evidence,
        case.features,
        probes.now,
        cfg=probes.cfg,
        resolver_ok=case.resolver_ok,
        team_signal=team_value,
    )
    case.unpopulated = case.inputs.unpopulated()
    case.next_evidence_index = probes.run.next_evidence_index
    return case
