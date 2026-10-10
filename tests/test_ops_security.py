"""Ops hardening: console mail, admin bootstrap, API statement timeout."""
import contextlib
import io

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
    assert settings['statement_timeout'] == '170000'
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
        response = TestClient(api.app, raise_server_exceptions=False).get('/groups')
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
