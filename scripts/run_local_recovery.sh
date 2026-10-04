#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# RunAtLoad invokes this helper after login/reboot. It only resumes an existing
# interrupted/incomplete run and exits without starting a new scheduled crawl.
exec "$PROJECT_ROOT/scripts/run_local_pipeline.sh" --resume-latest
