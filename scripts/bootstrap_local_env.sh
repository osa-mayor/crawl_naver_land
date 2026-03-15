#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="$(command -v python3)"

"$PYTHON_BIN" -m venv venv

"$PROJECT_ROOT/venv/bin/python3" -m pip install --upgrade pip
"$PROJECT_ROOT/venv/bin/python3" -m pip install -r requirements.txt
"$PROJECT_ROOT/venv/bin/python3" -m playwright install chromium

mkdir -p "$PROJECT_ROOT/runtime/logs" "$PROJECT_ROOT/runtime/locks" "$PROJECT_ROOT/runtime/runs"

echo "Bootstrap complete. Review local/config.json before first run."
