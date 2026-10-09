"""Builders for the operations and team reports."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC
from typing import Any

from sqlalchemy import and_, case, extract, func, select

from dashboard_reports import _appointment_conditions, _period_key, _period_starts, _staff_rows
from dashboard_service import (
    COMPLETED_ATTENDANCE,
    _appointment_company_ids,
    _branch_timezone,
    _company_scope_clause,
    _company_timezone_names,
    _local_moment,
    fetch_appointments_breakdown,
)
from models import Appointment, Company, Staff
from report_payload import (
    DECIMAL_FORMAT,
    MONEY_FORMAT,
    NUMBER_FORMAT,
    PERCENT_FORMAT,
    ReportRequest,
    card,
    chart,
    table,
    top_rows,
    without_empty_columns,
)

NO_SHOW_ATTENDANCE = -1
ONLINE_CREATED_USER_ID = 0
UNNAMED_STAFF = 'Без мастера'
STAFF_CHART_ROWS = 12
TEXT_FORMAT = 'text'
WEEKDAY_SHORT = ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')
WEEKDAY_FULL = ('Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье')
WEEKDAY_KEYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


def _share(part: int | float, whole: int | float) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


async def _records_by_period(req: ReportRequest, measures: dict[str, Any]) -> list[dict[str, Any]]:
    """Record counts per period, zero-filled across the requested window.

    SQL groups by day (bounded by the window length); the week/month roll-up happens on those few rows.
    `measures` maps a column name to the condition counted into it.
    """
    stmt = (
        select(
            Appointment.date.label('day'),
            func.count(Appointment.id).label('records'),
            *[
                func.coalesce(func.sum(case((condition, 1), else_=0)), 0).label(name)
                for name, condition in measures.items()
            ],
        )
        .where(and_(*_appointment_conditions(
            req.start,
            req.end,
            req.company_id,
            req.staff_id,
            allowed_company_ids=req.allowed_company_ids,
            factual_at=req.factual_at,
        )))
        .group_by(Appointment.date)
    )
    rows = {
        key: {'period': key, 'records': 0, **{name: 0 for name in measures}}
        for key in _period_starts(req.start, req.end, req.granularity)
    }
    for row in (await req.db.execute(stmt)).all():
        item = rows[_period_key(row.day, req.granularity)]
        item['records'] += int(row.records or 0)
        for name in measures:
            item[name] += int(getattr(row, name) or 0)
    return list(rows.values())


def _sum(rows: Iterable[dict[str, Any]], key: str) -> int:
    return sum(row[key] for row in rows)


# --- bookings_dynamics -------------------------------------------------------------------------------------------


async def _staff_records(req: ReportRequest) -> list[dict[str, Any]]:
    stmt = (
        select(
            Appointment.staff_id.label('staff_id'),
            # One Staff row per staff_id, so min() just picks that row's value.
            func.min(Staff.name).label('staff_name'),
            func.min(Company.title).label('company_title'),
            func.count(Appointment.id).label('records'),
            func.coalesce(
                func.sum(case((Appointment.attendance == COMPLETED_ATTENDANCE, 1), else_=0)), 0
            ).label('completed'),
            func.coalesce(
                func.sum(case((Appointment.attendance == NO_SHOW_ATTENDANCE, 1), else_=0)), 0
            ).label('no_show'),
        )
        .outerjoin(Staff, Staff.id == Appointment.staff_id)
        .outerjoin(Company, Company.id == Staff.company_id)
        .where(and_(*_appointment_conditions(
            req.start,
            req.end,
            req.company_id,
            req.staff_id,
            allowed_company_ids=req.allowed_company_ids,
            factual_at=req.factual_at,
        )))
        .group_by(Appointment.staff_id)
    )
    rows = []
    for row in (await req.db.execute(stmt)).all():
        records = int(row.records or 0)
        no_show = int(row.no_show or 0)
        named = row.staff_id is not None
        rows.append({
            'staff_id': row.staff_id,
            'staff_name': (row.staff_name or f'Сотрудник {row.staff_id}') if named else UNNAMED_STAFF,
            'company_title': row.company_title if named else None,
            'records': records,
            'completed': int(row.completed or 0),
            'no_show': no_show,
            'no_show_share': _share(no_show, records),
        })
    rows.sort(key=lambda item: (-item['records'], item['staff_id'] is None, item['staff_id'] or 0))
    return rows


async def build_bookings_dynamics(req: ReportRequest) -> dict[str, Any]:
    base = req.base
    period_rows = await _records_by_period(req, {
        'completed': Appointment.attendance == COMPLETED_ATTENDANCE,
        'no_show': Appointment.attendance == NO_SHOW_ATTENDANCE,
    })
    for row in period_rows:
        row['no_show_share'] = _share(row['no_show'], row['records'])
    staff_rows = await _staff_records(req)

    exact = await fetch_appointments_breakdown(
        req.db,
        req.start,
        req.end,
        req.company_id,
        req.staff_id,
        allowed_company_ids=req.allowed_company_ids,
        factual_at=req.factual_at,
    )
    totals = {
        'total': _sum(period_rows, 'records'),
        'completed': _sum(period_rows, 'completed'),
        'cancelled': _sum(period_rows, 'no_show'),
    }
    # The totals come from the same fetcher as the Overview cards. Only when it has no usable answer do the
    # locally counted period rows stand in, so the cards are never empty next to a filled table.
    if exact['source_status'] in {'ready', 'local'}:
        totals = {key: exact[key] for key in totals}
    base['cards'] = [
        card('Всего записей', totals['total'], NUMBER_FORMAT),
        card('Завершено', totals['completed'], NUMBER_FORMAT),
        card('Неявки', totals['cancelled'], NUMBER_FORMAT),
        card('Доля неявок', _share(totals['cancelled'], totals['total']), PERCENT_FORMAT),
    ]
    if exact['source_status'] == 'ready':
        scope_text = (
            'Карточки взяты из агрегатов YClients за период: неявками там считаются отменённые записи. '
            'Таблицы и графики посчитаны по записям, доступным в локальной базе, поэтому могут отличаться.'
        )
    else:
        scope_text = (
            'Учитываются записи, доступные в локальной базе: завершённые, неявки (клиент не пришёл) '
            'и остальные, которые ещё не отмечены. Записи, отменённые или удалённые в YClients, '
            'не синхронизируются и в отчёт не попадают. Доля неявок — от всех записей.'
        )
    base['notes'].append({'kind': 'info', 'title': 'Что считается', 'text': scope_text})

    labels = [row['period'] for row in period_rows]
    base['charts'] = [
        chart(
            'records_by_period',
            'Записи по периодам',
            'line',
            labels,
            [
                {'label': 'Всего записей', 'data': [row['records'] for row in period_rows], 'format': NUMBER_FORMAT},
                {'label': 'Завершено', 'data': [row['completed'] for row in period_rows], 'format': NUMBER_FORMAT},
                {'label': 'Неявки', 'data': [row['no_show'] for row in period_rows], 'format': NUMBER_FORMAT},
            ],
            x_kind='time',
        ),
        chart(
            'no_show_share_by_period',
            'Доля неявок по периодам',
            'line',
            labels,
            [{'label': 'Доля неявок', 'data': [row['no_show_share'] for row in period_rows], 'format': PERCENT_FORMAT}],
            x_kind='time',
        ),
    ]
    base['tables'] = [
        table(
            'periods',
            'Записи по периодам',
            [
                ('period', 'Период', TEXT_FORMAT),
                ('records', 'Всего записей', NUMBER_FORMAT),
                ('completed', 'Завершено', NUMBER_FORMAT),
                ('no_show', 'Неявки', NUMBER_FORMAT),
                ('no_show_share', 'Доля неявок', PERCENT_FORMAT),
            ],
            period_rows,
            row_key='period',
            x_kind='time',
        ),
        table(
            'staff_records',
            'Записи по мастерам',
            [
                ('staff_name', 'Мастер', TEXT_FORMAT),
                ('company_title', 'Филиал', TEXT_FORMAT),
                ('records', 'Всего записей', NUMBER_FORMAT),
                ('completed', 'Завершено', NUMBER_FORMAT),
                ('no_show', 'Неявки', NUMBER_FORMAT),
                ('no_show_share', 'Доля неявок', PERCENT_FORMAT),
            ],
            staff_rows,
            row_key='staff_id',
        ),
    ]
    base['raw'] = {'exact_aggregates': exact, 'by_period': period_rows, 'by_staff': staff_rows}
    return base


# --- peak_load ---------------------------------------------------------------------------------------------------


async def _local_visit_cells(req: ReportRequest) -> tuple[dict[tuple[int, int], list[int]], list[int]]:
    """Completed visits by (ISO weekday, local hour) as [visits, booked seconds]; visits without a time apart.

    PostgreSQL converts and groups in SQL. Other dialects (the SQLite test database has no timezone())
    group by the stored minute instead and convert those few rows in Python.
    """
    company_ids = await _appointment_company_ids(req.db, req.company_id, req.staff_id, req.allowed_company_ids)
    names = await _company_timezone_names(req.db, company_ids)
    companies_by_timezone: dict[str, list[int]] = defaultdict(list)
    for company_id in company_ids:
        companies_by_timezone[_branch_timezone(names.get(company_id)).key].append(company_id)

    conditions = _appointment_conditions(
        req.start,
        req.end,
        req.company_id,
        req.staff_id,
        attended_only=True,
        allowed_company_ids=req.allowed_company_ids,
        factual_at=req.factual_at,
    )
    visits = func.count(Appointment.id).label('visits')
    seconds = func.coalesce(func.sum(Appointment.seance_length), 0).label('seconds')
    postgres = req.db.get_bind().dialect.name == 'postgresql'
    cells: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
    unplaced = [0, 0]
    for timezone_name, ids in sorted(companies_by_timezone.items()):
        scope = Appointment.company_id.in_(ids)
        if postgres:
            local = _local_moment(Appointment.datetime, timezone_name)
            weekday = extract('isodow', local).label('weekday')
            hour = extract('hour', local).label('hour')
            stmt = select(weekday, hour, visits, seconds).where(and_(*conditions, scope)).group_by(weekday, hour)
            found = [(row.weekday, row.hour, row.visits, row.seconds) for row in (await req.db.execute(stmt)).all()]
        else:
            stmt = (
                select(Appointment.datetime.label('moment'), visits, seconds)
                .where(and_(*conditions, scope))
                .group_by(Appointment.datetime)
            )
            zone = _branch_timezone(timezone_name)
            found = []
            for row in (await req.db.execute(stmt)).all():
                local_moment = row.moment.replace(tzinfo=UTC).astimezone(zone) if row.moment else None
                found.append((
                    local_moment.isoweekday() if local_moment else None,
                    local_moment.hour if local_moment else None,
                    row.visits,
                    row.seconds,
                ))
        for weekday_value, hour_value, count, booked in found:
            target = unplaced if weekday_value is None else cells[(int(weekday_value), int(hour_value))]
            target[0] += int(count or 0)
            target[1] += int(booked or 0)
    return cells, unplaced


async def build_peak_load(req: ReportRequest) -> dict[str, Any]:
    base = req.base
    cells, unplaced = await _local_visit_cells(req)
    placed_visits = sum(cell[0] for cell in cells.values())
    visits_total = placed_visits + unplaced[0]
    booked_seconds = sum(cell[1] for cell in cells.values()) + unplaced[1]
    by_weekday = [sum(cell[0] for (day, _), cell in cells.items() if day == number) for number in range(1, 8)]
    by_hour: dict[int, int] = defaultdict(int)
    for (_, hour), cell in cells.items():
        by_hour[hour] += cell[0]
    hours = sorted(by_hour)

    peak_hour = max(by_hour, key=lambda hour: (by_hour[hour], -hour)) if placed_visits else None
    peak_day = by_weekday.index(max(by_weekday)) if placed_visits else None
    base['cards'] = [
        card('Визитов', visits_total, NUMBER_FORMAT),
        card('Забронировано часов', round(booked_seconds / 3600, 1), DECIMAL_FORMAT),
        card('Пиковый час', f'{peak_hour:02d}:00' if peak_hour is not None else None, TEXT_FORMAT),
        card('Пиковый день', WEEKDAY_FULL[peak_day] if peak_day is not None else None, TEXT_FORMAT),
    ]
    base['notes'].append({
        'kind': 'info',
        'title': 'Время и состав',
        'text': (
            'Завершённые визиты по дню недели и часу начала по местному времени филиала '
            '(если часовой пояс не задан — московское). «Забронировано часов» — сумма длительности этих визитов.'
        ),
    })
    if unplaced[0]:
        base['notes'].append({
            'kind': 'warning',
            'title': 'Визиты без времени',
            'text': f'{unplaced[0]} визитов не имеют времени начала: они учтены в карточках, но не в таблице и графиках.',
        })

    hour_rows = []
    for hour in hours:
        row = {'hour': f'{hour:02d}:00', 'total': by_hour[hour]}
        row.update({key: cells.get((number, hour), [0, 0])[0] for number, key in enumerate(WEEKDAY_KEYS, start=1)})
        hour_rows.append(row)
    base['charts'] = [
        chart(
            'visits_by_weekday',
            'Визиты по дням недели',
            'bar',
            list(WEEKDAY_SHORT),
            [{'label': 'Визиты', 'data': by_weekday, 'format': NUMBER_FORMAT}],
        ),
        chart(
            'visits_by_hour',
            'Визиты по часам (местное время)',
            'bar',
            [row['hour'] for row in hour_rows],
            [{'label': 'Визиты', 'data': [row['total'] for row in hour_rows], 'format': NUMBER_FORMAT}],
        ),
    ]
    base['tables'] = [
        table(
            'weekday_hour',
            'Визиты по часам и дням недели',
            [
                ('hour', 'Час', TEXT_FORMAT),
                *[(key, label, NUMBER_FORMAT) for key, label in zip(WEEKDAY_KEYS, WEEKDAY_SHORT)],
                ('total', 'Итого', NUMBER_FORMAT),
            ],
            hour_rows,
            row_key='hour',
        )
    ]
    base['raw'] = {
        'cells': [
            {'weekday': day, 'hour': hour, 'visits': cell[0], 'booked_seconds': cell[1]}
            for (day, hour), cell in sorted(cells.items())
        ],
        'visits_without_time': unplaced[0],
    }
    return base


# --- booking_channels --------------------------------------------------------------------------------------------


async def build_booking_channels(req: ReportRequest) -> dict[str, Any]:
    base = req.base
    period_rows = await _records_by_period(req, {
        'online': Appointment.created_user_id == ONLINE_CREATED_USER_ID,
        'by_staff': Appointment.created_user_id > ONLINE_CREATED_USER_ID,
    })
    for row in period_rows:
        row['unknown'] = row['records'] - row['online'] - row['by_staff']
        row['online_share'] = _share(row['online'], row['online'] + row['by_staff'])
    records = _sum(period_rows, 'records')
    online = _sum(period_rows, 'online')
    by_staff = _sum(period_rows, 'by_staff')
    unknown = records - online - by_staff
    base['cards'] = [
        card('Записей', records, NUMBER_FORMAT),
        card('Онлайн', online, NUMBER_FORMAT),
        card('Доля онлайн', _share(online, online + by_staff), PERCENT_FORMAT),
        card('Через администратора', by_staff, NUMBER_FORMAT),
    ]
    base['notes'].append({
        'kind': 'info',
        'title': 'Что считается',
        'text': (
            'Все записи, доступные в локальной базе (как в отчёте «Записи и неявки»), по дате визита. '
            '«Онлайн» — запись оформил сам клиент; «Через администратора» — запись создал пользователь YClients '
            '(администратор или мастер). Доля онлайн считается от записей с известным автором. '
            'Разбивка по источникам (Яндекс Карты, сайт и др.) пока недоступна: источник записи не синхронизируется.'
        ),
    })
    if unknown:
        base['notes'].append({
            'kind': 'warning',
            'title': 'Автор записи неизвестен',
            'text': f'У {unknown} записей автор не указан: они входят в «Записей», но не в онлайн и не в администратора.',
        })
    labels = [row['period'] for row in period_rows]
    datasets = [
        {'label': 'Онлайн', 'data': [row['online'] for row in period_rows], 'format': NUMBER_FORMAT},
        {'label': 'Через администратора', 'data': [row['by_staff'] for row in period_rows], 'format': NUMBER_FORMAT},
    ]
    if unknown:
        datasets.append(
            {'label': 'Автор не указан', 'data': [row['unknown'] for row in period_rows], 'format': NUMBER_FORMAT}
        )
    base['charts'] = [
        chart('channels_by_period', 'Записи по каналам', 'bar', labels, datasets, stacked=True, x_kind='time'),
        chart(
            'online_share_by_period',
            'Доля онлайн-записей',
            'line',
            labels,
            [{'label': 'Доля онлайн', 'data': [row['online_share'] for row in period_rows], 'format': PERCENT_FORMAT}],
            x_kind='time',
        ),
    ]
    columns = without_empty_columns(
        [
            ('period', 'Период', TEXT_FORMAT),
            ('records', 'Записей', NUMBER_FORMAT),
            ('online', 'Онлайн', NUMBER_FORMAT),
            ('by_staff', 'Через администратора', NUMBER_FORMAT),
            ('unknown', 'Автор не указан', NUMBER_FORMAT),
            ('online_share', 'Доля онлайн', PERCENT_FORMAT),
        ],
        period_rows,
        {'unknown'},
    )
    base['tables'] = [table('periods', 'Записи по каналам и периодам', columns, period_rows, row_key='period', x_kind='time')]
    base['raw'] = {'by_period': period_rows}
    return base


# --- staff_efficiency --------------------------------------------------------------------------------------------


async def _staff_branch_titles(req: ReportRequest) -> dict[int, str]:
    """Each staff row's own branch, instead of the alphabetical minimum over the branches of their visits."""
    stmt = select(Staff.id, Company.title).join(Company, Company.id == Staff.company_id)
    scope = _company_scope_clause(Staff.company_id, req.company_id, req.allowed_company_ids)
    if scope is not None:
        stmt = stmt.where(scope)
    if req.staff_id is not None:
        stmt = stmt.where(Staff.id == req.staff_id)
    return {int(row.id): row.title for row in (await req.db.execute(stmt)).all()}


