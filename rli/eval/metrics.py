"""spec.md §6 evaluation metrics over a replay dataset (PLAN.md M6).

This module is the measurement layer of the final evaluation. `rli.eval.
baseline` (PLAN.md M4) answered one question — "how does System B compare to
System A on dev/validation?" — for exactly two systems, and hard-refused the
`test` split by construction. M6 needs the same shape generalised: N systems
(`A`, `B`, `C`, ...), the spec.md §6 "Data quality" block, the spec.md §6
"Agent efficiency" block in full (failure modes, token accounting, early-stop
regret), and a *read*-side permission to touch the held-out split once, for
the final evaluation the spec asks for. `rli.eval.gates` turns the numbers
this module produces into pass/fail verdicts; `rli.eval.evaluate` renders
them.

Nothing here writes to the database. Every function is a pure read over
`runs`, `run_steps`, `evidence`, `postings` and `replay_datasets`.

--------------------------------------------------------------------------
The two hazards that shape almost every decision below
--------------------------------------------------------------------------

**1. `run_steps.cost_usd` mixes two incompatible units.** Rows with
`component='model'` hold REAL US dollars charged by the LLM API. Rows with
`component='probe'` hold unitless placeholder COST POINTS from
`[probe_costs]` (`low=1 medium=3 high=10`) — a preference ordering invented
so the controller's budget ledger has a number to compare, explicitly
documented in `config.toml` as "not a cost forecast". `runs.total_cost_usd`
is the SUM of both, and is therefore a meaningless quantity: it adds dollars
to points. Worse, it is meaningless *asymmetrically* — System A and System B
run probes and never call a model, so their totals are pure points, while
System C's total is points plus dollars. A single "total cost" column would
make C look ~1000x cheaper or ~1000x dearer than A depending on nothing but
the exchange rate nobody has measured.

So: `CostSplit` carries `probe_cost_points` and `model_cost_usd` as two
separate fields, computed by two separate `SUM(...) WHERE component = ?`
queries, and this module never adds them, never averages them together, and
never exposes a combined figure. `runs.total_cost_usd` is not read at all.

**2. System A structurally flatters every leaner system.** See
`SYSTEM_A_CAVEAT` below. It is attached to every `EfficiencyMetrics` (as
`structural_caveat`) so the number and its caveat cannot be separated by a
copy-paste into a report.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **The split gate is a *permission*, not a removal.** `rli.eval.baseline.
  _check_allowed_splits` refuses `"test"` unconditionally and stays exactly
  as it is — PLAN.md M4's interlock is still correct for M4's report.  M6
  gets its own gate, `resolve_allowed_splits`, which refuses `"test"` too
  unless the caller passes `allow_test=True` *explicitly*. It raises
  `rli.eval.baseline.HoldoutSplitRequestedError` — the same exception class,
  reused rather than redefined, so a caller that catches "someone tried to
  touch the holdout" catches both layers with one `except`. The default is
  still `("dev", "validation")`: a caller has to type the words to spend the
  holdout, and `rli/replay/build.py`'s BUILD-time refusal of `split="test"`
  is untouched, because building a dataset spends live probe calls whereas
  reading one does not.

* **Case identity, duplicate collapsing and the `unassigned` /
  `excluded_holdout` split are copied verbatim from `rli.eval.baseline`'s
  contract**, deliberately, so the two reports cannot disagree about which
  cases exist. The case key is `(runs.input_url, runs.replay_at)` (not
  `posting_id`: it is nullable, and a repost can map two URLs onto one
  posting); duplicates for one key collapse to the greatest
  `(started_at, id)` and are counted; a case whose posting has no split
  assignment is `unassigned` and excluded from everything, and a case whose
  split is real but out of scope is `excluded_holdout`. "Unknown split" is
  never treated as "safe to include" — an unassignable posting could belong
  to the holdout.

* **Dataset membership is a Python `endswith`, not SQL `LIKE`.** A run
  belongs to `dataset_id` iff `runs.mode='replay'` and
  `str(config_hash).endswith(f"|dataset:{dataset_id}")`. `rli.replay.build.
  case_state_at` writes inspection runs whose `config_hash` ends in
  `|case_state`, and those must not be counted as evaluation runs; a `LIKE
  '%dataset:m4%'` would additionally match a dataset named `m4-dev-200`.

* **Agreement is defined exactly as `rli.eval.baseline._compare` defines
  it**, because the M6 report sits next to the M4 report and a reader will
  compare the two numbers directly. Overall agreement counts a case as a
  match only when the REFERENCE action is not `None` and the two actions are
  equal (a case where the reference produced no decision is in the
  denominator and can never be a match). The per-class classes come from the
  REFERENCE system's actions, so a class the reference never produced
  contributes no term — macro agreement asks "for each action A actually
  recommends, how often does X agree?", and there is no such question for an
  action A never recommends. `macro_agreement` is the UNWEIGHTED mean of
  those per-class rates, which is the entire point: spec.md §6 requires it
  "reported with the action distribution so a default-heavy policy cannot
  pass trivially". A system that answers `quick_apply` to everything scores
  well on overall agreement whenever the corpus is `quick_apply`-heavy and
  scores ~1/k on macro. Both are always reported, and so are both action
  distributions.

* **`system == reference` is not special-cased.** Asking for `A` vs `A`
  yields `overall_agreement = macro_agreement = 1.0` by the same code path
  that produces every other figure. The report wants A's own probe, cost and
  latency block, and a self-comparison is the cheapest way to get it without
  a second code path that could drift.

* **"Unnecessary probes" is reported in BOTH directions, because the trace
  cannot answer the question anyone actually means.** What a reader wants is
  "probes whose result did not change the decision". That is a
  counterfactual, and no counterfactual is recorded: the trace holds the
  probe that ran and the decision that followed, never the decision that
  would have followed without it. So this module reports two literal,
  checkable proxies and labels them as proxies:
    - `unnecessary_probe_steps` — dynamic probe executions that produced
      ZERO `evidence` rows for that run. This is the strict "it returned
      nothing" reading. A probe that DID record evidence which then failed to
      move the action is NOT counted and is NOT recoverable from the trace.
      Note that on the current corpus `company_events` and
      `requirements_drift` frequently return "no events" / "no drift", which
      is a real, informative negative result — so a high rate here means
      "these probes usually find nothing", not "the agent was stupid".
    - `reference_extra_probes_no_action_change` — the counterpart: over
      paired cases where the two systems AGREE on the action, how many
      dynamic probes the reference ran did the candidate skip? Those are
      probes the fuller system spent that provably changed no decision *on
      this corpus*. Read with `SYSTEM_A_CAVEAT` firmly in mind when the
      reference is A.

* **Early-stop regret needs an opportunity denominator or it is
  uninterpretable.** A system that stops early can only "regret" stopping on
  a case where the reference actually went further. So `early_stop_
  opportunities` counts paired cases where `ref_dynamic - sys_dynamic` is
  non-empty, and `early_stop_regret_cases` counts those of them where the
  two actions differ. The rate is `cases / opportunities`, `None` when there
  were no opportunities — NOT `0.0`, which would read as "it never regrets"
  when the truth is "it never had the chance to".

  Regret uses plain `!=` on the two actions rather than the agreement rule
  above: two runs that both failed to produce a decision are not a
  disagreement to regret, and `reference_extra_probes_no_action_change`
  uses the matching plain `==` so the two readings partition the same set
  consistently.

* **`repeated_calls` counts a controller invariant violation, so it must be
  computed per run, not globally.** The same `(probe_name, args_hash)` pair
  legitimately recurs across runs (that is the tool cache working). Within
  ONE run the controller rejects a duplicate candidate outright
  (`candidate_rejected:duplicate`), so `sum(max(0, n - 1))` over each run's
  `(probe_name, args_hash)` groups is zero unless something is broken. A
  nonzero value is a real finding, not a tuning knob.

* **Undefined ratios are `None`, never `0.0`.** Every rate in this module
  divides by a count that can legitimately be zero (no runs, no
  opportunities, no classified reasons). `0.0` would be indistinguishable
  from a measured zero and would silently poison an average.

* **Corrupt or NULL stored data degrades; it never raises.** `final_
  decision` can be NULL (a failed run) or unparsable; `evidence_ids` can be
  a string where a list was expected; `postings.ats` can be missing for a
  replay subject the collector never committed. Every decoder here returns a
  neutral value and increments a counter (`decisions_missing`,
  `reasons_with_no_ids`, an `(missing)` bucket) rather than aborting a report
  over one bad row. A metrics module that crashes on the one corrupt row in
  a 100k-row trace is worse than useless.

--------------------------------------------------------------------------
What "probe execution" means here
--------------------------------------------------------------------------

A step counts as a probe EXECUTION iff `component='probe'` AND
`decision_type='probe_run'`. `probe_skipped:*` rows are controller rows and
never count; `probe_retry:<n>` rows are controller rows and never count
(the retried execution has its own `probe_run` row). Cost tiers come from
`rli.eval.report.cost_tier_for`, which builds its table from the probe
classes themselves — restating `medium`/`high` here would create a second
source of truth that a retier could silently desynchronise.
"""

from __future__ import annotations

import json
import re
import sqlite3
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rli.agent.loop import parse_tokens
from rli.config import Config
from rli.eval.baseline import HoldoutSplitRequestedError, load_split_map
from rli.eval.report import cost_tier_for
from rli.eval.runner import STEP_PROBE_RUN
from rli.models.time import now_utc, parse_utc
from rli.policy.claim_families import CLAIM_FAMILIES, FAMILY_KEYWORDS
from rli.policy.claim_families import classify_reason as _classify_reason
from rli.policy.inputs import CLAIM_FIRST_PUBLISHED as _FIRST_PUBLISHED
from rli.policy.inputs import PRIMARY_PUBLISH_QUALITIES as _PRIMARY_PUBLISH_QUALITIES
from rli.policy.splits import DEFAULT_SEED, SPLIT_METHOD_COMPANY_HASH
from rli.probes.board_snapshot import BoardSnapshotProbe
from rli.probes.registry import DYNAMIC_PROBES
from rli.probes.resolve_posting import ResolvePostingProbe
from rli.replay.leakage import check_dataset

