"""C2 — an OPTIONAL learned probe ranker (spec.md §4 "Probe ranking lifecycle"; PLAN.md M6).

spec.md §4 fixes the controller's v1 ranking as "deterministic cost-aware
ranking over valid candidates" and then offers an "Optional upgrade":

    "after enough replay data exists, train a small calibrated estimator on
    (case_state, candidate_probe) rows to predict whether that probe would
    move the partial-information action toward the full-probe reference
    action. Start with logistic regression ... Combine predicted value with
    measured money cost, latency, and failure rate. If learned ranking does
    not beat deterministic ranking, remove it."

This module is that upgrade path, and ONLY that path: it never runs a probe,
never calls the LLM, and never touches `rli.agent.controller`'s live ranking
(`score_candidate` / `rank_candidates` there are untouched — wiring a kept
model back into the controller is a follow-up this task does not attempt).
Everything here is a read of the replay trace already on disk, producing a
`RankerResult` that says whether a learned ranker would be worth keeping —
and, per spec.md §4's last sentence, `keep=False` is the CORRECT and
EXPECTED answer whenever the learned model does not clear the deterministic
baseline by a real margin, not a bug to chase away.

---------------------------------------------------------------------------
What is being learned, and from what
---------------------------------------------------------------------------

The estimator's job is exactly the sentence above: given the case state
available right before a dynamic probe `p` would run, predict whether
running `p` would have moved the acting system's partial-information
decision toward the reference system's full-probe decision. This module
never observes that counterfactual directly (nothing here re-runs a case
with and without one probe) — it approximates it from the traces that
already exist, by cross-system comparison. See "The label is a PROXY, not a
measurement" below for exactly what that approximation buys and costs.

One `TrainingRow` is built per `(paired replay case, dynamic probe p that
the REFERENCE system executed at that case)`. "Reference" defaults to `"A"`
(System A — Full probes, spec.md §6's reference system), because A is the
only system that (by construction, since its unresolved-question gate is
neutralised — see `rli.eval.system_a.ALL_DYNAMIC_INPUTS`) reliably executes
every dynamic probe a case has evidence for, giving the widest possible set
of `(case, probe)` pairs to learn from. A case where the reference did not
run any dynamic probe contributes no rows.

**Features** (all floats; every one is computed ONLY from information that
was available strictly BEFORE `p` ran in the reference's own run, so the
row can never leak the outcome of the probe it is scoring):

* `n_evidence`, and its breakdown by `evidence.source_quality` —
  `n_evidence_ats_native`, `n_evidence_page_structured`, `n_evidence_archive`,
  `n_evidence_news`, `n_evidence_enrichment` — the volume and provenance mix
  of everything already known.
* `has_publish_evidence` (an `evidence.claim_type` of `first_published` or
  `updated_at` has been seen), `has_board_present`, `has_board_absent` (the
  `board_snapshot` claims of the same names, see `rli.eval.case`) — coarse
  flags for the policy inputs spec.md §5 cares about most.
* `n_probes_before` — how many probes (dynamic or the always-run pair) the
  reference had already executed when `p` ran. This is `p`'s 0-based
  position in the reference's `probes_run` tuple, i.e. it does not need a
  separate query: `CaseRun.probes_run` is documented (see
  `rli.eval.metrics`) to list dynamic + always-run probe names in the order
  they actually executed.
* `probe_cost_points` — `p`'s numeric cost from `[probe_costs]`, read via
  `rli.eval.report.cost_tier_for` (the project's one source of truth for
  probe cost tiers — never restated here) and `cfg.probe_costs.value_for`.
  This is the SAME number the deterministic baseline (below) ranks on, so
  the learned model has to do strictly better than "know the price".
* `probe_is_<name>` for every `name` in `sorted(rli.probes.registry.
  DYNAMIC_PROBES)` — a one-hot identifying which probe this row scores,
  since nothing above says `p`'s own identity.

"Before `p`" is read off `probes_run` itself, not off timestamps: the
`evidence` rows counted for a row are exactly those whose `probe` is in
`probes_run[:probes_run.index(p)]` (the always-run pair are always in that
prefix, since the controller runs them before any dynamic probe). Rows are
looked up once per run and reused across every probe scored from that run
(`build_training_rows` caches the query keyed on `run_id`), so building the
full row set costs one `evidence` query per reference run, not one per row.

---------------------------------------------------------------------------
The label is a PROXY, not a measurement — this is the central judgment call
---------------------------------------------------------------------------

There is no ground truth "running `p` changed the action" signal in the
trace: a single run only shows what DID happen, never the counterfactual of
what would have happened without `p`. What the traces DO contain is other
systems that ran the SAME case with a DIFFERENT probe budget. This module
treats that as the closest available approximation to the counterfactual:

    label = 1  iff
        (a) at least one OTHER system ran this case, did NOT run `p`, and
            reached a DIFFERENT `recommended_action` than the reference's,
        AND
        (b) EVERY system that DID run `p` at this case (the reference
            included, trivially) reached the SAME action as the reference.

    label = 0 otherwise (including when the reference's own action is
    unknown — `None` `final_decision` — in which case no comparison is
    possible and the row cannot support the counterfactual either way).

Read in words: "some cheaper path landed somewhere else, and every path
that paid for `p` landed where the reference did" is the best trace-only
evidence this project has that `p` was the ingredient that mattered. It is
an imperfect proxy for at least three reasons, all worth stating plainly
rather than discovering later:

1. **Confounding.** Two systems can disagree for reasons that have nothing
   to do with `p` — a different probe entirely, a different LLM sampling
   outcome (System C), or plain policy-threshold sensitivity near a
   boundary. The label cannot distinguish "`p` was decisive" from "`p`
   happened to be the one thing this cheaper system skipped, among several
   it was missing".
2. **One-sided evidence.** The label only ever fires positive when a
   DISAGREEING cheaper run exists. A probe that is uniformly useless (every
   system agrees regardless) or uniformly useful-but-never-tested-without
   (every system that ever runs the case also runs `p`) is invisible to it
   and defaults to 0 — "not shown to help" rather than "shown not to help".
   This is a conservative bias, not a neutral one: the learned model is
   trained to be skeptical by default.
3. **System-count sparsity.** With three systems (A/B/C, and only A/B live
   today — no LLM endpoint was reachable for C; see the HARD RULES in the
   contract this module was built against), the "other system" in (a) is
   most often just B. A single comparison system is a thin base for a
   causal claim; this is exactly why `min_rows` exists and why the module
   never claims the learned ranker is calibrated, only that it either beats
   the deterministic baseline on held-out data or it does not.

None of this makes the exercise pointless — spec.md §4 explicitly asks for
"a small calibrated estimator" as an OPTIONAL upgrade to be judged on
held-out performance, not for a proof of causality — but it is why
`should_keep` defaults to a real margin (`min_auc_gain`, not `> 0`) rather
than any positive gain at all, and why a `keep=False` verdict on today's
data is treated as the expected, correct outcome throughout this module
and its docstring, not a defect to explain away.

---------------------------------------------------------------------------
The deterministic comparison baseline
---------------------------------------------------------------------------

spec.md §4's v1 ranking is "deterministic cost-aware ranking": among
eligible candidates, `rli.agent.controller.score_candidate` computes
`score = value / cost` where `value` is currently a CONSTANT for every
probe (see that module's docstring), so `rank_candidates`'s
`(-score, probe name)` sort degenerates, today, to exactly "cheapest first,
name breaking ties". The comparison score this module uses for that same
ordering is `-probe_cost_points` (higher score = cheaper = ranked first),
scored with `sklearn.metrics.roc_auc_score` against the same label the
learned model is judged against, and thresholded at ITS OWN holdout median
for the accuracy figure (a threshold-free ranking has no natural
"positive/negative" cutpoint; the median makes the comparison as favourable
to the deterministic baseline as a parameter-free choice can be, so a
learned-model win is not an artifact of an unfair baseline threshold). If a
future v2 ranking model ever makes `value` non-constant, this baseline
should be recomputed from the controller's actual `score_candidate` output
rather than re-deriving cost alone — that is out of this task's scope.

---------------------------------------------------------------------------
The temporal split
---------------------------------------------------------------------------

Rows are sorted by `replay_at` (an ISO-8601 UTC string, so lexical order IS
chronological order — the same convention `rli/db/schema.sql` documents for
every timestamp column) and the LATEST `config.test_fraction` share becomes
the holdout; the remainder trains. This is a temporal split, not a random
one, because the question this module answers — "would a model trained on
what we knew so far have generalized to what came next" — is the same
forward-only question spec.md §6's dev/validation/test splits ask about the
action policy itself; validating a probe-ranking model on rows shuffled
across time would let it "learn" from cases that, on the real clock, had
not happened yet. `holdout_rows = max(1, round(len(rows) * test_fraction))`
(rounded, floored to at least one row so a tiny-but-above-`min_rows` sample
still gets a nonempty holdout; capped at `len(rows) - 1` so training data is
never empty either).

---------------------------------------------------------------------------
Every other judgment call
---------------------------------------------------------------------------

* **`build_training_rows` is deterministic and NEVER imports or calls
  scikit-learn.** It is a pure reader: same database, same dataset, same
  splits in -> the same list of rows out, every time. Training happens only
  inside `train_ranker`, and only past the `min_rows` gate below.
* **Below `min_rows`, `train_ranker` returns `status="insufficient_data"`
  WITHOUT importing scikit-learn at all** (the import lives inside the
  `try` block that only runs once both the row-count and split-diversity
  gates pass) — a small dataset should fail fast and cheaply, not pay for
  an import that is about to be thrown away.
* **A split with a single label class is `status="degenerate"`, not a
  crash.** `roc_auc_score` (and, in practice, `LogisticRegression.fit`) is
  undefined when the training or holdout labels are all-0 or all-1 — every
  candidate probe in that slice either "always looked good" or "never did",
  which the temporal split can produce by bad luck even above `min_rows`
  when the dataset is small or heavily one-sided. This is reported, not
  raised, exactly like every other "undefined ratio" in this codebase.
* **Any other failure inside the `sklearn` fit/score step also degrades to
  `status="degenerate"` with the exception text in `note`**, rather than
  propagating. Stored replay data can be corrupt or degenerate in ways this
  module has not enumerated (e.g. a feature that is constant across the
  entire training slice, which some solvers reject); an optional upgrade
  that crashes the evaluation report is worse than one that reports "could
  not be trained on this data" and moves on.
* **`should_keep` requires BOTH `auc_gain >= config.min_auc_gain` AND
  `accuracy_gain >= config.min_accuracy_gain`**, matching spec.md §4's "if
  learned ranking does not beat deterministic ranking, remove it" —
  "beat" is read as "beat by a real margin on both the ranking metric AND
  the classification metric", not "beat by any positive amount on either
  one", so that noise in a small holdout cannot flip the verdict.
* **`RankerConfig.load` reads `[ranker]` directly with `tomllib`, never
  through `rli.config.load_config`.** `rli.config.Config` has
  `extra="forbid"`; today's `config.toml` (and `rli.config.Config`) has no
  `ranker` table or field, so a `load_config()` call that saw one would
  reject the entire file. Wiring `[ranker]` into `config.toml` for real
  needs exactly one additive line on `rli.config.Config` —
  `ranker: Ranker = Field(default_factory=Ranker)` (naming a `Ranker` model
  there, distinct from this module's `RankerConfig`, to keep `rli.config`'s
  own naming conventions) — which is outside this task's permitted edit
  set (`rli/config.py` is explicitly off limits). Until that line lands,
  `RankerConfig.load` resolves its own path the same way
  `rli.config._resolve_config_path` does (explicit argument > `$RLI_CONFIG`
  > `rli.config.REPO_ROOT / "config.toml"`) and reads the `[ranker]` table
  out of the raw TOML directly. A missing file or a missing `[ranker]`
  table both silently fall back to this module's documented defaults
  (`RankerConfig()`) rather than raising — this module has no "fail loudly"
  obligation for a table that, today, nothing requires to exist. An
  existing `[ranker]` table with an invalid value (wrong type, out of
  range) DOES raise (`pydantic.ValidationError`), matching
  `rli.config.load_config`'s "fail loudly" philosophy for a table a config
  author actually wrote.
* **Every numeric default below is an unmeasured PLACEHOLDER**, exactly
  like `rli.config.Agent`'s ranking-cost-model knobs: `min_rows=200` is a
  round number comfortably above the smallest sample scikit-learn's
  `LogisticRegression` can fit without warnings, not a power calculation;
  `test_fraction=0.25`, `min_auc_gain=0.05` and `min_accuracy_gain=0.0`
  encode "a quarter of the data as holdout" and "five AUC points is a real
  improvement, not noise" as reasonable starting points, pending real
  tuning once enough replay data exists to tune them against.
"""

