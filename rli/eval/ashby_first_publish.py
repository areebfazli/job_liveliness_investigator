"""When an Ashby "last published" date is also the first publication (spec.md §3
amendment 2026-10-07).

Ashby documents `publishedAt` as "when the job was LAST published"
(https://developers.ashbyhq.com/docs/public-job-posting-api), so it is stored
and cited as `last_published` and is not first-publish evidence on its own
(a re-publish moves it). This module states the one case in which it is:

1. our own collection captured the company's board BEFORE the posting's first
   sighting `first_observed`, and the last complete own capture before that
   sighting did NOT list the job (its time is `t_absent`). Only own captures
   count — never an archive capture — and only a `'complete'` one: a failed
   fetch writes no capture at all (`capture_attempts` only) and a partial
   capture is a coverage gap, so neither is ever an absence (spec.md §4);
2. `t_absent < v <= first_observed`, where `v` is a `last_published` value an
   own capture carried; and
3. `first_observed - v <= [thresholds].ashby_first_seen_max_lag_days`.

A job absent from our earlier capture and first seen shortly after its stated
publish time cannot have been published earlier and re-published in between,
so there "last published" is the first publication. Any own capture carrying
`v` works: a later re-publish changes the value, so an unchanged early value
proves no re-publish happened in between, and a later capture carrying a NEW
value never retracts it.

Pre-fix snapshot stamps (`rli.snapshots.run_windows`): a capture stamped
inside a pre-fix run window was really fetched at some moment up to the
window's end. So the first sighting is allowed up to that end
(`guard_bound`, the same slack as the first-published guard), and the
absence is taken at its window's end too — the latest moment that capture may
really have been fetched — so `t_absent < v` is never satisfied by a stamp
that is too early.

The claim is `first_published` (`ats_native`), `source_event_at = v`, and
`available_at` = the EARLIEST own capture at or before `as_of` that carried
`v` — never earlier (spec.md §3). It reaches the policy through the same
paths as every other first-publish claim: the replay builder's
`capture_date_claims` and the live case builder both call
`ashby_first_publish_claim`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta

from rli.models.time import ensure_aware, parse_utc, to_utc_z
from rli.policy.inputs import CLAIM_FIRST_PUBLISHED
from rli.probes.base import ProbeClaim
from rli.probes.repost_history import BOARD_HISTORY_URL_PLACEHOLDER
from rli.snapshots.run_windows import RunWindow, guard_bound

__all__ = ["ASHBY_FIRST_PUBLISH_PROBE", "ashby_first_publish_claim"]

#: The `evidence.probe` attribution of the claim on the LIVE path. Not a
#: probe (no network call); it names the inference, like
#: `rli.eval.case.REFRESH_MATCH_PROBE`. In replay the claim arrives inside
#: the `ARCHIVE_BOARD_STATE_PROBE` record built by `capture_date_claims`.
ASHBY_FIRST_PUBLISH_PROBE = "ashby_first_publish"


def _parse(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_utc(str(value))
    except ValueError:
        return None


def ashby_first_publish_claim(
    conn: sqlite3.Connection,
    *,
    ats: str | None,
    company_id: str | None,
    job_id: str | None,
    first_observed: datetime | None,
    as_of: datetime,
    max_lag_days: int,
    windows: Sequence[RunWindow] = (),
) -> ProbeClaim | None:
    """The inferred `first_published` claim for an Ashby posting at `as_of`, or None.

    `first_observed` is the posting's first sighting AS OF `as_of` (None, or
    a value after `as_of`, means we had not seen it yet: nothing to infer).
    Reads only captures stamped at or before `as_of`, so the answer at `T`
    never depends on a later capture. Greenhouse and Lever never qualify.
    """
    if ats != "ashby" or not company_id or not job_id or first_observed is None:
        return None
    as_of = ensure_aware(as_of, "as_of")
    first_observed = ensure_aware(first_observed, "first_observed")
    if first_observed > as_of:
        return None

    # Own captures of the company up to `as_of`, oldest first, with whether
    # each listed the job and the `last_published` value it carried.
    rows = conn.execute(
        """
        SELECT s.id AS id, s.captured_at AS captured_at,
               s.coverage_status AS coverage_status,
               j.job_id AS job_id, j.url AS url, j.last_published AS last_published
        FROM board_snapshots AS s
        LEFT JOIN board_snapshot_jobs AS j
               ON j.board_snapshot_id = s.id AND j.job_id = ?
        WHERE s.company_id = ? AND s.source = 'own'
        ORDER BY s.captured_at, s.id, j.id
        """,
        (str(job_id), company_id),
    ).fetchall()
    captures: list[tuple[datetime, sqlite3.Row]] = []
    for row in rows:
        stamp = _parse(row["captured_at"])
        if stamp is not None and stamp <= as_of:
            captures.append((stamp, row))

    # 1. The last COMPLETE own capture before the first sighting, which must
    #    not list the job; and no own capture from it up to the sighting may
    #    list the job either (that would contradict `first_observed`).
    before = [(stamp, row) for stamp, row in captures if stamp < first_observed]
    complete = [(stamp, row) for stamp, row in before if row["coverage_status"] == "complete"]
    if not complete:
        return None
    absent_at = complete[-1][0]
    if any(stamp >= absent_at and row["job_id"] is not None for stamp, row in before):
        return None
    # The latest moment the absence capture may really have been fetched.
    absent_bound = guard_bound(absent_at, windows) or absent_at
    seen_bound = guard_bound(first_observed, windows) or first_observed
    max_lag = timedelta(days=max_lag_days)

    # 2 + 3. Every value an own capture carried, at its EARLIEST carrying capture.
    earliest: dict[str, tuple[datetime, sqlite3.Row]] = {}
    for stamp, row in captures:
        raw = row["last_published"]
        if row["job_id"] is None or raw is None:
            continue
        earliest.setdefault(str(raw), (stamp, row))

    qualifying: list[tuple[datetime, datetime, str, sqlite3.Row]] = []
    for raw, (carried_at, row) in earliest.items():
        value = _parse(raw)
        if value is None:
            continue
        if absent_bound < value <= seen_bound and first_observed - value <= max_lag:
            qualifying.append((value, carried_at, raw, row))
    if not qualifying:
        return None

    # Earliest publication wins ("first" published); ties by earliest carrier.
    value, carried_at, raw, row = min(qualifying, key=lambda q: (q[0], q[1], q[2]))
    return ProbeClaim(
        claim_type=CLAIM_FIRST_PUBLISHED,
        value=raw,
        source_url=row["url"]
        or BOARD_HISTORY_URL_PLACEHOLDER.format(company_id=company_id, job_id=job_id),
        raw_excerpt=(
            f"Ashby publishedAt (last published) {raw} counts as the first publication: "
            f"own board capture {to_utc_z(absent_at)} did not list job {job_id}, the job "
            f"was first seen {to_utc_z(first_observed)}, and the stated date lies between "
            f"the two, within {max_lag_days} day(s) of the first sighting; first carried "
            f"by own capture {to_utc_z(carried_at)}"
        ),
        source_quality="ats_native",
        source_event_at=value,
        available_at=carried_at,
        fetched_at=carried_at,
    )
