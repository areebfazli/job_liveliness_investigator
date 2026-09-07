"""The fixed v1 action policy (spec.md §5) and its frozen thresholds.

spec.md §5 states the policy as a six-line table plus an "otherwise":

```text
closed                                             -> skip
open + recent primary publish evidence
     + no material negative event                  -> apply_now
open + mixed/weak evidence                         -> quick_apply
open + explicit freeze/pause or declared expiry
     + unresolved current status                   -> wait
repeated unchanged repost + long-lived history
     + corroborating weak hiring signals           -> skip
otherwise                                          -> quick_apply
```

Those rows OVERLAP — an open posting can simultaneously have weak evidence,
a declared expiry and an unchanged repost history — so a table alone is not a
function. PLAN.md M3 requires the overlaps and their precedence to be
documented before the policy is frozen. This is that document, and the code
below is a literal transcription of it.

--------------------------------------------------------------------------
Precedence: first match wins
--------------------------------------------------------------------------

Read as a chain of `if ... return`, in this order:

* **P1 `closed`** — `posting_state == "closed"`
  -> `skip`, `recheck_after_days = null`
* **P2 `state_unresolved`** — `posting_state` is UNKNOWN or `"unknown"`
  -> `wait`, with a recheck
* **P3a `freeze_or_pause`** — active AND `freeze_or_pause is True`
  -> `wait`, with a recheck
* **P3b `declared_expiry_unresolved`** — active AND `declared_expiry` is a
  datetime AND the current status is NOT resolved
  -> `wait`, with a recheck
* **P4 `repeated_repost`** — `repost_pattern == "repeated_unchanged"` AND
  `long_lived is True` AND `corroborating_hiring_signal is False`
  -> `skip`, `recheck_after_days = null`
* **P5 `open_recent_strong`** — `posting_state == "open"` AND
  `publish_recency == "recent"` AND `material_negative_event is not True`
  AND `evidence_quality == "strong"`
  -> `apply_now`, `recheck_after_days = null`
* **P6 `active_mixed_or_weak`** — active AND
  `evidence_quality in {"mixed", "weak"}`
  -> `quick_apply`, `recheck_after_days = null`
* **P7 `default`** — otherwise
  -> `quick_apply`, `recheck_after_days = null`

where `active` means `posting_state in {"open", "reposted"}` and
`current status resolved` means `posting_state == "open" AND
evidence_quality == "strong"`.

--------------------------------------------------------------------------
Why that order, and what each overlap costs
--------------------------------------------------------------------------

* **P1 before everything.** spec.md §5 lists `closed -> skip` first and it is
  unconditional: a closed posting cannot be worth effort whatever else is
  true. (`closed` is never read as `filled` — spec.md §5/§9.)
* **P2 exists because the §5 table has no row for it.** An unresolved
  `posting_state` would otherwise fall through to "otherwise -> quick_apply",
  telling the user to spend effort on a posting whose existence we could not
  confirm — usually because the resolver *failed*. `wait` + a recheck is the
  honest answer, and it is also the only action that leaves a probe with
  something to do. This is an addition to the §5 table, not a reading of it.
* **Why P3 is split in two.** spec.md §5's row reads "open + explicit
  freeze/pause **or** declared expiry + unresolved current status -> wait",
  and the two disjuncts do not carry the same weight. Attaching "unresolved
  current status" to *both* would let a well-evidenced open posting at a
  company that has publicly frozen hiring reach `apply_now` — the listing is
  up and the evidence is strong, so the status would count as "resolved" —
  which inverts the intent of the row and leaves the freeze clause with
  almost nothing to do. So:
    * **P3a**: an explicit, dated freeze/pause is decisive on its own. It is
      itself the reason the current status is unresolved: a live board
      listing is stale evidence about whether anyone is hiring behind it.
    * **P3b**: a declared expiry is NOT decisive on its own. spec.md §3 is
      explicit that `validThrough` is "publisher-declared expiry ... not
      proof of a ghost job", so it forces a `wait` only when the current
      status is otherwise unresolved (anything but `open` + `strong`).
* **P3 before P4 (wait beats skip).** A hiring freeze plus an unchanged
  long-lived repost satisfies both. `wait` is the recoverable error: the user
  rechecks and can still skip later, whereas `skip` is terminal and would
  discard a role that a lifted freeze reopens. spec.md §5 also lists the wait
  row above the repost row.
* **P3 before P5 (wait beats apply_now).** This is the case PLAN.md M3
  bullet 5 asks to be tested: a *recently published* posting under an
  explicit freeze, or with a declared expiry and weak evidence, is a `wait`,
  not an `apply_now`.
* **P4 before P5.** A repeated unchanged repost with no corroborating hiring
  signal is by construction "recently published" (the repost restarts the
  clock), so without this ordering the §5 repost row could never fire — the
  posting would always take the `apply_now` row first. P4 is precisely the
  guard against a fresh publish date that means nothing.
* **P6 before P7** is only a labelling distinction: both yield
  `quick_apply`. It is kept separate so the trace records *why*
  (`active_mixed_or_weak` vs a genuine fall-through) and so a future policy
  can change one without the other.

--------------------------------------------------------------------------
Unknown handling (the sentinel is not a value)
--------------------------------------------------------------------------

* `posting_state` UNKNOWN -> P2 `wait` (above).
* `material_negative_event` UNKNOWN counts as "no material negative event"
  for P5. **Judgment call, and the one most worth revisiting.** The
  alternative — requiring `company_events` to have run and returned `False`
  before `apply_now` is ever reachable — was rejected because event
  collection covers a minority of target companies, so it would make
  `apply_now` unreachable for most postings and turn the action distribution
  into a `quick_apply` monoculture that spec.md §6 explicitly warns about
  ("reported with the action distribution so a default-heavy policy cannot
  pass trivially"). The risk is bounded by the conjunct that P5 also
  requires `evidence_quality == "strong"`, i.e. a primary publish date and a
  confirmed open state. Note that unknown event coverage does **not** by
  itself make the evidence weak: `rli.policy.quality` scores the evidence
  about *the posting*, not about the company. To flip this decision, change
  `is not True` to `is False` on the P5 branch — one token — and the tests
  parametrized over `material_negative_event` will show the effect.
* `freeze_or_pause` UNKNOWN is NOT a freeze (P3a requires `is True`). A
  freeze is a positive claim; not having looked is not a reason to wait.
* `declared_expiry` UNKNOWN is not an expiry (P3b requires a real datetime),
  and it makes `recheck_after_days` fall back to the default.
* `corroborating_hiring_signal` UNKNOWN blocks P4, which requires `is False`
  ("checked, and no corroborating signal"). This matches spec.md §4's rule
  that `team_signal` is eligible "only when that input is unknown and the
  repost/long-lived branch is reachable": the branch is designed to wait for
  a real answer.
* `repost_pattern` UNKNOWN and `long_lived` UNKNOWN both block P4 — spec.md
  §4: "missing history never means flat hiring", and it certainly never
  means a repost.

--------------------------------------------------------------------------
`recheck_after_days` (spec.md §1)
--------------------------------------------------------------------------

`min(recheck_cap_days, days_until_validThrough)` when a declared expiry
exists, else `recheck_default_days` — and `null` for every action except
`wait`, since spec.md §1 attaches the field to `wait` alone.

Two details spec.md leaves open:

* **Rounding is `floor`.** An expiry 3.5 days out gives `3`, so the recheck
  never lands *after* the declared expiry. `ceil` would schedule the user to
  look on a day the posting may already be gone. An expiry exactly 3 days
  out gives `3` (the stated example).
* **A passed expiry gives `0`, not the default.** `min(cap, negative)` is
  negative, and "recheck in -2 days" is not a thing; the value is clamped to
  `0`, read as "recheck now". Falling back to the 14-day default would be
  worse: the one moment we know the declared expiry is behind us is the
  moment the answer is most likely to have changed. `0` is a legal value of
  `Decision.recheck_after_days` and callers must treat it as "immediately",
  not as "never".

--------------------------------------------------------------------------
`long_lived`
--------------------------------------------------------------------------

`PolicyInputs` has exactly the seven fields spec.md §5 names, and
`long_lived` is not one of them — it is a history *feature*
(`rli.history.features.PostingHistoryFeatures.long_lived`), which is why it
is threaded through `decide(..., long_lived=...)` as a keyword rather than
smuggled into the inputs model or folded into `repost_pattern`. Folding it in
would have made `repost_pattern` mean two different things at once and would
have corrupted `rli.policy.inputs.could_change_action`, which enumerates that
field's domain.
"""

