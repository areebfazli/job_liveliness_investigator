"""Typed configuration loader for `config.toml`.

Loads the project configuration via stdlib `tomllib` and validates it into a
tree of Pydantic v2 models. All threshold-like values in `config.toml` are
documented there as placeholders pending real tuning (see spec.md §5).

Two invariants shape this module:

* **Fail loudly.** spec.md §2 requires enforced allowlists, validated
  arguments and hard budgets. A silently-ignored typo in `config.toml`
  (`resolve_postings = [...]`, `max_retires = 3`) would disable exactly those
  guarantees without any error, so every model sets `extra="forbid"` and
  every numeric knob carries the constraint that makes a nonsensical value
  impossible rather than merely unlikely.
* **No naive datetimes.** `policy.frozen_at` participates in the
  point-in-time replay timeline (spec.md §3/§6) and is normalized through
  `rli.models.time.ensure_aware`, exactly like the model timestamps.
"""

from __future__ import annotations

import os
import tomllib
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rli.models.time import ensure_aware

__all__ = [
    "DEFAULT_CONFIG_FILENAME",
    "REPO_ROOT",
    "Allowlists",
    "Budgets",
    "Config",
    "Net",
    "Policy",
    "ProbeCosts",
    "RateLimit",
    "TeamSignal",
    "Thresholds",
    "load_config",
]

# Repo root derived module-relatively (`rli/config.py` -> `rli/` -> repo root)
# rather than from the process CWD, so `load_config()` resolves the same file
# whether it is called from the repo, from a cron job, or from a test whose
# CWD is a tmpdir.
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_FILENAME = "config.toml"

# Environment variable that overrides the default config location. An
# installed-package layout (site-packages) has no repo root next to the
# module, so a deployed install MUST set this.
CONFIG_ENV_VAR = "RLI_CONFIG"