from __future__ import annotations

import os
import sqlite3
import statistics
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rli.config import CONFIG_ENV_VAR, REPO_ROOT, Config
from rli.eval.metrics import DEFAULT_SPLITS, MetricsCase, MetricsCaseSet, collect_system_runs
from rli.eval.report import cost_tier_for
from rli.probes.registry import DYNAMIC_PROBES

__all__ = [
    "DEFAULT_RANKER_CONFIG_SECTION",
    "RankerConfig",
    "RankerResult",
    "TrainingRow",
    "build_training_rows",
    "evaluate_ranker",
    "should_keep",
    "train_ranker",
]

#: `[ranker]` — the TOML table `RankerConfig.load` reads. See the module
#: docstring's "RankerConfig.load" judgment call for why it is read
#: directly rather than through `rli.config.load_config`.
DEFAULT_RANKER_CONFIG_SECTION = "ranker"

#: Evidence-quality buckets, exactly `evidence.source_quality`'s CHECK
#: constraint in `rli/db/schema.sql` — kept as a tuple (not re-derived from
#: the schema, which this module does not import) so a feature is emitted
#: for every possible value even when a given case never saw one of them.
_SOURCE_QUALITIES: tuple[str, ...] = (
    "ats_native",
    "page_structured",
    "archive",
    "news",
    "enrichment",
)