__all__ = [
    "ALL_SPLITS",
    "ALWAYS_RUN_PROBES",
    "CLAIM_FAMILIES",
    "DEFAULT_MATCH_PRECISION_PATH",
    "DEFAULT_SPLITS",
    "FAMILY_KEYWORDS",
    "SYSTEM_A_CAVEAT",
    "CaseRun",
    "CitationSupport",
    "CostSplit",
    "DataQuality",
    "EfficiencyMetrics",
    "MetricsCase",
    "MetricsCaseSet",
    "agent_efficiency",
    "citation_support_for_runs",
    "collect_system_runs",
    "data_quality",
    "era_boundary",
    "era_for",
    "read_match_precision",
    "resolve_allowed_splits",
    "split_case_set_by_era",
    "split_map_for_dataset",
    "subset_case_set",
    "FrozenSplitWarning",
]

#: Every split name `rli.policy.splits` can assign.
ALL_SPLITS: tuple[str, ...] = ("dev", "validation", "test")

#: What an M6 entry point evaluates unless the caller explicitly opts into
#: the holdout. Same default as `rli.eval.baseline.ALLOWED_SPLITS`.
DEFAULT_SPLITS: tuple[str, ...] = ("dev", "validation")

#: The always-run probes of spec.md §4's first table. They are never dynamic
#: candidates, so they are excluded from every "did the agent choose to run
#: this?" figure (early-stop regret, unnecessary probes, ranker rows) while
#: still counting toward probe totals and cost.
ALWAYS_RUN_PROBES: tuple[str, ...] = (ResolvePostingProbe.name, BoardSnapshotProbe.name)

_MISSING = "(missing)"
_UNNAMED = "(unnamed)"

#: SQLite's default `SQLITE_MAX_VARIABLE_NUMBER` is 999 on older builds; a
#: dataset with more runs than that must not blow up an `IN (...)` clause.
_SQL_CHUNK = 400

SYSTEM_A_CAVEAT: str = (
    "STRUCTURAL CAVEAT: System A is not a neutral upper bound. `rli.eval.system_a` calls "
    "`eligible_probes(..., unpopulated_inputs=set(ALL_DYNAMIC_INPUTS))`, which makes spec.md "
    "§4's unresolved-question gate vacuous for A: A runs every dynamic probe that survives the "
    "history and licensing gates, whether or not that probe could change the action. System C "
    "is gated by `rli.policy.inputs.could_change_action` and therefore skips probes A always "
    "runs. Every probe-count comparison against A (medium/high probe use, cost points, latency, "
    "'unnecessary probes', early-stop regret) is biased in the leaner system's favour BY "
    "CONSTRUCTION, not by measurement. This is why spec.md §6's agent gate measures probe use "
    "against System B rather than against A. Read agreement-with-A as an accuracy figure, and "
    "probe-count-vs-A as an upper bound on achievable savings — never as evidence that A wasted "
    "work."
)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _bump(counts: dict[str, int], key: str, amount: int = 1) -> None:
    counts[key] = counts.get(key, 0) + amount


def _ratio(numerator: float, denominator: float) -> float | None:
    """`numerator / denominator`, or `None` when the ratio is undefined.

    Undefined ratios are `None` everywhere in this module (see the module
    docstring): `0.0` would be indistinguishable from a measured zero.
    """
    if denominator <= 0:
        return None
    return numerator / denominator


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _render(counts: Mapping[str, int]) -> str:
    if not counts:
        return "(none)"
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _chunks(values: Sequence[str], size: int = _SQL_CHUNK) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _decode_decision(payload: object) -> dict[str, object] | None:
    """Parse a stored `runs.final_decision`, or `None` if there is nothing usable.

    Same total, non-raising contract as `rli.eval.baseline._decode_decision`
    and `rli.eval.report._decode_decision` (both private to their modules): a
    NULL column, an empty string, invalid JSON or a non-object all decode to
    `None`, which every caller counts as `decisions_missing` rather than
    treating as an error.
    """
    if not isinstance(payload, str) or not payload.strip():
        return None
    try:
        decoded = json.loads(payload)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _dataset_suffix(dataset_id: str) -> str:
    return f"|dataset:{dataset_id}"


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# 1. Split permission and split map
# ---------------------------------------------------------------------------


def resolve_allowed_splits(
    requested: tuple[str, ...] | None = None, *, allow_test: bool = False
) -> tuple[str, ...]:
    """Validate an M6 split filter, refusing the holdout unless explicitly allowed.

    `requested=None` means "the default scope": `DEFAULT_SPLITS`
    (`dev`+`validation`) normally, `ALL_SPLITS` when `allow_test=True` —
    because a caller that has already asserted the right to read the holdout
    is asking for the final evaluation, which spec.md §6 defines over
    everything.

    Raises `rli.eval.baseline.HoldoutSplitRequestedError` (reused, not
    redefined, so one `except` catches both this gate and M4's) when
    `"test"` appears without `allow_test=True`, and `ValueError` for any name
    outside `ALL_SPLITS`. Duplicates are collapsed and the result is ordered
    by `ALL_SPLITS` so the value is canonical and comparable.
    """
    if requested is None:
        return ALL_SPLITS if allow_test else DEFAULT_SPLITS

    unknown = [name for name in requested if name not in ALL_SPLITS]
    if unknown:
        raise ValueError(
            f"unknown split name(s) in allowed_splits: {unknown!r} (known: {list(ALL_SPLITS)})"
        )
    if "test" in requested and not allow_test:
        raise HoldoutSplitRequestedError(
            "refusing to read the 'test' holdout: spec.md §6 keeps it for the final "
            "evaluation, so rli.eval requires an explicit allow_test=True. "
            f"got allowed_splits={tuple(requested)!r}"
        )
    return tuple(name for name in ALL_SPLITS if name in requested)


# Datasets built before schema version 5 recorded their temporal cutoffs only
# in free-text notes ("7-day grid, test cutoff 2026-09-15, validation cutoff
# 2026-09-07"); this is how those are recovered.
_NOTES_TEST_CUTOFF = re.compile(r"\btest cutoff\s+([0-9][0-9T:.\-+Z]*)", re.IGNORECASE)
_NOTES_VALIDATION_CUTOFF = re.compile(r"\bvalidation cutoff\s+([0-9][0-9T:.\-+Z]*)", re.IGNORECASE)


class FrozenSplitWarning(UserWarning):
    """A dataset's split assignment had to be RECONSTRUCTED, not read back."""


