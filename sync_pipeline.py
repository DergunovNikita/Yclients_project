"""
Production ETL pipeline for syncing YClients data into PostgreSQL.
"""
import time
import traceback
from datetime import date, timedelta, datetime
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import or_
from config import (
    DEFAULT_BRANCH_TIMEZONE,
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
    SYNC_DAYS, SCHEDULE_DAYS, DB_BATCH_SIZE,
    SYNC_HISTORY_START_DATE, SYNC_INCREMENTAL, SYNC_LOOKBACK_DAYS,
    SYNC_REFRESH_DAYS,
    YCLIENTS_REQUEST_DELAY, YCLIENTS_TIMEOUT,
    YCLIENTS_RETRY_TOTAL, YCLIENTS_RETRY_BACKOFF,
    YCLIENTS_RETRY_AFTER_MAX,
)
from clock import utcnow
from data_sources import SOURCE_YCLIENTS, adapter_from_payload
from yclients_api import YClientsAPI
from yclients_credentials import (
    YClientsCredentialValue,
    load_active_credentials_sync,
    mark_credential_failure_sync,
    mark_credential_success_sync,
)
from database import Database, init_database
from models import (
    Group, Company,
    ServiceCategoryCatalog, ServiceCatalog, Staff, StaffPositionMonth, Client,
    AccountCatalog, GoodCategoryCatalog, GoodCatalog,
    Appointment, Transaction, FinancialTransaction, GoodTransaction,
    Comment, StaffSchedule,
    SyncState, SyncSourceState, YClientsCredential,
)
from portal_staff_sync import crm_staff_clause
from sync_control import SyncControlService
from sync_parsing import (
    parse_date, parse_datetime, parse_datetime_end, parse_datetime_start, parse_int,
    parse_reference_id, parse_time,
)

TRANSACTIONAL_STATE_KEY = 'transactions_last_success_date'
HISTORICAL_COVERAGE_STATE_KEY = 'historical_source_coverage_v1'
INCREMENTAL_SYNC_MODE = 'incremental'
REFRESH_SYNC_MODE = 'refresh'
FULL_SYNC_MODE = 'full'
FULL_REFRESH_CLEANUP_STEP = 'Full refresh cleanup'
# Named once so checkpoint_step_names (below) and the per-company step list stay in sync —
# a step renamed in one place used to silently stop counting toward the checkpoint.
RECORDS_STEP = 'Записи'
FINANCIAL_TRANSACTIONS_STEP = 'Финансовые транзакции'
GOODS_TRANSACTIONS_STEP = 'Товарные транзакции'
COMMENTS_STEP = 'Комментарии'
PERSONAL_ACCOUNT_SOURCE = 'financial_transactions_detail'
APPOINTMENTS_SOURCE = 'appointments_detail'
GOODS_TRANSACTIONS_SOURCE = 'goods_transactions_detail'
STAFF_SCHEDULE_SOURCE = 'staff_schedules'
FULL_REFRESH_COVERAGE_SOURCES = (
    APPOINTMENTS_SOURCE,
    PERSONAL_ACCOUNT_SOURCE,
    GOODS_TRANSACTIONS_SOURCE,
)


def _company_timezone(company_data: dict, current: str | None = None) -> str:
    for value in (company_data.get('timezone'), current, DEFAULT_BRANCH_TIMEZONE):
        candidate = str(value or '').strip()
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue
        return candidate
    return DEFAULT_BRANCH_TIMEZONE


def _valid_staff_email(value) -> str | None:
    email = str(value or '').strip().casefold()
    local, sep, domain = email.partition('@')
    if not (local and sep and domain):
        return None
    if domain == 'portal.local' or '.' not in domain:
        return None
    return email


def format_duration(seconds: float) -> str:
    total_seconds = int(seconds)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин {secs} сек"
    if minutes:
        return f"{minutes} мин {secs} сек"
    return f"{seconds:.2f} сек"


def chunked(items: Iterable[int], size: int):
    items = list(items)
    batch_size = max(1, size)
    for idx in range(0, len(items), batch_size):
        yield items[idx:idx + batch_size]


def load_existing_branch_map(db, model, company_id: int, ids, id_column, source_type: str | None = None):
    """Rows of one branch by `id_column`; `source_type` narrows to one source for models that carry it."""
    existing = {}
    unique_ids = [item_id for item_id in dict.fromkeys(ids) if item_id is not None]
    for batch in chunked(unique_ids, DB_BATCH_SIZE):
        query = db.query(model).filter(model.company_id == company_id, id_column.in_(batch))
        if source_type is not None:
            query = query.filter(model.source_type == source_type)
        for obj in query.all():
            existing[getattr(obj, id_column.key)] = obj
    return existing


def load_existing_or_adopt_legacy_source_map(
    db,
    model,
    company_id: int,
    ids,
    id_column,
    source_type: str = SOURCE_YCLIENTS,
):
    ids = list(ids)  # walked twice: a generator would leave nothing to adopt
    existing = load_existing_branch_map(db, model, company_id, ids, id_column, source_type)
    missing = [
        external_id
        for external_id in dict.fromkeys(int(item_id) for item_id in ids if item_id is not None)
        if external_id not in existing
    ]
    # Rows from before external ids existed carry the YClients id as their primary key.
    for batch in chunked(missing, DB_BATCH_SIZE):
        for legacy in (
            db.query(model)
            .filter(model.id.in_(batch), model.company_id == company_id, model.external_id.is_(None))
            .all()
        ):
            legacy.external_id = legacy.id
            legacy.source_type = source_type
            existing[legacy.id] = legacy
    return existing


class ExternalPkAllocator:
    """Hands new rows their YClients id as primary key, unless the table already uses that key.

    Replaces one `db.get` per new row with one set query per chunk of ids. A row that does
    not get its own id is numbered by the database sequence, and that number may be an id
    still to come — the next lookup flushes and asks again, as the per-row `db.get` did.
    """

    def __init__(self, db, model, external_ids):
        self._db = db
        self._model = model
        self._unassigned = {int(item) for item in external_ids if item is not None}
        self._taken: set[int] = set()
        self._stale = True

    def kwargs(self, external_id: int) -> dict[str, int]:
        if self._stale:
            self._reload()
        self._unassigned.discard(external_id)
        if external_id in self._taken:
            self._stale = True
            return {}
        self._taken.add(external_id)
        return {'id': external_id}

    def _reload(self) -> None:
        self._db.flush()
        for batch in chunked(sorted(self._unassigned), DB_BATCH_SIZE):
            self._taken.update(
                row[0] for row in self._db.query(self._model.id).filter(self._model.id.in_(batch))
            )
        self._stale = False


def bulk_delete_by_ids(db, model, column, ids) -> int:
    deleted = 0
    unique_ids = [item_id for item_id in dict.fromkeys(ids) if item_id is not None]
    for batch in chunked(unique_ids, DB_BATCH_SIZE):
        deleted += (
            db.query(model)
            .filter(column.in_(batch))
            .delete(synchronize_session=False)
        )
    return deleted


def _apply_fields(obj, fields: dict) -> None:
    for name, value in fields.items():
        setattr(obj, name, value)


def _upsert_catalog(db, model, company_id: int, rows: list[dict], key_column, row_key, to_fields) -> None:
    """Insert or update one branch-scoped catalog row per payload row; rows without a key are skipped.

    `to_fields(row)` returns the columns shared by create and update; `updated_at` is added here.
    """
    now = utcnow()
    existing = load_existing_branch_map(db, model, company_id, (row_key(row) for row in rows), key_column)
    for row in rows:
        key = row_key(row)
        if key is None:
            continue
        fields = {**to_fields(row), 'updated_at': now}
        obj = existing.get(key)
        if not obj:
            obj = model(company_id=company_id, **{key_column.key: key}, **fields)
            db.add(obj)
            existing[key] = obj
        else:
            _apply_fields(obj, fields)


def _step_api(args, kwargs):
    """The YClients client a step was handed, if it was handed one."""
    candidate = kwargs.get('api', args[0] if args else None)
    return candidate if hasattr(candidate, 'last_dated_fetch_complete') else None


def _incomplete_window_note(api) -> str | None:
    """Report a step whose API told us its dated window may be short."""
    if api is None or api.last_dated_fetch_complete:
        return None
    return 'окно прочитано постранично, строки могли не приехать'


def _notify_progress(progress_callback, event: dict) -> None:
    """Deliver a progress event; progress is best-effort, so a failing callback must not abort the run."""
    try:
        progress_callback(event)
    except Exception as exc:
        print(f"  ! Не удалось записать прогресс: {type(exc).__name__}")