#: `evidence.claim_type` values that count as "we have seen a publish-date
#: claim" (`rli.probes.resolve_posting` emits both). Kept narrow and local
#: rather than importing `rli.eval.metrics.CLAIM_FAMILIES`'s broader
#: `"publish"` family, since this feature only needs these two literal
#: values and importing the family would couple this module's feature
#: shape to a data-quality taxonomy that may grow independently of it.
_PUBLISH_CLAIM_TYPES: frozenset[str] = frozenset({"first_published", "updated_at"})

#: `board_snapshot`'s two claim types (`rli.eval.case.CLAIM_BOARD_PRESENT` /
#: `rli.policy.inputs.CLAIM_BOARD_ABSENT`), restated as literals for the
#: same reason as `_PUBLISH_CLAIM_TYPES` above.
_CLAIM_BOARD_PRESENT = "board_present"
_CLAIM_BOARD_ABSENT = "board_absent"

#: The non-one-hot feature names every `TrainingRow.features` dict carries.
#: The full feature set (`feature_names` on a trained `RankerResult`) is
#: this tuple plus one `probe_is_<name>` entry per `sorted(DYNAMIC_PROBES)`
#: name, computed at row-build time in `_features_for` below.
_BASE_FEATURE_NAMES: tuple[str, ...] = (
    "n_evidence",
    *(f"n_evidence_{quality}" for quality in _SOURCE_QUALITIES),
    "has_publish_evidence",
    "has_board_present",
    "has_board_absent",
    "n_probes_before",
    "probe_cost_points",
)


