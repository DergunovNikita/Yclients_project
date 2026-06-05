#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_BIN="$ROOT_DIR/.venv/bin"
NODE_BIN="$LOCAL_BIN"
PYTHON_BIN="$LOCAL_BIN/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Local Python is not installed at $PYTHON_BIN" >&2
  exit 1
fi

if [[ ! -x "$NODE_BIN/node" ]]; then
  echo "Local Node.js is not installed at $NODE_BIN/node" >&2
  exit 1
fi

export PATH="$LOCAL_BIN:$PATH"

cd "$ROOT_DIR"

"$PYTHON_BIN" -m compileall \
  api.py \
  dashboard_reports.py \
  dashboard_routes.py \
  dashboard_service.py \
  models.py \
  plan_config.py \
  plan_import.py \
  tests/conftest.py \
  tests/test_dashboard_api.py

"$PYTHON_BIN" -m pytest -p no:capture "$@"

cd "$ROOT_DIR/web"
"$NODE_BIN/node" ./node_modules/vite/bin/vite.js build
