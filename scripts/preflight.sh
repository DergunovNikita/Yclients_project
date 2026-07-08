#!/usr/bin/env bash
# Fast local pre-commit gate. Most optional tools emit WARN when missing; checks
# that protect browser auth security fail closed.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_BIN="$ROOT_DIR/.venv/bin"
export PATH="$LOCAL_BIN:$PATH"

cd "$ROOT_DIR"

status=0
warn()  { printf '[preflight] WARN  %s\n' "$*"; }
info()  { printf '[preflight] ...   %s\n' "$*"; }
fail()  { printf '[preflight] FAIL  %s\n' "$*" >&2; status=1; }
ok()    { printf '[preflight] OK    %s\n' "$*"; }

# 1) ruff on the whole tree
if command -v ruff >/dev/null 2>&1; then
  info "ruff check ."
  if ruff check .; then
    ok "ruff"
  else
    fail "ruff reported issues"
  fi
else
  warn "ruff not installed — 'pip install -r requirements.txt' to enable"
fi

# 2) syntax check for staged .py files (falls back to changed files vs HEAD)
py_files=()
while IFS= read -r file; do
  [[ -n "$file" ]] && py_files+=("$file")
done < <(git diff --name-only --cached --diff-filter=ACMR -- '*.py' 2>/dev/null || true)
if [[ ${#py_files[@]} -eq 0 ]]; then
  while IFS= read -r file; do
    [[ -n "$file" ]] && py_files+=("$file")
  done < <(git diff --name-only --diff-filter=ACMR HEAD -- '*.py' 2>/dev/null || true)
fi
if [[ ${#py_files[@]} -gt 0 ]]; then
  info "compileall ${#py_files[@]} changed .py files"
  if python -m compileall -q "${py_files[@]}"; then
    ok  "compileall"
  else
    fail "compileall reported syntax errors"
  fi
else
  info "no changed .py files to compile"
fi

# 3) fast pytest subset (unit + api; skips heavy Postgres integration + web build)
info "pytest tests/test_sync_parsing.py tests/test_api.py -x -q"
if python -m pytest tests/test_sync_parsing.py tests/test_api.py -x -q; then
  ok "pytest fast subset"
else
  fail "pytest fast subset failed"
fi

# 4) auth frontend/proxy runtime tests, if local Node/npm are available
if [[ -x "$LOCAL_BIN/node" && -x "$LOCAL_BIN/npm" ]]; then
  info "npm run test:auth"
  if (cd web && "$LOCAL_BIN/npm" run test:auth); then
    ok "npm test:auth"
  else
    fail "npm test:auth failed"
  fi
else
  fail "local node/npm not installed — cannot run npm test:auth"
fi
if [[ -x "$LOCAL_BIN/node" && -x "$LOCAL_BIN/npm" && -d web/dist ]]; then
  info "npm run scan:auth-build"
  if (cd web && "$LOCAL_BIN/npm" run scan:auth-build); then
    ok "npm scan:auth-build"
  else
    fail "npm scan:auth-build failed"
  fi
fi

# 5) gitleaks on staged diff, if available
if command -v gitleaks >/dev/null 2>&1; then
  info "gitleaks (staged)"
  if gitleaks git --config .gitleaks.toml --redact --staged; then
    ok "gitleaks"
  else
    fail "gitleaks flagged potential secrets"
  fi
else
  warn "gitleaks not installed — see CLAUDE.md for install hint"
fi

if [[ $status -eq 0 ]]; then
  printf '[preflight] done — good to commit\n'
else
  printf '[preflight] done — issues above must be resolved before committing\n' >&2
fi
exit $status
