"""Builders for the client reports: base concentration, retention of new clients, churn."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, case, extract, func, select

from dashboard_reports import MAX_PERIOD_BUCKETS, _client_pareto_rows, _clients_rows
from dashboard_service import (
    COMPLETED_ATTENDANCE,
    DateRange,
    _appointment_factual_at_condition,
    _company_scope_clause,
    _DayShift,
    _service_revenue_union,
    business_appointment_condition,
    factual_branch_date,
    fetch_reporting_windows,
    reporting_window_clause,
    shift_months_back,
)
from models import Appointment, Company, FinancialTransaction, Staff
from payment_methods import month_label, short_month_label
from report_payload import (
    MONEY_FORMAT,
    NUMBER_FORMAT,
    PERCENT_FORMAT,
    ReportRequest,
    card,
    chart,
    table,
)

CHURN_DAYS = 90
# Revenue at risk reads the client's payments over the year before they left.
REVENUE_LOOKBACK_DAYS = 365
RETENTION_HORIZONS = (1, 3, 6, 12)
NO_MASTER = 'Без мастера'


def _month_starts(start: date, end: date) -> Iterator[date]:
    """First days of every calendar month touched by [start, end]; ValueError past `MAX_PERIOD_BUCKETS` months."""
    day = start.replace(day=1)
    for _ in range(MAX_PERIOD_BUCKETS):
        if day > end:
            return
        yield day
        day = shift_months_back(day, -1)
    if day <= end:
        raise ValueError('period is too long: choose a shorter one')


def _month_key(day: date) -> str:
    return f'{day.year:04d}-{day.month:02d}'


def _month_end(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def _share(part: int | float, whole: int | float) -> float | None:
    return 100.0 * part / whole if whole else None


def _visit_filters(req: ReportRequest) -> list[Any]:
    """The Overview's rules for which completed visits belong to a client (see `_client_recency_block`)."""
    filters = [
        Appointment.attendance == COMPLETED_ATTENDANCE,
        Appointment.client_id.is_not(None),
        business_appointment_condition(),
        reporting_window_clause(Appointment.company_id, Appointment.date),
        _appointment_factual_at_condition(req.factual_at),
    ]
    scope = _company_scope_clause(Appointment.company_id, req.company_id, req.allowed_company_ids)
    if scope is not None:
        filters.append(scope)
    return filters


async def _branch_horizons(req: ReportRequest) -> dict[int, date]:
    """Branches whose visits are known only up to an earlier day than today, with that day.

    A branch that left the tenant keeps no visits after its reporting window ends, so a client's
    silence there says nothing about whether they came back.
    """
    today = factual_branch_date(req.factual_at)
    windows = await fetch_reporting_windows(req.db, req.allowed_company_ids or [])
    return {
        company_id: window.end
        for company_id, window in windows.items()
        if window.end is not None and window.end < today
    }


# --- top_clients_pareto ---------------------------------------------------------------------------------


