#!/usr/bin/env bash
#
# Serves the dashboard (web/) over HTTP so the browser can fetch data.json and
# save settings. Opening index.html via file:// will NOT work because browsers
# block fetch() of local files.
#
# Uses server.py (stdlib only) which also handles POST /api/config (save the
# search settings) and POST /api/run (trigger a scrape now).
#
# Usage:
#   ./serve.sh           # serves on http://localhost:8000
#   PORT=9000 ./serve.sh # custom port

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PORT="${PORT:-8000}"

# Prefer the project venv so the runner triggered via the page has its deps.
if [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
  PYTHON="$PROJECT_DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3 || command -v python)"
fi

exec "$PYTHON" "$PROJECT_DIR/server.py"
