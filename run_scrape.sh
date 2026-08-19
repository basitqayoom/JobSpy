#!/usr/bin/env bash
#
# Runs the LinkedIn scrape once and writes a fresh web/data.json (+ scrape 3.csv).
# Intended to be invoked on an interval by launchd or cron (see README).
#
# All output is appended to scrape.log so scheduled runs are auditable.

set -euo pipefail

# Resolve the directory this script lives in (the project root), regardless of
# the caller's working directory. This matters for launchd/cron.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

LOG_FILE="$PROJECT_DIR/scrape.log"

# Activate a local virtualenv if one exists; otherwise fall back to python3.
if [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.venv/bin/activate"
  PYTHON="python"
elif [ -f "$PROJECT_DIR/venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/venv/bin/activate"
  PYTHON="python"
else
  PYTHON="$(command -v python3 || command -v python)"
fi

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') :: starting scrape ====="
  "$PYTHON" "scrape 3.py"
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') :: done ====="
  echo
} >> "$LOG_FILE" 2>&1
