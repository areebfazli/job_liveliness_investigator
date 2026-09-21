#!/usr/bin/env bash
# Idempotent daily job: run the board snapshot, and (weekly only) rebuild
# history from it.
#
# What it does:
#   1. `rli snapshot --db data/rli.db` — captures each target company's
#      current open jobs for today (UTC calendar day) and updates posting
#      lifecycle state. Safe to run more than once per day: a company
#      already captured today is skipped (zero network calls, zero writes),
#      so re-running this script (by hand, or because a timer fired twice)
#      never double-counts or re-hits company boards.
#   2. `rli history rebuild --db data/rli.db` — re-derives history from
#      board_snapshots. This is NOT run every day: it was measured (this
#      session, 2026-09-13, on a scratch copy of the real ~1.8GB production
#      DB, ~15,252 posting intervals across 78 companies) at 4m32s
#      wall-clock. That is already a large fraction of a 10-minute total
#      daily budget once snapshot's own network time (79 target companies'
#      board fetches) is added, and the DB only grows over time. Judgment
#      call: run history rebuild only on Sundays (UTC), and only when
#      today's snapshot actually captured a posting change (new/absent/
#      reappeared > 0) — no point re-deriving history from data that did
#      not change. Forward-looking note only, not implemented here: if the
#      DB grows enough that even the weekly run gets uncomfortably slow,
#      re-measure and consider `--company` scoping or moving rebuild off
#      the laptop entirely.
#
#      MEMORY CAP. On 2026-09-21 that weekly rebuild reached 7.1 GB RSS on
#      the expanded corpus (351 companies) and was OOM-killed on an 11 GB
#      laptop that is also running a desktop. `rli.history` was fixed to
#      hold one company at a time (measured peak afterwards: well under
#      100 MB), but this script no longer TRUSTS that: the rebuild runs
#      inside a transient systemd scope capped at 3 GB with swap disabled,
#      so the next regression kills the rebuild instead of the desktop. The
#      cap is ~30x the measured peak, so it can only fire on a real
#      regression. A kill is safe: `rli history rebuild` commits one
#      company at a time, so an interrupted run leaves every company either
#      fully re-derived or exactly as it was.
#
#      The peak RSS of each run is recorded in the log, so drift is visible
#      before it becomes another incident. Both the cap and the measurement
#      degrade gracefully: if `systemd-run` (or a usable user manager) or
#      `/usr/bin/time` is missing, the rebuild simply runs without them.
#
# Concurrency / idempotency guarantees:
#   - A non-blocking flock on data/.daily.lock ensures at most one instance
#     of this script runs at a time; a second invocation while one is still
#     running exits immediately (exit 1) rather than racing it.
#   - `rli snapshot`'s own per-company, per-UTC-day skip logic makes re-runs
#     within the same day a no-op for companies already captured.
#   - History rebuild is idempotent by construction (it fully re-derives
#     history from board_snapshots each time), so running it more than once
#     on the same data is wasted time but not incorrect.
#
# Invocation: designed to be run both as the command of the systemd service
# installed by scripts/setup_laptop_timer.sh, and directly by hand for
# manual runs / debugging. All paths are resolved relative to the repo root
# (via `cd` below), and `uv` is resolved from the caller's PATH rather than
# hardcoded, since systemd/cron environments have unpredictable/minimal PATH.
set -u
set -o pipefail
cd "$(dirname "$0")/.."

mkdir -p data/logs
# Log named by LOCAL date so a run just after midnight files under the day it
# belongs to (UTC naming made a 00:13 CEST run look like "yesterday").
LOG="data/logs/daily-$(date +%Y%m%d).log"

LOCKFILE="data/.daily.lock"
exec 200>"$LOCKFILE"
if ! flock -n 200; then
  echo "$(date -u +%FT%TZ) another instance is already running; exiting" | tee -a "$LOG"
  exit 1
fi

echo "$(date -u +%FT%TZ) === daily job start" | tee -a "$LOG"

echo "$(date -u +%FT%TZ) running rli snapshot" | tee -a "$LOG"
# NOTE: this relies on `set -o pipefail` above, not on PIPESTATUS. Because
# this pipeline runs inside the $(...) command substitution's own subshell,
# the parent shell's PIPESTATUS array is NOT updated by it (it stays stale
# from whatever pipeline last ran in this shell) — only the substitution's
# own exit status (captured here via $? right after the assignment) is
# reliable, and pipefail makes that equal `uv run rli snapshot`'s exit code
# (rather than tee's) as long as tee itself doesn't fail.
SNAPSHOT_OUTPUT=$(uv run rli snapshot --db data/rli.db 2>&1 | tee -a "$LOG")
SNAPSHOT_RC=$?

