#!/usr/bin/env bash
# Spaced repeat runs of the screener, each one logged, with an explore snapshot per
# category afterwards. Usage:  bash overnight.sh [runs] [gap_minutes]
#
# Note: outside market hours the underlying data barely moves, so back-to-back runs
# mostly re-confirm the same numbers. The point here is to prove the pipeline is
# repeatable and to catch anything that breaks intermittently (throttling, session
# expiry, MarketBeat layout changes).
set -u

RUNS="${1:-4}"
GAP_MIN="${2:-90}"
cd "$(dirname "$0")" || exit 1
mkdir -p out/overnight

export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

for i in $(seq 1 "$RUNS"); do
  STAMP=$(date +%Y%m%d_%H%M%S)
  LOG="out/overnight/run_${i}_${STAMP}.log"
  echo "=== run $i/$RUNS starting $(date '+%Y-%m-%d %H:%M:%S') -> $LOG"

  START=$(date +%s)
  python -u run.py all --headless --no-open > "$LOG" 2>&1
  STATUS=$?
  END=$(date +%s)
  echo "    pipeline exit=$STATUS in $(( (END - START) / 60 ))m $(( (END - START) % 60 ))s"

  # Snapshot each explore category against whatever the run just collected.
  for CAT in stocks etfs penny; do
    python -u run.py explore --category "$CAT" --top 15 \
      > "out/overnight/explore_${CAT}_${i}_${STAMP}.txt" 2>&1
  done

  if [ -f out/dashboard_latest.html ]; then
    cp out/dashboard_latest.html "out/overnight/dashboard_${i}_${STAMP}.html"
  fi

  if [ "$i" -lt "$RUNS" ]; then
    echo "    sleeping ${GAP_MIN}m until the next run"
    sleep $(( GAP_MIN * 60 ))
  fi
done

echo "=== all $RUNS runs finished $(date '+%Y-%m-%d %H:%M:%S')"
