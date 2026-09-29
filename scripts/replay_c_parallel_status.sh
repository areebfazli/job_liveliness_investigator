#!/usr/bin/env bash
# Progress of a sharded System C replay (scripts/replay_c_parallel.sh).
#
# Usage: scripts/replay_c_parallel_status.sh [DATASET ...]   (default: dev-7d company-7d)
#   env WORKERS, DB, LOG_DIR, RUN_DIR, RLI — same meaning and defaults as the launcher.
#
# Prints supervisor and per-worker liveness (with each worker's last log
# line), then per-shard completed/remaining for System C from
# `rli replay status --shards $WORKERS`. Read-only: safe while workers run.
set -u
cd "$(dirname "$0")/.."
WORKERS=${WORKERS:-4}
DB=${DB:-data/rli.db}
LOG_DIR=${LOG_DIR:-data/logs}
RUN_DIR=${RUN_DIR:-data/run}
RLI=${RLI:-uv run rli}
DATASETS=("$@"); [ ${#DATASETS[@]} -eq 0 ] && DATASETS=(dev-7d company-7d)

is_ours() {  # PID alive and running replay_c_parallel.sh with the given mode
  kill -0 "$1" 2>/dev/null \
    && tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | grep -q "replay_c_parallel.sh $2"
}

sup_pid=$(cat "$RUN_DIR/replay-c-parallel.pid" 2>/dev/null || true)
if [ -n "$sup_pid" ] && is_ours "$sup_pid" --supervise; then
  echo "supervisor: ALIVE pid $sup_pid"
else
  echo "supervisor: not running"
fi

for (( i = 0; i < WORKERS; i++ )); do
  f="$RUN_DIR/replay-c-w$i.pid"
  state="not running"
  if [ -f "$f" ]; then
    read -r pid ds shard < "$f"
    if is_ours "$pid" --worker; then
      state="ALIVE pid $pid dataset=$ds shard=$shard"
    else
      state="dead (stale pid file: $pid $ds $shard)"
    fi
  fi
  last=$(grep -v '^\s*$' "$LOG_DIR/replay-c-w$i.log" 2>/dev/null | tail -n 1 | cut -c1-160)
  echo "worker $i: $state"
  [ -n "$last" ] && echo "    last log: $last"
done

for ds in "${DATASETS[@]}"; do
  echo
  # Only the header and System C's block (its total line plus shard lines).
  $RLI replay status --dataset "$ds" --shards "$WORKERS" --db "$DB" \
    | awk '/^replay dataset/ {print; next} /^  [A-Z0-9]+:/ {show = ($1 == "C:")} show {print}'
done
