#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_BIN="$ROOT_DIR/.venv/bin"
export PATH="$LOCAL_BIN:$PATH"

STRICT=0
if [[ "${1:-}" == "--strict" ]]; then
  STRICT=1
fi

cd "$ROOT_DIR"

status=0
warn() { printf '[security] WARN  %s\n' "$*"; }
info() { printf '[security] ...   %s\n' "$*"; }
ok()   { printf '[security] OK    %s\n' "$*"; }
fail() { printf '[security] FAIL  %s\n' "$*" >&2; status=1; }

require_or_warn() {
  local tool="$1"
  local install_hint="$2"
  if command -v "$tool" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$STRICT" -eq 1 ]]; then
    fail "$tool is not installed; $install_hint"
  else
    warn "$tool is not installed; $install_hint"
  fi
  return 1
}

if require_or_warn gitleaks "install gitleaks to enable secret scanning"; then
  info "gitleaks full repository scan"
  if gitleaks git --config .gitleaks.toml --redact --no-banner; then
    ok "gitleaks"
  else
    fail "gitleaks flagged potential secrets"
  fi
fi

if require_or_warn pip-audit "python -m pip install pip-audit"; then
  info "pip-audit requirements.txt"
  if pip-audit -r requirements.txt --progress-spinner off; then
    ok "pip-audit"
  else
    fail "pip-audit found vulnerable Python dependencies"
  fi
fi

if command -v npm >/dev/null 2>&1; then
  info "npm ci --ignore-scripts --audit=false"
  if (cd web && npm ci --ignore-scripts --audit=false); then
    ok "npm ci"
  else
    fail "npm ci failed"
  fi

  audit_report="$(mktemp)"
  info "npm audit --audit-level=high"
  (
    cd web
    set +e
    npm audit --audit-level=high --json > "$audit_report"
    audit_status=$?
    set -e
    cat "$audit_report"
    if [[ "$audit_status" -gt 1 ]]; then
      exit "$audit_status"
    fi
    node scripts/audit-gate.mjs "$audit_report" audit-allowlist.json
  )
  audit_gate_status=$?
  rm -f "$audit_report"
  if [[ "$audit_gate_status" -eq 0 ]]; then
    ok "npm audit"
  else
    fail "npm audit found unapproved high or critical frontend dependency issues"
  fi
else
  if [[ "$STRICT" -eq 1 ]]; then
    fail "npm is not installed; install Node.js dependencies to enable frontend audit"
  else
    warn "npm is not installed; install Node.js dependencies to enable frontend audit"
  fi
fi

if require_or_warn semgrep "python -m pip install semgrep"; then
  info "semgrep Python/JavaScript security packs"
  semgrep_args=(
    scan
    --config p/python
    --config p/javascript
    --config p/security-audit
  )
  if [[ "$STRICT" -eq 1 ]]; then
    semgrep_args+=(--error --severity ERROR)
  fi
  if semgrep "${semgrep_args[@]}"; then
    ok "semgrep"
  else
    if [[ "$STRICT" -eq 1 ]]; then
      fail "semgrep found blocking security issues"
    else
      warn "semgrep reported findings; triage before making it blocking"
    fi
  fi
fi

if require_or_warn checkov "python -m pip install checkov"; then
  info "checkov Dockerfile/GitHub Actions/secrets scan"
  checkov_args=(
    -d .
    --framework dockerfile,github_actions,secrets
    --output cli
  )
  if [[ "$STRICT" -eq 0 ]]; then
    checkov_args+=(--soft-fail)
  fi
  if checkov "${checkov_args[@]}"; then
    ok "checkov"
  else
    if [[ "$STRICT" -eq 1 ]]; then
      fail "checkov found blocking IaC/workflow issues"
    else
      warn "checkov reported findings; triage before making it blocking"
    fi
  fi
fi

if [[ "$status" -eq 0 ]]; then
  printf '[security] done\n'
else
  printf '[security] done with issues\n' >&2
fi
exit "$status"
