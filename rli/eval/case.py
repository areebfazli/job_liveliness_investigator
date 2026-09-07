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
3. `rli.resolvers.common.normalize_domain` of the JSON-LD
   `hiringOrganization` domain the resolver extracted — a real observation
   about the posting, but from the page rather than from our own corpus;
4. `None` — unknown. Every downstream consumer treats that as "no history,
   no events", never as a default company.

At no point does this module INSERT a `companies` or `postings` row. Without
that rule a single evaluation run could invent a company, and the next run's
history features would be computed against a corpus the evaluation itself
manufactured (see `rli.eval.runner`'s write invariant). The cost is that
steps 3 and 4 leave `posting_row_exists` False, which in turn means no
history features and a NULL `evidence.posting_id` — the honest state for a
posting nobody has been snapshotting.

--------------------------------------------------------------------------
JUDGMENT CALL: event signals are pre-populated from the local store
--------------------------------------------------------------------------

`build_case_state` calls `rli.events.policy_signals.derive_policy_signals`
directly and feeds the result into `derive_policy_inputs(event_signals=...)`,
so `material_negative_event` and `freeze_or_pause` may already be populated
BEFORE the `company_events` probe has run.

Under spec.md §4's strict eligibility rule — "a candidate probe is eligible
only if it can populate at least one of [the unpopulated policy inputs]",
implemented as `populates & unpopulated` in `rli.probes.registry` — that
makes `CompanyEventsProbe` INELIGIBLE for any company whose events have
already been collected, because the only two inputs it populates are already
answered. This is deliberate, for two reasons:

* **It costs nothing to read.** spec.md §4 requires that historical company
  events be pre-collected and replayed by `available_at` ("do not live-search
  during benchmark replay"), and `rli.probes.company_events` therefore makes
  no network call at all — it reads the same local `company_events` table and
  the same `collection_status.csv` this function reads. Leaving the two
  inputs UNKNOWN until the probe "discovers" them would model a cost that
  does not exist and would let a system look thorough for spending a
  `medium`-cost step on data it already had.
* **The probe still earns its place.** It is what produces the CITED
  `EvidenceItem`s the explanation layer needs — `rli.policy.explain_stub`
  emits the freeze/layoff reasons only when `company_events` evidence is
  present, per spec.md §9 ("every user-facing reason maps to evidence") —
  and its `run_steps` row is what makes "we checked the event store" visible
  in the trace. System B's routing (`rli.eval.system_b`, rules R1/R3) routes
  it explicitly for that reason, rather than relying on the eligibility gate.

**The alternative, and what it would change.** Leaving both inputs UNKNOWN
until the probe runs would make `company_events` eligible on every case,
which under System A changes nothing (A runs every eligible probe anyway)
but inflates spec.md §6's medium/high-cost probe count for *any* system that
runs it — including the future System C, whose gate is "C medium/high-cost
probe use <= 70% of B". Since both A and B would gain the same probe on the
same cases, the ratio is roughly preserved either way; what would change is
the absolute cost figure spec.md §6 also asks to be reported, which would
then overstate the cost of a purely local read. The current choice keeps the
reported cost honest and keeps the eligibility rule's meaning intact ("is
this question still open?"), at the price of `company_events` appearing
ineligible in the registry while still being routed by name.

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
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.runner import STEP_PROBE_SKIPPED, ProbeRunner
from rli.events.policy_signals import derive_policy_signals
from rli.events.store import read_collection_status_csv
from rli.history.features import PostingHistoryFeatures, posting_features
from rli.models.case_file import CaseFile
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN, PolicyInputs, Unknown
from rli.models.probe import ProbeResult
from rli.policy.inputs import (
    CLAIM_BOARD_ABSENT,
    CLAIM_POSTING_STATE,
    derive_policy_inputs,
)
from rli.policy.quality import QualityVerdict, evidence_quality_detail
from rli.probes.base import Probe, ProbeClaim
from rli.probes.board_snapshot import BoardJob, BoardSnapshotArgs, BoardSnapshotProbe
from rli.probes.company_events import DEFAULT_COLLECTION_STATUS_CSV, CompanyEventsProbe
from rli.probes.lookups import posting_row
from rli.probes.registry import build_args
from rli.probes.resolve_posting import ResolvePostingArgs, ResolvePostingProbe
from rli.probes.team_signal import TeamSignalProbe
from rli.resolvers.common import normalize_domain

__all__ = [
    "BOARD_SNAPSHOT_URL_PLACEHOLDER",
    "CLAIM_BOARD_PRESENT",
    "CaseState",
    "build_case_state",
    "extend_case_state",
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
    event_signals: tuple[bool | Unknown, bool | Unknown] | None = None

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
    if company_id is None:
        company_id = normalize_domain(data.get("company_domain"))
    return posting_id, None, company_id


# ---------------------------------------------------------------------------
# Claims the runner owes (see the module docstring's contract section)
# ---------------------------------------------------------------------------


def _posting_state_claim(data: dict[str, Any], canonical_url: str, now: datetime) -> ProbeClaim:
    state = str(data.get("posting_state") or "unknown")
    ats = data.get("ats")
    return ProbeClaim(
        claim_type=CLAIM_POSTING_STATE,
        value=state,
        source_url=canonical_url,
        # An ATS adapter decided the state; JSON-LD alone is page-structured.
        source_quality="ats_native" if ats in _BOARD_ATSES else "page_structured",
        available_at=now,
        fetched_at=now,
    )


def _board_claim(
    *, jobs: list[BoardJob], ats: str, tenant: str, job_id: str, now: datetime
) -> ProbeClaim:
    match = next((job for job in jobs if job.job_id == job_id), None)
    if match is not None:
        return ProbeClaim(
            claim_type=CLAIM_BOARD_PRESENT,
            value=job_id,
            source_url=match.url
            or BOARD_SNAPSHOT_URL_PLACEHOLDER.format(ats=ats, tenant=tenant),
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
# Event signals
# ---------------------------------------------------------------------------


def _event_signals(
    conn: sqlite3.Connection,
    cfg: Config,
    company_id: str | None,
    now: datetime,
    collection_status_csv: str | Path | None = None,
) -> tuple[bool | Unknown, bool | Unknown] | None:
    """Pre-collected `(material_negative_event, freeze_or_pause)`, or `None`.

    `None` (not a pair of UNKNOWNs) when there is no company to ask about, so
    `derive_policy_inputs` reports both inputs UNKNOWN through its own
    documented `event_signals is None` path rather than through a value this
    module synthesized.

    The default status-file path is imported from `rli.probes.company_events`
    rather than restated, so the probe and the case state can never read
    different files. A missing file is an empty map — "no company has been
    investigated yet", which `derive_policy_signals` turns into UNKNOWN.

    `collection_status_csv` overrides that default, mirroring
    `rli.probes.company_events.CompanyEventsArgs.collection_status_csv`,
    which exists for exactly this reason. Without it, this function — and so
    every A/B run — reads a file from the working checkout, which makes two
    things unpleasant: a test's synthetic company id silently depends on
    whether it collides with a real row in `data/events/collection_status.csv`,
    and an M4 point-in-time replay could not pin the collection state it is
    replaying against. Threading the override keeps the probe and the case
    state pointed at ONE file, whichever file the caller chose.
    """
    if company_id is None:
        return None
    path = Path(collection_status_csv or DEFAULT_COLLECTION_STATUS_CSV)
    collection_status = read_collection_status_csv(path) if path.is_file() else {}
    return derive_policy_signals(
        conn,
        company_id,
        now,
        collection_status,
        window_days=cfg.thresholds.negative_event_window_days,
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
    collection_status_csv: str | Path | None = None,
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
        collection_status_csv: override for the pre-collected company-event
            status file (see `_event_signals`); `None` uses the project
            default. Pass it to pin the event-collection state a test or a
            replay is reasoning against.
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
    evidence = probes.run.save_evidence(
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
        board = probes.execute(
            BoardSnapshotProbe, BoardSnapshotArgs(ats=ats, tenant=tenant)
        )
        if not board.ok:
            failures.append(board)
        elif job_id:
            jobs: list[BoardJob] = list((board.data or {}).get("jobs") or [])
            evidence += probes.run.save_evidence(
                probe=BoardSnapshotProbe.name,
                claims=[
                    _board_claim(
                        jobs=jobs, ats=str(ats), tenant=str(tenant), job_id=str(job_id), now=now
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

    # -- derived state ----------------------------------------------------
    features = (
        posting_features(conn, cfg, posting_id, now=now)
        if posting_row_exists and posting_id is not None
        else None
    )
    event_signals = _event_signals(conn, cfg, company_id, now, collection_status_csv)

    inputs = derive_policy_inputs(
        evidence,
        features,
        event_signals,
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
        event_signals=event_signals,
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

    Two inputs cannot be read from evidence alone and are threaded through
    explicitly:

    * `event_signals` — `rli.probes.company_events` returns
      `material_negative_event` / `freeze_or_pause` as structured `data`, and
      its claims are dated news items rather than a boolean claim, so the
      probe's own answer replaces the pre-populated one from the case state
      when it ran. The two agree by construction (both come from
      `derive_policy_signals` over the same store at the same `as_of`); the
      probe's is preferred because it is the one the trace can point at.
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
            case.evidence += probes.run.save_evidence(
                probe=probe_cls.name, claims=claims, posting_id=evidence_posting_id
            )

    events = case.probe_results.get(CompanyEventsProbe.name)
    if events is not None and events.ok and events.data is not None:
        case.event_signals = (
            events.data["material_negative_event"],
            events.data["freeze_or_pause"],
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
        case.event_signals,
        probes.now,
        cfg=probes.cfg,
        resolver_ok=case.resolver_ok,
        team_signal=team_value,
    )
    case.unpopulated = case.inputs.unpopulated()
    case.next_evidence_index = probes.run.next_evidence_index
    return case