def _chart_labels(rows: list[dict[str, Any]]) -> list[str]:
    """Names for a category axis; people who share a name are told apart (comparison aligns categories by label)."""
    counts = Counter(row['staff_name'] for row in rows)
    return [
        f"{row['staff_name']} ({row['company_title'] or row['staff_id']})"
        if counts[row['staff_name']] > 1
        else row['staff_name']
        for row in rows
    ]


async def build_staff_efficiency(req: ReportRequest) -> dict[str, Any]:
    base = req.base
    rows = await _staff_rows(
        req.db, req.start, req.end, req.company_id, req.staff_id, req.allowed_company_ids, req.factual_at
    )
    titles = await _staff_branch_titles(req)
    for row in rows:
        if row['staff_id'] is None:
            row['staff_name'] = UNNAMED_STAFF
            row['company_title'] = None
        else:
            row['company_title'] = titles.get(int(row['staff_id']), row['company_title'])
        if not row['completed']:
            # Same rule as the card: no completed visit, no average (a master with only no-shows has no "0 ₽").
            row['avg_check'] = None
    total_revenue = sum(row['revenue'] for row in rows)
    total_completed = sum(row['completed'] for row in rows)
    base['notes'].append({
        'kind': 'formula',
        'title': 'Выручка услуг по сотрудникам',
        'text': (
            'Разрез включает физические оплаты услуг по дате платежа, как в Обзоре '
            'и План/факт; товары и пополнения показаны отдельно. '
            '«Без мастера» — записи, в которых сотрудник не указан.'
        ),
    })
    base['cards'] = [
        card('Сотрудников в отчете', sum(row['staff_id'] is not None for row in rows), NUMBER_FORMAT),
        card('Завершено записей', total_completed, NUMBER_FORMAT),
        card('Выручка услуг', total_revenue, MONEY_FORMAT),
        card(
            'Выручка услуг / завершенная запись',
            total_revenue / total_completed if total_completed else None,
            MONEY_FORMAT,
        ),
    ]
    top = top_rows(req, rows, STAFF_CHART_ROWS)
    # Told apart across every row, not just the top ones: the comparison run labels its full list the same way.
    labels = _chart_labels(rows)[:len(top)]
    base['charts'] = [
        chart(
            'staff_revenue',
            'Выручка услуг по сотрудникам',
            'bar',
            labels,
            [{'label': 'Выручка услуг', 'data': [row['revenue'] for row in top], 'format': MONEY_FORMAT}],
        ),
        chart(
            'staff_completed',
            'Завершенные записи',
            'bar',
            labels,
            [{'label': 'Записи', 'data': [row['completed'] for row in top], 'format': NUMBER_FORMAT}],
        ),
    ]
    base['tables'] = [
        table(
            'staff',
            'Сотрудники',
            [
                ('staff_name', 'Сотрудник', TEXT_FORMAT),
                ('company_title', 'Филиал', TEXT_FORMAT),
                ('completed', 'Завершено', NUMBER_FORMAT),
                ('appointments', 'Доступные записи', NUMBER_FORMAT),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('revenue', 'Выручка услуг', MONEY_FORMAT),
                ('avg_check', 'Выручка услуг / завершенная запись', MONEY_FORMAT),
            ],
            rows,
            row_key='staff_id',
        )
    ]
    base['raw'] = {'staff': rows, 'revenue_scope': 'services'}
    return base
