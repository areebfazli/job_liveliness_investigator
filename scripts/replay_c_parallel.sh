#!/usr/bin/env bash
# Replay System C over one or more datasets with WORKERS concurrent shard
# workers against the same database (`rli replay run --shard I/N`).
#
# Usage: scripts/replay_c_parallel.sh DATASET [DATASET ...]
#   env WORKERS    number of shard workers per dataset   (default 4)
#   env TOTAL_RPM  LLM requests/minute across ALL workers (default 160;
#                  each worker gets floor(TOTAL_RPM / WORKERS) via --rpm)
#   env DB         database path                          (default data/rli.db)
#   env LOG_DIR / RUN_DIR   logs and PID/lock files (default data/logs, data/run)
#   env RLI        the rli command                        (default "uv run rli")
#
# The command returns immediately: it starts a detached supervisor
# (log: data/logs/replay-c-parallel.log) that, for each dataset in order,
# launches WORKERS detached worker loops and waits for all of them before
# moving on. Each worker loop resumes its shard, and on exit code 3 (a 429)
# pauses 10 min for a transient limit or sleeps to the daily reset for a
# daily quota, exactly like scripts/replay_c_loop.sh. Per-worker logs:
# $LOG_DIR/replay-c-w{I}.log; PID files: $RUN_DIR/replay-c-w{I}.pid.
#
# Idempotent: re-running while a supervisor is alive does nothing, and a
# supervisor never starts a second loop for a shard whose worker is alive.
# Progress: scripts/replay_c_parallel_status.sh. Stop everything:
#   pkill -f replay_c_parallel.sh; pkill -f 'rli replay run --system C'
# A case killed mid-run is normally left non-completed and redone on resume.
set -u
SCRIPT="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a

WORKERS=${WORKERS:-4}
TOTAL_RPM=${TOTAL_RPM:-160}
DB=${DB:-data/rli.db}
LOG_DIR=${LOG_DIR:-data/logs}
RUN_DIR=${RUN_DIR:-data/run}
RLI=${RLI:-uv run rli}
export WORKERS TOTAL_RPM DB LOG_DIR RUN_DIR RLI
mkdir -p "$LOG_DIR" "$RUN_DIR"
SUPERVISOR_LOG=$LOG_DIR/replay-c-parallel.log
SUPERVISOR_LOCK=$RUN_DIR/replay-c-parallel.lock
SUPERVISOR_PID=$RUN_DIR/replay-c-parallel.pid

ts() { date -u +%FT%TZ; }

# --- helpers -----------------------------------------------------------------

worker_pid_file() { echo "$RUN_DIR/replay-c-w$1.pid"; }

# 0 when shard I's worker loop is alive (PID file names a live process that
# is one of ours; a stale file after a reboot or PID reuse reads as dead).
worker_alive() {
  local f pid
  f=$(worker_pid_file "$1")
  [ -f "$f" ] || return 1
  read -r pid _ < "$f" || return 1
  [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null \
    && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "replay_c_parallel.sh --worker"
}

sleep_until_reset() {
  local log=$1 now target
  now=$(date -u +%s)
  target=$(date -u -d "today 08:05" +%s)
  [ "$target" -le "$now" ] && target=$(date -u -d "tomorrow 08:05" +%s)
  echo "$(ts) quota exhausted; sleeping $(( (target - now) / 60 )) min until reset" >> "$log"
  sleep $(( target - now ))
}

# --- worker mode: one shard of one dataset, until it is done -------------------

run_worker() {
  local ds=$1 i=$2 n=$3 rpm=$4
  local log="$LOG_DIR/replay-c-w$i.log" last="$RUN_DIR/replay-c-w$i.last" rc
  # One loop per shard, ever: the lock is inherited by `rli replay run`, so it
  # is held until the last process of this loop exits.
  exec 8>"$RUN_DIR/replay-c-w$i.lock"
  if ! flock -n 8; then
    echo "$(ts) shard $i/$n: another worker holds the lock; not starting" >> "$log"
    exit 0
  fi
  local pid_file
  pid_file=$(worker_pid_file "$i")
  echo "$$ $ds $i/$n" > "$pid_file"
  # shellcheck disable=SC2064  # expand now: `$pid_file` is local to this function
  trap "rm -f '$pid_file'" EXIT

  while :; do
    echo "$(ts) === replay C dataset=$ds shard=$i/$n rpm=$rpm" >> "$log"
    $RLI replay run --system C --dataset "$ds" --shard "$i/$n" --rpm "$rpm" \
      --db "$DB" 2>&1 | tee -a "$log" > "$last"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ]; then
      # Exit 0 can still leave failed cases (counted in errors=, left for
      # resume). Re-walk while that makes progress; stop when it does not.
      local errors completed
      errors=$(grep -oE 'errors=[0-9]+' "$last" | head -1 | cut -d= -f2)
      completed=$(grep -oE ' completed=[0-9]+' "$last" | head -1 | cut -d= -f2)
      if [ "${errors:-0}" -gt 0 ] && [ "${completed:-0}" -gt 0 ]; then
        echo "$(ts) shard $i/$n: $errors failed case(s); re-walking in 60 s" >> "$log"
        sleep 60
        continue
      fi
      echo "$(ts) dataset=$ds shard=$i/$n complete (errors=${errors:-0})" >> "$log"
      break
    fi
    if [ "$rc" -eq 3 ]; then
      # Exit 3 covers every 429. Only a genuine DAILY quota waits for the
      # reset; a transient one (Mistral "backend_out_of_capacity", a
      # per-minute limit) just needs a short pause before resuming.
      if grep -qiE "PerDay|per day|RequestsPerDay|daily" "$last"; then
        sleep_until_reset "$log"
      else
        echo "$(ts) transient 429 (not a daily quota); pausing 10 min" >> "$log"
        sleep 600
      fi
      continue
    fi
    echo "$(ts) dataset=$ds shard=$i/$n failed rc=$rc; retrying in 10 min" >> "$log"
    sleep 600
  done
}