def run_sync_step(results, name: str, fn, *args, **kwargs):
    step_key = kwargs.pop('step_key', name)
    progress_callback = kwargs.pop('progress_callback', None)
    progress_context = kwargs.pop('progress_context', {}) or {}
    progress_pct = kwargs.pop('progress_pct', None)
    started_at = time.perf_counter()
    success = False
    step_error: str | None = None
    step_api = _step_api(args, kwargs)
    if step_api is not None:
        # The flag describes the window this step is about to read. Steps that never do a
        # dated fetch — catalogs — would otherwise inherit the verdict of
        # whichever step ran before them, on the client they all share.
        step_api.last_dated_fetch_complete = True
    try:
        success = bool(fn(*args, **kwargs))
        return success
    except Exception as e:
        # Fetch helpers raise rather than return False, and the call sits outside each
        # step's own try. Letting that unwind aborted every branch and tenant left in the
        # run over one endpoint's bad day, so the step is recorded as failed instead — the
        # same standing a step that returns False gets, no more. That is a real change for
        # steps outside `checkpoint_step_names` (clients, goods, schedules): a
        # raise anywhere used to abort the run outright; it now costs only this step, while
        # `error` keeps it distinguishable from a soft False so the run is still reported
        # failed. The traceback is printed because a raise here is as likely to be a bug in
        # this code as an unhappy endpoint.
        print(f"  ✗ Шаг прерван: {name}")
        traceback.print_exc()
        step_error = f'{type(e).__name__}: {e}'
        return False
    finally:
        elapsed = time.perf_counter() - started_at
        results.append({
            'name': name,
            'key': step_key,
            'success': success,
            'note': _incomplete_window_note(step_api),
            'error': step_error,
            'elapsed': elapsed,
        })
        if progress_callback is not None:
            _notify_progress(progress_callback, {
                **progress_context,
                'stage_key': step_key,
                'status': 'success' if success else 'warning',
                'elapsed_seconds': elapsed,
                'message': name,
                'progress_pct': progress_pct,
                'step_results': list(results),
            })
        status = 'OK' if success else 'WARN'
        print(f"  [{status}] {name}: {format_duration(elapsed)}")


def print_sync_summary(results):
    print("\n" + "=" * 60)
    print("  Итоги по этапам")
    print("=" * 60)
    for item in results:
        status = 'OK' if item['success'] else 'WARN'
        note = f"  ! {item['note']}" if item.get('note') else ''
        print(f"  [{status}] {item['name']:<28} {format_duration(item['elapsed'])}{note}")
    degraded = [item['name'] for item in results if item.get('note')]
    if degraded:
        # A window read through shifting page boundaries still counts as synced — refusing
        # it would pin the branch to full syncs forever — so it has to be visible here
        # instead, otherwise the only trace is one line buried in the step's own output.
        print(f"\n  Окна прочитаны постранично и могли прийти неполными: {len(degraded)}")
        for name in degraded:
            print(f"    - {name}")


def get_sync_state_value(db, key: str):
    state = db.get(SyncState, key)
    return state.value if state else None


def transactional_state_key(company_id: int) -> str:
    return f'{TRANSACTIONAL_STATE_KEY}:company:{int(company_id)}'


def historical_coverage_state_key(company_id: int) -> str:
    return f'{HISTORICAL_COVERAGE_STATE_KEY}:company:{int(company_id)}'


def full_sync_start_date(end_date: date) -> date:
    if SYNC_DAYS and SYNC_DAYS > 0:
        return end_date - timedelta(days=SYNC_DAYS)
    return min(SYNC_HISTORY_START_DATE, end_date)


def company_reporting_start(db, company_id: int) -> date | None:
    company = db.get(Company, int(company_id))
    return company.reporting_start_date if company is not None else None


def historical_sync_start_date(end_date: date, reporting_start: date | None = None) -> date:
    """Lower bound used when certifying the complete YClients history.

    A branch with a configured reporting start contributes no facts before it, so
    fetching earlier years would only reload rows every dashboard query discards.
    Branches without one keep the global floor, which is what a freshly onboarded
    tenant needs in order to discover its history in the first place.
    """
    floor = reporting_start if reporting_start is not None else SYNC_HISTORY_START_DATE
    return min(floor, end_date)


def refresh_sync_start_date(end_date: date, reporting_start: date | None = None) -> date:
    """Lower bound of the rolling window that re-reads recently changed facts.

    Incremental mode only re-reads `SYNC_LOOKBACK_DAYS` around its checkpoint, so a
    payment registered later than that never arrives at all: the visit stays in the
    database without its money, and the closed month under-reports revenue forever.
    Refresh mode reloads a wider trailing window instead of the whole history, which
    is cheap enough to run nightly. The branch floor still applies — a window that
    reaches past the reporting start would only fetch rows the dashboard discards.
    """
    floor = historical_sync_start_date(end_date, reporting_start)
    return max(floor, end_date - timedelta(days=max(0, SYNC_REFRESH_DAYS)))


def is_full_refresh_mode(mode: str | None) -> bool:
    """Both refresh and full drop their window before reloading it.

    Only the window differs, so every purge-and-reload decision keys off this instead
    of comparing against 'full' and silently leaving refresh as an append-only pass —
    which would keep transactions YClients has since deleted.
    """
    return (mode or '').strip().lower() in (REFRESH_SYNC_MODE, FULL_SYNC_MODE)


def has_complete_historical_source_coverage(
    db,
    company_id: int,
    through_date: date,
) -> bool:
    history_start = historical_sync_start_date(
        through_date,
        company_reporting_start(db, company_id),
    )
    covered_sources = {
        state.source
        for state in (
            db.query(SyncSourceState)
            .filter(
                SyncSourceState.company_id == int(company_id),
                SyncSourceState.source.in_(FULL_REFRESH_COVERAGE_SOURCES),
                SyncSourceState.period_start <= history_start,
                SyncSourceState.period_end >= through_date,
            )
            .all()
        )
    }
    return covered_sources == set(FULL_REFRESH_COVERAGE_SOURCES)


def has_valid_historical_coverage_marker(
    db,
    company_id: int,
) -> bool:
    raw_value = get_sync_state_value(db, historical_coverage_state_key(company_id))
    if not raw_value:
        return False
    try:
        certified_through = date.fromisoformat(raw_value)
    except ValueError:
        return False
    return has_complete_historical_source_coverage(
        db,
        company_id,
        certified_through,
    )


def resolve_transaction_window(db, end_date: date, state_key: str):
    full_start = full_sync_start_date(end_date)
    if not SYNC_INCREMENTAL:
        return full_start, FULL_SYNC_MODE

    raw_value = get_sync_state_value(db, state_key)
    if not raw_value:
        return full_start, FULL_SYNC_MODE

    try:
        last_success = date.fromisoformat(raw_value)
    except ValueError:
        return full_start, FULL_SYNC_MODE

    incremental_start = last_success - timedelta(days=max(0, SYNC_LOOKBACK_DAYS))
    return max(full_start, incremental_start), INCREMENTAL_SYNC_MODE


def resolve_sync_window(end_date: date, requested_mode: str):
    """Window reported for a run that resolved no branch (no credentials, nothing selected).

    Checkpoints are per branch, so there is none to read here: a windowed read has nothing
    behind it and reports the full history, like a branch without a checkpoint.
    """
    normalized_mode = (requested_mode or INCREMENTAL_SYNC_MODE).strip().lower()
    if normalized_mode == REFRESH_SYNC_MODE and SYNC_INCREMENTAL:
        return max(full_sync_start_date(end_date), refresh_sync_start_date(end_date)), REFRESH_SYNC_MODE
    return full_sync_start_date(end_date), FULL_SYNC_MODE


def resolve_company_sync_window(db, end_date: date, requested_mode: str, company_id: int):
    normalized_mode = (requested_mode or INCREMENTAL_SYNC_MODE).strip().lower()
    reporting_start = company_reporting_start(db, company_id)
    history_start = historical_sync_start_date(end_date, reporting_start)
    if normalized_mode == FULL_SYNC_MODE:
        return history_start, FULL_SYNC_MODE

    scoped_key = transactional_state_key(company_id)
    scoped_checkpoint = get_sync_state_value(db, scoped_key)
    if scoped_checkpoint and not has_valid_historical_coverage_marker(db, company_id):
        # One full pass is mandatory after introducing detailed source coverage.
        # Otherwise a legacy incremental checkpoint would only certify the recent
        # lookback window and every historical YoY value would remain unknown.
        return history_start, FULL_SYNC_MODE

    if normalized_mode == REFRESH_SYNC_MODE:
        # A branch that has never been synced needs the whole history, not a trailing
        # slice: refresh purges coverage for its window, and a branch with nothing
        # behind that window would be certified on a 90-day interval it cannot honor.
        # SYNC_INCREMENTAL=false is the operator's kill switch for windowed reads, so
        # it must silence refresh exactly as it silences incremental.
        if not SYNC_INCREMENTAL or not scoped_checkpoint:
            return history_start, FULL_SYNC_MODE
        return refresh_sync_start_date(end_date, reporting_start), REFRESH_SYNC_MODE

    if scoped_checkpoint:
        return resolve_transaction_window(db, end_date, scoped_key)

    return history_start, FULL_SYNC_MODE


def purge_source_window(db, model, company_id: int, start_date: str, end_date: str) -> int:
    """Drop one source's rows for a company inside the full-refresh window.

    Must be called from inside the transaction that reloads the same source. Deleting
    in a transaction of its own is what emptied the financial tables: the delete was
    committed, the reload then aborted on an id overflow, and the branch was left with
    no data at all.

    Datetime bounds are used for date-only columns too; a date compares equal to the
    start of its day, so the window semantics are unchanged.
    """
    if not start_date or not end_date:
        # An open-ended window would match the company's whole table; a purge is only
        # ever meant to clear the slice its reload is about to replace.
        raise ValueError('purge_source_window requires both bounds')
    query = db.query(model).filter(model.company_id == company_id)
    lower = parse_datetime_start(start_date)
    upper = parse_datetime_end(end_date)
    if lower is not None:
        query = query.filter(model.date >= lower)
    if upper is not None:
        query = query.filter(model.date <= upper)
    return query.delete(synchronize_session=False)