class Thresholds(BaseModel):
    """Deterministic thresholds used by history features and the action policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # spec.md §1: recheck_after_days = min(recheck_cap_days,
    # days_until_validThrough) when a declared expiry exists, else
    # recheck_default_days. The `14` in the spec text is the cap; it lives
    # here rather than as a literal in the policy code.
    recheck_default_days: int = Field(ge=1)
    recheck_cap_days: int = Field(ge=1)

    max_dynamic_steps: int = Field(ge=0)
    recent_publish_days: int = Field(ge=0)
    long_lived_days: int = Field(ge=0)

    # Similarity scores are ratios in [0, 1] (spec.md §4 repost matching).
    repost_title_similarity: float = Field(ge=0.0, le=1.0)
    repost_description_similarity: float = Field(ge=0.0, le=1.0)
    repost_team_similarity: float = Field(ge=0.0, le=1.0)
    repost_location_similarity: float = Field(ge=0.0, le=1.0)

    # Added for rli.history.matching (PLAN.md M2 #2). The four
    # `repost_*_similarity` knobs above stay the per-component minimums; only
    # these two genuinely new knobs are added, rather than duplicating them
    # into a separate `[matching]` table. Both carry defaults so a config
    # predating them (e.g. an inline TOML fixture) keeps validating, the same
    # convention `negative_event_window_days` below follows.
    #
    # Minimum weighted-mean similarity across the KNOWN components for a
    # repost link to be accepted. PLACEHOLDER — pending the hand-checked
    # 50-match precision validation in spec.md §4.
    repost_combined_min: float = Field(0.70, ge=0.0, le=1.0)
    # Maximum days from a posting's `first_seen_absent` to a candidate
    # repost's `first_observed` for that candidate to be considered at all.
    repost_max_gap_days: int = Field(120, ge=0)

    # Minimum observed history span (days) before rli.history.features will
    # express an opinion on a history-derived input such as `repost_pattern`.
    # Below it the feature is the `rli.models.policy_inputs.UNKNOWN` sentinel,
    # never a guessed value (spec.md §4: "missing history never means flat
    # hiring"). PLACEHOLDER — not yet tuned.
    min_history_days: int = Field(30, ge=0)

    # spec.md §5: lookback window (days) within which a `company_events` row
    # can still populate `material_negative_event` / `freeze_or_pause`.
    # Defaults to 180 so configs that predate this field (e.g. inline TOML
    # fixtures in tests) keep validating unchanged.
    negative_event_window_days: int = Field(180, ge=1)


class Budgets(BaseModel):
    """Hard caps on cost/latency for a single investigation run (spec.md §4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cost_usd: float = Field(gt=0.0)
    max_latency_s: float = Field(gt=0.0)


class RateLimit(BaseModel):
    """Per-host token-bucket rate limit parameters.

    `requests_per_second <= 0` would make the bucket refill never (an
    unbounded hang) and `burst < 1` would make it impossible to ever acquire
    a token, so both are rejected here rather than discovered as a stalled
    snapshot job (spec.md §4: per-host rate limits with backoff).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    requests_per_second: float = Field(gt=0.0)
    burst: int = Field(ge=1)


class Net(BaseModel):
    """Network retry/backoff/timeout knobs used by `rli.net` (spec.md §2).

    These live in config rather than as call-site defaults so that "no
    uncontrolled retry loops" is a single auditable setting instead of a
    value that drifts between probes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Retries *after* the initial attempt: total attempts = max_retries + 1.
    # 0 is legal and means "attempt once, never retry".
    max_retries: int = Field(ge=0)

    # Exponential backoff: delay ceiling = min(max_backoff_s,
    # backoff_base_s * 2**attempt), with jitter applied by rli.net.
    backoff_base_s: float = Field(gt=0.0)
    max_backoff_s: float = Field(gt=0.0)

    timeout_s: float = Field(gt=0.0)

    # Maximum redirect hops followed; each hop is allowlist-checked.
    max_redirects: int = Field(ge=0)

    # Token-bucket defaults for hosts with no explicit [rate_limits] entry.
    default_requests_per_second: float = Field(gt=0.0)
    default_burst: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_backoff_bounds(self) -> Net:
        # Cross-field rule, so it cannot be expressed as a Field constraint:
        # a ceiling below the base would silently clamp every retry to a
        # constant delay, defeating exponential backoff.
        if self.max_backoff_s < self.backoff_base_s:
            raise ValueError(
                f"net.max_backoff_s ({self.max_backoff_s}) must be >= "
                f"net.backoff_base_s ({self.backoff_base_s})"
            )
        return self


class Allowlists(BaseModel):
    """Per-probe network-domain allowlists (spec.md §2).

    `extra="forbid"` matters most here: a misspelled probe key would
    otherwise be dropped silently, leaving the real probe with an empty
    allowlist (every fetch refused) or — worse, if the misspelling is on a
    probe that defaults to `[]` — no visible sign that the intended hosts
    were never applied.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolve_posting: list[str] = []
    board_snapshot: list[str] = []
    repost_history: list[str] = []
    requirements_drift: list[str] = []
    company_events: list[str] = []
    team_signal: list[str] = []
    # JSON-LD `datePosted` (spec.md §3) is read from arbitrary employer
    # career pages, so this list is expected to be ["*"]. The remaining
    # hardening in rli.net.check_allowed (https-only, no userinfo, no IP
    # literals, no private/loopback/link-local targets) still applies.
    json_ld: list[str] = []
    # rli/archive backfill (M1 PLAN bullet 4): Wayback CDX + capture fetch.
    archive_backfill: list[str] = []


class ProbeCosts(BaseModel):
    """Numeric cost + latency estimates per `rli.probes.base.CostTier` (PLAN.md M3).

    `rli.probes.registry` reads these to rank eligible dynamic probes and to
    check them against `[budgets].max_cost_usd`/`max_latency_s`. Values are
    PLACEHOLDERS (arbitrary unitless cost points, and rough wall-clock
    seconds) pending real measurement from live probe runs; all carry
    defaults so a config predating this table keeps validating, the same
    convention `Thresholds.negative_event_window_days` established.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    low: int = Field(1, gt=0)
    medium: int = Field(3, gt=0)
    high: int = Field(10, gt=0)

    latency_low_s: float = Field(2.0, gt=0.0)
    latency_medium_s: float = Field(5.0, gt=0.0)
    latency_high_s: float = Field(12.0, gt=0.0)

    def value_for(self, tier: str) -> int:
        return {"low": self.low, "medium": self.medium, "high": self.high}[tier]

    def latency_for(self, tier: str) -> float:
        return {
            "low": self.latency_low_s,
            "medium": self.latency_medium_s,
            "high": self.latency_high_s,
        }[tier]


class TeamSignal(BaseModel):
    """`team_signal` probe feature flag (spec.md §4; PLAN.md M3).

    No licensed enrichment source exists yet (`rli.probes.team_signal.
    NullTeamSignalSource`), so this defaults to `False`: an unlicensed
    deployment never attempts the probe, and `corroborating_hiring_signal`
    (spec.md §5) stays the `UNKNOWN` sentinel. Flip to `True` only once a
    real `TeamSignalSource` is wired in.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False


class Policy(BaseModel):
    """Action-policy freeze state and frozen policy thresholds (spec.md §5).

    spec.md §5: "Terms such as `recent`, `long-lived`, and `material negative
    event` are configuration with documented frozen thresholds." This table
    is where those get frozen.

    Only ONE knob here is genuinely new — `contradiction_days`, which no
    other layer needs. The remaining four are `None`-by-default **overrides**
    of the identically named `[thresholds]` keys, not copies of them:
    `rli.policy.action.PolicyThresholds.from_config` reads `[thresholds]`
    unless `[policy]` names a value. Duplicating the numbers outright would
    create two sources of truth that silently disagree; leaving them only in
    `[thresholds]` would make it impossible to freeze the policy's notion of
    "recent" (spec.md §5) without also moving the history layer's. The
    override keeps one default and one auditable place to pin it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    frozen_at: datetime | None = None

    # Two `first_published` claims from different sources are treated as a
    # material contradiction (-> evidence_quality "mixed") when they differ
    # by more than this many days. Not an override: only the policy layer
    # has any use for it. Defaults so configs predating it (inline TOML
    # fixtures in tests) keep validating, matching the convention used by
    # `Thresholds.negative_event_window_days`.
    contradiction_days: int = Field(3, ge=0)

    # `None` = inherit the `[thresholds]` value of the same name.
    recent_publish_days: int | None = Field(None, ge=0)
    long_lived_days: int | None = Field(None, ge=0)
    recheck_default_days: int | None = Field(None, ge=1)
    recheck_cap_days: int | None = Field(None, ge=1)

    @field_validator("frozen_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime | None) -> datetime | None:
        # A naive freeze timestamp cannot be compared against the replay
        # clock (spec.md §3/§6), and TOML happily produces one from
        # `frozen_at = 2026-01-01T00:00:00` (no offset).
        if value is None:
            return None
        return ensure_aware(value, "policy.frozen_at")


class Config(BaseModel):
    """Root configuration object produced by `load_config`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thresholds: Thresholds
    budgets: Budgets
    net: Net
    rate_limits: dict[str, RateLimit]
    allowlists: Allowlists
    policy: Policy
    probe_costs: ProbeCosts = Field(default_factory=ProbeCosts)
    team_signal: TeamSignal = Field(default_factory=TeamSignal)


def _resolve_config_path(path: str | Path | None) -> Path:
    """Resolve the config path. Precedence: argument > `$RLI_CONFIG` > repo root."""
    if path is not None:
        return Path(path).expanduser()

    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()

    return REPO_ROOT / DEFAULT_CONFIG_FILENAME


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate the project configuration into a `Config`.

    Resolution order:

    1. an explicit `path` argument,
    2. the `RLI_CONFIG` environment variable,
    3. `<repo root>/config.toml`, where the repo root is derived
       module-relatively (`Path(__file__).resolve().parent.parent`).

    The repo-root fallback deliberately ignores the process CWD so that a
    cron snapshot job (spec.md §4) and a pytest run in a tmpdir load the
    same file. That fallback only exists for a source checkout: an installed
    package has no `config.toml` next to `rli/`, so a deployed install must
    set `RLI_CONFIG`.

    Raises `FileNotFoundError` naming the resolved path, and
    `pydantic.ValidationError` for any unknown key or out-of-range value.
    """
    resolved = _resolve_config_path(path)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"config file not found at {str(resolved)!r} "
            f"(pass an explicit path, set ${CONFIG_ENV_VAR}, or add "
            f"{DEFAULT_CONFIG_FILENAME} to the repo root {str(REPO_ROOT)!r})"
        )

    with resolved.open("rb") as f:
        raw = tomllib.load(f)
    return Config.model_validate(raw)
