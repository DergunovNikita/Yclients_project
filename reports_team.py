"""Builder for the staff leaderboard report."""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from dashboard_service import fetch_plan_fact
from report_payload import (
    MONEY_FORMAT,
    NUMBER_FORMAT,
    PERCENT_FORMAT,
    ReportCalculationError,
    ReportRequest,
    card,
    chart,
    ranking_table,
    table,
)

logger = logging.getLogger('yclients.reports')

LEVEL_HIDDEN_TABLES = frozenset({
    'extra_services_admin', 'cosmo_admin', 'opz_admin', 'reviews_admin', 'avg_check_plan_branch',
})


def _is_retryable(error: BaseException) -> bool:
    """Whether trying again could help: the connection or the pool failed, not the statement.

    A statement cancelled by `statement_timeout` reaches us as a plain `DBAPIError` (asyncpg's
    `QueryCanceledError` is not an `OperationalError`) and would time out again on the same window,
    so it, like a programming error, surfaces as a 500 instead of a "retry".
    """
    if isinstance(error, DBAPIError):
        return error.connection_invalidated or isinstance(error, (OperationalError, InterfaceError))
    return isinstance(error, (OSError, PoolTimeout))


async def build_staff_leaderboard(req: ReportRequest) -> dict[str, Any]:
    base = req.base
    start, end, company_id, staff_id = req.start, req.end, req.company_id, req.staff_id
    started_at = time.perf_counter()
    try:
        plan = await fetch_plan_fact(
            req.db,
            start,
            end,
            company_id,
            staff_id,
            allowed_company_ids=req.allowed_company_ids,
            force_allowed=req.allowed_company_ids is not None,
            for_ratings=True,
            # The boards read the branch row only when no staff is selected.
            include_branch_row=staff_id is None,
            factual_at=req.factual_at,
            staff_position_ids=req.staff_position_ids,
        )
    except (SQLAlchemyError, OSError) as error:
        if not _is_retryable(error):
            raise
        logger.exception(
            'staff_leaderboard failed stage=plan_fact start=%s end=%s company_id=%s',
            start.isoformat(), end.isoformat(), company_id,
        )
        try:
            await req.db.rollback()
        except Exception as rollback_error:  # noqa: BLE001 - the original failure is what the caller needs
            logger.warning('staff_leaderboard rollback failed: %s', type(rollback_error).__name__)
        raise ReportCalculationError('staff leaderboard plan/fact failed', stage='plan_fact') from error
    finally:
        logger.info(
            'staff_leaderboard plan_fact finished start=%s end=%s company_id=%s duration_ms=%s',
            start.isoformat(), end.isoformat(), company_id, round((time.perf_counter() - started_at) * 1000),
        )
    boards = plan.get('staff_leaderboards', {})
    level_filter = plan.get('level_filter')
    partial_reasons = set(boards.get('_partial_reasons') or [])
    if partial_reasons:
        base['source_status'] = 'partial'
        if 'extra_service_revenue' in partial_reasons:
            base['notes'].append({
                'kind': 'warning',
                'title': 'Часть рейтинга временно недоступна',
                'text': 'Не удалось рассчитать суммы допуслуг. Количество и проценты показаны полностью.',
            })

    staff_col = ('staff', 'Сотрудник', 'text')
    branch_col = ('company_title', 'Барбершоп', 'text')
    qty_col = ('qty', 'Кол-во, шт', NUMBER_FORMAT)
    sum_col = ('sum', 'Сумма', MONEY_FORMAT)
    avg_col = ('avg', 'Ср. чек, ₽', MONEY_FORMAT)
    money_ranking_options = [
        ('qty', 'По количеству'),
        ('sum', 'По сумме'),
        ('pct', 'По проценту'),
        ('avg', 'По среднему чеку'),
    ]
    extra_share_col = ('share_pct', 'Доля доп. услуг по метке, %', PERCENT_FORMAT)
    cosmo_share_col = ('share_pct', 'Доля продаж косметики, %', PERCENT_FORMAT)

    revenue_barber = boards.get('revenue_barber', [])
    if level_filter is not None:
        titles = ', '.join(level_filter.get('titles') or [])
        base['notes'].append({
            'kind': 'info',
            'title': 'Рейтинги по ролям барберов',
            'text': (
                f'Показаны только мастера выбранных ролей{f": {titles}" if titles else ""}. '
                'Рейтинги администраторов и итоги по барбершопам скрыты: у них нет роли.'
            ),
        })
    elif any(
        boards.get(key)
        for key in (
            'extra_services_admin',
            'opz_admin',
        )
    ):
        base['notes'].append({
            'kind': 'info',
            'title': 'Область расчета показателей администраторов',
            'text': (
                'Косметика относится к самому продавцу; '
                'ОПЗ и допуслуги — к записям под ответственностью администратора.'
            ),
        })
    if staff_id is not None:
        # An administrator has no revenue metric (cosmetics is theirs): no revenue card rather than a zero.
        selected_admin = any(group.get('category') == 'administrator' for group in plan.get('groups') or [])
        base['cards'] = [] if selected_admin else [
            card(
                'Личная выручка выбранного сотрудника',
                revenue_barber[0]['value'] if revenue_barber else 0,
                MONEY_FORMAT,
            ),
        ]
    else:
        base['cards'] = [
            card(
                'Топ выручка мастера',
                revenue_barber[0]['value'] if revenue_barber else 0,
                MONEY_FORMAT,
            ),
        ]
    charts = []
    if revenue_barber:
        charts.append(
            chart(
                'leaderboard_revenue_barber',
                'Топ по выручке — мастера',
                'bar',
                [row['staff'] for row in revenue_barber],
                [{'label': 'Выручка', 'data': [row['value'] for row in revenue_barber], 'format': MONEY_FORMAT}],
            )
        )
    if charts:
        base['charts'] = charts
    base['tables'] = [
        ranking_table(
            'extra_services',
            'Топ по допуслугам — мастера',
            [
                staff_col,
                branch_col,
                qty_col,
                sum_col,
                avg_col,
                ('pct', 'Доп. услуги, %', PERCENT_FORMAT),
                extra_share_col,
            ],
            boards.get('extra_services_barber_rankings', {}),
            'pct',
            money_ranking_options,
            hide_when_empty=True,
        ),
        ranking_table(
            'extra_services_admin',
            'Топ по допуслугам записей администратора',
            [staff_col, branch_col, qty_col, ('pct', 'Доп. услуги, %', PERCENT_FORMAT)],
            boards.get('extra_services_admin_rankings', {}),
            'pct',
            [('qty', 'По количеству'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        ranking_table(
            'cosmo_barber',
            'Топ по косметике — мастера',
            [
                staff_col,
                branch_col,
                qty_col,
                sum_col,
                avg_col,
                ('pct', 'Косметика, %', PERCENT_FORMAT),
                cosmo_share_col,
            ],
            boards.get('cosmo_barber_rankings', {}),
            'sum',
            money_ranking_options,
            hide_when_empty=True,
        ),
        ranking_table(
            'cosmo_admin',
            'Топ по косметике — админы',
            [
                staff_col,
                branch_col,
                qty_col,
                sum_col,
                avg_col,
                cosmo_share_col,
            ],
            boards.get('cosmo_admin_rankings', {}),
            'sum',
            [option for option in money_ranking_options if option[0] != 'pct'],
            hide_when_empty=True,
        ),
        ranking_table(
            'opz_barber',
            'Топ по ОПЗ — мастера',
            [staff_col, branch_col, qty_col, ('pct', 'ОПЗ, %', PERCENT_FORMAT)],
            boards.get('opz_barber_rankings', {}),
            'pct',
            [('qty', 'По количеству'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        ranking_table(
            'opz_admin',
            'Топ по ОПЗ в записях под ответственностью — администраторы',
            [staff_col, branch_col, qty_col, ('pct', 'ОПЗ, %', PERCENT_FORMAT)],
            boards.get('opz_admin_rankings', {}),
            'pct',
            [('qty', 'По количеству'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        # Not hide_when_empty: manual review facts are often unfilled, so this
        # top anchors the report (and shows its empty state) even when all
        # other leaderboards are empty for the period.
        table(
            'reviews_admin',
            'Топ по отзывам — админы',
            [staff_col, branch_col, ('value', 'Отзывы', NUMBER_FORMAT)],
            boards.get('reviews_admin', []),
        ),
        table(
            'revenue_barber',
            'Топ по выручке — мастера',
            [staff_col, branch_col, ('value', 'Выручка', MONEY_FORMAT)],
            revenue_barber,
            hide_when_empty=True,
        ),
        table(
            'avg_check_barber',
            'Топ по среднему чеку — мастера',
            [
                staff_col,
                branch_col,
                ('value', 'Ср. чек, ₽', MONEY_FORMAT),
                ('visits', 'Завершённые визиты', NUMBER_FORMAT),
            ],
            boards.get('avg_check_barber', []),
            hide_when_empty=True,
        ),
        table(
            'avg_check_plan_branch',
            'Топ выполнения плана среднего чека — барбершопы',
            [
                ('staff', 'Барбершоп', 'text'),
                ('plan', 'План', MONEY_FORMAT),
                ('fact', 'Факт', MONEY_FORMAT),
                ('pct', 'Выполнение, %', PERCENT_FORMAT),
            ],
            boards.get('avg_check_plan_branch', []),
            hide_when_empty=True,
        ),
        table(
            'avg_check_plan_staff',
            'Топ выполнения плана среднего чека — мастера',
            [
                staff_col,
                branch_col,
                ('plan', 'План', MONEY_FORMAT),
                ('fact', 'Факт', MONEY_FORMAT),
                ('pct', 'Выполнение, %', PERCENT_FORMAT),
            ],
            boards.get('avg_check_plan_staff', []),
            hide_when_empty=True,
        ),
    ]
    if level_filter is not None:
        # Administrators have no level, and a branch total says nothing about one level of its barbers.
        base['tables'] = [item for item in base['tables'] if item['id'] not in LEVEL_HIDDEN_TABLES]
    return base
