#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ -x "$PROJECT_ROOT/venv/bin/python3" ]; then
  PYTHON_BIN="$PROJECT_ROOT/venv/bin/python3"
else
  PYTHON_BIN="$(command -v python3)"
fi

export PYTHONUTF8=1
export PYTHONUNBUFFERED=1

exec "$PYTHON_BIN" -m local.backfill --config "$PROJECT_ROOT/local/config.json" "$@"
