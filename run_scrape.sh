#!/usr/bin/env bash
#
# Multi-profile scrape driver.
# Usage: run_scrape.sh <profile_name>
# Or:    run_scrape.sh all   (runs all profiles sequentially, staggered)
#
# After each profile's scrape, delivery.py priority flushes any new priority
# jobs immediately. Hourly + overnight cron entries handle the 'other' bucket.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
LOG_FILE="$PROJECT_DIR/scrape.log"

if [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.venv/bin/activate"
  PYTHON="python"
else
  PYTHON="$(command -v python3 || command -v python)"
fi

if [ $# -lt 1 ]; then
  echo "Usage: $0 <profile>|all" >&2
  exit 2
fi

run_profile() {
  local profile="$1"
  {
    echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') :: scrape :: $profile ===="
    "$PYTHON" scrape.py "$profile" || echo "scrape failed for $profile: $?"
    echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') :: flush :: $profile ===="
    "$PYTHON" delivery.py flush "$profile" || echo "delivery flush failed: $?"
    echo
  } >> "$LOG_FILE" 2>&1
}

if [ "$1" = "all" ]; then
  for profile in india usa uk germany netherlands ireland poland switzerland sweden canada australia singapore; do
    run_profile "$profile"
    sleep 2
  done
else
  run_profile "$1"
fi