from __future__ import annotations

import hashlib
import inspect
import math
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from rli.models.decision import EvidenceQuality, PostingState, RecommendedAction
from rli.models.policy_inputs import UNKNOWN, PolicyInputs, Unknown

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.config import Config

__all__ = [
    "PRECEDENCE",
    "PolicyOutcome",
    "PolicyThresholds",
    "decide",
    "policy_version",
]

PolicyBranch = Literal[
    "P1_closed",
    "P2_state_unresolved",
    "P3a_freeze_or_pause",
    "P3b_declared_expiry_unresolved",
    "P4_repeated_repost",
    "P5_open_recent_strong",
    "P6_active_mixed_or_weak",
    "P7_default",
]

# The precedence table above, machine-readable, so a test can assert that the
# documented order is the implemented order and so the trace can name the
# branch that fired. Order is load-bearing: first match wins.
PRECEDENCE: tuple[tuple[PolicyBranch, RecommendedAction], ...] = (
    ("P1_closed", "skip"),
    ("P2_state_unresolved", "wait"),
    ("P3a_freeze_or_pause", "wait"),
    ("P3b_declared_expiry_unresolved", "wait"),
    ("P4_repeated_repost", "skip"),
    ("P5_open_recent_strong", "apply_now"),
    ("P6_active_mixed_or_weak", "quick_apply"),
    ("P7_default", "quick_apply"),
)

