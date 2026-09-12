"""The future-leakage checker (spec.md §6 "Metrics"; PLAN.md M4).

spec.md §6 lists, under **Data quality**, "future-leakage violations (`0`
target)". This module is how that number is produced: it audits the canonical
trace (spec.md §7 — `runs` + `run_steps` + `evidence`) of a replay dataset and
reports every way a replayed run could have seen something it must not have.

It is a pure READER. It runs no probe, opens no connection of its own, and
writes nothing. That matters: a checker that could modify state could mask
the thing it is checking for, and this is the one component whose output is
allowed to gate a milestone.

--------------------------------------------------------------------------
The five checks, and why each is the right question
--------------------------------------------------------------------------

* **`evidence_after_t`** — an `evidence` row of a replay run whose
  `available_at` is strictly after that run's `replay_at`. This is spec.md
  §6's first replay rule read back out of the database: "expose only evidence
  with `available_at <= T`". It is checked against STORED rows rather than
  against the in-memory list the gate filtered, because the gate and the
  store are different code paths and only the store is what a later report
  reads.

* **`cache_miss`** — a `run_steps` row of a replay run with
  `cache_status = 'miss'` on a TOOL row, i.e. on any `component` other than
  `'model'`. `rli.eval.runner` defines `'miss'` as "at least one call
  reached the network", and `rli.replay.mode` writes `'hit'` unconditionally
  for a served record precisely so that a `'miss'` on a probe row in a
  replay run can mean exactly one thing: a probe executed through the LIVE
  path. That is the third replay rule ("live tool calls are forbidden in
  replay") caught structurally, from the trace, without trusting any
  in-process bookkeeping.

  `component = 'model'` rows are DELIBERATELY outside the rule, because
  spec.md §6 says the opposite thing about them. Its replay rules pair
  "live **tool** calls are forbidden in replay; results come only from the
  cached full-probe record" with "live **LLM** calls are allowed on cache
  miss (e.g. a changed investigator prompt) and are recorded, so the cache
  is complete for subsequent runs". A System C replay whose investigator
  prompt changed is therefore SUPPOSED to miss the LLM cache. Counting
  those put 199 spurious violations on `dev-300-v2` — every System C LLM
  step — and made spec.md §6's `0` target unreachable for the wrong reason.
  They are reported instead as `model_cache_misses`, below.

* **`net_call`** — a `replay_violation:net_call` controller step. The
  forbidden-network client (`rli.replay.mode.ReplayNetClient`) writes one
  before it raises, so an attempt is durable even though the call never
  happened. `cache_miss` and `net_call` are deliberately both kept: the first
  catches a probe that reached the network *successfully* through a
  misconfigured runner, the second catches one that tried and was stopped.
  Zero of the first and nonzero of the second means the guard is working.

* **`missing_probe_result`** — a `replay_violation:missing_probe_result`
  step. Not a leak; a dataset GAP. It is reported here because it is the
  other way a replay's numbers can be silently wrong, and because
  `rli.replay.mode` is explicit that a gap must be loud rather than degrade
  into a plausible-looking metric. A run that hit one is `'failed'`, so it
  would otherwise only show up as a slightly smaller denominator.

* **`input_without_evidence`** — a replay run whose trace shows the frozen
  policy taking a BRANCH that is unreachable unless a given policy input was
  decided, while the run holds no evidence claim that could have decided it
  with `available_at <= T`. `rli.eval.runner` writes one
  `policy_decision:<branch>:<rule>` controller step per finished run, for
  every system, and `rli.policy.action`'s branch table is a set of proof
  obligations: `P4_repeated_repost` needs
  `corroborating_hiring_signal is False` ("`corroborating_hiring_signal`
  UNKNOWN blocks P4, which requires `is False`"), `P3a_freeze_or_pause`
  needs `freeze_or_pause is True`, `P1_closed` needs a decided
  `posting_state`, and so on. So the branch is durable PROOF the input was
  decided; if no claim could have decided it at `<= T`, the value came from
  somewhere the point-in-time gate never saw.

  This is the general check for the CLASS of bug security review H4 belongs
  to — a policy input populated from something the point-in-time gate never
  sanctioned. Be precise about what it would and would not have caught: H4's
  own contaminated runs took `P6_active_mixed_or_weak` (536), `P1_closed`
  (110) and `P3c` (3) on `dev-300-v2`, and `P4_repeated_repost` fires ZERO
  times in any dataset built so far — so this rule alone would have called
  them clean too. Their `posting_state` WAS properly backed; the input the
  blob decided never reached a branch that depended on it. What surfaces
  those runs is `blob_input_exposures`, below. The two figures are
  complementary by design and neither replaces the other.

  `P2_state_unresolved` is the one branch carrying no obligation at all: it
  is precisely the branch that fires when `posting_state` is UNKNOWN.

Three reported figures are NOT violations. `failed_runs`: a run can fail for
reasons that have nothing to do with leakage, and conflating the two would
make the `0` target unreachable for the wrong reason. `model_cache_misses`:
the `component = 'model'` rows excluded from `cache_miss` above, which
spec.md §6 explicitly permits. `blob_input_exposures`: the DATASET-side half
of the H4 story, described next. None is in `counts`, none moves `total` or
`clean`, and each gets its own line in `describe()` — surfaced so a clean
report can never quietly hide a broken, an unexpectedly expensive, or a
contaminated replay.

--------------------------------------------------------------------------
`blob_input_exposures`, the dataset hazard behind `input_without_evidence`
--------------------------------------------------------------------------

A replay run that EXECUTED a probe, was served the
`replay_probe_results.data` blob stored for that `(dataset, posting, T)`,
and holds no claim at `<= T` that could back a policy input the blob
nonetheless ANSWERS. `rli.replay.build` writes the contract (its "a cached
`ProbeResult.data` payload is build-time" judgment call): a blob is
build-time and therefore "possibly after `T`", and is safe ONLY because
"every policy input is derived from claims plus `rli.history.features` under
the point-in-time corpus — never from a `data` blob". It even predicts the
failure: "a future consumer that started reading `data` directly would
reintroduce leakage that no test would catch."

It is a COUNTER rather than a violation, and the reason is precise. The
violation above asks "did a run DECIDE from an unbacked input?", which the
H4 code fix answers permanently: with `rli.eval.case` no longer reading the
blob and `derive_policy_inputs`'s `team_signal=` keyword gone, the input can
only be populated by a claim, so the rule is structurally unreachable and
`0` is attainable. This counter asks a different question — "is the DATASET
still CAPABLE of it?" — and it is a property of the stored records, not of
the code that reads them. That is why it stays a counter even now that the
records themselves have been fixed.

**What has changed: `team_signal` should report 0 on a FRESHLY BUILT
dataset.** `rli.probes.team_signal.TeamSignalArgs` now carries an `as_of`
(`rli.probes.registry.build_args` fills it from the run clock) and
`rli.replay.build` re-runs the probe once per `T` inside
`rli.replay.pit.point_in_time`, exactly the fix this section used to
prescribe. The consequence for this counter is structural rather than
statistical: the per-`T` record's `corroborating_hiring_signal` blob key is
non-null EXACTLY when `rli.probes.team_signal._decide` returned a verdict,
and that is the same condition under which the probe emits a
`corroborating_hiring_signal` CLAIM — one `if`, both outputs. That claim's
`available_at` is now a board-capture time at or before `T`, so it survives
the gate, lands in `supported`, and the blob key is backed. When `_decide`
returns Unknown the probe emits no claim AND writes `null`, which
`_is_decided` reads as "not decided". Neither branch can produce an
exposure, so the only remaining source of `team_signal` exposures is a
dataset built before the change.

**Why it remains a COUNTER and not a violation**, for two independent
reasons:

  (i) Datasets built BEFORE this change are still contaminated — one
  build-clock record under every `T`, its claims gated out at every
  archive-era `T`, its blob still answering the input — and an operator
  needs to see WHICH, by dataset, in order to decide what to rebuild.
  Turning the number into a violation would make every such dataset fail a
  check that no code change can fix, which is the failure mode described
  below.

  (ii) The hazard CLASS outlives the one probe. `company_events`' three
  keys are covered by this counter today and are expected to stay silent
  for the same structural reason (its claim and its blob collapse together
  on `searched_at > as_of`); they are here as a regression guard against
  its `as_of` ever being dropped, and `team_signal` is now in exactly that
  position too. Any future probe that writes a policy-input name into
  `ProbeResult.data` re-arms the counter without touching this module.

Gating spec.md §6's `0` target on this number is therefore still the wrong
design: reporting it as a number keeps every bit of its value — it is how an
operator sees which datasets are contaminated and must be rebuilt — while
the VIOLATION beside it is what the `0` target belongs on.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **Scope is `(mode='replay', config_hash ends with '|dataset:<id>')`**, the
  same identity `rli.eval.baseline.collect_cases` and
  `rli.replay.run.clear_replay_runs` use. Three modules agreeing on one
  definition of "this dataset's runs" is what makes "leakage 0" and "the
  baseline report" statements about the same set of rows.

* **`available_at > replay_at` is a STRING comparison, done in SQL.** Both
  columns are `rli.models.time.to_utc_z` values, whose fixed-width
  microsecond fraction makes lexical order chronological order (that module
  documents exactly this invariant, and exists for it). Parsing 100k rows in
  Python to compare them would be slower and would introduce a second
  timestamp semantics.

* **A replay run with a NULL `replay_at` is itself a violation**
  (`missing_replay_at`), not a row to skip. Without `T` there is no window,
  so nothing about that run can be audited — and an unauditable run inside an
  audited dataset is the failure mode this checker exists to prevent. Every
  windowed rule then drops it on its own terms: SQL `NULL` compares and
  joins as unknown, so such a run contributes to no other kind rather than
  to a fabricated one.

* **`cache_miss` is written as a deny-list of the one exempt component, not
  as an allow-list of the others.** The test is `component != 'model'`, not
  `component in ('probe', 'controller')`, so that a future fourth
  `run_steps.component` value is FLAGGED rather than silently skipped — a
  leak checker should fail loudly on a writer it does not know about. The
  split is made in Python over the rows the existing step query already
  returns, rather than in a second SQL pass, so the trace is still read
  exactly once.
  Including `'controller'` costs nothing today: `rli.eval.runner`'s
  controller steps (`ProbeRunner.note`, and `Run.step` for `run_failed`) and
  `rli.replay.mode`'s `net_call` step all omit `cache_status` entirely, so a
  controller row is always NULL. There is no code path that can legitimately
  write `'miss'` there, which is exactly why a `'miss'` there must be
  reported rather than excused.

* **(a) The violation detects a DECISION; the counter detects a DATASET.**
  Policy inputs are not part of the persisted trace: `runs` has no inputs
  column, `run_steps` has no payload column, and `runs.final_decision` is a
  `rli.models.decision.Decision` (posting state, action, quality, reason,
  evidence) which does not carry the input set. Nothing durable records that
  `rli.eval.case` read `data['corroborating_hiring_signal']`, so "a code
  path read the blob" is not a question a pure reader can answer directly.

  Two different questions ARE answerable from the trace, and they are split
  into the two figures deliberately:

    1. `input_without_evidence` (VIOLATION) — the run's
       `policy_decision:<branch>:*` step names a branch `rli.policy.action`
       cannot reach with the input UNKNOWN, and no claim at `<= T` could
       have decided it. That is proof-carrying: the branch is the proof, so
       the finding is about a decision that really was taken, and there is
       no inference about where the value came from beyond "not from the
       claims this run was allowed to see".
    2. `blob_input_exposures` (COUNTER) — the run executed the probe, was
       handed the stored `(dataset, posting, T)` record, that record's blob
       answers a policy input, and no claim at `<= T` backs it. That is a
       CAPABILITY, not an act: the dataset could feed an unbacked input to
       any future reader of `data`.

  The first is what the `0` target should gate on. The second is what an
  operator needs in order to know which datasets to rebuild. Collapsing them
  into one number would either make the gate unreachable (see (b)) or hide
  the hazard entirely — which is precisely the state H4 exploited.

* **(b) The violation CAN reach 0 after the H4 fix; the counter cannot, and
  that asymmetry is the reason for the split.** With `rli.eval.case` no
  longer reading the blob and `derive_policy_inputs`'s `team_signal=`
  keyword removed, `corroborating_hiring_signal` can only come from a
  `team_signal` claim, so a `P4_repeated_repost` branch without one is
  structurally impossible and the violation is attainable at `0` on the
  EXISTING `dev-300-v2` — no rebuild required. Measured on the real database
  after that fix, over 7,954 replay runs in four datasets: 6,636 took a
  branch carrying an obligation (`P6` 5,239, `P1` 590, `P7` 545, `P5` 224,
  `P3c` 30, `P3a` 8; `P4` and `P3b` 0), 132 took `P2`, and the remaining
  1,186 are the failed runs that never reached a decision at all. 0 of the
  6,636 lacked a backing claim at `<= T`.

  The counter could not reach 0 on an EXISTING dataset by any code change,
  because the exposure is baked into the stored records: when those datasets
  were built, `TeamSignalArgs` had no `as_of`, so one build-time record
  serves every `T` while its claims are gated out at an archive-era `T`. It
  needs the rebuild described above, which the `as_of` change has since made
  possible — but a rebuild is an operator action, not a code path, and that
  is precisely the asymmetry. Making the counter a violation was considered
  and rejected: a gate that no code change can satisfy stops being a gate,
  and a permanently-red `0` target teaches a reader to ignore it — the same
  failure, in the opposite direction, as the silent CLEAN that let the H4
  runs through.

  All of the figures in this section are PRE-FIX measurements, and are kept
  as the record of what the hazard cost rather than as a prediction. Two
  counts of that population are in circulation and they measure different
  things: the security review's **830** is the archive-era subset across two
  datasets, while this counter reported **645** on `dev-300-v2` and **380**
  on `company-150-v2` (1,025 together), being every served exposure
  regardless of era. The 7,954-run measurement above is from the same era —
  after the H4 CODE fix (the blob read and the `team_signal=` keyword), and
  before the `as_of` fix. Rebuilding either dataset against the current
  builder is what should take its `team_signal` component to 0; the numbers
  are not re-stated here for a rebuilt dataset until one exists to measure.

* **(c) False positives, and what is deliberately not implemented.** The
  violation admits none by construction: every branch in the table is one
  `rli.policy.action` cannot reach unless the input is decided (only `P2` is
  excluded, and only because it is the branch that FIRES on an unknown
  state), and each requirement's `backed_by` is a SUPERSET of the claim
  types that can populate that input, so a missing intersection is proof
  rather than suspicion. A run with a NULL `replay_at` is skipped here (it
  is already `missing_replay_at`) so that "no claim at `<= T`" cannot be
  true by vacuum, and a finding is deduped per `(run, input)` so two branch
  steps on one run cannot double-count. Every entry was verified against the
  real database before being included: 0 unbacked runs out of the 6,636 that
  carry an obligation.

  The counter is bounded six ways: only records with `ok = 1` (a failed
  probe's payload decides nothing); only runs that actually executed that
  probe, so the records `rli.replay.build` deliberately stores for probes a
  system never selects are not held against them; only the record whose
  `args_hash` the probe step recorded, i.e. the one `ReplayProbeStore.get`
  really served, so a stale record left by an earlier build with different
  args is not charged to the run; only runs matched to their case through
  `replay_cases` on `(canonical_url, replay_at)` — the identity
  `rli.replay.run` uses — rather than through `runs.posting_id`, which is
  the id the running system re-derived and is NULL for an archive-only
  posting (`rli.replay.mode`: those two "can legitimately differ"); only
  blob keys that are
  `rli.models.policy_inputs.PolicyInputs` field names, so ordinary
  diagnostic payload is ignored; and an absent key, a `null`, and every
  codec envelope all count as "not decided", so a blob that declines to
  answer is not an exposure. `company_events` is covered and is expected to
  stay silent: its args carry `as_of`, `rli.replay.build` re-runs it per
  `T`, and both its blob triple and its `company_events_searched` claim
  collapse together on `searched_at > as_of` — it is there as a regression
  guard against that `as_of` ever being dropped. `team_signal` is now in the
  same position and is expected to be silent on any dataset built after its
  `as_of` change, for the structural reason given above; on an older dataset
  it is the thing this counter is reporting. The one read path the
  counter misses is `ReplayProbeStore.claims`, which serves a record without
  writing a `run_steps` row; it is used only for `archive_board_state`,
  whose payload carries no policy-input key.

  `repost_history`'s `first_seen_absent` / `censoring` / `gap_days`, which
  `rli.replay.build` names in the same breath as the blob hazard, are NOT
  covered and cannot be by either shape. They are not policy inputs; they
  are `rli.history.features.PostingHistoryFeatures` fields, whose honest
  source is the point-in-time CORPUS and which leave behind no claim and no
  trace row. "No supporting claim at `<= T`" is therefore true of every
  honest run, so a rule over them would be false positives end to end. The
  only honest detector is to re-derive the features under
  `rli.replay.pit.point_in_time` and diff them against the blob — and that
  function issues `CREATE TEMP TABLE` / `DROP TABLE`, i.e. it writes. That
  is barred by this module's first property, so the gap is named here rather
  than filled badly.

* **`net_call_count` takes a live `ReplayNetPool`, not the database.** It is
  the in-process counterpart used by tests and by a caller that wants to
  assert "zero network attempts" for one run without waiting for it to
  commit. `rli.replay.mode` names it in its own docstring; the durable
  version of the same fact is the `net_call` check above.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import BaseModel, ConfigDict

from rli.eval.runner import STEP_POLICY_DECISION
from rli.events.policy_signals import (
    CLAIM_EVENTS_SEARCHED,
    COMPANY_EVENTS_PROBE,
    EVENT_CLAIM_TYPES,
)
from rli.policy.action import PRECEDENCE
from rli.policy.inputs import (
    CLAIM_DECLARED_EXPIRY,
    CLAIM_FIRST_PUBLISHED,
    CLAIM_POSTING_STATE,
    CLAIM_REFRESHED_AT,
    CLAIM_TEAM_SIGNAL,
)
from rli.probes.team_signal import TeamSignalProbe
from rli.replay.mode import STEP_REPLAY_VIOLATION

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.replay.mode import ReplayNetPool

__all__ = [
    "VIOLATION_KINDS",
    "LeakageReport",
    "Violation",
    "ViolationKind",
    "check_dataset",
    "net_call_count",
]

ViolationKind = str

#: Every violation kind this module can report. Exported so a caller can
#: assert on the set rather than restate the strings.
VIOLATION_KINDS: tuple[str, ...] = (
    "evidence_after_t",
    "cache_miss",
    "net_call",
    "missing_probe_result",
    "missing_replay_at",
    "input_without_evidence",
)

#: The one `run_steps.component` spec.md §6 exempts from the `cache_miss`
#: rule ("live **LLM** calls are allowed on cache miss"). See the docstring.
_MODEL_COMPONENT = "model"


class _InputRequirement(NamedTuple):
    """One policy input that a taken branch PROVES was decided, plus the claims
    that could legitimately have decided it.

    `input_name` is a `rli.models.policy_inputs.PolicyInputs` field name;
    `backed_by` is a SUPERSET of the claim types that can populate it, so a
    missing intersection is proof the claims path did not answer it — never a
    guess. `backing_label` is the readable form for a violation detail.
    """

    input_name: str
    backed_by: frozenset[str]
    backing_label: str


class _InputExposure(NamedTuple):
    """One `(probe, blob key)` pair through which a dataset can answer a policy
    input, and the evidence that would make that answer legitimate.

    `key` is a `rli.models.policy_inputs.PolicyInputs` field name AND the key
    the probe writes into `ProbeResult.data`; `backed_by` is the set of
    `evidence.claim_type` values any one of which, at `available_at <= T`,
    means the run could derive that input honestly from claims.
    """

    probe_name: str
    key: str
    backed_by: frozenset[str]
    #: Human-readable form of `backed_by` for the violation detail. Spelled
    #: out here because `EVENT_CLAIM_TYPES` is too long to list in a message.
    backing_label: str


#: `rli.policy.inputs`: "No `company_events` evidence at all therefore means
#: UNKNOWN, never False" — the searched claim is what makes a `False` a report
#: on a search that happened, and a dated event claim implies one too.
_COMPANY_EVENT_BACKING = frozenset({CLAIM_EVENTS_SEARCHED, *EVENT_CLAIM_TYPES})

#: Every policy input a `replay_probe_results.data` blob can answer today.
#: `CLAIM_TEAM_SIGNAL` is used three times over because the claim type, the
#: `PolicyInputs` field and the blob key really are one string
#: (`corroborating_hiring_signal`), and restating it would let them drift.
_INPUT_EXPOSURES: tuple[_InputExposure, ...] = (
    _InputExposure(
        probe_name=TeamSignalProbe.name,
        key=CLAIM_TEAM_SIGNAL,
        backed_by=frozenset({CLAIM_TEAM_SIGNAL}),
        backing_label=f"a {CLAIM_TEAM_SIGNAL!r}",
    ),
    _InputExposure(
        probe_name=COMPANY_EVENTS_PROBE,
        key="material_negative_event",
        backed_by=_COMPANY_EVENT_BACKING,
        backing_label=f"a {CLAIM_EVENTS_SEARCHED!r} or dated-event",
    ),
    _InputExposure(
        probe_name=COMPANY_EVENTS_PROBE,
        key="freeze_or_pause",
        backed_by=_COMPANY_EVENT_BACKING,
        backing_label=f"a {CLAIM_EVENTS_SEARCHED!r} or dated-event",
    ),
    _InputExposure(
        probe_name=COMPANY_EVENTS_PROBE,
        key="last_material_event_at",
        backed_by=_COMPANY_EVENT_BACKING,
        backing_label=f"a {CLAIM_EVENTS_SEARCHED!r} or dated-event",
    ),
)

_EVENTS_LABEL = f"a {CLAIM_EVENTS_SEARCHED!r} or dated-event"

#: `rli.policy.action._branch` is FIRST-MATCH-WINS, and `P2_state_unresolved`
#: is the branch that absorbs an unknown state (`resolved_state is None or ==
#: "unknown"`). So reaching any LATER branch proves `posting_state` was
#: decided, and `P1_closed`, the one earlier branch, proves it directly by
#: requiring `"closed"`. `P2` is therefore the ONLY branch that carries no
#: `posting_state` obligation — which is why the table below is derived from
#: `PRECEDENCE` minus that one name rather than listed by hand: a branch added
#: to the policy is covered automatically, and only a branch inserted BEFORE
#: `P2` that tolerates an unknown state would need revisiting here.
_UNRESOLVED_STATE_BRANCH = "P2_state_unresolved"

_POSTING_STATE_REQUIRED = _InputRequirement(
    input_name="posting_state",
    backed_by=frozenset({CLAIM_POSTING_STATE}),
    backing_label=f"a {CLAIM_POSTING_STATE!r}",
)

#: What each branch proves BEYOND a decided `posting_state`, read off
#: `rli.policy.action._branch`'s conditions. A branch absent from this map
#: still carries the `posting_state` obligation.
_EXTRA_REQUIREMENTS: dict[str, tuple[_InputRequirement, ...]] = {
    # `inputs.freeze_or_pause is True` — only `company_events` claims can
    # populate it, and `rli.events.policy_signals.signals_from_facts`
    # short-circuits to all-UNKNOWN without the search claim.
    "P3a_freeze_or_pause": (
        _InputRequirement(
            input_name="freeze_or_pause",
            backed_by=frozenset({CLAIM_EVENTS_SEARCHED, *EVENT_CLAIM_TYPES}),
            backing_label=_EVENTS_LABEL,
        ),
    ),
    # `isinstance(inputs.declared_expiry, datetime)` — `rli.policy.inputs`
    # returns a datetime only from a `declared_expiry` claim that carries a
    # `source_event_at`; `None` ("checked, none declared") does not reach here.
    "P3b_declared_expiry_unresolved": (
        _InputRequirement(
            input_name="declared_expiry",
            backed_by=frozenset({CLAIM_DECLARED_EXPIRY}),
            backing_label=f"a {CLAIM_DECLARED_EXPIRY!r}",
        ),
    ),
    # `inputs.material_negative_event is True` AND a datetime
    # `last_material_event_at`. Only the first is listed: the two share one
    # backing set, so a second requirement would double-count one finding.
    "P3c_material_event_unrefreshed": (
        _InputRequirement(
            input_name="material_negative_event",
            backed_by=frozenset({CLAIM_EVENTS_SEARCHED, *EVENT_CLAIM_TYPES}),
            backing_label=_EVENTS_LABEL,
        ),
    ),
    # `inputs.corroborating_hiring_signal is False` — spec.md §4 makes
    # `team_signal` its only source. THE H4 branch.
    "P4_repeated_repost": (
        _InputRequirement(
            input_name="corroborating_hiring_signal",
            backed_by=frozenset({CLAIM_TEAM_SIGNAL}),
            backing_label=f"a {CLAIM_TEAM_SIGNAL!r}",
        ),
    ),
    # `inputs.publish_recency == "recent"`, which needs
    # `last_publish_or_refresh` to be non-None — a primary `first_published`
    # claim or a corroborated `refreshed_at` claim, and nothing else.
    "P5_open_recent_strong": (
        _InputRequirement(
            input_name="publish_recency",
            backed_by=frozenset({CLAIM_FIRST_PUBLISHED, CLAIM_REFRESHED_AT}),
            backing_label=f"a {CLAIM_FIRST_PUBLISHED!r} or {CLAIM_REFRESHED_AT!r}",
        ),
    ),
    # `inputs.corroborating_hiring_signal is True` (spec.md §5, Amendment
    # 2026-09-12). The SAME obligation as `P4_repeated_repost`'s, from the
    # opposite side: P4 proves the input was decided `False`, P5b proves it
    # was decided `True`, and either answer can only have come from a
    # `corroborating_hiring_signal` claim, since spec.md §4 makes
    # `team_signal` its only source. The `posting_state` obligation the
    # branch also carries is added by `_BRANCH_REQUIREMENTS` below, which is
    # derived from `PRECEDENCE`, so only this extra one is listed here.
    "P5b_hiring_activity": (
        _InputRequirement(
            input_name="corroborating_hiring_signal",
            backed_by=frozenset({CLAIM_TEAM_SIGNAL}),
            backing_label=f"a {CLAIM_TEAM_SIGNAL!r}",
        ),
    ),
}

#: Branch -> the policy inputs reaching it PROVES were decided. Derived from
#: `rli.policy.action.PRECEDENCE` so the branch ids are imported, never
#: restated, and so a new branch cannot be silently uncovered.
_BRANCH_REQUIREMENTS: dict[str, tuple[_InputRequirement, ...]] = {
    branch: (_POSTING_STATE_REQUIRED, *_EXTRA_REQUIREMENTS.get(branch, ()))
    for branch, _ in PRECEDENCE
    if branch != _UNRESOLVED_STATE_BRANCH
}

_UNKNOWN_BRANCHES = set(_EXTRA_REQUIREMENTS) - {branch for branch, _ in PRECEDENCE}
if _UNKNOWN_BRANCHES:  # pragma: no cover - a typo, caught at import
    raise ValueError(
        f"_EXTRA_REQUIREMENTS names branches rli.policy.action does not have: "
        f"{sorted(_UNKNOWN_BRANCHES)}"
    )

_MISSING = object()


def _is_decided(value: Any) -> bool:
    """Does this blob value ANSWER its policy input, rather than duck it?

    A decided policy input serializes as a JSON scalar. Three things are
    therefore NOT an answer, and each is excluded for its own reason:

    * an absent key, obviously;
    * `null` — `rli.probes.team_signal` leaves
      `data['corroborating_hiring_signal']` at `None` for Unknown
      deliberately, so a data-blob reader cannot take it for a negative (its
      comment there still names the `rli.eval.case` fallback the H4 fix has
      since deleted). A `null` is the blob DECLINING to answer;
    * any `dict` or `list` — every envelope `rli.replay.mode`'s codec writes,
      the UNKNOWN sentinel included, is a JSON object, so this is exactly
      "not decided" without this module restating that codec's private
      envelope key, and a `list` is payload (`data['evidence']`), not an
      answer.
    """
    return value is not _MISSING and value is not None and not isinstance(value, dict | list)


class Violation(BaseModel):
    """One leakage finding, with enough context to reproduce it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ViolationKind
    run_id: str
    system: str
    replay_at: str | None = None
    posting_id: str | None = None
    detail: str = ""

    def describe(self) -> str:
        where = f"{self.system} run {self.run_id}"
        if self.replay_at is not None:
            where += f" @ T={self.replay_at}"
        return f"[{self.kind}] {where}: {self.detail}"


