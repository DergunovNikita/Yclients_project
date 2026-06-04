from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()
SYSTEM_SCHEMA = 'system'


class Group(Base):
    __tablename__ = 'groups'

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    access = Column(JSON)

    companies = relationship("Company", back_populates="group")


class Company(Base):
    __tablename__ = 'companies'

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    group_id = Column(Integer, ForeignKey('groups.id'), index=True)

    group = relationship("Group", back_populates="companies")
    services = relationship("Service", back_populates="company")
    staff = relationship("Staff", back_populates="company")
    clients = relationship("Client", back_populates="company")
    appointments = relationship("Appointment", back_populates="company")


class ServiceCategory(Base):
    __tablename__ = 'service_categories'

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    weight = Column(Integer)
    api_id = Column(String)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class ServiceCategoryCatalog(Base):
    __tablename__ = 'service_category_catalog'

    company_id = Column(Integer, ForeignKey('companies.id'), primary_key=True)
    category_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    weight = Column(Integer)
    api_id = Column(String)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index('ix_service_category_catalog_category_id', 'category_id'),
    )


class Service(Base):
    __tablename__ = 'services'

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    price_min = Column(Float)
    duration = Column(Integer)
    category_title = Column(String, index=True)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)

    company = relationship("Company", back_populates="services")


class ServiceCatalog(Base):
    __tablename__ = 'service_catalog'

    company_id = Column(Integer, ForeignKey('companies.id'), primary_key=True)
    service_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    price_min = Column(Float)
    duration = Column(Integer)
    category_id = Column(Integer)
    category_title = Column(String, index=True)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index('ix_service_catalog_service_id', 'service_id'),
        Index('ix_service_catalog_company_category', 'company_id', 'category_title'),
    )


class ServiceLabel(Base):
    """Manual service labels maintained outside YClients, e.g. dashboard flags from Sheets."""

    __tablename__ = 'service_labels'

    service_id = Column(Integer, ForeignKey('services.id'), primary_key=True)
    company_id = Column(Integer, ForeignKey('companies.id'), primary_key=True, index=True)
    is_extra = Column(Boolean, nullable=False, default=False)
    source = Column(String, default='google_sheet')
    updated_at = Column(DateTime, nullable=False)


class StaffPosition(Base):
    __tablename__ = 'staff_positions'

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class StaffPositionCatalog(Base):
    __tablename__ = 'staff_position_catalog'

    company_id = Column(Integer, ForeignKey('companies.id'), primary_key=True)
    position_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index('ix_staff_position_catalog_position_id', 'position_id'),
    )


class Staff(Base):
    __tablename__ = 'staff'

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    specialization = Column(String)
    position = Column(String, index=True)
    avatar_url = Column(String)
    rating = Column(Float)
    votes_count = Column(Integer)
    bookable = Column(Boolean, default=True)
    fired = Column(Integer, nullable=False, default=0, index=True)
    user_id = Column(Integer, index=True)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)

    company = relationship("Company", back_populates="staff")


class Client(Base):
    __tablename__ = 'clients'

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, index=True)
    email = Column(String)
    birth_date = Column(Date)
    visits_count = Column(Integer, default=0)
    last_visit_date = Column(Date, index=True)
    discount = Column(Float, default=0)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)

    company = relationship("Company", back_populates="clients")


class Account(Base):
    __tablename__ = 'accounts'

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    type = Column(Integer, index=True)
    comment = Column(Text)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class AccountCatalog(Base):
    __tablename__ = 'account_catalog'

    company_id = Column(Integer, ForeignKey('companies.id'), primary_key=True)
    account_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    type = Column(Integer, index=True)
    comment = Column(Text)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index('ix_account_catalog_account_id', 'account_id'),
    )