class RankerConfig(BaseModel):
    """`[ranker]` — knobs for the optional C2 learned probe ranker (spec.md §4).

    Every value here is an unmeasured PLACEHOLDER; see the module
    docstring's last judgment call for what each one encodes and why.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Below this many `TrainingRow`s, `train_ranker` refuses to train at
    #: all (`status="insufficient_data"`) rather than fit a model on a
    #: sample too small to trust.
    min_rows: int = Field(200, gt=0)
    #: Temporal holdout share (the latest `replay_at`-sorted slice). See the
    #: module docstring's "The temporal split" section.
    test_fraction: float = Field(0.25, gt=0.0, lt=1.0)
    #: The learned model must beat the deterministic baseline's holdout AUC
    #: by at least this much to be kept (spec.md §4: "if learned ranking
    #: does not beat deterministic ranking, remove it").
    min_auc_gain: float = Field(0.05, ge=0.0)
    #: ... and by at least this much holdout accuracy, too. Zero by default
    #: (AUC is the primary ranking metric; accuracy is a secondary check),
    #: but not skipped — see `should_keep`.
    min_accuracy_gain: float = Field(0.0, ge=0.0)
    #: `LogisticRegression(random_state=...)` — fixed so a re-run of the
    #: same rows reproduces the same `RankerResult` bit-for-bit.
    seed: int = 20260607
    #: `LogisticRegression(max_iter=...)`.
    max_iter: int = Field(1000, gt=0)
    #: `LogisticRegression(C=...)` — inverse regularisation strength.
    c: float = Field(1.0, gt=0.0)

    @classmethod
    def load(cls, path: str | Path | None = None) -> RankerConfig:
        """Read `[ranker]` from `config.toml`, defaulting when it is absent.

        Resolution order for the TOML file itself mirrors
        `rli.config.load_config`'s: an explicit `path` argument, then
        `$RLI_CONFIG`, then `rli.config.REPO_ROOT / "config.toml"`. Unlike
        `load_config`, this never raises for a missing file or a missing
        `[ranker]` table — both fall back to `RankerConfig()`'s documented
        defaults, per the module docstring's judgment call. A `[ranker]`
        table that IS present but invalid still raises
        `pydantic.ValidationError`, matching `rli.config`'s "fail loudly"
        convention for a table someone actually wrote.
        """
        if path is not None:
            resolved = Path(path).expanduser()
        else:
            from_env = os.environ.get(CONFIG_ENV_VAR)
            resolved = Path(from_env).expanduser() if from_env else REPO_ROOT / "config.toml"

        if not resolved.is_file():
            return cls()

        with resolved.open("rb") as handle:
            raw = tomllib.load(handle)

        table = raw.get(DEFAULT_RANKER_CONFIG_SECTION)
        if not isinstance(table, dict):
            return cls()
        return cls.model_validate(table)


class TrainingRow(BaseModel):
    """One `(replay case, candidate dynamic probe)` observation.

    See the module docstring for exactly how `features` and `label` are
    derived. `posting_id` / `replay_at` are carried along only so
    `train_ranker` can sort rows temporally; they are never features.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    posting_id: str
    replay_at: str
    probe: str
    features: dict[str, float] = {}
    label: int = 0