class LeakageReport(BaseModel):
    """The spec.md §6 "future-leakage violations (0 target)" figure, itemized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    systems: tuple[str, ...] = ()
    runs_checked: int = 0
    evidence_checked: int = 0
    steps_checked: int = 0
    failed_runs: int = 0
    # NOT a violation, and deliberately not in `counts`: spec.md §6 allows a
    # live LLM call on cache miss. Kept on the report, and on its own line in
    # `describe()`, so "System C reached the LLM 199 times" stays visible
    # instead of vanishing the moment it stopped being counted as leakage.
    model_cache_misses: int = 0
    # Also NOT a violation, for a different reason: it counts a DATASET
    # hazard, not a decided run, and it cannot reach 0 without rebuilding the
    # dataset. See the module docstring's `input_without_evidence` judgment
    # calls for why that makes it a number rather than a gate.
    blob_input_exposures: int = 0

    counts: dict[str, int] = {}
    violations: tuple[Violation, ...] = ()
    # Violations are capped in the itemized list so one systematic bug cannot
    # produce a million-line report; the COUNTS above are never capped.
    truncated: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def clean(self) -> bool:
        """True when spec.md §6's `0` target is met for this dataset."""
        return self.total == 0

    def describe(self) -> str:
        verdict = "CLEAN (0 violations)" if self.clean else f"{self.total} VIOLATION(S)"
        lines = [
            f"leakage check: dataset={self.dataset_id!r} "
            f"systems={', '.join(self.systems) or '(none)'} -> {verdict}",
            f"  runs checked={self.runs_checked} evidence rows={self.evidence_checked} "
            f"trace steps={self.steps_checked} failed runs={self.failed_runs}",
            f"  model cache misses={self.model_cache_misses} (not a violation: "
            "spec.md §6 allows live LLM calls on cache miss)",
            f"  blob input exposures={self.blob_input_exposures} (not a violation: "
            "a served `data` blob answers a policy input no claim backs at T; "
            "a NONZERO count needs a dataset rebuild to clear)",
        ]
        if self.counts:
            lines.append(
                "  by kind: "
                + " ".join(f"{kind}={count}" for kind, count in sorted(self.counts.items()))
            )
        lines.extend(f"  {violation.describe()}" for violation in self.violations)
        if self.truncated:
            lines.append(f"  ... and {self.truncated} more (itemization capped)")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def net_call_count(pool: ReplayNetPool) -> int:
    """How many forbidden live tool calls this replay pool refused.

    The in-process counterpart of the `net_call` trace check: `> 0` means a
    probe tried to reach the network from replay mode. It counts ATTEMPTS,
    not successes — `rli.replay.mode.ReplayNetClient` cannot make a request
    at all — so a nonzero value is a wiring bug in the system under test, not
    a leak that already happened.
    """
    return len(pool.attempts)


