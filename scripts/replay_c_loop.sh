#!/usr/bin/env bash
# Run System C over the replay datasets against a rate-limited LLM endpoint,
# resuming across daily quota resets and machine restarts.
#
# Usage: scripts/replay_c_loop.sh [dataset ...]      (default: dev-300-v2 company-150-v2)
# Re-run it any time the machine is on; completed cases are never re-spent.
# Exit code 3 from `rli replay run` means "daily quota exhausted"; we sleep
# until the next reset (midnight Pacific ≈ 08:00 UTC) and continue.
set -u
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
mkdir -p data/logs
LOG=data/logs/replay-c.log
DATASETS=("$@"); [ ${#DATASETS[@]} -eq 0 ] && DATASETS=(dev-300-v2 company-150-v2)

sleep_until_reset() {
  local now target
  now=$(date -u +%s)
  target=$(date -u -d "today 08:05" +%s)
  [ "$target" -le "$now" ] && target=$(date -u -d "tomorrow 08:05" +%s)
  echo "$(date -u +%FT%TZ) quota exhausted; sleeping $(( (target - now) / 60 )) min until reset" | tee -a "$LOG"
  sleep $(( target - now ))
}

for ds in "${DATASETS[@]}"; do
  while :; do
    echo "$(date -u +%FT%TZ) === replay C dataset=$ds" | tee -a "$LOG"
    uv run rli replay run --system C --dataset "$ds" --db data/rli.db >> "$LOG" 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then echo "$(date -u +%FT%TZ) dataset=$ds complete" | tee -a "$LOG"; break; fi
    if [ $rc -eq 3 ]; then sleep_until_reset; continue; fi
    echo "$(date -u +%FT%TZ) dataset=$ds failed rc=$rc; retrying in 10 min" | tee -a "$LOG"; sleep 600
  done
done
uv run rli replay status --dataset dev-300-v2 --db data/rli.db | tee -a "$LOG"