if [ "$SNAPSHOT_RC" -ne 0 ]; then
  echo "$(date -u +%FT%TZ) snapshot failed rc=$SNAPSHOT_RC" | tee -a "$LOG"
  exit "$SNAPSHOT_RC"
fi

# Pull new=/absent=/reappeared= out of the summary line, wherever it
# appears in the captured output (today it's the last line, but don't
# depend on that).
SUMMARY_LINE=$(printf '%s\n' "$SNAPSHOT_OUTPUT" | grep -oE 'companies: ok=[0-9]+ failed=[0-9]+ skipped=[0-9]+ \| postings: new=[0-9]+ absent=[0-9]+ reappeared=[0-9]+' | tail -n1)
NEW=$(printf '%s' "$SUMMARY_LINE" | grep -oE 'new=[0-9]+' | cut -d= -f2)
ABSENT=$(printf '%s' "$SUMMARY_LINE" | grep -oE 'absent=[0-9]+' | cut -d= -f2)
REAPPEARED=$(printf '%s' "$SUMMARY_LINE" | grep -oE 'reappeared=[0-9]+' | cut -d= -f2)
NEW=${NEW:-0}
ABSENT=${ABSENT:-0}
REAPPEARED=${REAPPEARED:-0}

TODAY_IS_SUNDAY=false
[ "$(date -u +%u)" = 7 ] && TODAY_IS_SUNDAY=true

CHANGE_COUNT=$((NEW + ABSENT + REAPPEARED))

if [ "$TODAY_IS_SUNDAY" != true ]; then
  echo "$(date -u +%FT%TZ) skipping history rebuild: not Sunday UTC" | tee -a "$LOG"
elif [ "$CHANGE_COUNT" -eq 0 ]; then
  echo "$(date -u +%FT%TZ) skipping history rebuild: no posting changes (new=$NEW absent=$ABSENT reappeared=$REAPPEARED)" | tee -a "$LOG"
else
  echo "$(date -u +%FT%TZ) running rli history rebuild (Sunday UTC, new=$NEW absent=$ABSENT reappeared=$REAPPEARED)" | tee -a "$LOG"

  REBUILD_CMD=(uv run rli history rebuild --db data/rli.db)

  # Record peak RSS and wall clock. `/usr/bin/time` (GNU time), not the
  # shell's `time` keyword, which has no -v and cannot write to a file.
  TIME_STATS=""
  if [ -x /usr/bin/time ]; then
    TIME_STATS="data/logs/.rebuild-time.$$"
    REBUILD_CMD=(/usr/bin/time -v -o "$TIME_STATS" "${REBUILD_CMD[@]}")
  fi

  # Cap the rebuild so a memory regression cannot take the desktop down
  # with it. MemorySwapMax=0 matters as much as MemoryMax: without it a
  # runaway rebuild thrashes gigabytes into swap and wedges the machine
  # long before the cgroup kills anything. `systemd-run --user` needs a
  # working user manager, so it is probed rather than assumed.
  if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope -q true >/dev/null 2>&1
  then
    REBUILD_CMD=(systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 "${REBUILD_CMD[@]}")
  else
    echo "$(date -u +%FT%TZ) note: systemd-run unavailable; rebuild runs uncapped" | tee -a "$LOG"
  fi

  "${REBUILD_CMD[@]}" >>"$LOG" 2>&1
  REBUILD_RC=$?

  if [ -n "$TIME_STATS" ] && [ -f "$TIME_STATS" ]; then
    cat "$TIME_STATS" >>"$LOG"
    PEAK_KB=$(awk '/Maximum resident set size/ {print $NF}' "$TIME_STATS")
    WALL=$(awk -F': ' '/Elapsed \(wall clock\) time/ {print $NF}' "$TIME_STATS")
    rm -f "$TIME_STATS"
    echo "$(date -u +%FT%TZ) history rebuild peak RSS ${PEAK_KB:-?} KB, wall ${WALL:-?}" | tee -a "$LOG"
  fi

  if [ "$REBUILD_RC" -ne 0 ]; then
    # rc 137 = SIGKILL, which is what MemoryMax looks like from here.
    echo "$(date -u +%FT%TZ) history rebuild failed rc=$REBUILD_RC" | tee -a "$LOG"
    if [ "$REBUILD_RC" -eq 137 ]; then
      echo "$(date -u +%FT%TZ) rc=137 is SIGKILL: likely the 3G MemoryMax. Derived state is still consistent (rebuild commits per company); re-run after investigating." | tee -a "$LOG"
    fi
    exit "$REBUILD_RC"
  fi
fi

echo "$(date -u +%FT%TZ) daily job complete" | tee -a "$LOG"
exit 0