def _dataset_split_row(conn: sqlite3.Connection, dataset_id: str) -> dict[str, object] | None:
    """The dataset header as a dict, tolerant of a pre-version-5 table.

    Returns `None` when there is no row (or no `replay_datasets` table at all,
    a database predating schema version 3). Columns a version-3/4 table lacks
    simply read as `None`.
    """
    try:
        row = conn.execute(
            "SELECT * FROM replay_datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _parse_optional(value: object) -> datetime | None:
    text = _str_or_none(value)
    if not text:
        return None
    try:
        return parse_utc(text)
    except (ValueError, TypeError):
        return None


def _parse_noted(text: str) -> datetime | None:
    """A cutoff written in dataset notes: a timestamp, or a bare date at UTC midnight.

    `rli replay build --cutoff` only accepts a full UTC timestamp, so a bare
    `2026-09-15` in hand-written notes is read as that day's UTC midnight.
    """
    parsed = _parse_optional(text)
    if parsed is not None:
        return parsed
    try:
        return datetime.fromisoformat(text.strip()).replace(tzinfo=UTC)
    except ValueError:
        return None


def _frozen_case_splits(conn: sqlite3.Connection, dataset_id: str) -> dict[str, str] | None:
    """`posting_id -> split` as stored on `replay_cases`, or None if not frozen.

    `None` unless EVERY case of the dataset carries a split: a half-frozen
    dataset (impossible from `rli.replay.build`, but a hand edit could make
    one) is treated as not frozen rather than silently mixed with a
    recomputation.
    """
    try:
        rows = conn.execute(
            "SELECT posting_id, split FROM replay_cases WHERE dataset_id = ?", (dataset_id,)
        ).fetchall()
    except sqlite3.Error:  # no `split` column: a pre-version-5 database
        return None
    if not rows or any(row["split"] is None for row in rows):
        return None
    return {str(row["posting_id"]): str(row["split"]) for row in rows}


def _run_posting_aliases(
    conn: sqlite3.Connection, dataset_id: str, case_splits: Mapping[str, str]
) -> dict[str, str]:
    """`runs.posting_id -> split` for replay runs whose id differs from the case's.

    `rli.eval.metrics.collect_system_runs` keys a case by the posting id the
    RUNNING system derived (`runs.posting_id`), which can differ from the
    dataset's own id (`rli.replay.mode.ReplayProbeRunner` explains when). A
    run IS its case, identified by `(input_url, replay_at)` exactly as
    `rli.replay.run` matches it, so the run's id inherits the case's split.
    Joined in Python: there is no index on `runs.input_url`.
    """
    try:
        cases = {
            (str(row["canonical_url"]), str(row["replay_at"])): str(row["posting_id"])
            for row in conn.execute(
                "SELECT posting_id, replay_at, canonical_url FROM replay_cases "
                "WHERE dataset_id = ?",
                (dataset_id,),
            )
        }
        runs = conn.execute(
            "SELECT DISTINCT posting_id, input_url, replay_at FROM runs "
            "WHERE mode = 'replay' AND posting_id IS NOT NULL AND replay_at IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return {}
    aliases: dict[str, str] = {}
    for row in runs:
        case_posting = cases.get((str(row["input_url"]), str(row["replay_at"])))
        run_posting = str(row["posting_id"])
        if case_posting is None or case_posting == run_posting:
            continue
        split = case_splits.get(case_posting)
        if split is not None and run_posting not in case_splits:
            aliases.setdefault(run_posting, split)
    return aliases


def split_map_for_dataset(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    split_kind: Literal["temporal", "company"] | None = None,
    cutoff: datetime | None = None,
    validation_cutoff: datetime | None = None,
    seed: int = DEFAULT_SEED,
) -> tuple[dict[str, str], str]:
    """`(posting_id -> split, split_kind_used)`, reproducing the dataset's own assignment.

    A replay dataset was built against one particular split assignment, and
    an evaluation that re-derived a *different* one would silently move cases
    between dev, validation and holdout — the exact failure the split exists
    to prevent. And re-deriving is NOT reproducing: the corpus grows every
    day, the greedy company split is a function of the whole corpus, and the
    temporal cutoffs a build used are not its `created_at`. So, in order:

    1. **Explicit arguments** (`split_kind` / `cutoff` / `validation_cutoff`)
       mean a deliberate re-split over today's corpus, exactly as before.
    2. **A frozen dataset** (schema version 5: every `replay_cases.split` set
       by `rli.replay.build`): those per-case splits are returned as stored.
       Postings outside the dataset (e.g. `rli.eval.gates.product_gate`'s
       outcome postings) get the dataset's recorded method/seed/cutoffs
       applied to today's corpus underneath — the stable company hash or the
       temporal cutoffs, both of which are fixed per posting.
    3. **An older dataset** (no stored splits): the build-time assignment is
       RECONSTRUCTED — the greedy split those builds used, over the postings
       that existed at the dataset's `created_at` (`load_split_map(as_of=)`),
       with the temporal cutoffs recovered from the dataset's notes when it
       recorded them there, else `created_at` as the old code did. A
       `FrozenSplitWarning` says so. The reconstruction is exact unless a
       later history rebuild moved a posting's `first_observed`.

    In cases 2 and 3 a replay run whose own `posting_id` differs from its
    case's inherits the case's split (`_run_posting_aliases`).

    Falls back to `("company", now_utc())` when the dataset row is absent
    (or the table has not been migrated in): a company-held-out split is the
    stricter of the two — it also keeps same-company postings together — so
    an unknown provenance degrades toward *more* separation, not less.
    Postings with a NULL `first_observed` are simply absent from the result
    and every caller treats a missing key as `unassigned`.
    """
    row = _dataset_split_row(conn, dataset_id)

    kind = split_kind
    if kind is None:
        stored = None if row is None else _str_or_none(row.get("split_kind"))
        kind = stored if stored in ("temporal", "company") else "company"

    created_at = None if row is None else _parse_optional(row.get("created_at"))
    overridden = split_kind is not None or cutoff is not None or validation_cutoff is not None

    if overridden or row is None:
        resolved_cutoff = cutoff if cutoff is not None else created_at
        mapping = load_split_map(
            conn,
            cutoff=resolved_cutoff if resolved_cutoff is not None else now_utc(),
            validation_cutoff=validation_cutoff,
            seed=seed,
            split_kind=kind,  # type: ignore[arg-type]
        )
        return {posting_id: str(split) for posting_id, split in mapping.items()}, str(kind)

    frozen = _frozen_case_splits(conn, dataset_id)
    method = _str_or_none(row.get("split_method"))
    if frozen is not None and method is not None:
        stored_seed = row.get("split_seed")
        stored_cutoff = _parse_optional(row.get("split_cutoff")) or created_at or now_utc()
        base = load_split_map(
            conn,
            cutoff=stored_cutoff,
            validation_cutoff=_parse_optional(row.get("split_validation_cutoff")),
            seed=int(stored_seed) if stored_seed is not None else seed,  # type: ignore[arg-type]
            split_kind=kind,  # type: ignore[arg-type]
            company_method="hash" if method == SPLIT_METHOD_COMPANY_HASH else "greedy",
        )
        mapping = {posting_id: str(split) for posting_id, split in base.items()}
        mapping.update(frozen)
        mapping.update(_run_posting_aliases(conn, dataset_id, frozen))
        return mapping, str(kind)

    # 3. A dataset built before splits were frozen: reconstruct.
    notes = _str_or_none(row.get("notes")) or ""
    test_match = _NOTES_TEST_CUTOFF.search(notes)
    validation_match = _NOTES_VALIDATION_CUTOFF.search(notes)
    noted_cutoff = _parse_noted(test_match.group(1)) if test_match else None
    noted_validation = _parse_noted(validation_match.group(1)) if validation_match else None
    resolved_cutoff = noted_cutoff or created_at or now_utc()
    warnings.warn(
        f"replay dataset {dataset_id!r} predates frozen splits (schema v5): its "
        f"{kind} split assignment is RECONSTRUCTED from the postings that existed at "
        f"its build time ({_str_or_none(row.get('created_at'))}) with the greedy "
        f"method, test cutoff {resolved_cutoff.isoformat()} "
        f"({'from its notes' if noted_cutoff else 'its created_at'}) and validation "
        f"cutoff {noted_validation.isoformat() if noted_validation else 'none'}; a later "
        "history rebuild that moved a posting's first_observed can still change it. "
        "Rebuild the dataset to freeze its splits.",
        FrozenSplitWarning,
        stacklevel=2,
    )
    base = load_split_map(
        conn,
        cutoff=resolved_cutoff,
        validation_cutoff=noted_validation,
        seed=seed,
        split_kind=kind,  # type: ignore[arg-type]
    )
    rebuilt = load_split_map(
        conn,
        cutoff=resolved_cutoff,
        validation_cutoff=noted_validation,
        seed=seed,
        split_kind=kind,  # type: ignore[arg-type]
        as_of=created_at,
    )
    mapping = {posting_id: str(split) for posting_id, split in base.items()}
    mapping.update({posting_id: str(split) for posting_id, split in rebuilt.items()})
    case_splits = {
        str(r["posting_id"]): mapping[str(r["posting_id"])]
        for r in conn.execute(
            "SELECT DISTINCT posting_id FROM replay_cases WHERE dataset_id = ?", (dataset_id,)
        )
        if str(r["posting_id"]) in mapping
    }
    mapping.update(_run_posting_aliases(conn, dataset_id, case_splits))
    return mapping, str(kind)


# ---------------------------------------------------------------------------
# 2. Case collection across N systems
# ---------------------------------------------------------------------------


class CaseRun(BaseModel):
    """One system's surviving run for one replay case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    run_id: str
    status: str
    action: str | None = None
    posting_state: str | None = None
    evidence_quality: str | None = None
    #: Probe names actually EXECUTED (`component='probe'` +
    #: `decision_type='probe_run'`), deduplicated, in first-execution order.
    #: Includes the always-run pair as well as dynamic probes. Deduplicated
    #: because every consumer asks a set question ("did this system run
    #: `company_events` here?"); a repeated execution is counted separately
    #: and much more loudly by `EfficiencyMetrics.repeated_calls`.
    probes_run: tuple[str, ...] = ()

    def dynamic_probes(self) -> frozenset[str]:
        """The DYNAMIC probes this run executed (spec.md §4's second table)."""
        return frozenset(name for name in self.probes_run if name in DYNAMIC_PROBES)


class MetricsCase(BaseModel):
    """One replay case `(input_url, replay_at)` and each system's run for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_url: str
    replay_at: str
    posting_id: str
    split: str
    runs: dict[str, CaseRun] = {}


class MetricsCaseSet(BaseModel):
    """Every in-scope replay case for one dataset, with per-system accounting.

    `cases` holds every case that passed the split gate for at least one
    system — not only fully paired ones, because per-system cost/probe blocks
    are scoped to `run_ids` and must include single-sided cases. Pairwise
    metrics call `cases_for(...)` to narrow to the cases where the systems
    they compare both ran.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    allowed_splits: tuple[str, ...]
    systems: tuple[str, ...]

    cases: tuple[MetricsCase, ...] = ()
    run_ids: dict[str, tuple[str, ...]] = {}
    counts_by_system: dict[str, int] = {}
    unassigned: int = 0
    excluded_holdout: int = 0
    duplicates_collapsed: int = 0
    #: Cases whose runs carry no `posting_id` (the posting's identity never
    #: resolved) but which ARE a case of this dataset — matched to their
    #: `replay_cases` row by `(input_url, replay_at)`, which also gives their
    #: split. Excluded from scoring like `unassigned`, but counted apart
    #: (by split) because they are not a split-map gap.
    identity_unresolved: int = 0
    identity_unresolved_by_split: dict[str, int] = {}

    def cases_for(self, *systems: str) -> tuple[MetricsCase, ...]:
        """The cases where EVERY named system produced a surviving run."""
        if not systems:
            return self.cases
        return tuple(case for case in self.cases if all(name in case.runs for name in systems))

    def describe(self) -> str:
        lines = [
            f"case set: dataset={self.dataset_id!r} "
            f"splits=[{', '.join(self.allowed_splits) or 'none'}] "
            f"systems=[{', '.join(self.systems) or 'none'}]",
            f"  cases={len(self.cases)} unassigned={self.unassigned} "
            f"identity_unresolved={self.identity_unresolved} "
            f"excluded_holdout={self.excluded_holdout} "
            f"duplicates_collapsed={self.duplicates_collapsed}",
            f"  runs by system: {_render(self.counts_by_system)}",
        ]
        if len(self.systems) > 1:
            paired = len(self.cases_for(*self.systems))
            lines.append(f"  cases where all {len(self.systems)} systems ran: {paired}")
        if "test" in self.allowed_splits:
            lines.append("  NOTE: the held-out 'test' split is IN SCOPE for this case set.")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _latest_by_case(rows: list[sqlite3.Row]) -> tuple[dict[tuple[str, str], sqlite3.Row], int]:
    """Group by `(input_url, replay_at)`, keep the greatest `(started_at, id)`, count the rest.

    Identical rule to `rli.eval.baseline._latest_by_case` (that helper is
    private to its module, and the two reports must not disagree about which
    run represents a case).
    """
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["input_url"], row["replay_at"]), []).append(row)

    latest: dict[tuple[str, str], sqlite3.Row] = {}
    duplicates = 0
    for key, group in groups.items():
        group.sort(key=lambda r: (r["started_at"], r["id"]))
        latest[key] = group[-1]
        duplicates += len(group) - 1
    return latest, duplicates


def _probes_run_by_run(
    conn: sqlite3.Connection, run_ids: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """`run_id -> executed probe names`, deduplicated, in first-execution order."""
    ordered: dict[str, list[str]] = {}
    for chunk in _chunks(run_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT run_id, probe_name
            FROM run_steps
            WHERE run_id IN ({placeholders})
                  AND component = 'probe' AND decision_type = ?
            ORDER BY run_id, step_index, id
            """,
            (*chunk, STEP_PROBE_RUN),
        ).fetchall()
        for row in rows:
            name = _str_or_none(row["probe_name"])
            if name is None:
                continue
            names = ordered.setdefault(row["run_id"], [])
            if name not in names:
                names.append(name)
    return {run_id: tuple(names) for run_id, names in ordered.items()}


def collect_system_runs(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    splits: Mapping[str, str],
    systems: Sequence[str] = ("A", "B", "C"),
    allowed_splits: tuple[str, ...] = DEFAULT_SPLITS,
) -> MetricsCaseSet:
    """Collect one dataset's replay runs for `systems`, split-gated.

    The N-system generalisation of `rli.eval.baseline.collect_cases`, with
    the same case key, the same duplicate rule and the same two exclusion
    buckets — see the module docstring. A system named in `systems` that has
    no runs at all is kept in `MetricsCaseSet.systems` with a count of zero,
    because "System C was not run" is a finding the report must state, not an
    absence to hide.

    Unlike `collect_cases` this function does NOT refuse the `test` split:
    the permission decision belongs to `resolve_allowed_splits`, which the
    caller has already made. Passing `allowed_splits=("test",)` here reads
    the holdout, which is exactly what spec.md §6's final evaluation does
    once.
    """
    wanted = tuple(dict.fromkeys(systems))
    if not wanted:
        return MetricsCaseSet(
            dataset_id=dataset_id, allowed_splits=tuple(allowed_splits), systems=()
        )

    suffix = _dataset_suffix(dataset_id)
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"""
        SELECT id, input_url, replay_at, posting_id, system, status,
               final_decision, started_at, config_hash
        FROM runs
        WHERE mode = 'replay' AND system IN ({placeholders})
              AND replay_at IS NOT NULL AND config_hash IS NOT NULL
        ORDER BY started_at, id
        """,
        wanted,
    ).fetchall()
    # `endswith` in Python, never SQL `LIKE`: see the module docstring.
    rows = [row for row in rows if str(row["config_hash"]).endswith(suffix)]

    latest_by_system: dict[str, dict[tuple[str, str], sqlite3.Row]] = {}
    duplicates_collapsed = 0
    for system in wanted:
        latest, duplicates = _latest_by_case([row for row in rows if row["system"] == system])
        latest_by_system[system] = latest
        duplicates_collapsed += duplicates

    keys: set[tuple[str, str]] = set()
    for latest in latest_by_system.values():
        keys |= set(latest)

    kept_rows: dict[str, sqlite3.Row] = {}
    case_rows: list[tuple[tuple[str, str], str, str, dict[str, sqlite3.Row]]] = []
    unassigned = 0
    excluded_holdout = 0
    identity_unresolved = 0
    identity_unresolved_by_split: dict[str, int] = {}
    dataset_cases = _dataset_case_postings(conn, dataset_id)

    for key in sorted(keys):
        present = {
            system: latest_by_system[system][key]
            for system in wanted
            if key in latest_by_system[system]
        }

        posting_id = None
        for system in wanted:
            row = present.get(system)
            if row is not None and row["posting_id"] is not None:
                posting_id = str(row["posting_id"])
                break

        if posting_id is None:
            # No run resolved the posting. If the case is a row of this
            # dataset, its split is known (frozen per case, or the split of
            # the dataset's own posting id): classify it as identity-
            # unresolved rather than as a split-map gap.
            case_posting = dataset_cases.get(key)
            case_split = None if case_posting is None else splits.get(case_posting)
            if case_split is None:
                unassigned += 1
            elif case_split not in allowed_splits:
                excluded_holdout += 1
            else:
                identity_unresolved += 1
                identity_unresolved_by_split[case_split] = (
                    identity_unresolved_by_split.get(case_split, 0) + 1
                )
            continue

        split = splits.get(posting_id)
        if split is None:
            unassigned += 1
            continue
        if split not in allowed_splits:
            excluded_holdout += 1
            continue

        case_rows.append((key, posting_id, str(split), present))
        for row in present.values():
            kept_rows[str(row["id"])] = row

    probes_by_run = _probes_run_by_run(conn, tuple(kept_rows))

    cases: list[MetricsCase] = []
    run_ids: dict[str, list[str]] = {system: [] for system in wanted}
    for key, posting_id, split, present in case_rows:
        case_runs: dict[str, CaseRun] = {}
        for system, row in present.items():
            run_id = str(row["id"])
            run_ids[system].append(run_id)
            decoded = _decode_decision(row["final_decision"]) or {}
            case_runs[system] = CaseRun(
                system=system,
                run_id=run_id,
                status=str(row["status"]),
                action=_str_or_none(decoded.get("recommended_action")),
                posting_state=_str_or_none(decoded.get("posting_state")),
                evidence_quality=_str_or_none(decoded.get("evidence_quality")),
                probes_run=probes_by_run.get(run_id, ()),
            )
        input_url, replay_at = key
        cases.append(
            MetricsCase(
                input_url=input_url,
                replay_at=replay_at,
                posting_id=posting_id,
                split=split,
                runs=case_runs,
            )
        )

    return MetricsCaseSet(
        dataset_id=dataset_id,
        allowed_splits=tuple(allowed_splits),
        systems=wanted,
        cases=tuple(cases),
        run_ids={system: tuple(ids) for system, ids in run_ids.items()},
        counts_by_system={system: len(ids) for system, ids in run_ids.items()},
        unassigned=unassigned,
        excluded_holdout=excluded_holdout,
        duplicates_collapsed=duplicates_collapsed,
        identity_unresolved=identity_unresolved,
        identity_unresolved_by_split=identity_unresolved_by_split,
    )


def _dataset_case_postings(conn: sqlite3.Connection, dataset_id: str) -> dict[tuple[str, str], str]:
    """`(canonical_url, replay_at) -> replay_cases.posting_id` for one dataset."""
    try:
        return {
            (str(row["canonical_url"]), str(row["replay_at"])): str(row["posting_id"])
            for row in conn.execute(
                "SELECT posting_id, replay_at, canonical_url FROM replay_cases "
                "WHERE dataset_id = ?",
                (dataset_id,),
            )
        }
    except sqlite3.Error:  # pragma: no cover - pre-replay schema
        return {}


def _case_set_for(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    splits: Mapping[str, str],
    systems: Sequence[str],
    allowed_splits: tuple[str, ...],
    case_set: MetricsCaseSet | None,
) -> MetricsCaseSet:
    """Reuse a caller-supplied case set when it covers `systems`, else build one.

    `rli.eval.evaluate` collects the case set once and threads it through
    every metric, so a 100-case dataset is not re-paired five times. A
    supplied set that does NOT cover a needed system is rebuilt rather than
    silently reporting that system as absent — a missing system in the cache
    is a caller bug, and reporting "System C has zero runs" because of it
    would be the worst possible failure mode for this report.
    """
    if case_set is not None and all(system in case_set.systems for system in systems):
        return case_set
    return collect_system_runs(
        conn,
        dataset_id=dataset_id,
        splits=splits,
        systems=systems,
        allowed_splits=allowed_splits,
    )


def era_boundary(conn: sqlite3.Connection) -> str | None:
    """The earliest `captured_at` among own-collected board snapshots, or `None`.

    dev-300-v3 pools two eras (spec.md §1): cases whose
    `replay_at` predates this project's own board-snapshot collection rest on
    Wayback captures alone — spec.md §1 calls that weak evidence — while
    cases at or after it also have an own `board_snapshots` row (`source=
    'own'`) available. This boundary is that cutover instant. `None` means
    this database has never recorded an own capture, so every case is
    archive-era (see `era_for`). An old database without the `board_snapshots`
    table degrades to `None` rather than raising, matching `_dataset_row` /
    `_count` in `rli.eval.evaluate`.
    """
    try:
        row = conn.execute(
            "SELECT MIN(captured_at) FROM board_snapshots WHERE source = 'own'"
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - defensive (pre-board_snapshots schema)
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def era_for(replay_at: str, boundary: str | None) -> str:
    """Which era one case's `replay_at` falls in: `"archive-era"` or `"live-era"`.

    Plain lexical `<` on ISO-8601 UTC strings, exactly like `ORDER BY
    started_at, id` and `_dataset_suffix` rely on elsewhere in this module —
    no datetime parsing. A `boundary` of `None` (no own capture ever
    recorded) puts everything in `"archive-era"`; a `replay_at` equal to the
    boundary counts as `"live-era"`, since the own capture already exists as
    of that instant.
    """
    if boundary is None or replay_at < boundary:
        return "archive-era"
    return "live-era"


def split_case_set_by_era(
    case_set: MetricsCaseSet, boundary: str | None
) -> dict[str, MetricsCaseSet]:
    """Partition a case set into `"live-era"` and `"archive-era"` halves.

    Both keys are always present, even when one half is empty, so a caller
    can index either unconditionally. `dataset_id`, `allowed_splits` and —
    CRITICALLY — `systems` are copied VERBATIM from `case_set` onto both
    halves, never narrowed to the systems that happen to have a run in that
    era. `_case_set_for`'s reuse check is `all(system in case_set.systems for
    system in systems)`; a narrowed `systems` tuple would make that check
    fail the next time a caller passes an era-filtered set into e.g.
    `agent_efficiency(..., case_set=split["live-era"])`, silently rebuilding
    a POOLED case set behind the caller's back and reporting pooled numbers
    under an era label — exactly the trap this function must not set.

    `unassigned`, `excluded_holdout` and `duplicates_collapsed` are
    pre-split-gate, whole-dataset bookkeeping concepts with no meaningful
    per-era reading, so both halves report them as `0`; the pooled
    `case_set` passed in already carries the real figures, so nothing is
    lost by zeroing them here.
    """
    cases_by_era: dict[str, list[MetricsCase]] = {"live-era": [], "archive-era": []}
    for case in case_set.cases:
        cases_by_era[era_for(case.replay_at, boundary)].append(case)
    return {era: subset_case_set(case_set, cases) for era, cases in cases_by_era.items()}


def subset_case_set(case_set: MetricsCaseSet, cases: Iterable[MetricsCase]) -> MetricsCaseSet:
    """`case_set` narrowed to `cases`, keeping `systems` VERBATIM.

    The one way to build a sub-case-set (an era, the probe-dependent cases):
    `systems` is never narrowed, for the reason `split_case_set_by_era`
    gives, and `run_ids` keeps the pooled order restricted to the kept
    cases. The pre-split-gate bookkeeping (`unassigned`, `excluded_holdout`,
    `duplicates_collapsed`) has no per-subset reading and is zeroed.
    """
    kept_cases = tuple(cases)
    kept_run_ids_by_system: dict[str, tuple[str, ...]] = {}
    for system in case_set.systems:
        kept = {case.runs[system].run_id for case in kept_cases if system in case.runs}
        kept_run_ids_by_system[system] = tuple(
            run_id for run_id in case_set.run_ids.get(system, ()) if run_id in kept
        )
    return MetricsCaseSet(
        dataset_id=case_set.dataset_id,
        allowed_splits=case_set.allowed_splits,
        systems=case_set.systems,
        cases=kept_cases,
        run_ids=kept_run_ids_by_system,
        counts_by_system={system: len(ids) for system, ids in kept_run_ids_by_system.items()},
        unassigned=0,
        excluded_holdout=0,
        duplicates_collapsed=0,
    )


# ---------------------------------------------------------------------------
# 3. Data quality (spec.md §6 "Data quality")
# ---------------------------------------------------------------------------

#: `CLAIM_FAMILIES` and `FAMILY_KEYWORDS` now live in `rli.policy.
#: claim_families` (imported above) — a shared lexicon between this module's
#: `CitationSupport` data-quality reporting and `rli.agent.explanation`'s
#: citation SUPPORT guard, which needs the same classifier without pulling in
#: this whole (heavier) eval-reporting module. They are re-exported here
#: under their original names — see `__all__` — so any existing importer of
#: `rli.eval.metrics.CLAIM_FAMILIES` / `FAMILY_KEYWORDS` keeps working
#: unchanged. `_classify_reason` (imported above as an alias of `rli.policy.
#: claim_families.classify_reason`) is kept private and under its original
#: name because every call site inside this module already spells it that
#: way.

DEFAULT_MATCH_PRECISION_PATH = "data/match_precision.md"

#: `87%`, `0.87`, `precision: 0.87`, `precision = 87%`. The percent form is
#: matched first inside one alternation so `precision: 87%` reads as 0.87 and
#: not as 87.0.
_PRECISION_PATTERN = re.compile(
    r"(?P<pct>\d+(?:\.\d+)?)\s*%|precision\s*[:=]\s*(?P<frac>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


class CitationSupport(BaseModel):
    """Do the decision's stated reasons actually cite evidence that exists and fits?

    spec.md §3 requires every reason to carry `evidence_ids`, and spec.md §6
    asks for citation support as a data-quality figure. Two separate
    questions are measured, because they fail for different reasons:

    * **id existence** — do the cited ids resolve to evidence this run
      actually recorded? A miss here is a plumbing bug (an id renamed or an
      evidence row dropped), and is measured over ALL reasons.
    * **support** — does at least one cited evidence item carry a
      `claim_type` from a family the reason's own text is about? A miss here
      is a reasoning bug (the text says "the posting was removed from the
      board" while citing a `first_published` date). It is measured only
      over reasons this module could classify at all.

    `reasons_unclassified` is reported rather than folded into either
    outcome: an unclassified reason is a gap in `FAMILY_KEYWORDS`, and
    charging it to the system under test would be measuring the lexicon.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_checked: int = 0
    runs_with_reasons: int = 0
    reasons_total: int = 0
    reasons_all_ids_exist: int = 0
    reasons_with_missing_ids: int = 0
    reasons_with_no_ids: int = 0
    reasons_classified: int = 0
    reasons_unclassified: int = 0
    reasons_supported: int = 0
    reasons_unsupported: int = 0
    id_existence_rate: float | None = None
    support_rate: float | None = None
    families_seen: dict[str, int] = {}

    def describe(self) -> str:
        lines = [
            f"citation support over {self.runs_checked} run(s) "
            f"({self.runs_with_reasons} with >=1 reason, {self.reasons_total} reason(s)):",
            f"  evidence ids: all_exist={self.reasons_all_ids_exist} "
            f"missing={self.reasons_with_missing_ids} none_cited={self.reasons_with_no_ids} "
            f"-> id_existence_rate={_pct(self.id_existence_rate)}",
            f"  classification: classified={self.reasons_classified} "
            f"unclassified={self.reasons_unclassified} "
            "(unclassified = no FAMILY_KEYWORDS match; counted, never guessed)",
            f"  support: supported={self.reasons_supported} "
            f"unsupported={self.reasons_unsupported} "
            f"-> support_rate={_pct(self.support_rate)} (of CLASSIFIED reasons only)",
            f"  families seen: {_render(self.families_seen)}",
        ]
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


class DataQuality(BaseModel):
    """spec.md §6's "Data quality" block for one dataset scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    allowed_splits: tuple[str, ...]
    systems: tuple[str, ...]

    runs_checked: int = 0
    postings_checked: int = 0

    ats_resolved_runs: int = 0
    ats_resolution_rate: float | None = None
    ats_distribution: dict[str, int] = {}

    #: ANY publish-family claim (first publish OR a refresh/last-published
    #: date). Kept for continuity; read the two splits below instead.
    publish_date_runs: int = 0
    publish_date_coverage: float | None = None
    publish_date_by_source_quality: dict[str, int] = {}
    #: A dated `first_published` claim from a PRIMARY source (`ats_native` /
    #: `page_structured`) — the claim `rli.policy.quality`'s Q2 rule and the
    #: recency rule read as a first publication. Claims the first-published
    #: guard refused are re-labelled and never count here.
    first_publish_runs: int = 0
    first_publish_coverage: float | None = None
    first_publish_by_source_quality: dict[str, int] = {}
    #: A refresh-type date only (`updated_at`, `last_published`,
    #: `refreshed_at`): evidence the posting moved, NOT when it first appeared.
    refresh_runs: int = 0
    refresh_coverage: float | None = None
    refresh_by_claim_type: dict[str, int] = {}

    repost_match_precision: float | None = None
    repost_match_precision_note: str = "pending"

    citation: CitationSupport

    leakage_violations: int = 0
    leakage_counts: dict[str, int] = {}
    leakage_clean: bool = True
    # The two figures `rli.replay.leakage` reports but deliberately does NOT
    # count as violations. They are carried through to here because this is
    # the durable report an operator actually reads: a `blob_input_exposures`
    # that only ever appeared on the CLI would leave `reports/evaluation.md`
    # saying "0 violations / CLEAN" for a dataset that still needs rebuilding
    # — the same silence that let security review H4 run for 830 runs.
    leakage_model_cache_misses: int = 0
    leakage_blob_input_exposures: int = 0
    #: Pre-fix own capture batches no recorded snapshot run window covers
    #: (`rli.snapshots.run_windows.uncovered_capture_batches`): leakage the
    #: `capture_fetched_after_t` rule cannot see. Reported, not counted.
    leakage_uncovered_capture_batches: int = 0

    def describe(self) -> str:
        lines = [
            f"data quality: dataset={self.dataset_id!r} "
            f"splits=[{', '.join(self.allowed_splits) or 'none'}] "
            f"systems=[{', '.join(self.systems) or 'none'}]",
            f"  scope: {self.runs_checked} run(s) over {self.postings_checked} posting(s)",
            f"  ATS resolution: {self.ats_resolved_runs}/{self.runs_checked} runs "
            f"= {_pct(self.ats_resolution_rate)} (a resolve_posting probe_run step, error IS NULL)",
            f"  ATS mix: {_render(self.ats_distribution)}",
            f"  first-publish coverage (dated first_published, ats_native/page_structured): "
            f"{self.first_publish_runs}/{self.runs_checked} runs = "
            f"{_pct(self.first_publish_coverage)}",
            f"  refresh / last-published coverage (updated_at, last_published, refreshed_at): "
            f"{self.refresh_runs}/{self.runs_checked} runs = {_pct(self.refresh_coverage)} "
            f"({_render(self.refresh_by_claim_type)})",
            f"  any publish-family claim: {self.publish_date_runs}/{self.runs_checked} runs "
            f"= {_pct(self.publish_date_coverage)}",
            f"  publish-date evidence by source quality (DISTINCT runs, a run may appear "
            f"under two): {_render(self.publish_date_by_source_quality)}",
            f"  repost match precision: {_pct(self.repost_match_precision)} "
            f"({self.repost_match_precision_note})",
            *(f"  {line}" for line in self.citation.describe().splitlines()),
            f"  future leakage: {self.leakage_violations} violation(s) "
            f"{'CLEAN' if self.leakage_clean else 'NOT CLEAN'} — {_render(self.leakage_counts)}",
            f"  future leakage (reported, not counted): "
            f"model cache misses={self.leakage_model_cache_misses} (spec.md §6 allows "
            f"live LLM calls on cache miss), blob input exposures="
            f"{self.leakage_blob_input_exposures} (a served `data` blob answers a policy "
            f"input no claim backs at T; a nonzero count needs a dataset rebuild to clear)",
        ]
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def read_match_precision(
    path: str | Path = DEFAULT_MATCH_PRECISION_PATH,
) -> tuple[float | None, str]:
    """`(precision, note)` read out of a hand-written repost match-precision memo.

    spec.md §6 wants "repost match precision on a hand-labeled sample". That
    label set is human work that does not exist yet, and this module refuses
    to invent it: there is no automatic fallback, no "estimate", and no
    `0.0`. When the file is absent the answer is `(None, "pending: ...")` and
    the report prints `n/a (pending)` — a visible hole is strictly better
    than a fabricated number that later gets quoted.

    When the file exists, the first `NN%`-shaped or `precision: 0.NN`-shaped
    token in it is taken. Deliberately lenient about the surrounding prose
    (it is a Markdown memo, not a data format) and deliberately strict about
    the range: a parsed value outside `[0, 1]` is rejected rather than
    clamped, since `precision: 87` most likely means the memo says something
    this parser has misread.
    """
    destination = Path(path)
    if not destination.exists():
        return None, f"pending: {destination} not present"
    try:
        text = destination.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        return None, f"pending: {destination} could not be read ({exc})"

    for match in _PRECISION_PATTERN.finditer(text):
        raw = match.group("pct")
        value = float(raw) / 100.0 if raw is not None else float(match.group("frac"))
        if 0.0 <= value <= 1.0:
            return value, f"parsed {value:.1%} from {destination}"
    return None, f"pending: no precision figure found in {destination}"


def _evidence_by_run(
    conn: sqlite3.Connection, run_ids: Sequence[str]
) -> dict[str, list[sqlite3.Row]]:
    rows_by_run: dict[str, list[sqlite3.Row]] = {}
    for chunk in _chunks(run_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT run_id, id, probe, claim_type, source_quality
            FROM evidence
            WHERE run_id IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        for row in rows:
            rows_by_run.setdefault(str(row["run_id"]), []).append(row)
    return rows_by_run


def _reason_entries(decision: Mapping[str, object]) -> list[tuple[str, list[str] | None]]:
    """`[(text, evidence_ids or None)]` from a decoded decision, tolerating junk.

    `None` for the ids means "the reason cited nothing usable" — either the
    key was absent or it held something that is not a list. Both are
    `reasons_with_no_ids`; distinguishing "absent" from "malformed" would add
    a counter nobody can act on differently.
    """
    raw = decision.get("reason")
    if not isinstance(raw, list):
        return []
    entries: list[tuple[str, list[str] | None]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        ids = item.get("evidence_ids")
        if isinstance(ids, list):
            entries.append((text, [str(value) for value in ids]))
        else:
            entries.append((text, None))
    return entries


def _citation_support(
    conn: sqlite3.Connection, run_ids: Sequence[str], decisions: Mapping[str, dict[str, object]]
) -> CitationSupport:
    evidence_rows = _evidence_by_run(conn, run_ids)

    runs_with_reasons = 0
    totals = {
        "reasons_total": 0,
        "reasons_all_ids_exist": 0,
        "reasons_with_missing_ids": 0,
        "reasons_with_no_ids": 0,
        "reasons_classified": 0,
        "reasons_unclassified": 0,
        "reasons_supported": 0,
        "reasons_unsupported": 0,
    }
    families_seen: dict[str, int] = {}

    for run_id in run_ids:
        decision = decisions.get(run_id)
        if decision is None:
            continue

        # The ids available to this run are the union of what the decision
        # carried and what the run persisted. The persisted `evidence` table
        # wins on claim_type for an id present in both: it is the durable
        # record, and a decision blob is a snapshot of it.
        claim_types: dict[str, str] = {}
        raw_evidence = decision.get("evidence")
        if isinstance(raw_evidence, list):
            for item in raw_evidence:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id")
                claim_type = item.get("claim_type")
                if item_id is not None and claim_type is not None:
                    claim_types[str(item_id)] = str(claim_type)
        for row in evidence_rows.get(run_id, ()):
            claim_types[str(row["id"])] = str(row["claim_type"])

        entries = _reason_entries(decision)
        if entries:
            runs_with_reasons += 1

        for text, ids in entries:
            totals["reasons_total"] += 1
            if not ids:
                totals["reasons_with_no_ids"] += 1
                all_exist = False
            else:
                all_exist = all(evidence_id in claim_types for evidence_id in ids)
                if all_exist:
                    totals["reasons_all_ids_exist"] += 1
                else:
                    totals["reasons_with_missing_ids"] += 1

            families = _classify_reason(text)
            if not families:
                totals["reasons_unclassified"] += 1
                continue
            totals["reasons_classified"] += 1
            for family in families:
                _bump(families_seen, family)

            accepted: set[str] = set()
            for family in families:
                accepted |= CLAIM_FAMILIES.get(family, frozenset())
            cited_types = {claim_types[i] for i in (ids or ()) if i in claim_types}
            if all_exist and (cited_types & accepted):
                totals["reasons_supported"] += 1
            else:
                totals["reasons_unsupported"] += 1

    return CitationSupport(
        runs_checked=len(run_ids),
        runs_with_reasons=runs_with_reasons,
        id_existence_rate=_ratio(totals["reasons_all_ids_exist"], totals["reasons_total"]),
        support_rate=_ratio(totals["reasons_supported"], totals["reasons_classified"]),
        families_seen=families_seen,
        **totals,
    )


def citation_support_for_runs(conn: sqlite3.Connection, run_ids: Sequence[str]) -> CitationSupport:
    """`CitationSupport` over exactly `run_ids` (one system's scoped runs, say).

    The same measurement `data_quality` makes for its System A scope, exposed
    so a report can state it per system: A and B cite the deterministic
    reasons, C cites what survived its explanation guard, R the
    deterministic reasons again.
    """
    decisions, _ = _decisions_for(conn, run_ids)
    return _citation_support(conn, run_ids, decisions)


def _decisions_for(
    conn: sqlite3.Connection, run_ids: Sequence[str]
) -> tuple[dict[str, dict[str, object]], int]:
    """`(run_id -> decoded decision, number that did not decode)`."""
    decoded: dict[str, dict[str, object]] = {}
    missing = 0
    for chunk in _chunks(run_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT id, final_decision FROM runs WHERE id IN ({placeholders})", chunk
        ).fetchall()
        for row in rows:
            decision = _decode_decision(row["final_decision"])
            if decision is None:
                missing += 1
            else:
                decoded[str(row["id"])] = decision
    return decoded, missing


def data_quality(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    splits: Mapping[str, str],
    systems: Sequence[str] = ("A",),
    allowed_splits: tuple[str, ...] = DEFAULT_SPLITS,
    case_set: MetricsCaseSet | None = None,
    match_precision_path: str | Path = DEFAULT_MATCH_PRECISION_PATH,
) -> DataQuality:
    """spec.md §6's "Data quality" figures over one dataset scope.

    Defaults to `systems=("A",)` because these are corpus properties, not
    system properties: whether a URL resolved to an ATS posting, and whether
    the archive carried a publish date, is a fact about the data, and System
    A — which runs every eligible probe — is the system that observes the
    most of it. Passing more systems widens the run scope and (harmlessly)
    counts the same posting once per system in the run-denominated rates,
    which is why `postings_checked` is reported alongside `runs_checked`.

    `cfg` is accepted for signature uniformity with the other entry points in
    this package (and so a future thresholds-driven check can be added
    without a signature change); this function reads nothing from it today.
    """
    wanted = tuple(dict.fromkeys(systems))
    resolved = _case_set_for(
        conn,
        dataset_id=dataset_id,
        splits=splits,
        systems=wanted,
        allowed_splits=allowed_splits,
        case_set=case_set,
    )

    run_ids: list[str] = []
    posting_ids: set[str] = set()
    for case in resolved.cases:
        scoped = [case.runs[system] for system in wanted if system in case.runs]
        if not scoped:
            continue
        posting_ids.add(case.posting_id)
        run_ids.extend(run.run_id for run in scoped)

    runs_checked = len(run_ids)

    # --- ATS resolution -----------------------------------------------------
    ats_resolved_runs = 0
    for chunk in _chunks(run_ids):
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT run_id) AS n
            FROM run_steps
            WHERE run_id IN ({placeholders})
                  AND component = 'probe' AND decision_type = ?
                  AND probe_name = ? AND error IS NULL
            """,
            (*chunk, STEP_PROBE_RUN, ResolvePostingProbe.name),
        ).fetchone()
        ats_resolved_runs += int(row["n"] or 0)

    ats_distribution: dict[str, int] = {}
    ordered_postings = sorted(posting_ids)
    seen_postings: set[str] = set()
    for chunk in _chunks(ordered_postings):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT posting_id, ats FROM postings WHERE posting_id IN ({placeholders})", chunk
        ).fetchall()
        for row in rows:
            seen_postings.add(str(row["posting_id"]))
            _bump(ats_distribution, str(row["ats"] or _MISSING))
    # A replay subject the collector never committed a `postings` row for is
    # counted, not dropped: it is exactly the coverage gap this block exists
    # to surface. `rli.replay` deliberately keeps no FK from `replay_cases`.
    orphans = len(posting_ids) - len(seen_postings)
    if orphans > 0:
        _bump(ats_distribution, _MISSING, orphans)

    # --- Publish-date coverage ---------------------------------------------
    publish_types = tuple(sorted(CLAIM_FAMILIES["publish"]))
    publish_runs: set[str] = set()
    by_quality: dict[str, set[str]] = {}
    first_runs: set[str] = set()
    first_by_quality: dict[str, set[str]] = {}
    refresh_runs: set[str] = set()
    refresh_by_type: dict[str, set[str]] = {}
    for chunk in _chunks(run_ids):
        run_placeholders = ",".join("?" for _ in chunk)
        type_placeholders = ",".join("?" for _ in publish_types)
        rows = conn.execute(
            f"""
            SELECT DISTINCT run_id, claim_type, source_quality,
                   source_event_at IS NOT NULL AS dated
            FROM evidence
            WHERE run_id IN ({run_placeholders})
                  AND claim_type IN ({type_placeholders})
            """,
            (*chunk, *publish_types),
        ).fetchall()
        for row in rows:
            run_id = str(row["run_id"])
            quality = str(row["source_quality"])
            claim_type = str(row["claim_type"])
            publish_runs.add(run_id)
            by_quality.setdefault(quality, set()).add(run_id)
            if claim_type == _FIRST_PUBLISHED:
                if row["dated"] and quality in _PRIMARY_PUBLISH_QUALITIES:
                    first_runs.add(run_id)
                    first_by_quality.setdefault(quality, set()).add(run_id)
            else:
                refresh_runs.add(run_id)
                refresh_by_type.setdefault(claim_type, set()).add(run_id)

    # --- Citations ----------------------------------------------------------
    decisions, _ = _decisions_for(conn, run_ids)
    citation = _citation_support(conn, run_ids, decisions)

    # --- Leakage ------------------------------------------------------------
    try:
        leakage = check_dataset(conn, dataset_id)
        leakage_counts = dict(leakage.counts)
        leakage_violations = leakage.total
        leakage_clean = leakage.clean
        leakage_model_cache_misses = leakage.model_cache_misses
        leakage_blob_input_exposures = leakage.blob_input_exposures
        leakage_uncovered_capture_batches = leakage.uncovered_capture_batches
    except Exception as exc:  # pragma: no cover - defensive
        # An audit that could not run is NOT an audit that passed. Reporting
        # `clean=True` here would be the single most dangerous default in
        # this module.
        leakage_counts = {f"leakage_check_error: {type(exc).__name__}": 1}
        leakage_violations = 1
        leakage_clean = False
        # Left at 0 because the audit did not run: `leakage_clean=False`
        # above is what says so. A 0 here is not a claim that the dataset is
        # unexposed, and no caller should read it as one.
        leakage_model_cache_misses = 0
        leakage_blob_input_exposures = 0
        leakage_uncovered_capture_batches = 0

    precision, precision_note = read_match_precision(match_precision_path)

    return DataQuality(
        dataset_id=dataset_id,
        allowed_splits=tuple(allowed_splits),
        systems=wanted,
        runs_checked=runs_checked,
        postings_checked=len(posting_ids),
        ats_resolved_runs=ats_resolved_runs,
        ats_resolution_rate=_ratio(ats_resolved_runs, runs_checked),
        ats_distribution=ats_distribution,
        publish_date_runs=len(publish_runs),
        publish_date_coverage=_ratio(len(publish_runs), runs_checked),
        first_publish_runs=len(first_runs),
        first_publish_coverage=_ratio(len(first_runs), runs_checked),
        first_publish_by_source_quality={
            quality: len(runs) for quality, runs in sorted(first_by_quality.items())
        },
        refresh_runs=len(refresh_runs),
        refresh_coverage=_ratio(len(refresh_runs), runs_checked),
        refresh_by_claim_type={
            claim_type: len(runs) for claim_type, runs in sorted(refresh_by_type.items())
        },
        publish_date_by_source_quality={
            quality: len(runs) for quality, runs in sorted(by_quality.items())
        },
        repost_match_precision=precision,
        repost_match_precision_note=precision_note,
        citation=citation,
        leakage_violations=leakage_violations,
        leakage_counts=leakage_counts,
        leakage_clean=leakage_clean,
        leakage_model_cache_misses=leakage_model_cache_misses,
        leakage_blob_input_exposures=leakage_blob_input_exposures,
        leakage_uncovered_capture_batches=leakage_uncovered_capture_batches,
    )


# ---------------------------------------------------------------------------
# 4. Agent efficiency (spec.md §6 "Agent efficiency")
# ---------------------------------------------------------------------------


class CostSplit(BaseModel):
    """Probe cost POINTS and model DOLLARS, kept apart on purpose.

    See hazard 1 in the module docstring. These two numbers share a database
    column (`run_steps.cost_usd`) and share nothing else: one is a unitless
    placeholder ordering from `[probe_costs]`, the other is real money.
    There is deliberately no `total` property on this model — the whole point
    is that no correct total exists.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_cost_points: float = 0.0
    model_cost_usd: float = 0.0
    probe_steps: int = 0
    model_steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    mean_probe_cost_points: float | None = None
    mean_model_cost_usd: float | None = None

    def describe(self) -> str:
        return "\n".join(
            [
                f"  probe cost: {self.probe_cost_points:.2f} POINTS over {self.probe_steps} "
                f"step(s) (mean {_num(self.mean_probe_cost_points)}/run) — "
                "unitless [probe_costs] placeholders, NOT dollars",
                f"  model cost: ${self.model_cost_usd:.4f} USD over {self.model_steps} "
                f"step(s) (mean ${_num(self.mean_model_cost_usd, 4)}/run) — real dollars",
                f"  tokens: in={self.input_tokens} out={self.output_tokens}",
                "  (points and dollars are never summed; runs.total_cost_usd, which does sum "
                "them, is not read)",
            ]
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


class EfficiencyMetrics(BaseModel):
    """One system's spec.md §6 "Agent efficiency" block, measured against a reference.

    Every ratio is `None` when its denominator is zero. `structural_caveat`
    travels with the numbers rather than being printed once at the top of a
    report, so a table pasted elsewhere carries its own warning.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    reference: str

    runs: int = 0
    paired_cases: int = 0

    action_distribution: dict[str, int] = {}
    reference_action_distribution: dict[str, int] = {}
    overall_agreement: float | None = None
    macro_agreement: float | None = None
    per_class_agreement: dict[str, float] = {}
    per_class_counts: dict[str, int] = {}
    confusion_matrix: dict[str, dict[str, int]] = {}

    probe_counts: dict[str, int] = {}
    probe_counts_by_tier: dict[str, int] = {}
    medium_high_probe_steps: int = 0
    mean_medium_high_probes_per_run: float | None = None

    cost: CostSplit
    total_latency_ms: float = 0.0
    mean_latency_ms: float | None = None

    repeated_calls: int = 0
    invalid_arguments: int = 0
    runs_with_failed_probe: int = 0
    recovered_runs: int = 0
    recovery_rate: float | None = None

    early_stop_regret_cases: int = 0
    early_stop_opportunities: int = 0
    early_stop_regret_rate: float | None = None

    unnecessary_probe_steps: int = 0
    unnecessary_probe_rate: float | None = None
    reference_extra_probes_no_action_change: int = 0

    decisions_missing: int = 0
    structural_caveat: str = SYSTEM_A_CAVEAT

    def describe(self) -> str:
        lines = [
            f"system {self.system} vs reference {self.reference}: "
            f"runs={self.runs} paired_cases={self.paired_cases}",
            f"  actions ({self.system}): {_render(self.action_distribution)}",
            f"  actions ({self.reference}): {_render(self.reference_action_distribution)}",
            f"  agreement: overall={_pct(self.overall_agreement)} "
            f"macro={_pct(self.macro_agreement)} "
            f"(macro classes come from {self.reference}'s actions)",
        ]
        per_class = " ".join(
            f"{action}={value:.1%}(n={self.per_class_counts.get(action, 0)})"
            for action, value in sorted(self.per_class_agreement.items())
        )
        lines.append(f"  per-class agreement: {per_class or '(none)'}")
        lines.extend(
            [
                f"  probe steps: {_render(self.probe_counts)}",
                f"  by cost tier: {_render(self.probe_counts_by_tier)}",
                f"  medium/high probe steps: {self.medium_high_probe_steps} "
                f"(mean {_num(self.mean_medium_high_probes_per_run)}/run) "
                "— spec.md §6 agent-gate numerator",
            ]
        )
        lines.append(self.cost.describe())
        lines.extend(
            [
                f"  latency: total={self.total_latency_ms:.0f} ms "
                f"mean={_num(self.mean_latency_ms, 0)} ms "
                "(summed step latency, a lower bound on wall clock)",
                f"  failure modes: repeated_calls={self.repeated_calls} "
                f"invalid_arguments={self.invalid_arguments}",
                f"  recovery: {self.recovered_runs}/{self.runs_with_failed_probe} run(s) with a "
                f"failed probe still completed = {_pct(self.recovery_rate)}",
                f"  early-stop regret: {self.early_stop_regret_cases}/"
                f"{self.early_stop_opportunities} opportunities "
                f"= {_pct(self.early_stop_regret_rate)} "
                f"(opportunity = {self.reference} ran a dynamic probe {self.system} did not)",
                f"  unnecessary probes ({self.system}, produced no evidence): "
                f"{self.unnecessary_probe_steps} step(s) = "
                f"{_pct(self.unnecessary_probe_rate)} of dynamic executions "
                "— a literal 'returned nothing' proxy, NOT 'changed no decision'",
                f"  probes {self.reference} ran that {self.system} skipped with no action "
                f"change: {self.reference_extra_probes_no_action_change}",
            ]
        )
        if self.decisions_missing:
            lines.append(f"  NOTE: {self.decisions_missing} run(s) have no parsable final_decision")
        lines.append(f"  {self.structural_caveat}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _action_distribution(runs: Iterable[CaseRun]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in runs:
        _bump(counts, run.action if run.action is not None else _MISSING)
    return counts


def _step_metrics(
    conn: sqlite3.Connection, run_ids: Sequence[str], runs: int
) -> tuple[dict[str, object], CostSplit]:
    """Everything derivable from `run_steps` for one system's scoped runs."""
    probe_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    medium_high = 0
    invalid_arguments = 0
    probe_cost_points = 0.0
    model_cost_usd = 0.0
    probe_steps = 0
    model_steps = 0
    input_tokens = 0
    output_tokens = 0
    per_run_args: dict[str, dict[tuple[str, str], int]] = {}
    runs_with_failed_probe: set[str] = set()
    dynamic_executions: list[tuple[str, str]] = []

    for chunk in _chunks(run_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT run_id, component, decision_type, probe_name, args_hash, cost_usd, error
            FROM run_steps
            WHERE run_id IN ({placeholders})
            ORDER BY run_id, step_index, id
            """,
            chunk,
        ).fetchall()
        for row in rows:
            run_id = str(row["run_id"])
            component = str(row["component"])
            decision_type = str(row["decision_type"])
            cost = float(row["cost_usd"] or 0.0)

            if component == "model":
                model_steps += 1
                model_cost_usd += cost
                tokens = parse_tokens(decision_type)
                if tokens is not None:
                    input_tokens += tokens[0]
                    output_tokens += tokens[1]
                continue

            if component == "controller":
                if decision_type == "candidate_rejected:invalid_args":
                    invalid_arguments += 1
                continue

            # component == 'probe'
            probe_steps += 1
            probe_cost_points += cost
            if row["error"] is not None:
                runs_with_failed_probe.add(run_id)
            if decision_type != STEP_PROBE_RUN:
                continue

            name = _str_or_none(row["probe_name"])
            key = name if name is not None else _UNNAMED
            _bump(probe_counts, key)
            tier = cost_tier_for(name)
            _bump(tier_counts, tier)
            if tier in ("medium", "high"):
                medium_high += 1
            if name is not None and name in DYNAMIC_PROBES:
                dynamic_executions.append((run_id, name))
            args_key = (key, str(row["args_hash"]))
            bucket = per_run_args.setdefault(run_id, {})
            bucket[args_key] = bucket.get(args_key, 0) + 1

    repeated_calls = sum(
        max(0, count - 1) for bucket in per_run_args.values() for count in bucket.values()
    )

    cost = CostSplit(
        probe_cost_points=probe_cost_points,
        model_cost_usd=model_cost_usd,
        probe_steps=probe_steps,
        model_steps=model_steps,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        mean_probe_cost_points=_ratio(probe_cost_points, runs),
        mean_model_cost_usd=_ratio(model_cost_usd, runs),
    )
    extras: dict[str, object] = {
        "probe_counts": probe_counts,
        "probe_counts_by_tier": tier_counts,
        "medium_high_probe_steps": medium_high,
        "invalid_arguments": invalid_arguments,
        "repeated_calls": repeated_calls,
        "runs_with_failed_probe": runs_with_failed_probe,
        "dynamic_executions": dynamic_executions,
    }
    return extras, cost


def _unnecessary_probe_steps(
    conn: sqlite3.Connection, dynamic_executions: Sequence[tuple[str, str]]
) -> int:
    """Dynamic executions that recorded no `evidence` row for their own probe.

    See the module docstring: this is the strict "it returned nothing"
    reading of spec.md §6's "unnecessary probes", and it is the only reading
    the trace can support. Counted per EXECUTION, not per run, so a probe run
    twice with no evidence contributes 2 (and also contributes to
    `repeated_calls`, which is the more serious finding).
    """
    if not dynamic_executions:
        return 0
    run_ids = sorted({run_id for run_id, _ in dynamic_executions})
    produced: set[tuple[str, str]] = set()
    for chunk in _chunks(run_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT DISTINCT run_id, probe FROM evidence WHERE run_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            produced.add((str(row["run_id"]), str(row["probe"])))
    return sum(1 for pair in dynamic_executions if pair not in produced)


def _completed_runs(conn: sqlite3.Connection, run_ids: Iterable[str]) -> set[str]:
    ordered = sorted(set(run_ids))
    completed: set[str] = set()
    for chunk in _chunks(ordered):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT id FROM runs WHERE id IN ({placeholders}) AND status = 'completed'", chunk
        ).fetchall()
        completed |= {str(row["id"]) for row in rows}
    return completed


def _total_latency_ms(conn: sqlite3.Connection, run_ids: Sequence[str]) -> float:
    total = 0.0
    for chunk in _chunks(run_ids):
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            f"SELECT SUM(COALESCE(total_latency_ms, 0)) AS total "
            f"FROM runs WHERE id IN ({placeholders})",
            chunk,
        ).fetchone()
        total += float(row["total"] or 0.0)
    return total


def agent_efficiency(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    splits: Mapping[str, str],
    system: str,
    reference: str = "A",
    allowed_splits: tuple[str, ...] = DEFAULT_SPLITS,
    case_set: MetricsCaseSet | None = None,
) -> EfficiencyMetrics:
    """spec.md §6's "Agent efficiency" block for `system`, compared against `reference`.

    Agreement, early-stop regret and the reference-extra-probe counterpart
    are computed over the cases where BOTH systems produced a surviving run;
    every other figure (probe counts, cost, latency, failure modes) is scoped
    to `system`'s own run ids, paired or not, exactly as `rli.eval.baseline`
    scopes its per-system block. `system == reference` is legal and yields a
    trivially perfect agreement — that is how the report gets the reference's
    own cost and probe figures without a second code path.

    `cfg` is accepted for signature uniformity across this package; the cost
    tiers come from `rli.eval.report.cost_tier_for` (built from the probe
    classes) rather than from `cfg.probe_costs`, so there is one source of
    truth for what "medium/high" means.
    """
    wanted = tuple(dict.fromkeys((system, reference)))
    resolved = _case_set_for(
        conn,
        dataset_id=dataset_id,
        splits=splits,
        systems=wanted,
        allowed_splits=allowed_splits,
        case_set=case_set,
    )

    run_ids = tuple(resolved.run_ids.get(system, ()))
    runs = len(run_ids)
    system_runs = [case.runs[system] for case in resolved.cases if system in case.runs]
    reference_runs = [case.runs[reference] for case in resolved.cases if reference in case.runs]

    paired = resolved.cases_for(system, reference)

    # --- Agreement (definitions mirror rli.eval.baseline._compare) -----------
    overall_agreement: float | None = None
    per_class_agreement: dict[str, float] = {}
    per_class_counts: dict[str, int] = {}
    confusion: dict[str, dict[str, int]] = {}
    if paired:
        matches = 0
        for case in paired:
            ref_action = case.runs[reference].action
            sys_action = case.runs[system].action
            if ref_action is not None and ref_action == sys_action:
                matches += 1
            ref_key = ref_action if ref_action is not None else _MISSING
            sys_key = sys_action if sys_action is not None else _MISSING
            confusion.setdefault(ref_key, {})
            _bump(confusion[ref_key], sys_key)
        overall_agreement = matches / len(paired)

        classes = sorted(
            {
                case.runs[reference].action
                for case in paired
                if case.runs[reference].action is not None
            }
        )
        for action in classes:
            in_class = [case for case in paired if case.runs[reference].action == action]
            hits = sum(1 for case in in_class if case.runs[system].action == action)
            per_class_counts[action] = len(in_class)
            per_class_agreement[action] = hits / len(in_class)
    macro_agreement = (
        sum(per_class_agreement.values()) / len(per_class_agreement)
        if per_class_agreement
        else None
    )

    # --- Early-stop regret and its counterpart ------------------------------
    opportunities = 0
    regret_cases = 0
    reference_extra = 0
    for case in paired:
        sys_run = case.runs[system]
        ref_run = case.runs[reference]
        extra = ref_run.dynamic_probes() - sys_run.dynamic_probes()
        actions_agree = sys_run.action == ref_run.action
        if extra:
            opportunities += 1
            if not actions_agree:
                regret_cases += 1
        if actions_agree:
            reference_extra += len(extra)

    # --- Steps, cost, failure modes -----------------------------------------
    extras, cost = _step_metrics(conn, run_ids, runs)
    dynamic_executions: list[tuple[str, str]] = extras["dynamic_executions"]  # type: ignore[assignment]
    failed_probe_runs: set[str] = extras["runs_with_failed_probe"]  # type: ignore[assignment]
    recovered = failed_probe_runs & _completed_runs(conn, failed_probe_runs)
    unnecessary = _unnecessary_probe_steps(conn, dynamic_executions)

    _, decisions_missing = _decisions_for(conn, run_ids)
    medium_high: int = extras["medium_high_probe_steps"]  # type: ignore[assignment]
    total_latency_ms = _total_latency_ms(conn, run_ids)

    return EfficiencyMetrics(
        system=system,
        reference=reference,
        runs=runs,
        paired_cases=len(paired),
        action_distribution=_action_distribution(system_runs),
        reference_action_distribution=_action_distribution(reference_runs),
        overall_agreement=overall_agreement,
        macro_agreement=macro_agreement,
        per_class_agreement=per_class_agreement,
        per_class_counts=per_class_counts,
        confusion_matrix=confusion,
        probe_counts=extras["probe_counts"],  # type: ignore[arg-type]
        probe_counts_by_tier=extras["probe_counts_by_tier"],  # type: ignore[arg-type]
        medium_high_probe_steps=medium_high,
        mean_medium_high_probes_per_run=_ratio(medium_high, runs),
        cost=cost,
        total_latency_ms=total_latency_ms,
        mean_latency_ms=_ratio(total_latency_ms, runs),
        repeated_calls=extras["repeated_calls"],  # type: ignore[arg-type]
        invalid_arguments=extras["invalid_arguments"],  # type: ignore[arg-type]
        runs_with_failed_probe=len(failed_probe_runs),
        recovered_runs=len(recovered),
        recovery_rate=_ratio(len(recovered), len(failed_probe_runs)),
        early_stop_regret_cases=regret_cases,
        early_stop_opportunities=opportunities,
        early_stop_regret_rate=_ratio(regret_cases, opportunities),
        unnecessary_probe_steps=unnecessary,
        unnecessary_probe_rate=_ratio(unnecessary, len(dynamic_executions)),
        reference_extra_probes_no_action_change=reference_extra,
        decisions_missing=decisions_missing,
    )
