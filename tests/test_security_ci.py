import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_security_workflow_declares_expected_tools_and_modes():
    workflow = (ROOT / '.github/workflows/security.yml').read_text()
    deploy_workflow = (ROOT / '.github/workflows/deploy-vm.yml').read_text()

    assert 'python-dependency-audit:' in workflow
    assert 'frontend-dependency-audit:' in workflow
    assert 'pip-audit -r requirements.txt' in workflow
    assert 'npm ci --ignore-scripts --audit=false' in workflow
    assert 'npm audit --audit-level=high' in workflow
    assert 'node scripts/audit-gate.mjs ../npm-audit.json audit-allowlist.json' in workflow
    assert 'semgrep scan' in workflow
    assert '--config p/python' in workflow
    assert '--config p/javascript' in workflow
    assert 'checkov' in workflow
    assert '--framework dockerfile,github_actions,secrets' in workflow
    assert 'continue-on-error: true' not in workflow
    assert '--soft-fail' not in workflow
    assert 'github/codeql-action/upload-sarif' in workflow
    assert workflow.count('security-events: write') == 2

    assert 'gitleaks/gitleaks-action' in deploy_workflow
    assert 'GITLEAKS_CONFIG: .gitleaks.toml' in deploy_workflow


def test_local_security_script_documents_same_gate():
    script = (ROOT / 'scripts/security-check.sh').read_text()
    readme = (ROOT / 'README.md').read_text()

    for expected in (
        'gitleaks git',
        'pip-audit -r requirements.txt',
        'npm ci --ignore-scripts --audit=false',
        'npm audit --audit-level=high',
        'node scripts/audit-gate.mjs',
        'semgrep',
        'checkov',
    ):
        assert expected in script

    assert './scripts/security-check.sh --strict' in readme
    assert 'pip-audit' in readme
    assert 'npm audit --audit-level=high' in readme
    assert 'web/audit-allowlist.json' in readme
    assert 'Semgrep' in readme
    assert 'Checkov' in readme


def test_frontend_audit_allowlist_entries_are_explicit():
    allowlist = json.loads((ROOT / 'web/audit-allowlist.json').read_text())

    assert allowlist['allowed']
    for entry in allowlist['allowed']:
        assert entry['package']
        assert entry['advisoryIds']
        assert entry['expires']
        assert len(entry['reason']) >= 40
