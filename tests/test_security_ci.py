from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_security_workflow_fails_closed():
    workflow = (ROOT / '.github/workflows/security.yml').read_text()

    assert 'continue-on-error: true' not in workflow
    assert '--soft-fail' not in workflow


def test_deploy_waits_for_every_check():
    workflow = (ROOT / '.github/workflows/deploy-vm.yml').read_text()
    needs = workflow.split('  deploy-vm:', 1)[1].split('steps:', 1)[0]

    # A broken frontend (tests, build, token scan) must stop the deploy, not just turn a check red.
    for job in ('secrets-scan', 'test', 'frontend'):
        assert f'- {job}' in needs


def test_deploy_refuses_to_report_success_without_vm_secrets():
    workflow = (ROOT / '.github/workflows/deploy-vm.yml').read_text()

    assert 'refusing to report a successful production deploy' in workflow


def test_python_requirements_are_pinned_exactly():
    for name in ('requirements.txt', 'requirements-dev.txt'):
        for raw in (ROOT / name).read_text().splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line or line.startswith('-r '):
                continue
            assert '==' in line and '>' not in line and '<' not in line, f'{name}: {line} is not pinned with =='
