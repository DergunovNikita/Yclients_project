#!/usr/bin/env bash

if [[ -n "${ZSH_VERSION:-}" ]]; then
  SCRIPT_PATH="${(%):-%x}"
else
  SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
fi

ROOT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
LOCAL_BIN="$ROOT_DIR/.venv/bin"

if [[ ! -d "$LOCAL_BIN" ]]; then
  echo "Local environment is not installed at $LOCAL_BIN" >&2
  return 1 2>/dev/null || exit 1
fi

case ":$PATH:" in
  *":$LOCAL_BIN:"*) ;;
  *) export PATH="$LOCAL_BIN:$PATH" ;;
esac

export VIRTUAL_ENV="$ROOT_DIR/.venv"
export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:--p no:capture}"
echo "Using local tools from $LOCAL_BIN"