class RankerResult(BaseModel):
    """The verdict on training a probe ranker from a set of `TrainingRow`s.

    `keep` is `False` unless `status == "trained"` and the learned model
    beats the deterministic comparison baseline by at least
    `RankerConfig.min_auc_gain` / `min_accuracy_gain` (see `should_keep`).
    Every ratio/metric is `None`, never `0.0`, when it is not defined for
    this result's `status` — the same convention every other result model
    in `rli.eval` uses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["insufficient_data", "degenerate", "trained"]
    rows: int = 0
    train_rows: int = 0
    holdout_rows: int = 0
    positive_rate: float | None = None
    holdout_positive_rate: float | None = None
    feature_names: tuple[str, ...] = ()
    learned_auc: float | None = None
    learned_accuracy: float | None = None
    deterministic_auc: float | None = None
    deterministic_accuracy: float | None = None
    auc_gain: float | None = None
    accuracy_gain: float | None = None
    keep: bool = False
    note: str = ""

    def describe(self) -> str:
        lines = [
            f"C2 learned probe ranker: status={self.status} rows={self.rows} "
            f"train={self.train_rows} holdout={self.holdout_rows}",
        ]
        if self.positive_rate is not None:
            lines.append(
                f"  positive_rate={self.positive_rate:.1%} "
                f"holdout_positive_rate={_pct(self.holdout_positive_rate)}"
            )
        if self.status == "trained":
            lines.append(
                f"  learned:      auc={_fmt(self.learned_auc)} "
                f"accuracy={_fmt(self.learned_accuracy)}"
            )
            lines.append(
                f"  deterministic: auc={_fmt(self.deterministic_auc)} "
                f"accuracy={_fmt(self.deterministic_accuracy)}"
            )
            lines.append(
                f"  gain: auc={_fmt(self.auc_gain)} accuracy={_fmt(self.accuracy_gain)}"
            )
        lines.append(f"  keep={self.keep}" + (f" — {self.note}" if self.note else ""))
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _mean(values: Sequence[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _dynamic_probes_in_order(probes_run: Sequence[str]) -> list[str]:
    """Distinct entries of `probes_run` that are dynamic probes, first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for name in probes_run:
        if name in DYNAMIC_PROBES and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _features_for(
    evidence_rows: Sequence[sqlite3.Row], *, probe: str, n_probes_before: int, cfg: Config
) -> dict[str, float]:
    quality_counts = dict.fromkeys(_SOURCE_QUALITIES, 0)
    claim_types: set[str] = set()
    for row in evidence_rows:
        quality = row["source_quality"]
        if quality in quality_counts:
            quality_counts[quality] += 1
        claim_types.add(row["claim_type"])

    features: dict[str, float] = {
        "n_evidence": float(len(evidence_rows)),
        **{
            f"n_evidence_{quality}": float(count) for quality, count in quality_counts.items()
        },
        "has_publish_evidence": 1.0 if claim_types & _PUBLISH_CLAIM_TYPES else 0.0,
        "has_board_present": 1.0 if _CLAIM_BOARD_PRESENT in claim_types else 0.0,
        "has_board_absent": 1.0 if _CLAIM_BOARD_ABSENT in claim_types else 0.0,
        "n_probes_before": float(n_probes_before),
        "probe_cost_points": float(cfg.probe_costs.value_for(cost_tier_for(probe))),
    }
    for name in sorted(DYNAMIC_PROBES):
        features[f"probe_is_{name}"] = 1.0 if name == probe else 0.0
    return features


