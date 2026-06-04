#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_BIN="$ROOT_DIR/.tools/node/bin"

if [[ ! -x "$NODE_BIN/node" || ! -x "$NODE_BIN/npm" ]]; then
  echo "Local Node.js is not installed at $NODE_BIN" >&2
  exit 1
fi

export PATH="$NODE_BIN:$PATH"

cd "$ROOT_DIR"

python -m compileall \
  api.py \
  dashboard_routes.py \
  dashboard_service.py \
  models.py \
  plan_config.py \
  plan_import.py \
  tests/conftest.py \
  tests/test_dashboard_api.py

python -m pytest -p no:capture "$@"

cd "$ROOT_DIR/web"
npm run build