def _dataset_runs(conn: sqlite3.Connection, dataset_id: str) -> list[sqlite3.Row]:
    suffix = f"|dataset:{dataset_id}"
    rows = conn.execute(
        """
        SELECT id, system, replay_at, posting_id, input_url, status, config_hash
        FROM runs
        WHERE mode = 'replay' AND config_hash IS NOT NULL
        ORDER BY replay_at, system, id
        """
    ).fetchall()
    return [row for row in rows if str(row["config_hash"]).endswith(suffix)]


def check_dataset(
    conn: sqlite3.Connection, dataset_id: str, *, max_items: int = 50
) -> LeakageReport:
    """Audit every replay run of `dataset_id` (see the module docstring).

    `max_items` caps the ITEMIZED violation list only; `counts` and `total`
    are always complete, so `report.clean` is never an artefact of the cap.
    """
    runs = _dataset_runs(conn, dataset_id)
    by_id = {row["id"]: row for row in runs}
    counts: dict[str, int] = {}
    found: list[Violation] = []
    evidence_checked = 0
    steps_checked = 0
    model_cache_misses = 0
    failed_runs = sum(1 for row in runs if row["status"] == "failed")

    def record(kind: str, run: sqlite3.Row, detail: str) -> None:
        counts[kind] = counts.get(kind, 0) + 1
        if len(found) < max_items:
            found.append(
                Violation(
                    kind=kind,
                    run_id=run["id"],
                    system=str(run["system"]),
                    replay_at=run["replay_at"],
                    posting_id=run["posting_id"],
                    detail=detail,
                )
            )

    for run in runs:
        if run["replay_at"] is None:
            record(
                "missing_replay_at",
                run,
                "a replay run with no replay_at defines no `available_at <= T` "
                "window and cannot be audited at all",
            )

    if not by_id:
        return LeakageReport(dataset_id=dataset_id, counts=counts, violations=tuple(found))

    placeholders = ",".join("?" for _ in by_id)
    run_ids = list(by_id)

    # -- rule 1: available_at <= T ----------------------------------------
    evidence_checked = int(
        conn.execute(
            f"SELECT COUNT(*) FROM evidence WHERE run_id IN ({placeholders})", run_ids
        ).fetchone()[0]
    )
    leaked = conn.execute(
        f"""
        SELECT e.run_id AS run_id, e.id AS evidence_id, e.probe AS probe,
               e.claim_type AS claim_type, e.available_at AS available_at,
               r.replay_at AS replay_at
        FROM evidence AS e
        JOIN runs AS r ON r.id = e.run_id
        WHERE e.run_id IN ({placeholders})
          AND r.replay_at IS NOT NULL
          AND e.available_at > r.replay_at
        ORDER BY e.run_id, e.id
        """,
        run_ids,
    ).fetchall()
    for row in leaked:
        record(
            "evidence_after_t",
            by_id[row["run_id"]],
            f"evidence {row['evidence_id']} from probe {row['probe']!r} "
            f"({row['claim_type']}) has available_at={row['available_at']} > "
            f"T={row['replay_at']}",
        )

    # -- rules 3 and the dataset-gap check --------------------------------
    steps_checked = int(
        conn.execute(
            f"SELECT COUNT(*) FROM run_steps WHERE run_id IN ({placeholders})", run_ids
        ).fetchone()[0]
    )
    for row in conn.execute(
        f"""
        SELECT run_id, step_index, component, probe_name, cache_status,
               decision_type, error
        FROM run_steps
        WHERE run_id IN ({placeholders})
          AND (cache_status = 'miss' OR decision_type LIKE ?)
        ORDER BY run_id, step_index
        """,
        (*run_ids, f"{STEP_REPLAY_VIOLATION}:%"),
    ).fetchall():
        run = by_id[row["run_id"]]
        if row["cache_status"] == "miss":
            # A model row is spec.md §6's permitted live LLM call, counted
            # and not charged; every other component is a TOOL call, which
            # the same paragraph forbids. Split in Python rather than in two
            # SQL passes so the trace is still read exactly once.
            if str(row["component"]) == _MODEL_COMPONENT:
                model_cache_misses += 1
            else:
                record(
                    "cache_miss",
                    run,
                    f"step {row['step_index']} (component={row['component']!r}, "
                    f"probe={row['probe_name']!r}) recorded cache_status='miss', "
                    "i.e. at least one call reached the network",
                )
        decision_type = str(row["decision_type"])
        if decision_type == f"{STEP_REPLAY_VIOLATION}:net_call":
            record(
                "net_call",
                run,
                f"step {row['step_index']}: {row['error']}",
            )
        elif decision_type == f"{STEP_REPLAY_VIOLATION}:missing_probe_result":
            record(
                "missing_probe_result",
                run,
                f"step {row['step_index']} (probe={row['probe_name']!r}): "
                "the dataset has no cached result for this probe at this T",
            )

    # -- rule 5 and the dataset-hazard counter -----------------------------
    # Both read the same fact — "which claim types could this run have
    # decided a policy input from, at `available_at <= T`?" — so they share
    # one set-based query for it, and neither ever queries per run.
    by_probe: dict[str, list[_InputExposure]] = {}
    for exposure in _INPUT_EXPOSURES:
        by_probe.setdefault(exposure.probe_name, []).append(exposure)
    probe_names = sorted(by_probe)
    backing_types = sorted(
        {claim for item in _INPUT_EXPOSURES for claim in item.backed_by}
        | {
            claim
            for requirements in _BRANCH_REQUIREMENTS.values()
            for requirement in requirements
            for claim in requirement.backed_by
        }
    )

    supported: dict[str, set[str]] = {}
    for row in conn.execute(
        f"""
        SELECT DISTINCT e.run_id AS run_id, e.claim_type AS claim_type
        FROM evidence AS e
        JOIN runs AS r ON r.id = e.run_id
        WHERE e.run_id IN ({placeholders})
          AND r.replay_at IS NOT NULL
          AND e.available_at <= r.replay_at
          AND e.claim_type IN ({",".join("?" for _ in backing_types)})
        """,
        (*run_ids, *backing_types),
    ).fetchall():
        supported.setdefault(str(row["run_id"]), set()).add(str(row["claim_type"]))

    # -- rule 5: `input_without_evidence`, proved from the branch taken ----
    # `rli.eval.runner` writes one `policy_decision:<branch>:<rule>`
    # controller step per finished run, for EVERY system. A branch in
    # `_BRANCH_REQUIREMENTS` is unreachable unless its input was decided, so
    # the branch is a durable proof that it WAS — and if no claim could have
    # decided it at `<= T`, the value came from somewhere the gate never saw.
    # Neither the prefix match nor the branch match uses `LIKE`: `_` is a LIKE
    # wildcard and both `policy_decision` and every branch id contain several,
    # so `substr(...) = ?` is the honest "starts with" and the branch itself is
    # split out in Python.
    prefix = f"{STEP_POLICY_DECISION}:"
    proved: set[tuple[str, str]] = set()
    for row in conn.execute(
        f"""
        SELECT DISTINCT run_id, decision_type
        FROM run_steps
        WHERE run_id IN ({placeholders})
          AND component = 'controller'
          AND substr(decision_type, 1, ?) = ?
        ORDER BY run_id, decision_type
        """,
        (*run_ids, len(prefix), prefix),
    ).fetchall():
        run_id = str(row["run_id"])
        run = by_id[run_id]
        if run["replay_at"] is None:
            # Already reported as `missing_replay_at`; with no window, "no
            # backing claim at <= T" would be true by vacuum, not by fault.
            continue
        branch = str(row["decision_type"]).split(":")[1]
        for requirement in _BRANCH_REQUIREMENTS.get(branch, ()):
            if requirement.backed_by & supported.get(run_id, set()):
                continue
            # One finding per (run, input): two branch steps on one run, or
            # two branches sharing a requirement, are one unbacked input.
            if (run_id, requirement.input_name) in proved:
                continue
            proved.add((run_id, requirement.input_name))
            record(
                "input_without_evidence",
                run,
                f"the frozen policy took branch {branch!r}, which "
                f"`rli.policy.action` can only reach with policy input "
                f"{requirement.input_name!r} DECIDED, but the run holds no "
                f"{requirement.backing_label} claim with available_at <= "
                f"T={run['replay_at']} that could have decided it",
            )

    # -- the `blob_input_exposures` counter (NOT a violation) --------------
    # The same question asked of the DATASET rather than of a decision: was
    # this run handed a build-time `data` blob that answers a policy input
    # nothing at `<= T` backs?
    #
    # `data` is parsed with plain `json.loads` rather than
    # `rli.replay.mode.decode_probe_data`, which would validate every
    # `ProbeClaim` in `data['evidence']` — hundreds of times over — to answer
    # a question about four top-level scalars.
    exposed: set[tuple[str, str]] = set()

    # Three small indexed reads, joined in Python. Expressing the same join in
    # one statement makes SQLite drive `runs` off the thousand-element `r.id
    # IN (...)` list once per stored record — millions of index probes for a
    # few thousand rows — so the sets are fetched separately and matched here.
    cases = {
        (str(row["canonical_url"]), str(row["replay_at"])): str(row["posting_id"])
        for row in conn.execute(
            "SELECT posting_id, replay_at, canonical_url FROM replay_cases WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchall()
    }
    records = {
        (
            str(row["posting_id"]),
            str(row["replay_at"]),
            str(row["probe_name"]),
            str(row["args_hash"]),
        ): row["data"]
        for row in conn.execute(
            f"""
            SELECT posting_id, replay_at, probe_name, args_hash, data
            FROM replay_probe_results
            WHERE dataset_id = ? AND ok = 1
              AND probe_name IN ({",".join("?" for _ in probe_names)})
            """,
            (dataset_id, *probe_names),
        ).fetchall()
    }

    for row in conn.execute(
        f"""
        SELECT DISTINCT run_id, probe_name, args_hash
        FROM run_steps
        WHERE run_id IN ({placeholders})
          AND component = 'probe'
          AND args_hash IS NOT NULL
          AND probe_name IN ({",".join("?" for _ in probe_names)})
        """,
        (*run_ids, *probe_names),
    ).fetchall():
        run_id = str(row["run_id"])
        run = by_id[run_id]
        if run["replay_at"] is None or run["input_url"] is None:
            continue
        # The DATASET's posting id, via the `(canonical_url, replay_at)`
        # identity `rli.replay.run` uses -- not `runs.posting_id`, which is
        # the id the running system re-derived and is NULL for an
        # archive-only posting (`rli.replay.mode`: those two "can
        # legitimately differ").
        posting_id = cases.get((str(run["input_url"]), str(run["replay_at"])))
        if posting_id is None:
            continue
        probe_name = str(row["probe_name"])
        # Keyed on the `args_hash` the step recorded, i.e. the record
        # `ReplayProbeStore.get` actually served, so a stale record left by an
        # earlier build with different args is never charged to this run.
        data = records.get((posting_id, str(run["replay_at"]), probe_name, str(row["args_hash"])))
        if data is None:
            continue
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            # Unreadable payload: it cannot have exposed anything, because
            # `rli.replay.mode.decode_probe_data` raises on it at SERVE time
            # and the run handed it is already 'failed' (see `failed_runs`).
            continue
        if not isinstance(payload, dict):  # pragma: no cover - only a corrupt row
            continue

        for exposure in by_probe[probe_name]:
            value = payload.get(exposure.key, _MISSING)
            if not _is_decided(value):
                continue
            if exposure.backed_by & supported.get(run_id, set()):
                continue
            # One count per (run, input), not per stored record.
            exposed.add((run_id, exposure.key))

    total = sum(counts.values())
    return LeakageReport(
        dataset_id=dataset_id,
        systems=tuple(sorted({str(row["system"]) for row in runs})),
        runs_checked=len(runs),
        evidence_checked=evidence_checked,
        steps_checked=steps_checked,
        failed_runs=failed_runs,
        model_cache_misses=model_cache_misses,
        blob_input_exposures=len(exposed),
        counts=counts,
        violations=tuple(found),
        truncated=max(0, total - len(found)),
    )