class Storage(Base):
    __tablename__ = 'storages'

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    for_services = Column(Boolean, default=False)
    for_sale = Column(Boolean, default=False)
    comment = Column(Text)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class StorageCatalog(Base):
    __tablename__ = 'storage_catalog'

    company_id = Column(Integer, ForeignKey('companies.id'), primary_key=True)
    storage_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    for_services = Column(Boolean, default=False)
    for_sale = Column(Boolean, default=False)
    comment = Column(Text)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index('ix_storage_catalog_storage_id', 'storage_id'),
    )


class GoodCategory(Base):
    __tablename__ = 'good_categories'

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    parent_category_id = Column(Integer, index=True)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class GoodCategoryCatalog(Base):
    __tablename__ = 'good_category_catalog'

    company_id = Column(Integer, ForeignKey('companies.id'), primary_key=True)
    category_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    parent_category_id = Column(Integer, index=True)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index('ix_good_category_catalog_category_id', 'category_id'),
    )


class Good(Base):
    __tablename__ = 'goods'

    good_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    cost = Column(Float)
    actual_cost = Column(Float)
    barcode = Column(String, index=True)
    unit_short_title = Column(String)
    category_id = Column(Integer, index=True)
    last_change_date = Column(DateTime, index=True)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class GoodCatalog(Base):
    __tablename__ = 'good_catalog'

    company_id = Column(Integer, ForeignKey('companies.id'), primary_key=True)
    good_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    cost = Column(Float)
    actual_cost = Column(Float)
    barcode = Column(String, index=True)
    unit_short_title = Column(String)
    category_id = Column(Integer, index=True)
    last_change_date = Column(DateTime, index=True)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index('ix_good_catalog_good_id', 'good_id'),
        Index('ix_good_catalog_company_category', 'company_id', 'category_id'),
    )


class Appointment(Base):
    __tablename__ = 'appointments'

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)
    staff_id = Column(Integer, index=True)
    client_id = Column(Integer, index=True)
    created_user_id = Column(Integer, index=True)
    date = Column(Date, index=True)
    datetime = Column(DateTime)
    create_date = Column(DateTime)
    seance_length = Column(Integer)
    attendance = Column(Integer, default=0, index=True)
    comment = Column(Text)

    company = relationship("Company", back_populates="appointments")
    transactions = relationship("Transaction", back_populates="appointment")


class Transaction(Base):
    __tablename__ = 'transactions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    appointment_id = Column(Integer, ForeignKey('appointments.id'), index=True)
    service_id = Column(Integer, index=True)
    service_title = Column(String)
    cost = Column(Float)
    first_cost = Column(Float)
    amount = Column(Integer, default=1)
    company_id = Column(Integer, index=True)

    appointment = relationship("Appointment", back_populates="transactions")


class FinancialTransaction(Base):
    __tablename__ = 'financial_transactions'

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer)
    expense_id = Column(Integer)
    date = Column(DateTime, index=True)
    amount = Column(Float)
    comment = Column(Text)
    account_id = Column(Integer, index=True)
    client_id = Column(Integer, index=True)
    master_id = Column(Integer, index=True)
    record_id = Column(Integer, index=True)
    visit_id = Column(Integer)
    sold_item_id = Column(Integer)
    sold_item_type = Column(String)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class GoodTransaction(Base):
    __tablename__ = 'goods_transactions'

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer)
    type_id = Column(Integer)
    good_id = Column(Integer, index=True)
    good_title = Column(String)
    storage_id = Column(Integer, index=True)
    storage_title = Column(String)
    amount = Column(Float)
    cost_per_unit = Column(Float)
    cost = Column(Float)
    discount = Column(Float)
    master_id = Column(Integer, index=True)
    client_id = Column(Integer, index=True)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)
    date = Column(DateTime, index=True)


