"""rli.replay — point-in-time replay of the spec.md §6 systems (PLAN.md M4).

spec.md §6 "Replay" states four rules for a simulated system at historical
time `T`, and this package is those four rules plus the tooling to build,
run and audit a dataset against them:

```text
* expose only evidence with `available_at <= T`
* expose a dynamic result only if the simulated system selects that probe
* live **tool** calls are forbidden in replay; results come only from the
  cached full-probe record
* live **LLM** calls are allowed on cache miss ... and are recorded
```

* `mode` — the cached full-probe record, the `available_at <= T` gate, the
  exposure log, and the forbidden network (rules 1-3).
* `pit` — the point-in-time view of the COLLECTION CORPUS: the half of "what
  could this system know at `T`" that is not evidence, and the one the
  evidence gate cannot enforce.
* `build` — collects the record (System A, live, once per posting), lays the
  evaluation grid, and derives the archive-era board state.
* `run` — replays a system (A, B, or any callable with the same contract)
  over a dataset.
* `leakage` — audits the resulting trace and produces spec.md §6's
  "future-leakage violations (`0` target)".

The dependency direction is one-way: `rli.replay` knows about `rli.eval`,
and `rli.eval` knows only about `rli.eval.runner.ReplayHook` — a three-field
record with no import from this package. That is what keeps a live run's code
path free of every line of replay machinery.

`rli.replay.build` is the ONLY module here that touches the network, and only
while collecting the record. Everything after it — every replayed run, every
metric, every audit — reaches the network never.
"""

from rli.replay.build import (
    DEFAULT_GRID_STEP_DAYS,
    BuildSummary,
    archive_state_claims,
    build_dataset,
    capture_date_claims,
    case_state_at,
    grid_times,
)
from rli.replay.leakage import LeakageReport, Violation, check_dataset, net_call_count
from rli.replay.mode import (
    ARCHIVE_BOARD_STATE_PROBE,
    ReplayContext,
    ReplayProbeRunner,
    ReplayProbeStore,
    ReplayViolation,
    replay_hook,
)
from rli.replay.pit import point_in_time
from rli.replay.run import ReplayRunSummary, run_replay

__all__ = [
    "ARCHIVE_BOARD_STATE_PROBE",
    "DEFAULT_GRID_STEP_DAYS",
    "BuildSummary",
    "LeakageReport",
    "ReplayContext",
    "ReplayProbeRunner",
    "ReplayProbeStore",
    "ReplayRunSummary",
    "ReplayViolation",
    "Violation",
    "archive_state_claims",
    "build_dataset",
    "capture_date_claims",
    "case_state_at",
    "check_dataset",
    "grid_times",
    "net_call_count",
    "point_in_time",
    "replay_hook",
    "run_replay",
]
