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

py_files=()
if ! command -v git >/dev/null 2>&1; then
  echo "git is required to discover tracked Python files" >&2
  exit 1
fi
while IFS= read -r file; do
  [[ -n "$file" ]] && py_files+=("$file")
done < <(git ls-files '*.py')
if [[ ${#py_files[@]} -eq 0 ]]; then
  echo "No tracked Python files found to compile" >&2
  exit 1
fi

"$PYTHON_BIN" -m compileall "${py_files[@]}"

"$PYTHON_BIN" -m pytest -p no:capture "$@"

cd "$ROOT_DIR/web"
"$NODE_BIN/npm" run test:auth
"$NODE_BIN/node" ./node_modules/vite/bin/vite.js build
"$NODE_BIN/npm" run scan:auth-build
