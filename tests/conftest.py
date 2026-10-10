import os
import secrets
from datetime import datetime, timedelta

# Before anything imports config: APP_ENV now fails CLOSED, so any value outside
# NON_PRODUCTION_ENV_NAMES — including unset — is treated as production and
# validate_production_config() raises at import time. A clone with no .env would otherwise
# abort at collection, before a single test runs. setdefault, so an explicitly set APP_ENV
# (CI, or a test that deliberately exercises the production path) still wins.
os.environ.setdefault('APP_ENV', 'test')

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models import (
    Appointment,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    GoodCatalog,
    GoodCategoryCatalog,
    GoodTransaction,
    Group,
    ManualFactMetric,
    ManualPaymentAmount,
    PlanBranchSetting,
    PlanMetric,
    PlanStaffInput,
    PortalAccount,
    PortalAuditEvent,
    PortalBranch,
    PortalEmailToken,
    PortalMetricVisibility,
    PortalRefreshToken,
    PortalUser,
    PortalUserBranch,
    ReportUsageEvent,
    SyncJob,
    SyncJobEvent,
    SyncRun,
    YClientsCredential,
    YClientsCredentialCompany,
    Service,
    ServiceCatalog,
    ServiceKpiAssignment,
    ServiceKpiGroup,
    ServiceCategoryCatalog,
    ServiceLabel,
    Staff,
    StaffPositionCatalog,
    StaffSchedule,
    AccountCatalog,
    StorageCatalog,
    SyncSourceState,
    Transaction,
    Base,
)


PUBLIC_TABLES = [
    Group.__table__,
    Company.__table__,
    ServiceCategoryCatalog.__table__,
    Service.__table__,
    ServiceCatalog.__table__,
    ServiceLabel.__table__,
    ServiceKpiGroup.__table__,
    ServiceKpiAssignment.__table__,
    StaffPositionCatalog.__table__,
    Staff.__table__,
    Client.__table__,
    AccountCatalog.__table__,
    StorageCatalog.__table__,
    GoodCategoryCatalog.__table__,
    GoodCatalog.__table__,
    Appointment.__table__,
    Transaction.__table__,
    FinancialTransaction.__table__,
    SyncSourceState.__table__,
    GoodTransaction.__table__,
    Comment.__table__,
    StaffSchedule.__table__,
    PlanMetric.__table__,
    PlanBranchSetting.__table__,
    PlanStaffInput.__table__,
    ManualFactMetric.__table__,
    ManualPaymentAmount.__table__,
]


@pytest.fixture(autouse=True)
def isolate_api_auth(monkeypatch):
    """Keep tests independent from local .env API tokens."""
    import api
    import auth_deps

    monkeypatch.setattr(api, 'API_KEY', '', raising=False)
    monkeypatch.setattr(api, 'SYNC_API_TOKEN', '')
    monkeypatch.setattr(auth_deps, 'API_KEY', '')
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)


@pytest.fixture(autouse=True)
def never_write_usage_events_to_the_configured_database(monkeypatch):
    """`import api` initialises the engine from .env, so a route test with a signed-in principal would write
    its usage event to that real database (and did, into the local copy of production). The writer logs a
    warning and drops the event; tests that look at usage events point the factory at their own database."""
    import report_usage

    def refuse():
        raise RuntimeError('tests must not write to the configured database')

    monkeypatch.setattr(report_usage, 'get_async_session_factory', refuse)


@pytest_asyncio.fixture
async def async_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.execute(__import__('sqlalchemy').text("ATTACH DATABASE ':memory:' AS system"))
        await conn.run_sync(Base.metadata.create_all, tables=PUBLIC_TABLES + [
            PortalAccount.__table__,
            PortalBranch.__table__,
            PortalUser.__table__,
            PortalUserBranch.__table__,
            PortalMetricVisibility.__table__,
            PortalEmailToken.__table__,
            PortalRefreshToken.__table__,
            YClientsCredential.__table__,
            YClientsCredentialCompany.__table__,
            SyncRun.__table__,
            SyncJob.__table__,
            PortalAuditEvent.__table__,
            SyncJobEvent.__table__,
            ReportUsageEvent.__table__,
        ])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def new_session_row(user_id: int, *, session_id: int | None = None) -> PortalRefreshToken:
    """A live refresh-session row; access tokens are only valid while their session row is."""
    now = datetime.utcnow()
    return PortalRefreshToken(
        id=session_id,
        user_id=user_id,
        token_hash=secrets.token_hex(32),
        expires_at=now + timedelta(days=7),
        last_used_at=now,
        created_at=now,
    )


async def issue_access_token(db: AsyncSession, user_id: int, role: str, token_version: int = 0) -> str:
    """Persist a real session for the user and return an access token bound to it, as login does."""
    from auth_service import create_access_token

    row = new_session_row(user_id)
    db.add(row)
    await db.flush()
    session_id = row.id
    await db.commit()
    return create_access_token(user_id, role, token_version, session_id)