def purge_appointment_window(db, company_id: int, start_date: str, end_date: str) -> tuple[int, int]:
    """Drop appointments in the window together with the service transactions under them."""
    appointment_ids = [
        row[0]
        for row in (
            db.query(Appointment.id)
            .filter(
                Appointment.company_id == company_id,
                Appointment.date >= parse_datetime_start(start_date),
                Appointment.date <= parse_datetime_end(end_date),
            )
            .all()
        )
    ]
    deleted_transactions = (
        bulk_delete_by_ids(db, Transaction, Transaction.appointment_id, appointment_ids)
        if appointment_ids
        else 0
    )
    deleted_appointments = purge_source_window(db, Appointment, company_id, start_date, end_date)
    return deleted_appointments, deleted_transactions


def purge_full_refresh_window(db, company_id: int, start_date: str, end_date: str, schedule_end_date: str):
    """Invalidate coverage bookkeeping ahead of a full refresh.

    Fact rows are deliberately left alone here. Each source drops its own window inside
    the transaction that reloads it, so a source that fails to load keeps the data it
    had. Invalidating coverage up front is safe in the other direction: if the reload
    never lands, the branch simply stays in full mode until it does.
    """
    try:
        window_start = parse_date(start_date)
        # Each source must be invalidated over exactly the span it reloads. Appointments
        # run to the schedule horizon, so stopping at end_date would leave their future
        # tail in place and turn the purge into a middle slice — and a middle slice keeps
        # only the newer side of the one contiguous interval the row can hold, silently
        # discarding the branch's history. Harmless while full mode always started at
        # that history; a rolling refresh window ends there every night.
        source_window_ends = {APPOINTMENTS_SOURCE: parse_date(schedule_end_date)}
        default_window_end = parse_date(end_date)
        for source in FULL_REFRESH_COVERAGE_SOURCES:
            invalidate_sync_source_coverage(
                db,
                company_id,
                (source,),
                window_start,
                source_window_ends.get(source, default_window_end),
            )
        historical_state = db.get(
            SyncState,
            historical_coverage_state_key(company_id),
        )
        if historical_state is not None:
            db.delete(historical_state)
        db.commit()
        print("  ✓ Full refresh: покрытие источников сброшено")
        return True
    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка очистки перед full refresh: {e}")
        return False


def invalidate_sync_source_coverage(
    db,
    company_id: int,
    sources: Iterable[str],
    period_start: date,
    period_end: date,
) -> None:
    """Remove a purged interval without advertising a gap as synchronized."""
    states = (
        db.query(SyncSourceState)
        .filter(
            SyncSourceState.company_id == company_id,
            SyncSourceState.source.in_(tuple(sources)),
            SyncSourceState.period_start <= period_end,
            SyncSourceState.period_end >= period_start,
        )
        .all()
    )
    for state in states:
        left_end = period_start - timedelta(days=1)
        right_start = period_end + timedelta(days=1)
        has_left = state.period_start <= left_end
        has_right = right_start <= state.period_end
        if has_right:
            # One row can hold only one contiguous interval. Prefer the newer
            # side when a middle slice is removed; this never bridges the gap.
            state.period_start = right_start
        elif has_left:
            state.period_end = left_end
        else:
            db.delete(state)


def steps_successful(results, step_names) -> bool:
    named_steps = [item for item in results if item['key'] in step_names]
    return bool(named_steps) and all(item['success'] for item in named_steps)


def get_target_companies(db, company_ids: Iterable[int] | None = None):
    query = db.query(Company).filter(Company.id.isnot(None))
    if company_ids is not None:
        ids = [int(item) for item in company_ids]
        if not ids:
            return []
        query = query.filter(Company.id.in_(ids))
    return query.order_by(Company.title.asc(), Company.id.asc()).all()


def _build_api_for_credential(credential: YClientsCredentialValue) -> YClientsAPI | None:
    adapter = adapter_from_payload(
        SOURCE_YCLIENTS,
        partner_token=credential.partner_token,
        login=credential.login,
        password=credential.password,
        request_delay=YCLIENTS_REQUEST_DELAY,
        timeout=YCLIENTS_TIMEOUT,
        retry_total=YCLIENTS_RETRY_TOTAL,
        retry_backoff=YCLIENTS_RETRY_BACKOFF,
        retry_after_max=YCLIENTS_RETRY_AFTER_MAX,
    )
    if not adapter.authenticate():
        return None
    return adapter.build_sync_client()


def _undecryptable_credentials(db, loaded: list[YClientsCredentialValue], portal_account_id, credential_id):
    """Active credentials the loader skipped because they would not decrypt."""
    query = db.query(
        YClientsCredential.id, YClientsCredential.portal_account_id, YClientsCredential.title,
    ).filter(YClientsCredential.is_active.is_(True))
    if portal_account_id is not None:
        query = query.filter(YClientsCredential.portal_account_id == portal_account_id)
    if credential_id is not None:
        query = query.filter(YClientsCredential.id == int(credential_id))
    loaded_ids = {credential.id for credential in loaded}
    return [row for row in query.order_by(YClientsCredential.id).all() if row.id not in loaded_ids]


def format_company_label(company: Company) -> str:
    title = (company.title or '').strip() or f'Company {company.id}'
    external_id = getattr(company, 'external_id', None)
    suffix = company.id if external_id in (None, company.id) else f'{company.id}/yc:{external_id}'
    return f"{title} ({suffix})"


def _db_company_id(company_id: str, db_company_id: int | None = None) -> int:
    return int(db_company_id if db_company_id is not None else company_id)


def _company_external_id(company: Company) -> int:
    return int(getattr(company, 'external_id', None) or company.id)


def _client_external_id(client_data) -> int | None:
    if not isinstance(client_data, dict):
        return None
    value = client_data.get('id')
    return int(value) if value is not None else None


def _prepare_client_map(db, company_id: int, client_payloads: Iterable[dict]) -> dict[int, Client]:
    payload_by_external_id: dict[int, dict] = {}
    for payload in client_payloads:
        external_id = _client_external_id(payload)
        if external_id is not None:
            payload_by_external_id[external_id] = payload

    existing_clients = load_existing_or_adopt_legacy_source_map(
        db,
        Client,
        company_id,
        payload_by_external_id.keys(),
        Client.external_id,
    )
    changed = False
    for external_id, payload in payload_by_external_id.items():
        obj = existing_clients.get(external_id)
        if obj is None:
            obj = Client(
                external_id=external_id,
                source_type=SOURCE_YCLIENTS,
                name=payload.get('name', '') or '',
                phone=payload.get('phone'),
                email=payload.get('email'),
                birth_date=parse_date(payload.get('birth_date')),
                visits_count=payload.get('visits_count', 0),
                last_visit_date=parse_date(payload.get('last_visit_date')),
                discount=payload.get('discount', 0),
                company_id=company_id,
            )
            db.add(obj)
            existing_clients[external_id] = obj
            changed = True
        else:
            if payload.get('name') is not None:
                obj.name = payload.get('name') or ''
            if payload.get('phone') is not None:
                obj.phone = payload.get('phone')
            if payload.get('email') is not None:
                obj.email = payload.get('email')

    if changed:
        db.flush()
    return existing_clients


def _internal_client_id(client_map: dict[int, Client], client_data) -> int | None:
    external_id = _client_external_id(client_data)
    if external_id is None:
        return None
    client = client_map.get(external_id)
    return int(client.id) if client is not None and client.id is not None else None


def _payload_id(payload) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get('id')
    return int(value) if value is not None else None


def _staff_external_id(value) -> int | None:
    if isinstance(value, dict):
        value = value.get('id')
    return int(value) if value is not None else None


def _load_staff_map(db, company_id: int, staff_ids: Iterable[int | None]) -> dict[int, Staff]:
    external_ids = [int(item) for item in dict.fromkeys(staff_ids) if item is not None]
    return load_existing_or_adopt_legacy_source_map(
        db,
        Staff,
        company_id,
        external_ids,
        Staff.external_id,
    )


def _internal_staff_id(staff_map: dict[int, Staff], external_staff_id) -> int | None:
    external_id = _staff_external_id(external_staff_id)
    if external_id is None:
        return None
    staff = staff_map.get(external_id)
    return int(staff.id) if staff is not None and staff.id is not None else None


# ===================================================================
# 1. Сети и компании
# ===================================================================