class Comment(Base):
    __tablename__ = 'comments'

    id = Column(Integer, primary_key=True)
    type = Column(String)
    master_id = Column(Integer, index=True)
    text = Column(Text)
    date = Column(DateTime, index=True)
    rating = Column(Float)
    user_id = Column(Integer)
    user_name = Column(String)
    record_id = Column(Integer, index=True)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class StaffSchedule(Base):
    __tablename__ = 'staff_schedules'

    id = Column(Integer, primary_key=True, autoincrement=True)
    staff_id = Column(Integer, index=True)
    date = Column(Date, index=True)
    slot_from = Column(Time)
    slot_to = Column(Time)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class AnalyticsOverall(Base):
    __tablename__ = 'analytics_overall'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date_from = Column(Date, nullable=False, index=True)
    date_to = Column(Date, nullable=False, index=True)
    fetched_at = Column(DateTime)

    income_total = Column(Float)
    income_total_prev = Column(Float)
    income_total_change = Column(Float)
    income_services = Column(Float)
    income_services_prev = Column(Float)
    income_goods = Column(Float)
    income_goods_prev = Column(Float)
    income_average = Column(Float)
    income_average_prev = Column(Float)
    income_average_services = Column(Float)
    income_average_services_prev = Column(Float)

    fullness_current = Column(Float)
    fullness_previous = Column(Float)
    fullness_change = Column(Float)

    records_completed = Column(Integer)
    records_pending = Column(Integer)
    records_canceled = Column(Integer)
    records_total = Column(Integer)
    records_total_prev = Column(Integer)
    records_change = Column(Float)

    clients_total = Column(Integer)
    clients_new = Column(Integer)
    clients_new_percent = Column(Float)
    clients_return = Column(Integer)
    clients_return_percent = Column(Float)
    clients_active = Column(Integer)
    clients_lost = Column(Integer)
    clients_lost_percent = Column(Float)

    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class AnalyticsDailyMetric(Base):
    __tablename__ = 'analytics_daily_metrics'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    metric_type = Column(String, nullable=False, index=True)
    label = Column(String)
    value = Column(Float)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class AnalyticsSourceMetric(Base):
    __tablename__ = 'analytics_record_sources'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date_from = Column(Date, index=True)
    date_to = Column(Date, index=True)
    label = Column(String, nullable=False)
    value = Column(Integer)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class AnalyticsStatusMetric(Base):
    __tablename__ = 'analytics_record_statuses'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date_from = Column(Date, index=True)
    date_to = Column(Date, index=True)
    label = Column(String, nullable=False)
    value = Column(Integer)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class ZReport(Base):
    __tablename__ = 'z_reports'

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_date = Column(Date, nullable=False, index=True)

    clients = Column(Integer)
    clients_average = Column(Float)
    records = Column(Integer)
    records_average = Column(Float)
    visit_records = Column(Integer)
    visit_records_average = Column(Float)
    non_visit_records = Column(Integer)
    non_visit_records_average = Column(Float)
    targets = Column(Integer)
    targets_paid = Column(Float)
    goods_count = Column(Integer)
    goods_paid = Column(Float)
    certificates_count = Column(Integer)
    certificates_paid = Column(Float)
    abonement_count = Column(Integer)
    abonement_paid = Column(Float)

    total_accounts = Column(Float)
    total_discount = Column(Float)
    currency = Column(String)

    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class ZReportPayment(Base):
    __tablename__ = 'z_report_payments'

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_date = Column(Date, nullable=False, index=True)
    payment_group = Column(String, nullable=False, index=True)
    title = Column(String)
    amount = Column(Float)
    company_id = Column(Integer, ForeignKey('companies.id'), index=True)


class PlanMetric(Base):
    """Manually maintained branch and staff plan values for plan-vs-fact dashboards."""

    __tablename__ = 'plan_metrics'

    id = Column(Integer, primary_key=True, autoincrement=True)
    period_start = Column(Date, nullable=False, index=True)
    period_end = Column(Date, nullable=False, index=True)
    company_id = Column(Integer, ForeignKey('companies.id'), nullable=False, index=True)
    staff_id = Column(Integer, ForeignKey('staff.id'), nullable=True, index=True)
    staff_category = Column(String, index=True)
    metric_code = Column(String, nullable=False, index=True)
    value = Column(Float, nullable=False)
    source = Column(String, default='manual')
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index(
            'uq_plan_metric_period_company_metric_branch',
            'period_start',
            'period_end',
            'company_id',
            'metric_code',
            unique=True,
            sqlite_where=staff_id.is_(None),
            postgresql_where=staff_id.is_(None),
        ),
        Index(
            'uq_plan_metric_period_company_staff_metric',
            'period_start',
            'period_end',
            'company_id',
            'staff_id',
            'metric_code',
            unique=True,
            sqlite_where=staff_id.is_not(None),
            postgresql_where=staff_id.is_not(None),
        ),
    )