_ACTIVE_STATES = ("open", "reposted")
_EVIDENCE_QUALITIES = ("strong", "mixed", "weak")


class PolicyThresholds(BaseModel):
    """The frozen thresholds of spec.md §5, resolved from `config.toml`.

    spec.md §5: "Terms such as `recent`, `long-lived`, and `material negative
    event` are configuration with documented frozen thresholds." Nothing in
    this package may hard-code one of these numbers; they are read from here
    and they are hashed into `policy_version()`.

    `from_config` reads `[thresholds]` and lets `[policy]` override, so there
    is a single default per value and a single place (`[policy]`) to pin it
    at freeze time. See `rli.config.Policy`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recent_publish_days: int = Field(ge=0)
    long_lived_days: int = Field(ge=0)
    contradiction_days: int = Field(ge=0)
    recheck_default_days: int = Field(ge=1)
    recheck_cap_days: int = Field(ge=1)
    frozen_at: datetime | None = None

    @classmethod
    def from_config(cls, cfg: Config) -> PolicyThresholds:
        policy, thresholds = cfg.policy, cfg.thresholds
        return cls(
            recent_publish_days=(
                policy.recent_publish_days
                if policy.recent_publish_days is not None
                else thresholds.recent_publish_days
            ),
            long_lived_days=(
                policy.long_lived_days
                if policy.long_lived_days is not None
                else thresholds.long_lived_days
            ),
            contradiction_days=policy.contradiction_days,
            recheck_default_days=(
                policy.recheck_default_days
                if policy.recheck_default_days is not None
                else thresholds.recheck_default_days
            ),
            recheck_cap_days=(
                policy.recheck_cap_days
                if policy.recheck_cap_days is not None
                else thresholds.recheck_cap_days
            ),
            frozen_at=policy.frozen_at,
        )

    @classmethod
    def coerce(cls, cfg: Config | PolicyThresholds | None) -> PolicyThresholds:
        """Accept a `Config`, an already-resolved `PolicyThresholds`, or `None`.

        `None` loads the project config. Every public entry point in this
        package takes `Config | PolicyThresholds | None` so tests can pass a
        single explicit threshold object without building a whole config.
        """
        if isinstance(cfg, PolicyThresholds):
            return cfg
        if cfg is None:
            from rli.config import load_config

            return cls.from_config(load_config())
        return cls.from_config(cfg)

    def fingerprint(self) -> str:
        """Canonical serialization of the values that change decisions.

        `frozen_at` is deliberately excluded: it records *when* the policy was
        frozen and changes no decision, so freezing an unchanged policy must
        not invent a new `policy_version()`.
        """
        return "|".join(
            f"{name}={getattr(self, name)}"
            for name in (
                "recent_publish_days",
                "long_lived_days",
                "contradiction_days",
                "recheck_default_days",
                "recheck_cap_days",
            )
        )


class PolicyOutcome(BaseModel):
    """The decision core: the §1 output fields the policy alone determines.

    `evidence_quality`, `hypotheses`, `reason` and `evidence` complete a
    `rli.models.decision.Decision`; the policy does not own them.
    `branch` is internal trace detail (spec.md §2: the run trace, not the user
    output, stores controller decisions) and names the row of `PRECEDENCE`
    that fired.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    posting_state: PostingState
    recommended_action: RecommendedAction
    recheck_after_days: int | None = None
    branch: PolicyBranch