def sync_groups_and_companies(api: YClientsAPI, db, portal_account_id: int):
    print("\n── Сети и компании ──")

    groups = api.get_groups()
    if not groups:
        return False

    print(f"  Найдено сетей: {len(groups)}")

    try:
        group_ids = [group_data.get('id') for group_data in groups if group_data.get('id') is not None]
        company_ids = [
            company_data.get('id')
            for group_data in groups
            for company_data in (group_data.get('companies') or [])
            if company_data.get('id') is not None
        ]
        existing_groups = {
            group.external_id: group
            for group in (
                db.query(Group)
                .filter(
                    Group.portal_account_id == portal_account_id,
                    Group.external_id.in_(group_ids),
                )
                .all()
            )
        }
        existing_companies = {
            company.external_id: company
            for company in (
                db.query(Company)
                .filter(
                    Company.portal_account_id == portal_account_id,
                    Company.source_type == SOURCE_YCLIENTS,
                    Company.external_id.in_(company_ids),
                )
                .all()
            )
        }

        for group_data in groups:
            group_id = group_data.get('id')
            if group_id is None:
                continue

            group = existing_groups.get(group_id)
            if not group:
                legacy_group = db.get(Group, group_id)
                if legacy_group is not None and legacy_group.external_id is None and (
                    legacy_group.portal_account_id is None or legacy_group.portal_account_id == portal_account_id
                ):
                    group = legacy_group
                    group.title = group_data.get('title', '') or group.title
                    group.portal_account_id = portal_account_id
                    group.external_id = group_id
                else:
                    group = Group(
                        title=group_data.get('title', ''),
                        portal_account_id=portal_account_id,
                        external_id=group_id,
                    )
                    db.add(group)
                    db.flush()
            else:
                group.title = group_data.get('title', '') or group.title
                group.portal_account_id = group.portal_account_id or portal_account_id
                group.external_id = group.external_id or group_id

            for company_data in group_data.get('companies') or []:
                company_id = company_data.get('id')
                if company_id is None:
                    continue

                company = existing_companies.get(company_id)
                if not company:
                    legacy_company = db.get(Company, company_id)
                    if (
                        legacy_company is not None
                        and legacy_company.external_id is None
                        and (
                            legacy_company.portal_account_id is None
                            or legacy_company.portal_account_id == portal_account_id
                        )
                    ):
                        company = legacy_company
                        company.title = company_data.get('title', '') or company.title
                        company.group_id = group.id
                        company.portal_account_id = portal_account_id
                        company.external_id = company_id
                        company.source_type = SOURCE_YCLIENTS
                        company.timezone = _company_timezone(company_data, company.timezone)
                    else:
                        db.add(Company(
                            title=company_data.get('title', ''),
                            group_id=group.id,
                            portal_account_id=portal_account_id,
                            external_id=company_id,
                            source_type=SOURCE_YCLIENTS,
                            timezone=_company_timezone(company_data),
                        ))
                else:
                    # An answer without a title must not blank the stored one (materialize_branches does the same).
                    company.title = company_data.get('title', '') or company.title
                    company.group_id = group.id
                    company.portal_account_id = company.portal_account_id or portal_account_id
                    company.external_id = company.external_id or company_id
                    company.timezone = _company_timezone(company_data, company.timezone)

        db.commit()
        print("  ✓ Сети и компании сохранены")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 2. Категории услуг
# ===================================================================

