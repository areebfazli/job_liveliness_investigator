"""The point-in-time view of the COLLECTION CORPUS (spec.md §6; PLAN.md M4).

spec.md §6 states four replay rules. `rli.replay.mode` implements three of
them — the `available_at <= T` evidence gate, the "only if the system selects
that probe" exposure rule, and the ban on live tool calls. This module is the
fourth, and it is the one that is easy to forget because nothing about it
looks like a tool call:

    A system replayed at historical time `T` must not read a corpus that
    contains observations made after `T`.

The evidence gate alone does not achieve that. Evidence is only one of the
two things a system reads; the other is the collection corpus itself, and
`rli.eval.case` reads it in five decision-affecting places:

* `rli.history.features.posting_features` — `first_seen_absent`,
  `reappeared_at`, `long_lived`, `repost_pattern`. Every one of these is a
  lifecycle aggregate summarizing the WHOLE capture history, including
  captures taken after `T`. Replaying 2026-01 against a `postings` row whose
  `first_seen_absent` is 2026-06 hands the policy the future.
* `rli.history.features.coverage_window` — `history_days`, which gates
  `repost_pattern`. (It no longer gates `long_lived`: spec.md §5's Amendment
  2026-09-10 replaced that hedge with `age_days`, which `posting_features`
  now measures from the earliest of the publish date, the earliest ARCHIVE
  capture in `posting_snapshots` / `board_snapshot_jobs`, and
  `first_observed` — all three of which this module already shadows.)
* `rli.eval.case`'s refresh match — the `content_hash` /`description_hash`
  series in `posting_snapshots` and `board_snapshot_jobs`. A capture taken
  after `T` must not be allowed to corroborate a refresh at `T`; both tables
  are shadowed, so it cannot.
* `rli.probes.lookups.has_usable_history` and each probe's `eligible()` —
  which decide WHICH probes a system may run, i.e. spec.md §6's
  medium/high-cost probe count. A company that had six days of history at
  `T` and has six months now would make `repost_history` eligible in replay
  when it could not have been.
* `rli.events.policy_signals.derive_policy_signals` — already `as_of`-gated
  on `available_at`, and correct on its own, but it reads the same
  `company_events` table this module filters, so filtering costs nothing and
  keeps one rule instead of two.

--------------------------------------------------------------------------
How: temp-schema shadowing, not a copy of the database
--------------------------------------------------------------------------

SQLite resolves an unqualified table name by searching `temp` first, then
`main`, then attached schemas. So a `TEMP VIEW` named `board_snapshots`
shadows `main.board_snapshots` for every unqualified read on that
connection — including reads made by code that has never heard of replay
(`rli.history.closures`, `rli.history.features`, `rli.probes.lookups`). That
is the whole trick, and it is why this module needs no edit anywhere else.

What is deliberately NOT shadowed: `runs`, `run_steps`, `evidence`,
`replay_datasets`, `replay_cases`, `replay_probe_results`, `tool_cache`,
`llm_cache`, `companies`, `outcomes`. The first three are what the replay run
WRITES (they must land in the real database, and they do, because no temp
object shadows them); the replay tables are the cached record the run reads
its probe results from, which is not corpus; `companies` is an identity
registry, not an observation (see the judgment call below).

Foreign keys are unaffected: SQLite resolves `evidence.posting_id ->
postings(posting_id)` against `main`, never against a shadowing temp table,
so a replay run's evidence rows still reference the real `postings`.

--------------------------------------------------------------------------
`postings` is a TEMP TABLE, not a view, and its lifecycle is RE-DERIVED
--------------------------------------------------------------------------

The five lifecycle columns (`first_observed`, `last_seen_open`,
`first_seen_absent`, `reappeared_at`, `replacement_job_id`) are aggregates
over the whole capture history; there is no `WHERE ... <= T` that makes them
point-in-time, because the answer is a different aggregate, not a subset of
rows. So `postings` is copied into `temp`, its lifecycle columns are set to
NULL, and `rli.history.closures.apply_to_postings` re-derives them from the
captures that survive the filter — the same code, the same merge rule, the
same interval-censored semantics as the live pipeline. Re-implementing that
derivation here would create a second source of truth about what
`first_seen_absent` means, which is exactly the failure this codebase avoids
everywhere else.

--------------------------------------------------------------------------
The connection must never hold a read snapshot across the `yield`
--------------------------------------------------------------------------

Installing the context WRITES: `_install_postings` runs `UPDATE postings`
(twice, through `apply_to_postings`) and `_install_repost_links` re-points
`replacement_job_id`. Under Python's legacy sqlite3 transaction control a DML
statement opens an implicit `BEGIN`, and that `BEGIN` takes a WAL read
snapshot of `main` — even though every one of those writes lands in `temp`,
because the statements read `main` to produce them.

Handing that open transaction back at the `yield` is the failure this
context has to prevent. The block's first write (a replay run's
`INSERT INTO runs`) then has to upgrade read->write, and SQLite refuses the
upgrade with `SQLITE_BUSY_SNAPSHOT` (517) if any other connection committed
to `main` after the snapshot was taken: the writer cannot be shown a
database that differs from the one it has already been reading. That refusal
is NOT a lock contention, so `busy_timeout` (5 s, set by
`rli.db.connect`) does not retry it — it fails in ~0 ms, and it fails with
the text "database is locked", which reads like contention and is not.
Worse, nothing resets the snapshot afterwards, so every remaining case at
that `T` fails the same way on a connection that is wedged rather than busy.

So this context commits immediately before the `yield`, and rolls back if
the block raises. The commit is safe for the shadows and has to come AFTER
all three installers: temp objects live in the `temp` schema for the life of
the CONNECTION, not of the transaction, so `COMMIT` keeps them (and a later
`ROLLBACK` inside the block keeps them too) — but a rollback of the
transaction that CREATED them would take them with it, which is exactly what
the exception path wants, and what makes a half-installed context tear
itself down.

`rli.replay.run` holds one connection for a whole multi-day System C replay,
so the invariant is that connection's: it may hold a read snapshot only for
as long as one statement needs it, never across a case.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **The `postings` ROW is kept even for a posting we had not seen at `T`;
  only its observational columns are nulled.** A `postings` row is an
  identity registry entry (`"{ats}:{tenant}:{job_id}"`), not an observation:
  `rli.eval.case._resolve_identity` uses it to answer "what company is this
  ATS tenant?", which is not a point-in-time fact and does not become one by
  being learned later. Deleting the row would change `company_id` resolution
  and therefore which company's history a case is scored against — a much
  larger distortion than keeping an identity we happen to know. What the row
  no longer carries is any claim about when the posting was seen, which is
  the part that would leak.

* **`companies` is not filtered at all**, for the same reason, and because
  `postings.company_id` and `board_snapshots.company_id` are foreign keys
  into it; filtering it would break the corpus rather than restrict it.

* **A `repost_links` row survives only when both its endpoints are visible
  at `T`.** The link's own `matched_at` is when WE ran the matcher, which is
  "now" for every row in the table — filtering on it would delete every link
  at every archive-era `T` and report `repost_pattern='none'` (a positive
  claim: "closed and nothing matched") for every closed posting, which is a
  fabricated negative, not caution. The honest reading is "could this link
  have been computed at `T`?", and `rli.history.matching` needs exactly two
  things to compute one: the old posting observed absent, and the new
  posting observed at all. Both are re-derived above, so the test is a join
  against the point-in-time lifecycle. `replacement_job_id` is then restored
  from the surviving links under the same tiebreak
  (`combined_score DESC, new_posting_id`) that
  `rli.history.features._classify_repost_pattern` and
  `rli.probes.repost_history._best_repost_link` use, so all three agree on
  which link is "the" link.

* **The re-derivation is scoped to `company_ids` when given.** Re-deriving
  every company's lifecycle costs a full pass over every capture, per `T`,
  and a replay case only ever reads its own company's history. Companies
  outside the scope keep NULL lifecycle columns — which reads downstream as
  "no history", the conservative value, never as a fabricated one. Pass
  `company_ids=None` to re-derive everything.

* **Cases are grouped by `T`, one context per `T`.** Installing the view set
  is cheap but re-deriving lifecycles is not; `rli.replay.run` therefore
  opens one `point_in_time` per distinct `T` and replays every case at that
  `T` inside it.

* **Nesting is refused, not silently ignored.** A nested `point_in_time`
  would either see the outer context's already-filtered corpus (double
  filtering, wrong answer) or clobber it on exit. `PointInTimeError` names
  the objects that are already installed instead.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime

from rli.history.closures import apply_to_postings
from rli.models.time import ensure_aware, to_utc_z

__all__ = [
    "PIT_SHADOWED_TABLES",
    "PointInTimeError",
    "installed_shadows",
    "point_in_time",
]

#: Every corpus table this module shadows in the `temp` schema. Exported so a
#: test (and `rli.replay.leakage`) can assert the set rather than restate it.
PIT_SHADOWED_TABLES: tuple[str, ...] = (
    "board_snapshots",
    "board_snapshot_jobs",
    "posting_snapshots",
    "capture_attempts",
    "company_events",
    "postings",
    "repost_links",
)

_LIFECYCLE_COLUMNS = (
    "first_observed",
    "last_seen_open",
    "first_seen_absent",
    "reappeared_at",
    "replacement_job_id",
)


class PointInTimeError(RuntimeError):
    """A point-in-time corpus view could not be installed or removed cleanly."""


def installed_shadows(conn: sqlite3.Connection) -> list[str]:
    """The `PIT_SHADOWED_TABLES` names currently present in the `temp` schema."""
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM temp.sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    return [name for name in PIT_SHADOWED_TABLES if name in names]


def _drop_shadows(conn: sqlite3.Connection) -> None:
    """Remove every shadow this module installed, whatever kind it is.

    `DROP VIEW` on a table (and vice versa) is an error in SQLite, and
    `postings` is a table while the rest are views — so the kind is read back
    from `temp.sqlite_master` rather than assumed. Names come from
    `PIT_SHADOWED_TABLES`, never from the database, so the interpolation is
    over a fixed literal set.
    """
    kinds = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, type FROM temp.sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    for name in PIT_SHADOWED_TABLES:
        kind = kinds.get(name)
        if kind == "view":
            conn.execute(f"DROP VIEW temp.{name}")
        elif kind == "table":
            conn.execute(f"DROP TABLE temp.{name}")


# SQLite forbids bound parameters inside a VIEW definition ("parameters are
# not allowed in views"), so `T` has to be inlined as a literal. It is
# validated against the exact shape `rli.models.time.to_utc_z` produces before
# it is interpolated — not because `T` is user input (it never is; it comes
# from `replay_cases.replay_at`), but because "we inline a timestamp into SQL"
# must be a checked statement rather than an assumed one.
_UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def _sql_timestamp_literal(stamp: str) -> str:
    if not _UTC_Z.match(stamp):
        raise PointInTimeError(
            f"refusing to inline {stamp!r} into a point-in-time view definition: "
            "it is not a to_utc_z timestamp (YYYY-MM-DDTHH:MM:SS.ffffffZ)"
        )
    return f"'{stamp}'"


def _install_views(conn: sqlite3.Connection, stamp: str) -> None:
    """Row-level `<= T` filters for the five append-only capture tables.

    Each table is filtered on the column that records WHEN the observation
    was made, never on when the row was written: `board_snapshots.captured_at`
    (a Wayback capture backfilled today still belongs to its capture date),
    `capture_attempts.attempted_at`, and `company_events.available_at` — the
    column spec.md §3 defines the replay window on.

    `board_snapshot_jobs` has no timestamp of its own; it is filtered through
    its parent capture, which is the only correct reading (a job row's
    observation time IS its capture's).
    """
    literal = _sql_timestamp_literal(stamp)
    conn.execute(
        "CREATE TEMP VIEW board_snapshots AS "
        f"SELECT * FROM main.board_snapshots WHERE captured_at <= {literal}"
    )
    conn.execute(
        "CREATE TEMP VIEW board_snapshot_jobs AS "
        "SELECT j.* FROM main.board_snapshot_jobs AS j "
        "JOIN main.board_snapshots AS s ON s.id = j.board_snapshot_id "
        f"WHERE s.captured_at <= {literal}"
    )
    conn.execute(
        "CREATE TEMP VIEW posting_snapshots AS "
        f"SELECT * FROM main.posting_snapshots WHERE captured_at <= {literal}"
    )
    conn.execute(
        "CREATE TEMP VIEW capture_attempts AS "
        f"SELECT * FROM main.capture_attempts WHERE attempted_at <= {literal}"
    )
    conn.execute(
        "CREATE TEMP VIEW company_events AS "
        f"SELECT * FROM main.company_events WHERE available_at <= {literal}"
    )


def _install_postings(
    conn: sqlite3.Connection, moment: datetime, company_ids: Sequence[str] | None
) -> None:
    """Copy `postings`, blank its lifecycle, and re-derive it from surviving captures."""
    conn.execute("CREATE TEMP TABLE postings AS SELECT * FROM main.postings")
    conn.execute("CREATE INDEX temp.idx_pit_postings_id ON postings (posting_id)")
    conn.execute("CREATE INDEX temp.idx_pit_postings_company ON postings (company_id, ats_job_id)")
    conn.execute(
        "UPDATE postings SET " + ", ".join(f"{column} = NULL" for column in _LIFECYCLE_COLUMNS)
    )

    targets = (
        list(company_ids)
        if company_ids is not None
        else [row[0] for row in conn.execute("SELECT DISTINCT company_id FROM board_snapshots")]
    )
    for company_id in targets:
        # The live derivation, verbatim, against the filtered captures. It
        # writes only to `postings`, which is the temp copy.
        apply_to_postings(conn, company_id, now=moment)

    # `apply_to_postings` creates a row for any job seen in a capture that has
    # no `postings` row. In the temp copy that would be a posting the real
    # database does not have, and `rli.eval.runner.Run.set_posting_id` would
    # then try to point `runs.posting_id` at an id `main.postings` lacks,
    # failing the foreign key. The corpus this module models is a RESTRICTION
    # of the real one; it may never contain more.
    conn.execute(
        "DELETE FROM postings WHERE posting_id NOT IN (SELECT posting_id FROM main.postings)"
    )


def _install_repost_links(conn: sqlite3.Connection) -> None:
    """Keep only links whose two endpoints are both visible at `T`, then re-point
    `postings.replacement_job_id` at the surviving winner (see the docstring)."""
    conn.execute(
        """
        CREATE TEMP VIEW repost_links AS
        SELECT l.*
        FROM main.repost_links AS l
        JOIN temp.postings AS old_p ON old_p.posting_id = l.old_posting_id
        JOIN temp.postings AS new_p ON new_p.posting_id = l.new_posting_id
        WHERE old_p.first_seen_absent IS NOT NULL
          AND new_p.first_observed IS NOT NULL
        """
    )
    conn.execute(
        """
        UPDATE postings
           SET replacement_job_id = (
               SELECT l.new_posting_id
               FROM repost_links AS l
               WHERE l.old_posting_id = postings.posting_id
               ORDER BY l.combined_score DESC, l.new_posting_id
               LIMIT 1
           )
        """
    )


@contextmanager
def point_in_time(
    conn: sqlite3.Connection,
    moment: datetime,
    *,
    company_ids: Sequence[str] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Restrict `conn`'s corpus reads to observations made at or before `moment`.

    Yields the SAME connection: everything this context does is installed in
    its `temp` schema, so a caller passes the connection on unchanged and
    every unqualified corpus read inside the block — including reads made by
    `rli.history`, `rli.probes` and `rli.eval.case`, none of which know this
    module exists — sees the point-in-time corpus.

    Writes are unaffected. `runs`, `run_steps` and `evidence` are not
    shadowed, so a replay run recorded inside this block lands in the real
    database exactly as a live run would.

    The connection is COMMITTED immediately before the yield and rolled back
    if the block raises, because it must never hold a read snapshot across
    the yield: the installers write, an implicit `BEGIN` therefore takes a
    WAL read snapshot of `main`, and the block's first write would have to
    upgrade it — which SQLite refuses outright (`SQLITE_BUSY_SNAPSHOT`, not
    retried by `busy_timeout`) once any other connection has committed to
    `main`, wedging the connection for every later case at this `T`. The
    module docstring has the full account; the temp shadows are unaffected by
    either the commit or the rollback.

    The boundary is INCLUSIVE (`<= T`), matching spec.md §3's
    `available_at <= T`: an observation made at exactly `T` was available
    at `T`.

    Arguments:
        conn: the corpus connection. Must not already be inside a
            `point_in_time` block (`PointInTimeError`).
        moment: spec.md §6's historical time `T`. Must be timezone-aware —
            a naive datetime cannot be placed on the replay timeline
            (`rli.models.time`).
        company_ids: restrict the lifecycle re-derivation to these companies
            (see the module docstring's scoping judgment call). `None`
            re-derives every company that has a surviving capture.
    """
    moment = ensure_aware(moment, "moment")
    already = installed_shadows(conn)
    if already:
        raise PointInTimeError(
            "a point-in-time corpus view is already installed on this connection "
            f"(temp objects: {already}); nesting would double-filter the corpus. "
            "Open one context per T and replay every case for that T inside it."
        )

    try:
        _install_views(conn, to_utc_z(moment))
        _install_postings(conn, moment, company_ids)
        _install_repost_links(conn)
        # Release the WAL read snapshot the installers' writes opened, so the
        # block starts in autocommit. See the module docstring — this is the
        # difference between a replay that can run beside anything else and a
        # connection that wedges on the first `INSERT INTO runs`.
        conn.commit()
        yield conn
    except BaseException:
        # Whatever went wrong, this connection must not leave the context
        # holding a transaction: the caller keeps using it for the next `T`,
        # and a half-open one would either wedge it (a read snapshot) or
        # leave a partial write (e.g. `_delete_run_ids` between its
        # `evidence` and `runs` deletes) for the `finally` to commit below.
        # `rli.eval.runner` commits after every write, so nothing a completed
        # case recorded is rolled back here; only work the failure
        # interrupted. Rolling back BEFORE `_drop_shadows` also means a
        # failure DURING installation discards the shadows it created —
        # `_drop_shadows` then finds nothing, which is the correct end state.
        conn.rollback()
        raise
    finally:
        _drop_shadows(conn)
        # The drops are DDL, which Python's legacy transaction control does
        # not wrap in an implicit `BEGIN`; the commit is for whatever the
        # BLOCK left uncommitted on the normal path, and is a no-op after the
        # rollback above.
        conn.commit()
