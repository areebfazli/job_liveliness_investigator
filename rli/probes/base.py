"""`Probe` base class and execution context (spec.md §2/§4; PLAN.md M1/M3).

A probe is a pure function of `(args, ctx) -> ProbeResult`: it may read the
network (through `ctx`'s `NetClient` factory, which binds the probe's own
config-declared allowlist) and, for history-gated probes, read the database
for lookups — but it never writes to the database and never raises for a
network/parse problem (spec.md §2: "Structured probe failure ...";
this task: "Do not write to DB inside probes; return data, let the caller
persist ... so replay can cache them"). Persistence lives in
`rli.probes.persist`, called by whatever drives the probe (agent loop /
snapshot cron), not by the probe itself.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel

from rli.config import Config
from rli.models.probe import ProbeResult
from rli.models.time import now_utc
from rli.net import NetClient

__all__ = ["CostTier", "Probe", "ProbeClaim", "ProbeContext"]

CostTier = Literal["low", "medium", "high"]

# The probe-name strings this codebase currently defines. Kept here (rather
# than importing `rli.config.Allowlists.model_fields` at class-definition
# time) so `Probe.name` type-checks against a concrete literal; every value
# must still exist as a field on `Allowlists` or `Probe.allowlist` raises.
_KNOWN_PROBE_NAMES = (
    "resolve_posting",
    "board_snapshot",
    "repost_history",
    "requirements_drift",
    "company_events",
    "team_signal",
    "json_ld",
)


class ProbeClaim(BaseModel):
    """One fact a probe observed, shaped like `EvidenceItem` minus `id`/`run_id`/`probe`.

    The caller that drives a probe (agent loop step, or a direct probe
    invocation) assigns the run-local `id` (`"e1"`, `"e2"`, ...), the
    `run_id`, and already knows the `probe` name — so probes return
    `ProbeClaim`s and the caller upgrades each into a full `EvidenceItem`
    (spec.md §3). Field names and types otherwise match `EvidenceItem`
    exactly so that upgrade is a straight `EvidenceItem(id=..., run_id=...,
    probe=..., **claim.model_dump())`.
    """

    claim_type: str
    value: str
    source_url: str
    raw_excerpt: str | None = None
    source_quality: Literal["ats_native", "page_structured", "archive", "news", "enrichment"]
    source_event_at: datetime | None = None
    available_at: datetime
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class ProbeContext:
    """Everything a probe needs besides its own validated `args`.

    * `conn` — a read-only-by-convention SQLite connection, for probes that
      need history lookups (e.g. `repost_history` checking whether usable
      history exists). Probes must not write through it.
    * `config` — the loaded `Config` (thresholds, allowlists, budgets).
    * `net_client_factory` — builds a `NetClient` bound to one probe name's
      allowlist (`Config.allowlists.<name>`), e.g. `ctx.net_client("json_ld")`
      to fetch a career page under the permissive `json_ld` allowlist even
      from inside the `resolve_posting` probe.
    * `now` — injectable clock, so `available_at`/`fetched_at` are
      deterministic in tests and consistent across one probe run.
    * `collection_status_csv` — the pinned location of a piece of
      PRE-COLLECTED CORPUS REFERENCE DATA (`company_events`' collection-status
      file). It is in the same category as `conn`: a handle on the corpus
      this run is reasoning against, chosen by whoever opened the run, and
      identical for every probe in it. `None` means "wherever the probe's own
      default points", which is the repo's `data/events/collection_status.csv`.

      It is deliberately NOT part of any probe's `args`, and therefore not
      part of `args_hash`. `rli.eval.runner.ProbeRunner.execute` hashes
      `args.model_dump()` into `args_hash`, and `rli.replay.build` stores the
      `company_events` dataset record under that hash while `rli.replay.run`
      looks it up: a machine-local filesystem path inside the args would make
      every stored record machine-specific and every lookup miss on another
      checkout. The path pins WHICH corpus is read; it is not part of the
      QUESTION being asked, which is what `args_hash` identifies.
    """

    conn: sqlite3.Connection
    config: Config
    net_client_factory: Callable[[str], NetClient]
    now: Callable[[], datetime] = field(default=now_utc)
    collection_status_csv: str | Path | None = None

    def net_client(self, probe_name: str) -> NetClient:
        return self.net_client_factory(probe_name)


class Probe(ABC):
    """Base class for all probes (spec.md §4 "Always run" / "Dynamic probes").

    Subclasses set the `ClassVar`s below and implement `run`. `ArgsModel` is
    the Pydantic model `run`'s `args` must already be validated against —
    validation itself happens at the call site (typically the controller),
    not inside `run`, so a schema violation surfaces as a `pydantic.
    ValidationError` before any network call is attempted.
    """

    name: ClassVar[str]
    cost_tier: ClassVar[CostTier]
    history_required: ClassVar[bool] = False
    ArgsModel: ClassVar[type[BaseModel]]

    @classmethod
    def allowlist(cls, config: Config) -> list[str]:
        """This probe's network-domain allowlist from `config.toml` (spec.md §2)."""
        if cls.name not in _KNOWN_PROBE_NAMES:
            raise ValueError(
                f"probe {cls.name!r} is not a known probe name; expected one of "
                f"{_KNOWN_PROBE_NAMES}"
            )
        return list(getattr(config.allowlists, cls.name))

    @abstractmethod
    def run(self, args: BaseModel, ctx: ProbeContext) -> ProbeResult:
        """Execute the probe. Must never raise for a network/parse problem."""
        raise NotImplementedError