def _label_for(case: MetricsCase, reference: str, probe: str) -> int:
    """1 iff the counterfactual the module docstring describes is satisfied."""
    ref_run = case.runs[reference]
    ref_action = ref_run.action
    if ref_action is None:
        return 0

    disagreeing_cheaper_run = False
    for system, run in case.runs.items():
        if system == reference or probe in run.probes_run or run.action is None:
            continue
        if run.action != ref_action:
            disagreeing_cheaper_run = True
            break
    if not disagreeing_cheaper_run:
        return 0

    for system, run in case.runs.items():
        if probe not in run.probes_run or run.action is None:
            continue
        if run.action != ref_action:
            return 0
    return 1


def build_training_rows(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    splits: Mapping[str, str],
    allowed_splits: tuple[str, ...] = DEFAULT_SPLITS,
    reference: str = "A",
    systems: Sequence[str] = ("A", "B", "C"),
    case_set: MetricsCaseSet | None = None,
) -> list[TrainingRow]:
    """Build every `(case, probe)` training row for `dataset_id` (deterministic, never trains).

    One row per dynamic probe the REFERENCE system executed at each replay
    case in `case_set` (built via `rli.eval.metrics.collect_system_runs`
    when not supplied). A case where the reference produced no run, or ran
    no dynamic probe, contributes no rows. See the module docstring for the
    exact feature and label construction. This function never imports or
    calls scikit-learn and never trains anything — it is a pure reader.
    """
    if case_set is None:
        case_set = collect_system_runs(
            conn,
            dataset_id=dataset_id,
            splits=splits,
            systems=systems,
            allowed_splits=allowed_splits,
        )

    rows: list[TrainingRow] = []
    evidence_cache: dict[str, list[sqlite3.Row]] = {}

    for case in case_set.cases:
        ref_run = case.runs.get(reference)
        if ref_run is None:
            continue
        probes_run = ref_run.probes_run
        ordered_dynamic = _dynamic_probes_in_order(probes_run)
        if not ordered_dynamic:
            continue

        if ref_run.run_id not in evidence_cache:
            evidence_cache[ref_run.run_id] = conn.execute(
                "SELECT probe, claim_type, source_quality FROM evidence WHERE run_id = ?",
                (ref_run.run_id,),
            ).fetchall()
        evidence_rows = evidence_cache[ref_run.run_id]

        for probe in ordered_dynamic:
            index = probes_run.index(probe)
            earlier_probes = set(probes_run[:index])
            filtered = [row for row in evidence_rows if row["probe"] in earlier_probes]
            features = _features_for(filtered, probe=probe, n_probes_before=index, cfg=cfg)
            rows.append(
                TrainingRow(
                    posting_id=case.posting_id,
                    replay_at=case.replay_at,
                    probe=probe,
                    features=features,
                    label=_label_for(case, reference, probe),
                )
            )

    return rows


