from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models import (
    Account,
    Appointment,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    Good,
    GoodCatalog,
    GoodCategory,
    GoodCategoryCatalog,
    GoodTransaction,
    Group,
    ManualFactMetric,
    PlanBranchSetting,
    PlanMetric,
    PlanStaffInput,
    PortalAccount,
    PortalAuditEvent,
    PortalBranch,
    PortalEmailToken,
    PortalRefreshToken,
    PortalUser,
    PortalUserBranch,
    SyncJob,
    SyncJobEvent,
    SyncRun,
    YClientsCredential,
    YClientsCredentialCompany,
    Service,
    ServiceCatalog,
    ServiceKpiAssignment,
    ServiceKpiGroup,
    ServiceCategory,
    ServiceCategoryCatalog,
    ServiceLabel,
    Staff,
    StaffPosition,
    StaffPositionCatalog,
    StaffSchedule,
    Storage,
    AccountCatalog,
    StorageCatalog,
    SyncSourceState,
    Transaction,
    Base,
)


PUBLIC_TABLES = [
    Group.__table__,
    Company.__table__,
    ServiceCategory.__table__,
    ServiceCategoryCatalog.__table__,
    Service.__table__,
    ServiceCatalog.__table__,
    ServiceLabel.__table__,
    ServiceKpiGroup.__table__,
    ServiceKpiAssignment.__table__,
    StaffPosition.__table__,
    StaffPositionCatalog.__table__,
    Staff.__table__,
    Client.__table__,
    Account.__table__,
    AccountCatalog.__table__,
    Storage.__table__,
    StorageCatalog.__table__,
    GoodCategory.__table__,
    GoodCategoryCatalog.__table__,
    Good.__table__,
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
]


@pytest.fixture(autouse=True)
def isolate_api_auth(monkeypatch):
    """Keep tests independent from local .env API tokens."""
    import api
    import auth_deps
    import dashboard_routes
    import dashboard_service

    monkeypatch.setattr(api, 'API_KEY', '')
    monkeypatch.setattr(api, 'SYNC_API_TOKEN', '')
    monkeypatch.setattr(auth_deps, 'API_KEY', '')
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)
    monkeypatch.setattr(dashboard_routes, 'SYNC_API_TOKEN', '')
    monkeypatch.setattr(dashboard_service.yclients_analytics, 'PARTNER_TOKEN', '')
    monkeypatch.setattr(dashboard_service.yclients_analytics, 'LOGIN', '')
    monkeypatch.setattr(dashboard_service.yclients_analytics, 'PASSWORD', '')


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
            PortalEmailToken.__table__,
            PortalRefreshToken.__table__,
            YClientsCredential.__table__,
            YClientsCredentialCompany.__table__,
            SyncRun.__table__,
            SyncJob.__table__,
            PortalAuditEvent.__table__,
            SyncJobEvent.__table__,
        ])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()