def _branch(
    inputs: PolicyInputs,
    quality: EvidenceQuality,
    long_lived: bool | Unknown,
) -> tuple[PolicyBranch, RecommendedAction]:
    """Pure branch selection — the precedence table in the module docstring.

    Kept separate from `decide` so `policy_version()` hashes exactly the
    logic that determines the action, and so the table can be tested without
    a clock.
    """
    state = inputs.posting_state
    resolved_state = None if isinstance(state, Unknown) else state
    active = resolved_state in _ACTIVE_STATES

    # P1 — closed is unconditional (spec.md §5, first row).
    if resolved_state == "closed":
        return "P1_closed", "skip"

    # P2 — we do not know the state at all (not in the §5 table; see docstring).
    if resolved_state is None or resolved_state == "unknown":
        return "P2_state_unresolved", "wait"

    # P3a — an explicit, dated freeze/pause is decisive on its own.
    if active and inputs.freeze_or_pause is True:
        return "P3a_freeze_or_pause", "wait"

    # P3b — a declared expiry, only while the current status is unresolved.
    has_declared_expiry = isinstance(inputs.declared_expiry, datetime)
    status_resolved = resolved_state == "open" and quality == "strong"
    if active and has_declared_expiry and not status_resolved:
        return "P3b_declared_expiry_unresolved", "wait"

    # P4 — repeated unchanged repost + long-lived history + weak hiring signal.
    if (
        inputs.repost_pattern == "repeated_unchanged"
        and long_lived is True
        and inputs.corroborating_hiring_signal is False
    ):
        return "P4_repeated_repost", "skip"

    # P5 — open + recent primary publish evidence + no material negative event.
    if (
        resolved_state == "open"
        and inputs.publish_recency == "recent"
        and inputs.material_negative_event is not True
        and quality == "strong"
    ):
        return "P5_open_recent_strong", "apply_now"

    # P6 — open/reposted with mixed or weak evidence.
    if active and quality in ("mixed", "weak"):
        return "P6_active_mixed_or_weak", "quick_apply"

    # P7 — otherwise.
    return "P7_default", "quick_apply"