def should_keep(result: RankerResult, config: RankerConfig | None = None) -> bool:
    """`True` iff the learned model beats the deterministic baseline by a real margin.

    spec.md §4: "If learned ranking does not beat deterministic ranking,
    remove it." Requires `status == "trained"` AND both `auc_gain >=
    config.min_auc_gain` AND `accuracy_gain >= config.min_accuracy_gain` —
    see the module docstring's judgment call on why both gates apply.
    """
    config = config or RankerConfig()
    if result.status != "trained":
        return False
    if result.auc_gain is None or result.accuracy_gain is None:
        return False
    return (
        result.auc_gain >= config.min_auc_gain
        and result.accuracy_gain >= config.min_accuracy_gain
    )


def _finalize(result: RankerResult, config: RankerConfig) -> RankerResult:
    return result.model_copy(update={"keep": should_keep(result, config)})


def _temporal_split(
    rows: Sequence[TrainingRow], test_fraction: float
) -> tuple[list[TrainingRow], list[TrainingRow]]:
    """Sort `rows` by `replay_at` and split off the latest `test_fraction` share.

    `replay_at` is an ISO-8601 UTC string, so lexical sort order IS
    chronological order (see the module docstring's "The temporal split").
    The holdout size is `max(1, round(len(rows) * test_fraction))`, capped
    at `len(rows) - 1` so training data is never empty. Exposed privately
    (rather than inlined in `train_ranker`) so the split itself — "is the
    holdout really the latest share, with the documented boundary size" —
    can be tested in isolation from model fitting.
    """
    rows_sorted = sorted(rows, key=lambda row: row.replay_at)
    n = len(rows_sorted)
    holdout_n = max(1, round(n * test_fraction))
    holdout_n = min(holdout_n, n - 1) if n > 1 else 0
    return rows_sorted[: n - holdout_n], rows_sorted[n - holdout_n :]