def _frequency_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Visit-count buckets over the clients who visited in the period, by their whole history.

    Same base and buckets as the Overview's frequency block. A client who only paid for an earlier
    visit has no visit in the period, so the Overview does not count them; their revenue gets its own
    row instead of silently inflating a bucket.
    """
    buckets = {
        '1 визит': {'bucket': '1 визит', 'clients': 0, 'revenue': 0.0},
        '2-3 визита': {'bucket': '2-3 визита', 'clients': 0, 'revenue': 0.0},
        '4+ визита': {'bucket': '4+ визита', 'clients': 0, 'revenue': 0.0},
    }
    paid_only = {'bucket': 'Оплатили визиты прошлых периодов', 'clients': 0, 'clients_pct': None, 'revenue': 0.0}
    visitors = sum(1 for row in rows if row['visits'] > 0)
    for row in rows:
        if row['visits'] <= 0:
            paid_only['clients'] += 1
            paid_only['revenue'] += row['revenue']
            continue
        lifetime = int(row['lifetime_visits'] or 0)
        key = '1 визит' if lifetime <= 1 else '2-3 визита' if lifetime <= 3 else '4+ визита'
        buckets[key]['clients'] += 1
        buckets[key]['revenue'] += row['revenue']
    out = list(buckets.values())
    for item in out:
        item['clients_pct'] = _share(item['clients'], visitors) or 0.0
    if paid_only['clients']:
        out.append(paid_only)
    return out


async def build_top_clients_pareto(req: ReportRequest) -> dict[str, Any]:
    rows = await _clients_rows(
        req.db, req.start, req.end, req.company_id, req.staff_id, req.allowed_company_ids, req.factual_at,
        include_lifetime_visits=True,
    )
    identified = [row for row in rows if row['client_id'] is not None]
    anonymous = [row for row in rows if row['client_id'] is None]
    anonymous_revenue = sum(row['revenue'] for row in anonymous)
    identified_revenue = sum(row['revenue'] for row in identified)
    total_revenue = identified_revenue + anonymous_revenue
    pareto_rows = _client_pareto_rows(identified, total_revenue)
    if anonymous_revenue:
        pareto_rows.append({
            'bucket': 'Оплаты без клиента',
            'clients': None,
            'clients_pct': None,
            'revenue': anonymous_revenue,
            'revenue_pct': _share(anonymous_revenue, total_revenue),
            'avg_revenue_per_client': None,
        })
    frequency_rows = _frequency_rows(identified)
    avg_revenue = identified_revenue / len(identified) if identified else None

    base = req.base
    base['cards'] = [
        card('Клиентов', len(identified), NUMBER_FORMAT),
        # Visits and revenue are the branch totals the Overview shows; the part without a client card is
        # the residual line of the Pareto table.
        card('Визитов', sum(row['visits'] for row in rows), NUMBER_FORMAT),
        card('Выручка клиентов', total_revenue, MONEY_FORMAT),
        card('Средний доход на клиента', avg_revenue, MONEY_FORMAT),
    ]
    base['charts'] = [
        chart(
            'client_pareto',
            'Концентрация выручки по клиентским бакетам',
            'bar',
            [row['bucket'] for row in pareto_rows],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in pareto_rows], 'format': MONEY_FORMAT}],
        ),
        chart(
            'visit_frequency',
            'Клиенты по числу визитов за всю историю',
            'doughnut',
            [row['bucket'] for row in frequency_rows],
            [{'label': 'Клиентов', 'data': [row['clients'] for row in frequency_rows], 'format': NUMBER_FORMAT}],
        ),
    ]
    base['tables'] = [
        table(
            'client_pareto',
            'Pareto-бакеты клиентов',
            [
                ('bucket', 'Бакет', 'text'),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('clients_pct', 'Доля клиентов', PERCENT_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('revenue_pct', 'Доля выручки', PERCENT_FORMAT),
                ('avg_revenue_per_client', 'Доход на клиента', MONEY_FORMAT),
            ],
            pareto_rows,
            row_key='bucket',
        ),
        table(
            'visit_frequency',
            'Частотность визитов за всю историю',
            [
                ('bucket', 'Частотность', 'text'),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('clients_pct', 'Доля клиентов', PERCENT_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
            ],
            frequency_rows,
            row_key='bucket',
        ),
    ]
    base['notes'].append({
        'kind': 'formula',
        'title': 'Как считается',
        'text': (
            'Клиенты — те, у кого в периоде был завершённый визит или оплата услуги. Выручка — по дате оплаты услуг, '
            'как в Обзоре. Бакеты делят клиентов по выручке за период. Частотность считает визиты клиента '
            'в филиале за всю историю до конца периода, поэтому её доли совпадают с блоком частотности в Обзоре.'
        ),
    })
    base['raw'] = {
        'pareto': pareto_rows,
        'visit_frequency': frequency_rows,
        'anonymous_residual': {'visits': sum(row['visits'] for row in anonymous), 'revenue': anonymous_revenue},
        'revenue_scope': 'services',
    }
    return base


# --- retention_3_6_12 -----------------------------------------------------------------------------------


def _month_index(column):
    return extract('year', column) * 12 + extract('month', column)


async def _cohort_rows(req: ReportRequest, first_from: date, first_to: date) -> list[Any]:
    """Per (branch, month of first visit): cohort size and how many came back within each horizon.

    A client's first visit is their earliest completed visit in the branch under the Overview's rules,
    so a month's cohort is exactly that month's `new_clients`. A return is a visit on a later day; it
    counts within k months when fewer than k whole months passed since the first visit.
    """
    partition = [Appointment.company_id, Appointment.client_id]
    visits = (
        select(
            Appointment.company_id.label('company_id'),
            Appointment.client_id.label('client_id'),
            Appointment.staff_id.label('staff_id'),
            Appointment.date.label('date'),
            func.row_number()
            .over(
                partition_by=partition,
                order_by=(Appointment.date.asc(), Appointment.datetime.asc().nulls_last(), Appointment.id.asc()),
            )
            .label('rn'),
            func.min(Appointment.date).over(partition_by=partition).label('first_date'),
        )
        .where(*_visit_filters(req))
        .subquery()
    )
    clients = (
        select(
            visits.c.company_id,
            visits.c.client_id,
            func.max(visits.c.first_date).label('first_date'),
            func.max(case((visits.c.rn == 1, visits.c.staff_id))).label('first_staff_id'),
            func.min(case((visits.c.date > visits.c.first_date, visits.c.date))).label('second_date'),
        )
        .group_by(visits.c.company_id, visits.c.client_id)
        .subquery()
    )
    elapsed = (
        _month_index(clients.c.second_date)
        - _month_index(clients.c.first_date)
        - case((extract('day', clients.c.second_date) < extract('day', clients.c.first_date), 1), else_=0)
    )
    year = extract('year', clients.c.first_date).label('year')
    month = extract('month', clients.c.first_date).label('month')
    returned = [
        func.coalesce(func.sum(case((elapsed < horizon, 1), else_=0)), 0).label(f'within_{horizon}')
        for horizon in RETENTION_HORIZONS
    ]
    stmt = (
        select(clients.c.company_id, year, month, func.count().label('size'), *returned)
        .where(clients.c.first_date >= first_from, clients.c.first_date <= first_to)
        .group_by(clients.c.company_id, year, month)
    )
    if req.staff_id is not None:
        stmt = stmt.where(clients.c.first_staff_id == req.staff_id)
    return (await req.db.execute(stmt)).all()


async def build_retention_3_6_12(req: ReportRequest) -> dict[str, Any]:
    today = factual_branch_date(req.factual_at)
    horizons = await _branch_horizons(req)
    cohort_months = list(_month_starts(req.start, min(req.end, today)))
    rows = await _cohort_rows(req, cohort_months[0], _month_end(cohort_months[-1])) if cohort_months else []
    by_month: dict[date, list[Any]] = {}
    for row in rows:
        by_month.setdefault(date(int(row.year), int(row.month), 1), []).append(row)

    # Last day whose visits are all known: a branch's window end, else yesterday (today is still being booked).
    yesterday = today - timedelta(days=1)

    def mature(company_id: int, cohort: date, horizon: int) -> bool:
        """Every client of the cohort has had `horizon` full months since their first visit."""
        return shift_months_back(_month_end(cohort), -horizon) <= horizons.get(company_id, yesterday)

    cohort_rows = []
    mature_totals = {horizon: [0, 0] for horizon in RETENTION_HORIZONS}  # horizon -> [returned, cohort size]
    for cohort in cohort_months:
        branches = by_month.get(cohort, [])
        item: dict[str, Any] = {
            'cohort': month_label(_month_key(cohort)),
            'cohort_month': _month_key(cohort),
            'size': sum(int(row.size) for row in branches),
        }
        for horizon in RETENTION_HORIZONS:
            ready = [row for row in branches if mature(int(row.company_id), cohort, horizon)]
            came_back = sum(int(getattr(row, f'within_{horizon}')) for row in ready)
            ready_size = sum(int(row.size) for row in ready)
            item[f'returned_{horizon}'] = came_back if ready else None
            item[f'pct_{horizon}'] = _share(came_back, ready_size)
            mature_totals[horizon][0] += came_back
            mature_totals[horizon][1] += ready_size
        cohort_rows.append(item)

    base = req.base
    base['cards'] = [
        card('Новых клиентов (когорты периода)', sum(item['size'] for item in cohort_rows), NUMBER_FORMAT),
        card('Вернулись в течение 3 мес', _share(*mature_totals[3]), PERCENT_FORMAT),
        card('Вернулись в течение 6 мес', _share(*mature_totals[6]), PERCENT_FORMAT),
    ]
    base['charts'] = [
        chart(
            'retention_3m',
            'Доля вернувшихся в течение 3 месяцев, по месяцу первого визита',
            'line',
            [short_month_label(item['cohort_month']) for item in cohort_rows],
            [{'label': 'Вернулись за 3 мес', 'data': [item['pct_3'] for item in cohort_rows], 'format': PERCENT_FORMAT}],
            x_kind='time',
        )
    ]
    base['tables'] = [
        table(
            'retention_cohorts',
            'Когорты новых клиентов',
            [
                ('cohort', 'Месяц первого визита', 'text'),
                ('size', 'Новых клиентов', NUMBER_FORMAT),
                *((f'pct_{horizon}', f'Вернулись за {horizon} мес', PERCENT_FORMAT) for horizon in RETENTION_HORIZONS),
            ],
            cohort_rows,
            row_key='cohort',
            x_kind='time',
        )
    ]
    base['notes'].append({
        'kind': 'formula',
        'title': 'Как считается',
        'text': (
            'Когорта — клиенты, у которых в этом месяце был первый завершённый визит в филиале; её размер '
            'равен числу новых клиентов в Обзоре за тот же календарный месяц. Вернувшимся считается клиент '
            'с повторным визитом в любой другой день в течение указанного числа месяцев после первого. '
            'Когорты берутся целыми месяцами, пересекающими период.'
        ),
    })
    base['notes'].append({
        'kind': 'info',
        'title': 'Незавершённые когорты',
        'text': (
            'Пустое значение (—) означает, что с конца месяца когорты ещё не прошло нужного срока, '
            'и доля пока неизвестна. Итоговые карточки считают только те когорты, для которых срок прошёл. '
            'У филиала, вышедшего из отчётности, срок отсчитывается до даты окончания отчётности.'
        ),
    })
    if req.staff_id is not None:
        base['notes'].append({
            'kind': 'info',
            'title': 'Фильтр по мастеру',
            'text': (
                'В когорту входят клиенты, чей первый визит в филиале был у выбранного мастера; '
                'возвращение засчитывается к любому мастеру филиала.'
            ),
        })
    base['raw'] = {'cohorts': cohort_rows, 'horizons': list(RETENTION_HORIZONS)}
    return base


# --- revenue_at_risk ------------------------------------------------------------------------------------


def _churn_cutoff(company_column, horizons: dict[int, date], today: date):
    """Latest last-visit day that is already `CHURN_DAYS` old: today's, or the branch's earlier horizon."""
    default = today - timedelta(days=CHURN_DAYS)
    special = [
        (company_column == company_id, horizon - timedelta(days=CHURN_DAYS))
        for company_id, horizon in horizons.items()
    ]
    return case(*special, else_=default) if special else default


def _lookback_start(day: date) -> date:
    try:
        return day - timedelta(days=REVENUE_LOOKBACK_DAYS - 1)
    except OverflowError:  # a window opened at the dawn of the calendar has no earlier payments to read
        return date.min


async def build_revenue_at_risk(req: ReportRequest) -> dict[str, Any]:
    today = factual_branch_date(req.factual_at)
    horizons = await _branch_horizons(req)
    filters = _visit_filters(req)
    ranked = (
        select(
            Appointment.company_id.label('company_id'),
            Appointment.client_id.label('client_id'),
            Appointment.staff_id.label('staff_id'),
            Appointment.date.label('date'),
            func.row_number()
            .over(
                partition_by=[Appointment.company_id, Appointment.client_id],
                order_by=(Appointment.date.desc(), Appointment.datetime.desc().nulls_last(), Appointment.id.desc()),
            )
            .label('rn'),
        )
        .where(*filters)
        .subquery()
    )
    churned_stmt = select(
        ranked.c.company_id,
        ranked.c.client_id,
        ranked.c.staff_id.label('last_staff_id'),
        ranked.c.date.label('last_date'),
    ).where(
        ranked.c.rn == 1,
        ranked.c.date >= req.start,
        ranked.c.date <= req.end,
        ranked.c.date <= _churn_cutoff(ranked.c.company_id, horizons, today),
    )
    if req.staff_id is not None:
        churned_stmt = churned_stmt.where(ranked.c.staff_id == req.staff_id)
    # Read twice below (revenue and the grouping); a CTE keeps the window function to one pass.
    churned = churned_stmt.cte('churned')

    # A churned client's last visit lies in [start, end], so the payments read are bounded by that
    # window widened back by the lookback; without it the join scans every payment ever made.
    service_rows = _service_revenue_union(
        DateRange(_lookback_start(req.start), min(req.end, today)),
        req.company_id,
        None,
        None,
        req.allowed_company_ids,
        req.factual_at,
        extra_columns=(
            Appointment.company_id.label('company_id'),
            Appointment.client_id.label('client_id'),
            FinancialTransaction.date.label('paid_at'),
        ),
    )
    revenue = (
        select(
            service_rows.c.company_id,
            service_rows.c.client_id,
            func.sum(service_rows.c.amount).label('revenue'),
        )
        .select_from(
            service_rows.join(
                churned,
                and_(
                    churned.c.company_id == service_rows.c.company_id,
                    churned.c.client_id == service_rows.c.client_id,
                ),
            )
        )
        .where(
            service_rows.c.paid_at >= _DayShift(churned.c.last_date, -(REVENUE_LOOKBACK_DAYS - 1)),
            service_rows.c.paid_at < _DayShift(churned.c.last_date, 1),
        )
        .group_by(service_rows.c.company_id, service_rows.c.client_id)
        .subquery()
    )
    year = extract('year', churned.c.last_date).label('year')
    month = extract('month', churned.c.last_date).label('month')
    grouped = (
        select(
            churned.c.company_id,
            Company.title.label('company_title'),
            churned.c.last_staff_id,
            Staff.name.label('staff_name'),
            year,
            month,
            func.count().label('clients'),
            func.coalesce(func.sum(revenue.c.revenue), 0.0).label('revenue'),
        )
        .select_from(churned)
        .outerjoin(
            revenue,
            and_(revenue.c.company_id == churned.c.company_id, revenue.c.client_id == churned.c.client_id),
        )
        .outerjoin(Staff, Staff.id == churned.c.last_staff_id)
        .outerjoin(Company, Company.id == churned.c.company_id)
        .group_by(churned.c.company_id, Company.title, churned.c.last_staff_id, Staff.name, year, month)
    )
    rows = (await req.db.execute(grouped)).all()

    # The share's base is the clients old enough to have left: a visit inside the maturity cutoff, like the
    # numerator's. Recent clients cannot have churned yet, so counting them would deflate the share.
    period_clients = (
        select(Appointment.company_id, Appointment.client_id)
        .where(
            *filters,
            Appointment.date >= req.start,
            Appointment.date <= req.end,
            Appointment.date <= _churn_cutoff(Appointment.company_id, horizons, today),
        )
        .group_by(Appointment.company_id, Appointment.client_id)
    )
    if req.staff_id is not None:
        period_clients = period_clients.where(Appointment.staff_id == req.staff_id)
    share_base_clients = int(await req.db.scalar(select(func.count()).select_from(period_clients.subquery())) or 0)

    months: dict[date, dict[str, Any]] = {}
    staff: dict[str, dict[str, Any]] = {}
    for row in rows:
        monthly_revenue = float(row.revenue or 0) / 12
        entry = months.setdefault(date(int(row.year), int(row.month), 1), {'clients': 0, 'revenue': 0.0})
        entry['clients'] += int(row.clients)
        entry['revenue'] += monthly_revenue
        key = str(row.last_staff_id) if row.last_staff_id is not None else f'none:{row.company_id}'
        member = staff.setdefault(key, {
            'staff_key': key,
            'staff_name': row.staff_name or NO_MASTER,
            'company_title': row.company_title,
            'clients': 0,
            'revenue': 0.0,
        })
        member['clients'] += int(row.clients)
        member['revenue'] += monthly_revenue

    churned_total = sum(item['clients'] for item in months.values())
    monthly_at_risk = sum(item['revenue'] for item in months.values())
    cutoff_day = today - timedelta(days=CHURN_DAYS)
    month_rows = [
        {
            'month': month_label(_month_key(month_start)),
            'month_iso': _month_key(month_start),
            'clients': months.get(month_start, {}).get('clients', 0),
            'revenue': months.get(month_start, {}).get('revenue', 0.0),
        }
        for month_start in _month_starts(req.start, min(req.end, cutoff_day))
    ]
    staff_rows = sorted(staff.values(), key=lambda item: (-item['clients'], -item['revenue'], item['staff_key']))
    for item in staff_rows:
        item['clients_pct'] = _share(item['clients'], churned_total) or 0.0

    base = req.base
    base['cards'] = [
        card('Ушедших клиентов', churned_total, NUMBER_FORMAT),
        card('Доля от клиентов периода', _share(churned_total, share_base_clients), PERCENT_FORMAT),
        card('Ежемесячная выручка под риском', monthly_at_risk, MONEY_FORMAT),
    ]
    base['charts'] = [
        chart(
            'churn_by_month',
            'Ушедшие клиенты по месяцу последнего визита',
            'bar',
            [short_month_label(item['month_iso']) for item in month_rows],
            [{'label': 'Ушедших клиентов', 'data': [item['clients'] for item in month_rows], 'format': NUMBER_FORMAT}],
            x_kind='time',
        )
    ]
    base['tables'] = [
        table(
            'churn_by_month',
            'Ушедшие клиенты по месяцу последнего визита',
            [
                ('month', 'Месяц последнего визита', 'text'),
                ('clients', 'Ушедших клиентов', NUMBER_FORMAT),
                ('revenue', 'Выручка под риском в месяц', MONEY_FORMAT),
            ],
            month_rows,
            row_key='month',
            x_kind='time',
        ),
        table(
            'churn_by_staff',
            'Ушедшие клиенты по последнему мастеру',
            [
                ('staff_name', 'Мастер', 'text'),
                ('company_title', 'Филиал', 'text'),
                ('clients', 'Ушедших клиентов', NUMBER_FORMAT),
                ('clients_pct', 'Доля ушедших', PERCENT_FORMAT),
                ('revenue', 'Выручка под риском в месяц', MONEY_FORMAT),
            ],
            staff_rows,
            row_key='staff_key',
        ),
    ]
    base['notes'].append({
        'kind': 'formula',
        'title': 'Кто считается ушедшим',
        'text': (
            f'Ушедший — клиент, чей последний завершённый визит в филиале (у любого мастера) пришёлся на период '
            f'и после которого прошло не меньше {CHURN_DAYS} дней без нового визита. Выручка под риском в месяц — '
            f'сумма оплат услуг ушедшего клиента за {REVENUE_LOOKBACK_DAYS} дней до последнего визита включительно, '
            f'делённая на 12. Доля считается от клиентов, у которых в периоде был завершённый визит '
            f'не позже этого срока: недавние клиенты ещё не могли уйти и в основу не входят.'
        ),
    })
    if req.end > cutoff_day:
        base['notes'].append({
            'kind': 'info',
            'title': 'Недавние клиенты пока не учтены',
            'text': (
                f'Клиенты, чей последний визит позже {cutoff_day.strftime("%d.%m.%Y")}, ещё не могли уйти: '
                f'с их визита прошло меньше {CHURN_DAYS} дней. Они войдут в отчёт, когда срок истечёт.'
            ),
        })
    if req.staff_id is not None:
        base['notes'].append({
            'kind': 'info',
            'title': 'Фильтр по мастеру',
            'text': (
                'Показаны клиенты, чей последний визит в филиале был у выбранного мастера. '
                'Доля — от клиентов, которых мастер обслужил в периоде.'
            ),
        })
    base['raw'] = {
        'months': month_rows,
        'staff': staff_rows,
        'churn_days': CHURN_DAYS,
        'share_base_clients': share_base_clients,
    }
    return base