def _recheck_after_days(
    declared_expiry: datetime | None | Unknown,
    now: datetime,
    thresholds: PolicyThresholds,
) -> int:
    """spec.md §1's rule; see the module docstring for floor/clamp rationale."""
    if not isinstance(declared_expiry, datetime):
        return thresholds.recheck_default_days
    days_until = math.floor((declared_expiry - now).total_seconds() / 86400.0)
    return max(0, min(thresholds.recheck_cap_days, days_until))


def decide(
    inputs: PolicyInputs,
    quality: EvidenceQuality,
    now: datetime,
    cfg: Config | PolicyThresholds | None = None,
    *,
    long_lived: bool | Unknown = UNKNOWN,
) -> PolicyOutcome:
    """Apply the frozen v1 action policy (spec.md §5).

    Deterministic and side-effect free: the same `(inputs, quality, now,
    thresholds, long_lived)` always yields the same `PolicyOutcome`, which is
    what lets System A, System B and System C share one frozen policy
    (spec.md §6) and what makes replay reproducible.

    Arguments:
        inputs: the seven spec.md §5 policy inputs.
        quality: `rli.policy.quality.evidence_quality`'s verdict. It is an
            argument rather than a `PolicyInputs` field because spec.md §1
            defines it as a derived output of the evidence, not an
            unresolved question a probe can answer.
        now: decision clock, timezone-aware; used only for
            `recheck_after_days`.
        cfg: `Config`, `PolicyThresholds`, or `None` for the project config.
        long_lived: `PostingHistoryFeatures.long_lived` (see module docstring).
    """
    from rli.models.time import ensure_aware

    if quality not in _EVIDENCE_QUALITIES:
        # An unrecognized quality would silently behave as "not strong" and
        # quietly suppress every apply_now, so it fails loudly instead.
        raise ValueError(
            f"evidence_quality must be one of {_EVIDENCE_QUALITIES}, got {quality!r}"
        )

    thresholds = PolicyThresholds.coerce(cfg)
    now = ensure_aware(now, "now")

    branch, action = _branch(inputs, quality, long_lived)
    state = inputs.posting_state
    posting_state: PostingState = "unknown" if isinstance(state, Unknown) else state

    return PolicyOutcome(
        posting_state=posting_state,
        recommended_action=action,
        # spec.md §1 attaches recheck_after_days to `wait` only; every other
        # action reports null rather than a number the user should ignore.
        recheck_after_days=(
            _recheck_after_days(inputs.declared_expiry, now, thresholds)
            if action == "wait"
            else None
        ),
        branch=branch,
    )


# The functions whose source defines the policy. A change to any of them must
# change `policy_version()`; a change anywhere else in this module (docstrings
# included) must not.
_VERSIONED_FUNCTIONS = (_branch, _recheck_after_days)


def policy_version(cfg: Config | PolicyThresholds | None = None) -> str:
    """A stable id for "this policy, with these thresholds".

    spec.md §6 requires A, B and C to share one frozen policy and requires
    replay to be reproducible; a run that records only "the policy" cannot be
    audited after a threshold moves. This hashes both halves of what actually
    determines an action: the source of the branch/recheck functions, and the
    frozen threshold values (`PolicyThresholds.fingerprint`, which excludes
    `frozen_at`).

    Reformatting `_branch` changes the version even if behaviour is
    identical — accepted deliberately: a false "the policy changed" is
    cheap to investigate, a false "nothing changed" is a silent evaluation
    bug. Requires importable source (a source checkout, not a zipimport).
    """
    thresholds = PolicyThresholds.coerce(cfg)
    digest = hashlib.blake2b(digest_size=16)
    for function in _VERSIONED_FUNCTIONS:
        digest.update(inspect.getsource(function).encode("utf-8"))
        digest.update(b"\x00")
    digest.update(repr(PRECEDENCE).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(thresholds.fingerprint().encode("utf-8"))
    return f"policy-v1:{digest.hexdigest()}"