def sync_service_categories(api: YClientsAPI, db, company_id: str, db_company_id: int | None = None):
    print("\n── Категории услуг ──")

    categories = api.get_service_categories(company_id)
    if not categories:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(categories)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        _upsert_catalog(
            db,
            ServiceCategoryCatalog,
            cid,
            categories,
            ServiceCategoryCatalog.category_id,
            lambda c: c.get('id'),
            lambda c: {'title': c.get('title', ''), 'weight': c.get('weight'), 'api_id': c.get('api_id')},
        )

        db.commit()
        print(f"  ✓ Категории услуг сохранены ({len(categories)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 3. Услуги
# ===================================================================

def sync_services(api: YClientsAPI, db, company_id: str, db_company_id: int | None = None):
    print("\n── Услуги ──")

    services = api.get_services(company_id)
    if services is None:
        print("  Источник недоступен")
        return False

    print(f"  Найдено: {len(services)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        if any(
            not isinstance(service, dict)
            or service.get('id') is None
            or (
                service.get('category')
                and not isinstance(service.get('category'), dict)
            )
            for service in services
        ):
            raise ValueError('invalid services snapshot')
        category_by_service_id = {}
        if services and not any(service.get('category') for service in services):
            category_rows = db.query(ServiceCategoryCatalog).filter(
                ServiceCategoryCatalog.company_id == cid,
            ).all()
            for category in category_rows:
                category_services = api.get_services(company_id, category_id=category.category_id)
                if category_services is None:
                    raise ValueError('service category snapshot is unavailable')
                if any(
                    not isinstance(category_service, dict)
                    or category_service.get('id') is None
                    for category_service in category_services
                ):
                    raise ValueError('invalid service category snapshot')
                for category_service in category_services:
                    category_service_id = category_service.get('id')
                    category_by_service_id[category_service_id] = {
                        'id': category.category_id,
                        'title': category.title,
                    }

        # A successful company-level response is the current YClients snapshot. Keep
        # historical catalog rows for old transactions, but hide anything not seen now.
        db.query(ServiceCatalog).filter(ServiceCatalog.company_id == cid).update(
            {ServiceCatalog.is_active: False},
            synchronize_session='fetch',
        )

        def service_fields(service_data: dict) -> dict:
            service_id = service_data.get('id')
            category_title = None
            category_id = None
            if 'category' in service_data and service_data['category']:
                category_id = service_data['category'].get('id')
                category_title = service_data['category'].get('title')
            elif service_id in category_by_service_id:
                category_id = category_by_service_id[service_id].get('id')
                category_title = category_by_service_id[service_id].get('title')
            return {
                'title': service_data.get('title', ''),
                'price_min': service_data.get('price_min'),
                'duration': service_data.get('duration'),
                'category_id': category_id,
                'category_title': category_title,
                'is_active': True,
            }

        _upsert_catalog(
            db, ServiceCatalog, cid, services, ServiceCatalog.service_id, lambda s: s.get('id'), service_fields
        )

        db.commit()
        print(f"  ✓ Услуги сохранены ({len(services)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 5. Сотрудники
# ===================================================================

POSITION_ID_MAX = 2**31 - 1  # `staff.position_id` is INTEGER: one out-of-range id must not fail the whole step


def _position_external_id(value) -> int | None:
    try:
        position_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return position_id if 0 < position_id <= POSITION_ID_MAX else None


def _snapshot_staff_positions(db, company_id: int, positions: dict[int, tuple[int | None, str | None]]) -> None:
    """Record the current position of non-fired staff in the branch's current month (the last sync of the month wins).

    Fired staff are skipped: YClients often leaves a stale position on them, and the level history is
    only read for people who still work.
    """
    if not positions:
        return
    month = _branch_today(db, company_id).replace(day=1)
    now = utcnow()
    stored = {
        row.staff_id: row
        for row in db.query(StaffPositionMonth).filter(
            StaffPositionMonth.month == month,
            StaffPositionMonth.staff_id.in_(list(positions)),
        )
    }
    for staff_id, (position_id, position_title) in positions.items():
        fields = {
            'company_id': company_id,
            'position_id': position_id,
            'position_title': position_title,
            'updated_at': now,
        }
        row = stored.get(staff_id)
        if row is None:
            db.add(StaffPositionMonth(staff_id=staff_id, month=month, **fields))
        else:
            _apply_fields(row, fields)


def sync_staff(api: YClientsAPI, db, company_id: str, db_company_id: int | None = None):
    print("\n── Сотрудники ──")

    staff_list = api.get_staff(company_id)
    if not staff_list:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(staff_list)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        staff_ids = {int(staff_member.get('id')) for staff_member in staff_list if staff_member.get('id') is not None}
        existing_staff = _load_staff_map(db, cid, staff_ids)
        pk_allocator = ExternalPkAllocator(db, Staff, staff_ids)
        active_staff_ids: set[int] = set()
        month_positions: dict[int, tuple[int | None, str | None]] = {}
        for s in staff_list:
            staff_id = _staff_external_id(s)
            if staff_id is None:
                continue

            position_title = None
            position_id = None
            pos = s.get('position')
            if isinstance(pos, dict):
                position_title = pos.get('title')
                position_id = _position_external_id(pos.get('id'))
            elif isinstance(pos, str):
                position_title = pos

            user_id = s.get('user_id')
            fired = int(bool(s.get('is_fired'))) if s.get('fired') is None else int(s.get('fired') or 0)
            email = _valid_staff_email(s.get('email'))

            fields = {
                'external_id': staff_id,
                'source_type': SOURCE_YCLIENTS,
                'name': s.get('name', ''),
                'email': email,
                'specialization': s.get('specialization'),
                'position': position_title,
                'position_id': position_id,
                'rating': s.get('rating'),
                'votes_count': s.get('votes_count'),
                'bookable': s.get('bookable', True),
                'fired': fired,
                'user_id': user_id,
                'company_id': cid,
            }
            obj = existing_staff.get(staff_id)
            if not obj:
                obj = Staff(**pk_allocator.kwargs(staff_id), **fields)
                db.add(obj)
                db.flush()
                existing_staff[staff_id] = obj
            else:
                _apply_fields(obj, fields)

            if obj.id is not None and not fired:
                active_staff_ids.add(int(obj.id))
                month_positions[int(obj.id)] = (position_id, position_title)

        # Portal-created rows never appear in the YClients list, so firing every
        # unlisted row made portal managers vanish from «Работник» after each sync.
        # Their `fired` belongs to the portal, exactly as this branch owns the CRM rows.
        # A mirror row can land on its own user's id; it has no external_id, so only that tells it from a CRM row.
        stale_query = db.query(Staff).filter(
            Staff.company_id == cid,
            or_(Staff.portal_user_id.is_(None), crm_staff_clause(Staff.portal_user_id)),
        )
        if active_staff_ids:
            stale_query = stale_query.filter(~Staff.id.in_(active_staff_ids))
        stale_query.update({Staff.fired: 1}, synchronize_session=False)
        _snapshot_staff_positions(db, cid, month_positions)

        db.commit()
        print(f"  ✓ Сотрудники сохранены ({len(staff_list)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 6. Клиенты
# ===================================================================

def sync_clients(api: YClientsAPI, db, company_id: str, db_company_id: int | None = None):
    print("\n── Клиенты ──")

    clients = api.get_clients(company_id)
    if not clients:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(clients)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        existing_clients = load_existing_or_adopt_legacy_source_map(
            db,
            Client,
            cid,
            (_client_external_id(client) for client in clients),
            Client.external_id,
        )
        for c in clients:
            client_id = _client_external_id(c)
            if client_id is None:
                continue

            fields = {
                'name': c.get('name', ''),
                'phone': c.get('phone'),
                'email': c.get('email'),
                'birth_date': parse_date(c.get('birth_date')),
                'visits_count': c.get('visits_count', 0),
                'last_visit_date': parse_date(c.get('last_visit_date')),
                'discount': c.get('discount', 0),
                'company_id': cid,
            }
            obj = existing_clients.get(client_id)
            if not obj:
                obj = Client(external_id=client_id, source_type=SOURCE_YCLIENTS, **fields)
                db.add(obj)
                # A listing page boundary can repeat a client; the repeat must update this row, not insert a twin.
                existing_clients[client_id] = obj
            else:
                _apply_fields(obj, fields)

        db.commit()
        print(f"  ✓ Клиенты сохранены ({len(clients)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 7. Кассы
# ===================================================================

def sync_accounts(api: YClientsAPI, db, company_id: str, db_company_id: int | None = None):
    print("\n── Кассы ──")

    accounts = api.get_accounts(company_id)
    if not accounts:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(accounts)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        _upsert_catalog(
            db,
            AccountCatalog,
            cid,
            accounts,
            AccountCatalog.account_id,
            lambda a: a.get('id'),
            lambda a: {'title': a.get('title', ''), 'type': a.get('type'), 'comment': a.get('comment')},
        )

        db.commit()
        print(f"  ✓ Кассы сохранены ({len(accounts)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 9. Категории товаров
# ===================================================================

def sync_good_categories(api: YClientsAPI, db, company_id: str, db_company_id: int | None = None):
    print("\n── Категории товаров ──")

    categories = api.get_good_categories(company_id)
    if not categories:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(categories)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        _upsert_catalog(
            db,
            GoodCategoryCatalog,
            cid,
            categories,
            GoodCategoryCatalog.category_id,
            lambda c: c.get('id'),
            lambda c: {'title': c.get('title', ''), 'parent_category_id': c.get('parent_category_id')},
        )

        db.commit()
        print(f"  ✓ Категории товаров сохранены ({len(categories)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 10. Товары
# ===================================================================

def sync_goods(api: YClientsAPI, db, company_id: str, db_company_id: int | None = None):
    print("\n── Товары ──")

    goods = api.get_goods(company_id)
    if not goods:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(goods)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        _upsert_catalog(
            db,
            GoodCatalog,
            cid,
            goods,
            GoodCatalog.good_id,
            lambda g: g.get('good_id') or g.get('id') or None,
            lambda g: {
                'title': g.get('title', ''),
                'cost': g.get('cost'),
                'actual_cost': g.get('actual_cost'),
                'barcode': g.get('barcode'),
                'unit_short_title': g.get('unit_short_title'),
                'category_id': g.get('category_id'),
                'last_change_date': parse_datetime(g.get('last_change_date')),
            },
        )

        db.commit()
        print(f"  ✓ Товары сохранены ({len(goods)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 12. Записи (appointments) и транзакции (услуги внутри записи)
# ===================================================================

def _settle_empty_source_window(
    db, company_id: str, source: str, start_date: str | None, end_date: str | None,
    db_company_id: int | None,
) -> bool:
    """Record that a source returned no rows for the period so the gap is not re-fetched forever.

    Deliberately does not delete, even in a purge-and-reload mode. An empty list is the
    least verified answer the client can hand back — one well-formed
    `{"success": true, "data": []}` on the first probe decides a window of any width, and
    looks identical to a branch that genuinely has nothing — so deleting on it could drop
    a whole history with nothing to restore from. Rows YClients removed inside a window
    that then answers empty linger until a pass that returns data purges them, which is
    the recoverable direction and the same one `_window_safe_to_purge` chooses.
    """
    if not start_date or not end_date:
        return True
    try:
        cid = _db_company_id(company_id, db_company_id)
        if db is not None:
            mark_sync_source_coverage(db, cid, source, start_date, end_date)
            db.commit()
        return True
    except Exception as e:
        if db is not None:
            db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


def sync_records(api: YClientsAPI, db, company_id: str,
                 start_date: str = None, end_date: str = None,
                 db_company_id: int | None = None, full_refresh: bool = False):
    print("\n── Записи (визиты) ──")

    records = api.get_records(company_id, start_date=start_date, end_date=end_date)
    if records is None:
        print("  Источник недоступен")
        return False
    if not records:
        print("  Нет записей за указанный период")
        return _settle_empty_source_window(
            db, company_id, APPOINTMENTS_SOURCE, start_date, end_date, db_company_id
        )

    print(f"  Найдено: {len(records)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        if _window_safe_to_purge(api, full_refresh, 'записи'):
            purged, purged_tx = purge_appointment_window(db, cid, start_date, end_date)
            print(f"  Full refresh: удалено записей {purged}, транзакций {purged_tx}")
        tx_count = 0
        record_ids = [r.get('id') for r in records if r.get('id') is not None]
        existing_records = load_existing_or_adopt_legacy_source_map(
            db,
            Appointment,
            cid,
            record_ids,
            Appointment.external_id,
        )
        deleted_tx = bulk_delete_by_ids(
            db,
            Transaction,
            Transaction.appointment_id,
            (record.id for record in existing_records.values()),
        )
        client_map = _prepare_client_map(
            db,
            cid,
            (record.get('client') or {} for record in records),
        )
        staff_map = _load_staff_map(
            db,
            cid,
            (record.get('staff_id') for record in records),
        )

        appointments_with_services = []
        for r in records:
            record_id = r.get('id')
            if record_id is None:
                continue

            client_data = r.get('client') or {}
            client_id = _internal_client_id(client_map, client_data)
            staff_id = _internal_staff_id(staff_map, r.get('staff_id'))
            created_user_id = r.get('created_user_id')

            fields = {
                'staff_id': staff_id,
                'client_id': client_id,
                'created_user_id': created_user_id,
                'date': parse_date(r.get('date')),
                'datetime': parse_datetime(r.get('datetime')),
                'create_date': parse_datetime(r.get('create_date')),
                'seance_length': r.get('seance_length'),
                'attendance': r.get('attendance', 0),
                'comment': r.get('comment'),
            }
            obj = existing_records.get(record_id)
            if not obj:
                obj = Appointment(external_id=record_id, source_type=SOURCE_YCLIENTS, company_id=cid, **fields)
                db.add(obj)
                # A long paginated fetch can return the same record twice when upstream
                # rows shift between pages. Without this the second copy looks new and
                # collides on uq_appointments_company_source_external.
                existing_records[record_id] = obj
            else:
                _apply_fields(obj, fields)

            appointments_with_services.append((obj, r.get('services') or []))

        # One flush for the whole window instead of one per new appointment: the ids
        # come back in insertion order, so they are the ones the per-row flush handed out.
        db.flush()
        for obj, services in appointments_with_services:
            for svc in services:
                tx = Transaction(
                    appointment_id=obj.id,
                    service_id=svc.get('id'),
                    service_title=svc.get('title', ''),
                    cost=svc.get('cost'),
                    first_cost=svc.get('first_cost'),
                    amount=svc.get('amount', 1),
                    company_id=cid,
                )
                db.add(tx)
                tx_count += 1

        mark_sync_source_coverage(
            db, cid, APPOINTMENTS_SOURCE, start_date, end_date
        )
        db.commit()
        print(
            f"  ✓ Записи сохранены ({len(records)} шт.), "
            f"пересобрано транзакций: {tx_count}, удалено старых: {deleted_tx}"
        )
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 13. Финансовые транзакции
# ===================================================================

def mark_sync_source_coverage(
    db,
    company_id: int,
    source: str,
    start_date: str | None,
    end_date: str | None,
):
    if not start_date or not end_date:
        return
    state = db.get(
        SyncSourceState,
        {'company_id': company_id, 'source': source},
    )
    period_start = parse_date(start_date)
    period_end = parse_date(end_date)
    if state is None:
        state = SyncSourceState(
            company_id=company_id,
            source=source,
            period_start=period_start,
            period_end=period_end,
        )
        db.add(state)
    else:
        intervals_touch = (
            period_start <= state.period_end + timedelta(days=1)
            and period_end >= state.period_start - timedelta(days=1)
        )
        if intervals_touch:
            state.period_start = min(state.period_start, period_start)
            state.period_end = max(state.period_end, period_end)
        elif period_end > state.period_end:
            # The schema stores one contiguous interval. Never bridge a gap and
            # falsely advertise it as complete; retain the newest interval.
            state.period_start = period_start
            state.period_end = period_end


def _window_safe_to_purge(api, full_refresh: bool, source_label: str) -> bool:
    """Refuse to drop a window the fetch could not read reliably.

    Purge-and-reload only holds when the reload is complete. A day too busy for one page
    is read through the shifting boundaries the split exists to avoid, so deleting the
    window would strip rows the fetch never returned. Skipping the purge degrades to
    append-only for that window: rows deleted upstream linger until the next clean pass,
    which is the recoverable direction.
    """
    if not full_refresh:
        return False
    if getattr(api, 'last_dated_fetch_complete', True):
        return True
    print(f"  ! Окно прочитано постранично, очистка пропущена ({source_label})")
    return False


def sync_financial_transactions(api: YClientsAPI, db, company_id: str,
                                start_date: str = None, end_date: str = None,
                                db_company_id: int | None = None, full_refresh: bool = False):
    print("\n── Финансовые транзакции ──")

    txns = api.get_financial_transactions(company_id,
                                          start_date=start_date,
                                          end_date=end_date)
    if txns is None:
        print("  Источник недоступен")
        return False
    if not txns:
        print("  Нет данных")
        return _settle_empty_source_window(
            db, company_id, PERSONAL_ACCOUNT_SOURCE, start_date, end_date, db_company_id,
        )

    try:
        cid = _db_company_id(company_id, db_company_id)
        print(f"  Найдено: {len(txns)}")
        if _window_safe_to_purge(api, full_refresh, 'финансовые транзакции'):
            purged = purge_source_window(db, FinancialTransaction, cid, start_date, end_date)
            print(f"  Full refresh: удалено транзакций {purged}")
        existing_txns = load_existing_or_adopt_legacy_source_map(
            db,
            FinancialTransaction,
            cid,
            (txn.get('id') for txn in txns),
            FinancialTransaction.external_id,
        )
        pk_allocator = ExternalPkAllocator(db, FinancialTransaction, (_payload_id(t) for t in txns))
        client_map = _prepare_client_map(
            db,
            cid,
            (txn.get('client') or {} for txn in txns),
        )
        staff_map = _load_staff_map(
            db,
            cid,
            (
                _staff_external_id(txn.get('master') or {})
                for txn in txns
                if isinstance(txn.get('master') or {}, dict)
            ),
        )
        for t in txns:
            tid = _payload_id(t)
            if tid is None:
                continue
            obj = existing_txns.get(tid)

            account = t.get('account') or {}
            client = t.get('client') or {}
            master = t.get('master') or {}
            expense = t.get('expense') or {}
            expense_title = expense.get('title') if isinstance(expense, dict) else None
            internal_client_id = _internal_client_id(client_map, client)
            internal_master_id = _internal_staff_id(staff_map, master) if isinstance(master, dict) else None

            fields = {
                'external_id': tid,
                'source_type': SOURCE_YCLIENTS,
                'document_id': parse_int(t.get('document_id')),
                'expense_id': parse_int(expense.get('id')) if isinstance(expense, dict) else None,
                'expense_title': expense_title,
                'date': parse_datetime(t.get('date')),
                'amount': t.get('amount'),
                'comment': t.get('comment'),
                'account_id': parse_int(account.get('id')) if isinstance(account, dict) else None,
                'client_id': internal_client_id,
                'master_id': internal_master_id,
                # `record_id: 0` means the sale had no visit behind it; visit_id
                # carries the same sentinel but nothing keys off it.
                'record_id': parse_reference_id(t.get('record_id')),
                'visit_id': parse_int(t.get('visit_id')),
                'sold_item_id': parse_int(t.get('sold_item_id')),
                'sold_item_type': t.get('sold_item_type'),
                'company_id': cid,
            }
            if not obj:
                obj = FinancialTransaction(**pk_allocator.kwargs(tid), **fields)
                db.add(obj)
                existing_txns[tid] = obj
            else:
                _apply_fields(obj, fields)

        mark_sync_source_coverage(db, cid, PERSONAL_ACCOUNT_SOURCE, start_date, end_date)

        db.commit()
        print(f"  ✓ Финансовые транзакции сохранены ({len(txns)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 14. Товарные транзакции
# ===================================================================

def sync_goods_transactions(api: YClientsAPI, db, company_id: str,
                            start_date: str = None, end_date: str = None,
                            db_company_id: int | None = None, full_refresh: bool = False):
    print("\n── Товарные транзакции ──")

    txns = api.get_goods_transactions(company_id,
                                      start_date=start_date,
                                      end_date=end_date)
    if txns is None:
        print("  Источник недоступен")
        return False
    if not txns:
        print("  Нет данных")
        return _settle_empty_source_window(
            db, company_id, GOODS_TRANSACTIONS_SOURCE, start_date, end_date, db_company_id,
        )

    print(f"  Найдено: {len(txns)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        if _window_safe_to_purge(api, full_refresh, 'товарные транзакции'):
            purged = purge_source_window(db, GoodTransaction, cid, start_date, end_date)
            print(f"  Full refresh: удалено транзакций {purged}")
        existing_txns = load_existing_or_adopt_legacy_source_map(
            db,
            GoodTransaction,
            cid,
            (txn.get('id') for txn in txns),
            GoodTransaction.external_id,
        )
        pk_allocator = ExternalPkAllocator(db, GoodTransaction, (_payload_id(t) for t in txns))
        client_map = _prepare_client_map(
            db,
            cid,
            (txn.get('client') or {} for txn in txns),
        )
        staff_map = _load_staff_map(
            db,
            cid,
            (
                _staff_external_id(txn.get('master') or {})
                for txn in txns
                if isinstance(txn.get('master') or {}, dict)
            ),
        )
        for t in txns:
            tid = _payload_id(t)
            if tid is None:
                continue
            obj = existing_txns.get(tid)

            good = t.get('good') or {}
            storage = t.get('storage') or {}
            master = t.get('master') or {}
            client = t.get('client') or {}
            good_id = good.get('id') if isinstance(good, dict) else None
            good_title = good.get('title') if isinstance(good, dict) else None
            storage_id = storage.get('id') if isinstance(storage, dict) else None
            storage_title = storage.get('title') if isinstance(storage, dict) else None
            internal_client_id = _internal_client_id(client_map, client)
            internal_master_id = _internal_staff_id(staff_map, master) if isinstance(master, dict) else None

            tx_date = parse_datetime(t.get('create_date') or t.get('date'))

            fields = {
                'external_id': tid,
                'source_type': SOURCE_YCLIENTS,
                'document_id': t.get('document_id'),
                'type_id': t.get('type_id'),
                'good_id': good_id,
                'good_title': good_title,
                'storage_id': storage_id,
                'storage_title': storage_title,
                'amount': t.get('amount'),
                'cost_per_unit': t.get('cost_per_unit'),
                'cost': t.get('cost'),
                'discount': t.get('discount'),
                'master_id': internal_master_id,
                'client_id': internal_client_id,
                'company_id': cid,
                'date': tx_date,
            }
            if not obj:
                obj = GoodTransaction(**pk_allocator.kwargs(tid), **fields)
                db.add(obj)
                existing_txns[tid] = obj
            else:
                _apply_fields(obj, fields)

        mark_sync_source_coverage(
            db, cid, GOODS_TRANSACTIONS_SOURCE, start_date, end_date
        )
        db.commit()
        print(f"  ✓ Товарные транзакции сохранены ({len(txns)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 15. Комментарии / отзывы
# ===================================================================

def sync_comments(api: YClientsAPI, db, company_id: str,
                  start_date: str = None, end_date: str = None,
                  db_company_id: int | None = None, full_refresh: bool = False):
    print("\n── Комментарии / отзывы ──")

    comments = api.get_comments(company_id,
                                start_date=start_date,
                                end_date=end_date)
    if comments is None:
        print("  Источник недоступен")
        return False
    if not comments:
        print("  Нет данных")
        # No coverage source is tracked for comments, and an empty answer never deletes.
        return True

    print(f"  Найдено: {len(comments)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        if _window_safe_to_purge(api, full_refresh, 'комментарии'):
            purged = purge_source_window(db, Comment, cid, start_date, end_date)
            print(f"  Full refresh: удалено комментариев {purged}")
        existing_comments = load_existing_or_adopt_legacy_source_map(
            db,
            Comment,
            cid,
            (comment.get('id') for comment in comments),
            Comment.external_id,
        )
        pk_allocator = ExternalPkAllocator(db, Comment, (_payload_id(c) for c in comments))
        staff_map = _load_staff_map(
            db,
            cid,
            (comment.get('master_id') for comment in comments),
        )
        for c in comments:
            cmt_id = _payload_id(c)
            if cmt_id is None:
                continue
            obj = existing_comments.get(cmt_id)
            master_id = _internal_staff_id(staff_map, c.get('master_id'))
            fields = {
                'external_id': cmt_id,
                'source_type': SOURCE_YCLIENTS,
                'type': c.get('type'),
                'master_id': master_id,
                'text': c.get('text'),
                'date': parse_datetime(c.get('date')),
                'rating': c.get('rating'),
                'user_id': c.get('user_id'),
                'user_name': c.get('user_name'),
                'record_id': c.get('record_id'),
                'company_id': cid,
            }
            if not obj:
                obj = Comment(**pk_allocator.kwargs(cmt_id), **fields)
                db.add(obj)
                existing_comments[cmt_id] = obj
            else:
                _apply_fields(obj, fields)

        db.commit()
        print(f"  ✓ Комментарии сохранены ({len(comments)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 20. Графики работы сотрудников
# ===================================================================

def _branch_today(db, company_id: int) -> date:
    timezone = _company_timezone({}, getattr(db.get(Company, company_id), 'timezone', None))
    return datetime.now(ZoneInfo(timezone)).date()


def sync_staff_schedules(api: YClientsAPI, db, company_id: str,
                         start_date: str = None, end_date: str = None,
                         db_company_id: int | None = None):
    print("\n── Графики работы сотрудников ──")

    schedules = api.get_staff_schedule(company_id,
                                       start_date=start_date,
                                       end_date=end_date)
    if schedules is None:
        print("  Источник недоступен")
        return False

    print(f"  Найдено записей расписания: {len(schedules)}")

    try:
        cid = _db_company_id(company_id, db_company_id)
        staff_map = _load_staff_map(
            db,
            cid,
            (
                entry.get('staff_id')
                for entry in schedules
                if isinstance(entry, dict)
            ),
        )
        parsed_start = parse_date(start_date)
        parsed_end = parse_date(end_date)
        if start_date and parsed_start is None:
            raise ValueError('invalid staff schedule window start')
        if end_date and parsed_end is None:
            raise ValueError('invalid staff schedule window end')

        parsed_slots: list[tuple[int, date, object, object]] = []
        seen_slots: set[tuple[int, date, object, object]] = set()
        skipped_entries = 0
        for entry in schedules:
            if not isinstance(entry, dict):
                raise ValueError('invalid staff schedule entry')
            staff_id = _internal_staff_id(staff_map, entry.get('staff_id'))
            schedule_date = parse_date(entry.get('date'))
            slots = entry.get('slots')
            if staff_id is None or schedule_date is None:
                raise ValueError('staff schedule entry cannot be mapped')
            # From 20:00 UTC until our UTC date rolls over, YClients also returns the day
            # before start_date. That day is outside the window replaced below, and storing
            # it again doubled every day's slots; anything further out still fails closed.
            if parsed_start is not None and schedule_date == parsed_start - timedelta(days=1):
                skipped_entries += 1
                continue
            if parsed_start is not None and schedule_date < parsed_start:
                raise ValueError('staff schedule entry is outside the requested window')
            if parsed_end is not None and schedule_date > parsed_end:
                raise ValueError('staff schedule entry is outside the requested window')
            if slots is None:
                slots = []
            if not isinstance(slots, list):
                raise ValueError('invalid staff schedule slots')
            for slot in slots:
                if not isinstance(slot, dict):
                    raise ValueError('invalid staff schedule slot')
                slot_from = parse_time(slot.get('from'))
                slot_to = parse_time(slot.get('to'))
                if slot_from is None or slot_to is None:
                    raise ValueError('invalid staff schedule slot time')
                slot_key = (staff_id, schedule_date, slot_from, slot_to)
                if slot_key in seen_slots:
                    continue
                seen_slots.add(slot_key)
                parsed_slots.append(slot_key)
        if skipped_entries:
            print(f"  Пропущено записей за день до окна: {skipped_entries}")

        delete_from = parsed_start
        if not schedules:
            # An empty answer is indistinguishable from a blip, and past shifts feed the administrator KPIs and
            # cannot be re-read. Only days that the next run reads again may be cleared by it.
            branch_today = _branch_today(db, cid)
            delete_from = branch_today if delete_from is None else max(delete_from, branch_today)
        delete_query = db.query(StaffSchedule).filter(StaffSchedule.company_id == cid)
        if delete_from is not None:
            delete_query = delete_query.filter(StaffSchedule.date >= delete_from)
        if parsed_end is not None:
            delete_query = delete_query.filter(StaffSchedule.date <= parsed_end)
        deleted_slots = delete_query.delete(synchronize_session=False)

        for staff_id, schedule_date, slot_from, slot_to in parsed_slots:
            db.add(
                StaffSchedule(
                    staff_id=staff_id,
                    date=schedule_date,
                    slot_from=slot_from,
                    slot_to=slot_to,
                    company_id=cid,
                )
            )

        mark_sync_source_coverage(
            db,
            cid,
            STAFF_SCHEDULE_SOURCE,
            start_date,
            end_date,
        )
        db.commit()
        print(f"  ✓ Графики сохранены ({len(parsed_slots)} слотов), удалено старых: {deleted_slots}")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# Основная функция
# ===================================================================

def execute_sync(
    mode: str = 'incremental',
    end_date: date | None = None,
    *,
    portal_account_id: int | None = None,
    credential_id: int | None = None,
    company_ids: Iterable[int] | None = None,
    progress_callback=None,
    database: Database | None = None,
):
    print("=" * 60)
    print("  YClients → PostgreSQL: синхронизация")
    print("=" * 60)

    # A caller that already built a Database (sync_orchestrator's run_sync_job) passes it
    # in so the run shares one engine/pool instead of opening a second one here.
    if database is None:
        database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    empty_result = {
        'completed': False,
        'success': False,
        'step_results': [],
        'mode': mode,
        'window_start': None,
        'window_end': None,
        'companies_count': 0,
    }

    if not database.test_connection():
        return empty_result

    db = database.get_db()
    step_results: list[dict] = []
    overall_success = False
    companies_count = 0

    end = end_date or date.today()
    default_start, default_sync_mode = resolve_sync_window(end, mode)
    default_sd = default_start.isoformat()
    ed = end.isoformat()
    schedule_end = end + timedelta(days=SCHEDULE_DAYS)
    result_window_start: date | None = None
    result_modes: set[str] = set()
    checkpoint_step_names = {
        FULL_REFRESH_CLEANUP_STEP,
        RECORDS_STEP,
        FINANCIAL_TRANSACTIONS_STEP,
        GOODS_TRANSACTIONS_STEP,
        COMMENTS_STEP,
    }

    requested_company_ids = [int(item) for item in dict.fromkeys(company_ids or [])]

    def emit_progress(
        stage_key: str,
        status: str = 'info',
        progress_pct: int | None = None,
        message: str | None = None,
        *,
        credential: YClientsCredentialValue | None = None,
        company_id: int | None = None,
        payload: dict | None = None,
    ) -> None:
        if progress_callback is None:
            return
        _notify_progress(progress_callback, {
            'portal_account_id': credential.portal_account_id if credential else portal_account_id,
            'credential_id': credential.id if credential else credential_id,
            'company_id': company_id,
            'stage_key': stage_key,
            'status': status,
            'progress_pct': progress_pct,
            'message': message,
            'payload': payload or {},
            'step_results': list(step_results),
        })

    try:
        credentials = load_active_credentials_sync(db, portal_account_id=portal_account_id)
        if credential_id is not None:
            credentials = [credential for credential in credentials if credential.id == int(credential_id)]
        # The loader drops a credential it cannot decrypt without a word, and the run would
        # then read as healthy for that tenant. Recorded as a step so it counts as a warning.
        for undecryptable in _undecryptable_credentials(db, credentials, portal_account_id, credential_id):
            label = f"#{undecryptable.portal_account_id} · {undecryptable.title}"
            print(f"! Не удалось расшифровать учётные данные «{undecryptable.title}» — пропуск тенанта")
            step_results.append({
                'name': f"Credentials decrypt [{label}]",
                'key': 'credentials_decrypt',
                'success': False,
                'elapsed': 0.0,
            })
            emit_progress(
                'credentials_decrypt',
                'failed',
                5,
                f'Credential decrypt failed: {undecryptable.title}',
                payload={'credential_id': undecryptable.id, 'portal_account_id': undecryptable.portal_account_id},
            )
        if not credentials:
            print('! Нет активных учётных данных YClients. Добавьте их через личный кабинет.')
            emit_progress('credentials', 'warning', 100, 'No active YClients credentials')
            return {
                **empty_result,
                'step_results': list(step_results),
                'completed': True,
                'mode': default_sync_mode,
                'window_start': default_sd,
                'window_end': ed,
            }

        print(
            "Параметры: "
            f"SYNC_DAYS={SYNC_DAYS}, "
            f"SCHEDULE_DAYS={SCHEDULE_DAYS}, "
            f"REQUEST_DELAY={YCLIENTS_REQUEST_DELAY}, "
            f"TIMEOUT={YCLIENTS_TIMEOUT}"
        )
        print(
            f"Режим синхронизации транзакций: {mode or INCREMENTAL_SYNC_MODE} "
            f"(окно определяется по филиалам, lookback={SYNC_LOOKBACK_DAYS} дн., "
            f"refresh={SYNC_REFRESH_DAYS} дн.)"
        )
        print(f"Активных учётных данных: {len(credentials)}")
        emit_progress(
            'credentials',
            'success',
            5,
            f'Active credentials: {len(credentials)}',
            payload={'credentials_count': len(credentials)},
        )

        for credential in credentials:
            tenant_label = f"#{credential.portal_account_id} · {credential.title}"
            print(f"\n{'=' * 60}")
            print(f"  Тенант {tenant_label}")
            print(f"{'=' * 60}")

            auth_error: str | None = None
            try:
                company_api = _build_api_for_credential(credential)
            except Exception as exc:
                # One tenant's bad auth reply must not stop the tenants after it in this run.
                company_api = None
                auth_error = exc.__class__.__name__
                print(f"! Ошибка авторизации учётных данных «{credential.title}»: {auth_error}")
            if company_api is None:
                print(f"! Не удалось авторизовать учётные данные «{credential.title}» — пропуск тенанта")
                mark_credential_failure_sync(db, credential.id, auth_error or 'YClients authentication failed')
                step_results.append({
                    'name': f"Credentials auth [{tenant_label}]",
                    'key': 'credentials_auth',
                    'success': False,
                    'elapsed': 0.0,
                })
                emit_progress(
                    'credentials_auth',
                    'failed',
                    10,
                    f'Credential auth failed: {credential.title}',
                    credential=credential,
                    payload={'error': auth_error} if auth_error else None,
                )
                continue
            mark_credential_success_sync(db, credential.id)
            emit_progress(
                'credentials_auth',
                'success',
                10,
                f'Credential auth succeeded: {credential.title}',
                credential=credential,
            )

            run_sync_step(
                step_results,
                f"Сети и компании [{tenant_label}]",
                sync_groups_and_companies,
                company_api,
                db,
                step_key='Сети и компании',
                progress_callback=progress_callback,
                progress_context={
                    'portal_account_id': credential.portal_account_id,
                    'credential_id': credential.id,
                },
                progress_pct=15,
                portal_account_id=credential.portal_account_id,
            )

            credential_company_ids = list(credential.company_ids)
            if requested_company_ids:
                if credential_company_ids:
                    scoped_company_ids = sorted(set(requested_company_ids) & set(credential_company_ids))
                else:
                    scoped_company_ids = []
            else:
                scoped_company_ids = credential_company_ids
            if not scoped_company_ids:
                print(f"! Тенант {tenant_label}: у credentials нет назначенных филиалов")
                emit_progress(
                    'companies',
                    'warning',
                    20,
                    f'No assigned companies for credential: {credential.title}',
                    credential=credential,
                )
                continue
            target_companies = get_target_companies(db, scoped_company_ids)
            if not target_companies:
                print(f"! Тенант {tenant_label}: нет филиалов для синхронизации")
                emit_progress(
                    'companies',
                    'warning',
                    20,
                    f'No companies for credential: {credential.title}',
                    credential=credential,
                )
                continue

            print(f"✓ Тенант {tenant_label}: найдено филиалов {len(target_companies)}")
            companies_count += len(target_companies)
            emit_progress(
                'companies',
                'success',
                20,
                f'Companies selected: {len(target_companies)}',
                credential=credential,
                payload={'company_ids': [company.id for company in target_companies]},
            )

            for company in target_companies:
                company_start, company_sync_mode = resolve_company_sync_window(
                    db,
                    end,
                    mode,
                    company.id,
                )
                company_sd = company_start.isoformat()
                result_window_start = (
                    company_start
                    if result_window_start is None
                    else min(result_window_start, company_start)
                )
                result_modes.add(company_sync_mode)
                company_id = str(_company_external_id(company))
                company_label = format_company_label(company)
                print(f"\n{'─' * 60}")
                print(f"Филиал: {company_label}")
                window_hint = {
                    INCREMENTAL_SYNC_MODE: f'lookback={SYNC_LOOKBACK_DAYS} дн.',
                    REFRESH_SYNC_MODE: f'окно={SYNC_REFRESH_DAYS} дн., с перечиткой',
                    FULL_SYNC_MODE: 'вся история, с перечиткой',
                }.get(company_sync_mode, company_sync_mode)
                print(
                    f"Окно транзакций: {company_sync_mode} "
                    f"({company_sd} .. {ed}, {window_hint})"
                )
                print(f"{'─' * 60}")

                company_step_start = len(step_results)
                if is_full_refresh_mode(company_sync_mode):
                    cleanup_success = run_sync_step(
                        step_results,
                        f"{FULL_REFRESH_CLEANUP_STEP} [{company_label}]",
                        purge_full_refresh_window,
                        db,
                        company.id,
                        company_sd,
                        ed,
                        schedule_end.isoformat(),
                        step_key=FULL_REFRESH_CLEANUP_STEP,
                        progress_callback=progress_callback,
                        progress_context={
                            'portal_account_id': credential.portal_account_id,
                            'credential_id': credential.id,
                            'company_id': company.id,
                        },
                        progress_pct=35,
                    )
                    if not cleanup_success:
                        print(f"! Full refresh cleanup failed; филиал пропущен [{company_label}]")
                        print(f"! Состояние инкрементальной синхронизации не обновлено [{company_label}]")
                        continue

                # Each windowed source drops and reloads its own window in one
                # transaction, so a source that fails keeps the data it already had.
                refresh = {'full_refresh': is_full_refresh_mode(company_sync_mode)}
                company_steps = [
                    ("Категории услуг", sync_service_categories, {}),
                    ("Услуги", sync_services, {}),
                    ("Сотрудники", sync_staff, {}),
                    ("Клиенты", sync_clients, {}),
                    ("Кассы", sync_accounts, {}),
                    ("Категории товаров", sync_good_categories, {}),
                    ("Товары", sync_goods, {}),
                    (
                        RECORDS_STEP,
                        sync_records,
                        {'start_date': company_sd, 'end_date': schedule_end.isoformat(), **refresh},
                    ),
                    (
                        FINANCIAL_TRANSACTIONS_STEP,
                        sync_financial_transactions,
                        {'start_date': company_sd, 'end_date': ed, **refresh},
                    ),
                    (
                        GOODS_TRANSACTIONS_STEP,
                        sync_goods_transactions,
                        {'start_date': company_sd, 'end_date': ed, **refresh},
                    ),
                    (COMMENTS_STEP, sync_comments, {'start_date': company_sd, 'end_date': ed, **refresh}),
                    (
                        "Графики сотрудников",
                        sync_staff_schedules,
                        {'start_date': ed, 'end_date': schedule_end.isoformat()},
                    ),
                ]
                for name, fn, kwargs in company_steps:
                    run_sync_step(
                        step_results,
                        f"{name} [{company_label}]",
                        fn,
                        company_api,
                        db,
                        company_id,
                        db_company_id=company.id,
                        step_key=name,
                        progress_callback=progress_callback,
                        progress_context={
                            'portal_account_id': credential.portal_account_id,
                            'credential_id': credential.id,
                            'company_id': company.id,
                        },
                        progress_pct=50,
                        **kwargs,
                    )

                company_step_results = step_results[company_step_start:]
                if is_full_refresh_mode(company_sync_mode):
                    # The marker asserts one thing only — that the coverage rows span the
                    # branch's whole history — and that is read back from the rows
                    # themselves. Restoring it is therefore independent of whether every
                    # step succeeded: a source that owns no coverage (comments)
                    # failing must not cost the branch a full history pass tomorrow, now
                    # that the nightly job is a narrow window rather than that pass.
                    if has_complete_historical_source_coverage(db, company.id, end):
                        SyncControlService().set_state(
                            db,
                            historical_coverage_state_key(company.id),
                            ed,
                        )
                    else:
                        print(
                            "! Полное историческое покрытие источников не подтверждено; "
                            f"инкрементальный режим не включен [{company_label}]"
                        )
                if steps_successful(company_step_results, checkpoint_step_names):
                    SyncControlService().set_state(db, transactional_state_key(company.id), ed)
                    print(f"✓ Обновлено состояние инкрементальной синхронизации [{company_label}]: {ed}")
                else:
                    print(f"! Состояние инкрементальной синхронизации не обновлено [{company_label}]")

        # A step that raised is not the same as one that declined: the first is a bug or an
        # endpoint out of contract, and it used to abort the run. It no longer does — the
        # remaining branches still sync — so the run's own status has to carry it instead.
        crashed = [item['name'] for item in step_results if item.get('error')]
        if crashed:
            print(f"\n  Шаги, прерванные исключением: {len(crashed)}")
            for name in crashed:
                print(f"    - {name}")
        overall_success = (
            companies_count > 0
            and not crashed
            and steps_successful(step_results, checkpoint_step_names)
        )

        print("\n" + "=" * 60)
        print("  Синхронизация завершена!")
        print("=" * 60)

    finally:
        print_sync_summary(step_results)
        db.close()

    return {
        'completed': True,
        'success': overall_success,
        'step_results': step_results,
        'mode': next(iter(result_modes))
        if len(result_modes) == 1
        else ('mixed' if result_modes else default_sync_mode),
        'window_start': (result_window_start or default_start).isoformat(),
        'window_end': ed,
        'companies_count': companies_count,
    }


if __name__ == "__main__":
    # This used to call execute_sync() directly — no advisory lock, no run tracking — right
    # next to a detached production sync nobody watches (see main.py / sync_orchestrator.py).
    # Nothing in docker-compose*.yml, docker/entrypoint.sh or sync.sh runs this file this
    # way (the `sync` service runs main.py); nothing should. Route through the guarded
    # entry point instead: `python main.py` or `./sync.sh`.
    raise SystemExit(
        "sync_pipeline.py must not be run directly: it skips the advisory lock and run "
        "tracking. Use 'python main.py' or './sync.sh' instead."
    )