class ManualFactMetric(Base):
    """Manually entered fact values for metrics unavailable from YClients."""

    __tablename__ = 'manual_fact_metrics'

    id = Column(Integer, primary_key=True, autoincrement=True)
    period_start = Column(Date, nullable=False, index=True)
    period_end = Column(Date, nullable=False, index=True)
    company_id = Column(Integer, ForeignKey('companies.id'), nullable=False, index=True)
    staff_id = Column(Integer, ForeignKey('staff.id'), nullable=False, index=True)
    metric_code = Column(String, nullable=False, index=True)
    value = Column(Float, nullable=False)
    source = Column(String, default='dashboard')
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index(
            'uq_manual_fact_metric_period_company_staff_metric',
            'period_start',
            'period_end',
            'company_id',
            'staff_id',
            'metric_code',
            unique=True,
        ),
    )


class PortalAccount(Base):
    """Logical owner account for the product portal (future multi-tenant)."""

    __tablename__ = 'portal_accounts'
    __table_args__ = {'schema': SYSTEM_SCHEMA}

    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(255), nullable=False, default='default')
    created_at = Column(DateTime, nullable=False)


class PortalBranch(Base):
    """Maps a portal account to YClients companies (salon branches)."""

    __tablename__ = 'portal_branches'
    __table_args__ = {'schema': SYSTEM_SCHEMA}

    id = Column(Integer, primary_key=True, autoincrement=True)
    portal_account_id = Column(Integer, ForeignKey(f'{SYSTEM_SCHEMA}.portal_accounts.id'), nullable=False)
    company_id = Column(Integer, ForeignKey('companies.id'), nullable=False)


class SyncState(Base):
    __tablename__ = 'sync_state'
    __table_args__ = {'schema': SYSTEM_SCHEMA}

    key = Column(String, primary_key=True)
    value = Column(String)
    updated_at = Column(DateTime, index=True)


class SyncRun(Base):
    __tablename__ = 'sync_runs'
    __table_args__ = {'schema': SYSTEM_SCHEMA}

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger_type = Column(String, nullable=False, index=True)
    mode = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, index=True)
    initiator = Column(String)
    started_at = Column(DateTime, nullable=False, index=True)
    finished_at = Column(DateTime, index=True)
    log_path = Column(String)
    message = Column(Text)


class SyncStepRun(Base):
    __tablename__ = 'sync_step_runs'
    __table_args__ = {'schema': SYSTEM_SCHEMA}

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey(f'{SYSTEM_SCHEMA}.sync_runs.id'), nullable=False, index=True)
    step_name = Column(String, nullable=False, index=True)
    step_key = Column(String, index=True)
    status = Column(String, nullable=False, index=True)
    elapsed_seconds = Column(Float)
    created_at = Column(DateTime, nullable=False, index=True)


class SyncJob(Base):
    __tablename__ = 'sync_jobs'
    __table_args__ = {'schema': SYSTEM_SCHEMA}

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String, nullable=False, index=True)
    initiator = Column(String)
    status = Column(String, nullable=False, index=True)
    requested_at = Column(DateTime, nullable=False, index=True)
    started_at = Column(DateTime, index=True)
    finished_at = Column(DateTime, index=True)
    run_id = Column(Integer, ForeignKey(f'{SYSTEM_SCHEMA}.sync_runs.id'), index=True)
    error_message = Column(Text)
