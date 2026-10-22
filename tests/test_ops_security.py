"""Ops hardening: console mail, admin bootstrap, API statement timeout."""
import contextlib
import io
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api
import auth_service
import create_portal_admin
import database
from models import Base, PortalAccount, PortalUser



def printed(func, *args):
    # capsys trips over the installed pytest-asyncio, so capture stdout by hand.
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        func(*args)
    return buffer.getvalue()


RESET_LINK = 'https://portal.example/reset-password.html?token=SECRET-TOKEN'


@pytest.mark.parametrize(
    ('is_production', 'link_visible'),
    [(True, False), (False, True)],
)
def test_console_mail_prints_the_link_only_outside_production(monkeypatch, is_production, link_visible):
    monkeypatch.setattr(auth_service, 'AUTH_CONSOLE_EMAIL', True)
    monkeypatch.setattr(auth_service, 'IS_PRODUCTION', is_production)

    out = printed(auth_service.send_auth_email, 'user@example.com', 'Reset your password', f'Open {RESET_LINK}')

    assert 'user@example.com' in out
    assert 'Reset your password' in out
    assert ('SECRET-TOKEN' in out) is link_visible


def test_async_engine_has_a_statement_timeout_and_the_sync_engine_does_not(monkeypatch):
    captured = {}

    def fake_create_async_engine(url, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(database, 'create_async_engine', fake_create_async_engine)
    monkeypatch.setattr(database, '_async_session_factory', None)
    database.init_async_database('db.example', 5432, 'yclients_db', 'user', 'pw')

    settings = captured['connect_args']['server_settings']
    # Above nginx's proxy_read_timeout (150 s), below Vercel's maxDuration (180 s).
    assert 150_000 < int(settings['statement_timeout']) < 180_000
    assert settings['plan_cache_mode'] == 'force_custom_plan'

    sync_kwargs = {}
    monkeypatch.setattr(database, 'create_engine', lambda url, **kwargs: sync_kwargs.update(kwargs) or object())
    database.Database('db.example', 5432, 'yclients_db', 'user', 'pw')
    assert 'statement_timeout' not in str(sync_kwargs)


def test_a_cancelled_statement_is_a_json_500_not_a_hang(monkeypatch):
    class Cancelled(Exception):
        pass

    class CancelledSession:
        async def execute(self, *_args, **_kwargs):
            raise DBAPIError('SELECT 1', {}, Cancelled('canceling statement due to statement timeout'))

    async def override_db():
        yield CancelledSession()

    api.app.dependency_overrides[api.get_async_db] = override_db
    try:
        response = TestClient(api.app, raise_server_exceptions=False).get('/dashboard/branches')
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json() == {'detail': 'Internal server error'}


@pytest.fixture
def admin_db(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=[PortalAccount.__table__, PortalUser.__table__])
    factory = sessionmaker(engine, expire_on_commit=False)

    class FakeDatabase:
        def test_connection(self):
            return True

        def get_db(self):
            return factory()

    monkeypatch.setattr(create_portal_admin, 'init_database', lambda *_args: FakeDatabase())
    monkeypatch.delenv('PORTAL_ADMIN_PASSWORD', raising=False)
    yield factory
    engine.dispose()


def run_bootstrap(monkeypatch, *argv):
    monkeypatch.setattr('sys.argv', ['create_portal_admin.py', *argv])
    return create_portal_admin.main()


def load_admin(factory, email):
    with factory() as db:
        return db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one()


def test_bootstrap_creates_a_missing_admin_with_the_password_from_the_environment(monkeypatch, admin_db):
    monkeypatch.setenv('PORTAL_ADMIN_PASSWORD', 'first-password-1')

    assert run_bootstrap(monkeypatch, '--email', 'Admin@Example.com') == 0

    user = load_admin(admin_db, 'admin@example.com')
    assert user.role == 'platform_admin'
    assert auth_service.verify_password('first-password-1', user.password_hash)


def test_bootstrap_leaves_an_existing_user_alone(monkeypatch, admin_db):
    monkeypatch.setenv('PORTAL_ADMIN_PASSWORD', 'first-password-1')
    run_bootstrap(monkeypatch, '--email', 'admin@example.com')
    with admin_db() as db:
        user = db.execute(select(PortalUser)).scalar_one()
        user.password_hash = auth_service.hash_password('changed-by-admin')
        user.is_active = False
        db.commit()

    monkeypatch.setenv('PORTAL_ADMIN_PASSWORD', 'from-the-deploy-env')
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert run_bootstrap(monkeypatch, '--email', 'admin@example.com') == 0

    user = load_admin(admin_db, 'admin@example.com')
    assert auth_service.verify_password('changed-by-admin', user.password_hash)
    assert user.is_active is False
    assert 'exists, skipped' in out.getvalue()


def test_reset_password_flag_restores_the_old_overwrite(monkeypatch, admin_db):
    monkeypatch.setenv('PORTAL_ADMIN_PASSWORD', 'first-password-1')
    run_bootstrap(monkeypatch, '--email', 'admin@example.com')
    with admin_db() as db:
        user = db.execute(select(PortalUser)).scalar_one()
        user.is_active = False
        db.commit()

    run_bootstrap(monkeypatch, '--email', 'admin@example.com', '--password', 'forced-password-2', '--reset-password')

    user = load_admin(admin_db, 'admin@example.com')
    assert auth_service.verify_password('forced-password-2', user.password_hash)
    assert user.is_active is True


def test_bootstrap_without_a_password_is_a_usage_error(monkeypatch, admin_db):
    with pytest.raises(SystemExit) as excinfo:
        run_bootstrap(monkeypatch, '--email', 'admin@example.com')

    assert excinfo.value.code == 2


def test_bootstrap_refuses_a_password_bcrypt_cannot_hash_in_full(monkeypatch, admin_db):
    monkeypatch.setenv('PORTAL_ADMIN_PASSWORD', 'ж' * 40)  # 80 bytes in UTF-8, 40 characters

    with contextlib.redirect_stdout(io.StringIO()):
        assert run_bootstrap(monkeypatch, '--email', 'admin@example.com') == 2

    with admin_db() as db:
        assert db.execute(select(PortalUser)).first() is None


def test_bootstrap_skips_an_existing_user_whatever_the_environment_password(monkeypatch, admin_db):
    monkeypatch.setenv('PORTAL_ADMIN_PASSWORD', 'first-password-1')
    run_bootstrap(monkeypatch, '--email', 'admin@example.com')
    monkeypatch.setenv('PORTAL_ADMIN_PASSWORD', 'ж' * 40)

    with contextlib.redirect_stdout(io.StringIO()):
        assert run_bootstrap(monkeypatch, '--email', 'admin@example.com') == 0


# --- deploy/vm/yclients-auto-deploy.sh, run against stubbed git/docker/curl ---

DEPLOY_SCRIPT = Path(__file__).resolve().parents[1] / 'deploy' / 'vm' / 'yclients-auto-deploy.sh'
STUBS = {
    'flock': 'exit 0',
    'sleep': 'exit 0',
    'git': (
        'case "$1" in\n'
        '  rev-parse) if [ "$2" = HEAD ]; then echo "${LOCAL_REV:-rev-new}"; else echo rev-new; fi ;;\n'
        'esac'
    ),
    'curl': 'echo "$@" >> "$STUB_LOG"; exit "${CURL_EXIT:-0}"',
    'docker': (
        'echo "docker $*" >> "$STUB_LOG"\n'
        'case "$*" in\n'
        '  "compose exec -T api cat /app/REVISION") echo "${DEPLOYED_REV:-rev-old}" ;;\n'
        '  "image inspect"*) echo sha256:old-image ;;\n'
        '  "compose run --rm migrate") exit "${MIGRATE_EXIT:-0}" ;;\n'
        'esac'
    ),
}


def run_deploy(tmp_path, env_file='', **env):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(f'#!/usr/bin/env bash\n{body}\n')
        stub.chmod(0o755)
    app_dir = tmp_path / 'app'
    (app_dir / '.git').mkdir(parents=True)
    (app_dir / '.env').write_text(env_file)
    log = tmp_path / 'stub.log'
    log.touch()
    result = subprocess.run(
        ['bash', str(DEPLOY_SCRIPT)],
        env={
            'PATH': f'{bin_dir}:{os.environ["PATH"]}',
            'APP_DIR': str(app_dir),
            'LOCK_FILE': str(tmp_path / 'lock'),
            'STUB_LOG': str(log),
            'HEALTH_RETRIES': '2',
            **env,
        },
        capture_output=True,
        text=True,
    )
    return result, log.read_text()


def test_deploy_fails_when_the_served_revision_is_already_current_but_unhealthy(tmp_path):
    """A timer tick that deployed first and failed the probe must not read as a green "nothing to deploy"."""
    result, log = run_deploy(tmp_path, DEPLOYED_REV='rev-new', CURL_EXIT='22')

    assert result.returncode == 1
    assert 'did not become healthy' in result.stderr
    assert 'compose build' not in log


def test_deploy_is_a_no_op_when_the_served_revision_is_current_and_healthy(tmp_path):
    result, log = run_deploy(tmp_path, DEPLOYED_REV='rev-new')

    assert result.returncode == 0
    assert 'Nothing to deploy' in result.stdout
    assert 'compose build' not in log


def test_deploy_reads_env_keys_without_executing_the_file(tmp_path):
    """Compose's dotenv accepts `SMTP_FROM=Portal <no-reply@x.com>`; a shell would take it as a redirection."""
    env_file = 'SMTP_FROM=Portal <no-reply@x.com>\nSOME KEY=a b\nAPI_PORT=8123 # host port\n'
    result, log = run_deploy(tmp_path, env_file=env_file, DEPLOYED_REV='rev-new')

    assert result.returncode == 0, result.stderr
    assert 'http://127.0.0.1:8123/health/ready' in log


def test_deploy_reads_a_quoted_value_from_a_crlf_env_file(tmp_path):
    env_file = (
        'PORTAL_ADMIN_PASSWORD="s3cret"\r\nPORTAL_ADMIN_NAME="Ann # Lee"\r\n'
        'PORTAL_ADMIN_EMAIL=admin@example.com\r\n'
    )
    result, log = run_deploy(tmp_path, env_file=env_file)

    assert result.returncode == 0, result.stderr
    assert '--email admin@example.com --full-name Ann # Lee' in log


def test_failed_migration_puts_the_previous_image_tag_back(tmp_path):
    result, log = run_deploy(tmp_path, MIGRATE_EXIT='1')

    assert result.returncode == 1
    assert 'docker tag sha256:old-image yclients_bi_system-app:local' in log
    assert 'compose up -d' not in log


def test_successful_deploy_keeps_the_new_image_and_bootstraps_the_admin(tmp_path):
    env_file = 'PORTAL_ADMIN_PASSWORD="s3cret"\nPORTAL_ADMIN_EMAIL=admin@example.com\n'
    result, log = run_deploy(tmp_path, env_file=env_file)

    assert result.returncode == 0, result.stderr
    assert 'docker tag' not in log
    assert 'create_portal_admin.py --email admin@example.com --full-name Portal Admin' in log
    assert 's3cret' not in log