# --- supervisor mode: datasets in order, WORKERS shards each -------------------

supervise() {
  exec 9>"$SUPERVISOR_LOCK"
  if ! flock -n 9; then
    echo "$(ts) another supervisor is running; exiting" >> "$SUPERVISOR_LOG"
    exit 0
  fi
  echo "$$" > "$SUPERVISOR_PID"
  trap 'rm -f "$SUPERVISOR_PID"' EXIT
  local rpm=$(( TOTAL_RPM / WORKERS ))

  for ds in "$@"; do
    echo "$(ts) === dataset=$ds workers=$WORKERS rpm/worker=$rpm (total $TOTAL_RPM)" >> "$SUPERVISOR_LOG"
    for (( i = 0; i < WORKERS; i++ )); do
      if worker_alive "$i"; then
        echo "$(ts) shard $i/$WORKERS: worker already alive ($(cat "$(worker_pid_file "$i")")); not starting another" >> "$SUPERVISOR_LOG"
        continue
      fi
      # The lock/fd 9 must not leak into the worker, or a worker would keep
      # the supervisor "running" after the supervisor exits.
      setsid nohup bash "$SCRIPT" --worker "$ds" "$i" "$WORKERS" "$rpm" \
        > /dev/null 2>&1 < /dev/null 9>&- &
      echo "$(ts) shard $i/$WORKERS: started worker pid $!" >> "$SUPERVISOR_LOG"
      sleep 2  # stagger: the first model calls of N workers need not collide
    done
    # Wait for EVERY worker of this dataset before the next one.
    sleep 5
    while :; do
      local alive=0
      for (( i = 0; i < WORKERS; i++ )); do worker_alive "$i" && alive=$(( alive + 1 )); done
      [ "$alive" -eq 0 ] && break
      sleep 60
    done
    echo "$(ts) dataset=$ds: all workers finished" >> "$SUPERVISOR_LOG"
    $RLI replay status --dataset "$ds" --shards "$WORKERS" --db "$DB" >> "$SUPERVISOR_LOG" 2>&1
  done
  echo "$(ts) all datasets done: $*" >> "$SUPERVISOR_LOG"
}

# --- entry point ----------------------------------------------------------------

case "${1:-}" in
  --worker)
    shift
    run_worker "$@"
    ;;
  --supervise)
    shift
    supervise "$@"
    ;;
  ""|-h|--help)
    sed -n '2,25p' "$SCRIPT"
    exit 2
    ;;
  *)
    if ! [[ "$WORKERS" =~ ^[1-9][0-9]*$ ]] || ! [[ "$TOTAL_RPM" =~ ^[0-9]+$ ]]; then
      echo "WORKERS must be >= 1 and TOTAL_RPM >= 0 (got WORKERS=$WORKERS TOTAL_RPM=$TOTAL_RPM)" >&2
      exit 2
    fi
    if [ "$TOTAL_RPM" -gt 0 ] && [ $(( TOTAL_RPM / WORKERS )) -lt 1 ]; then
      echo "TOTAL_RPM=$TOTAL_RPM is less than one request/minute per worker" >&2
      exit 2
    fi
    if [ -f "$SUPERVISOR_PID" ] && kill -0 "$(cat "$SUPERVISOR_PID")" 2>/dev/null; then
      echo "supervisor already running (pid $(cat "$SUPERVISOR_PID")); nothing started."
      echo "progress: scripts/replay_c_parallel_status.sh $*"
      exit 0
    fi
    setsid nohup bash "$SCRIPT" --supervise "$@" > /dev/null 2>&1 < /dev/null &
    echo "started supervisor pid $! for: $* (WORKERS=$WORKERS, TOTAL_RPM=$TOTAL_RPM -> --rpm $(( TOTAL_RPM / WORKERS )) each)"
    echo "logs: $SUPERVISOR_LOG, $LOG_DIR/replay-c-w{0..$(( WORKERS - 1 ))}.log"
    echo "progress: scripts/replay_c_parallel_status.sh $*"
    ;;
esac