def train_ranker(
    rows: Sequence[TrainingRow], config: RankerConfig | None = None
) -> RankerResult:
    """Temporally split `rows`, train a logistic-regression ranker, and score it.

    Returns `status="insufficient_data"` (no scikit-learn import at all)
    below `config.min_rows`; `status="degenerate"` when the train or
    holdout split has a single label class (or when fitting/scoring raises
    for any other reason — see the module docstring); `status="trained"`
    otherwise, with `keep` set by `should_keep`. See the module docstring
    for the exact temporal split and the deterministic comparison baseline.
    """
    config = config or RankerConfig()

    n = len(rows)
    all_labels = [row.label for row in rows]
    feature_names = tuple(sorted({name for row in rows for name in row.features}))
    positive_rate = _mean(all_labels)

    if n < config.min_rows:
        return RankerResult(
            status="insufficient_data",
            rows=n,
            positive_rate=positive_rate,
            feature_names=feature_names,
            note=f"only {n} row(s); need >= {config.min_rows} to train",
        )

    train_rows, holdout_rows = _temporal_split(rows, config.test_fraction)

    train_labels = [row.label for row in train_rows]
    holdout_labels = [row.label for row in holdout_rows]
    holdout_positive_rate = _mean(holdout_labels)

    base_result = RankerResult(
        status="degenerate",
        rows=n,
        train_rows=len(train_rows),
        holdout_rows=len(holdout_rows),
        positive_rate=positive_rate,
        holdout_positive_rate=holdout_positive_rate,
        feature_names=feature_names,
    )

    if len(set(train_labels)) < 2 or len(set(holdout_labels)) < 2:
        return _finalize(
            base_result.model_copy(
                update={
                    "note": (
                        "train or holdout labels are a single class; "
                        "AUC is undefined for this split"
                    )
                }
            ),
            config,
        )

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import accuracy_score, roc_auc_score

        x_train = [[row.features.get(name, 0.0) for name in feature_names] for row in train_rows]
        x_holdout = [
            [row.features.get(name, 0.0) for name in feature_names] for row in holdout_rows
        ]

        model = LogisticRegression(max_iter=config.max_iter, C=config.c, random_state=config.seed)
        model.fit(x_train, train_labels)

        learned_scores = [proba[1] for proba in model.predict_proba(x_holdout)]
        learned_auc = float(roc_auc_score(holdout_labels, learned_scores))
        learned_preds = model.predict(x_holdout)
        learned_accuracy = float(accuracy_score(holdout_labels, learned_preds))

        det_scores = [-row.features.get("probe_cost_points", 0.0) for row in holdout_rows]
        deterministic_auc = float(roc_auc_score(holdout_labels, det_scores))
        threshold = statistics.median(det_scores)
        det_preds = [1 if score >= threshold else 0 for score in det_scores]
        deterministic_accuracy = float(accuracy_score(holdout_labels, det_preds))
    except Exception as exc:  # noqa: BLE001 - degrade, never raise (see module docstring)
        return _finalize(
            base_result.model_copy(update={"note": f"training failed: {exc!r}"}),
            config,
        )

    trained = base_result.model_copy(
        update={
            "status": "trained",
            "learned_auc": learned_auc,
            "learned_accuracy": learned_accuracy,
            "deterministic_auc": deterministic_auc,
            "deterministic_accuracy": deterministic_accuracy,
            "auc_gain": learned_auc - deterministic_auc,
            "accuracy_gain": learned_accuracy - deterministic_accuracy,
            "note": (
                f"trained on {len(train_rows)} row(s), scored on {len(holdout_rows)} "
                "holdout row(s) (latest by replay_at)"
            ),
        }
    )
    return _finalize(trained, config)


def evaluate_ranker(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    splits: Mapping[str, str],
    allowed_splits: tuple[str, ...] = DEFAULT_SPLITS,
    config: RankerConfig | None = None,
    case_set: MetricsCaseSet | None = None,
    reference: str = "A",
) -> RankerResult:
    """`build_training_rows` + `train_ranker` in one call — the `evaluate.py` entry point.

    `config` defaults to `RankerConfig.load()` (reading `[ranker]` from
    `config.toml` if present) rather than to `RankerConfig()`'s bare
    defaults, since this is the wired-up entry point a caller with no
    opinion on ranker knobs should get the project's configured values
    from, exactly as `rli.eval.evaluate.evaluate` will call it.
    """
    config = config or RankerConfig.load()
    rows = build_training_rows(
        conn,
        cfg,
        dataset_id=dataset_id,
        splits=splits,
        allowed_splits=allowed_splits,
        reference=reference,
        case_set=case_set,
    )
    return train_ranker(rows, config)
