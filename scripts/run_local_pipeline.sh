#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p "$PROJECT_ROOT/runtime/logs"

if [ -x "$PROJECT_ROOT/venv/bin/python3" ]; then
  PYTHON_BIN="$PROJECT_ROOT/venv/bin/python3"
else
  PYTHON_BIN="$(command -v python3)"
fi

export PYTHONUTF8=1
export PYTHONUNBUFFERED=1

exec /usr/bin/caffeinate -dimsu "$PYTHON_BIN" -m local.orchestrator --config "$PROJECT_ROOT/local/config.json"
