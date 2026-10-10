"""Builder for the staff leaderboard report."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

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
# What makes a leaderboard run worth retrying: the database (or its connection) failed, not the request or the code.
INFRASTRUCTURE_ERRORS = (SQLAlchemyError, OSError)


async def build_staff_leaderboard(req: ReportRequest) -> dict[str, Any]:
    return await _leaderboard_payload(
        req.db, req.base, req.start, req.end, req.company_id, req.staff_id, req.allowed_company_ids, req.factual_at
    )


async def _leaderboard_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    try:
        return await _leaderboard_payload_impl(
            db,
            base,
            start,
            end,
            company_id,
            staff_id,
            allowed_company_ids,
            factual_at,
        )
    except (ReportCalculationError, *INFRASTRUCTURE_ERRORS) as error:
        # Only a failing database is retryable; anything else is a defect (500) or a refused input (400).
        logger.exception(
            'staff_leaderboard failed stage=%s start=%s end=%s company_id=%s',
            getattr(error, 'stage', 'payload'), start.isoformat(), end.isoformat(), company_id,
        )
        try:
            await db.rollback()
        except Exception as rollback_error:  # noqa: BLE001 - the original failure is what the caller needs
            logger.warning('staff_leaderboard rollback failed: %s', type(rollback_error).__name__)
        if isinstance(error, ReportCalculationError):
            raise
        raise ReportCalculationError('staff leaderboard calculation failed', stage='payload') from error
    finally:
        logger.info(
            'staff_leaderboard finished start=%s end=%s company_id=%s duration_ms=%s',
            start.isoformat(), end.isoformat(), company_id, round((time.perf_counter() - started_at) * 1000),
        )


async def _leaderboard_payload_impl(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    stage_started = time.perf_counter()
    try:
        plan = await fetch_plan_fact(
            db,
            start,
            end,
            company_id,
            staff_id,
            allowed_company_ids=allowed_company_ids,
            force_allowed=allowed_company_ids is not None,
            include_extra_service_revenue=True,
            include_all_staff_in_leaderboards=True,
            include_staff_leaderboards=True,
            factual_at=factual_at,
        )
    except INFRASTRUCTURE_ERRORS as error:
        raise ReportCalculationError('staff leaderboard plan/fact failed', stage='plan_fact') from error
    logger.info(
        'staff_leaderboard plan_fact ready start=%s end=%s company_id=%s duration_ms=%s',
        start.isoformat(), end.isoformat(), company_id, round((time.perf_counter() - stage_started) * 1000),
    )
    boards = plan.get('staff_leaderboards', {})
    partial_reasons = set(boards.get('_partial_reasons') or [])
    if partial_reasons:
        base['source_status'] = 'partial'
        if 'extra_service_revenue' in partial_reasons:
            base['notes'].append({
                'kind': 'warning',
                'title': 'Часть рейтинга временно недоступна',
                'text': 'Не удалось рассчитать суммы допуслуг. Количество и проценты показаны полностью.',
            })
        if 'staff_schedules' in partial_reasons:
            base['notes'].append({
                'kind': 'warning',
                'title': 'Не все графики администраторов загружены',
                'text': (
                    'Администраторы без полного покрытия выбранного периода исключены '
                    'из рейтинга допуслуг.'
                ),
            })

    staff_col = ('staff', 'Сотрудник', 'text')
    branch_col = ('company_title', 'Барбершоп', 'text')
    qty_col = ('qty', 'Кол-во, шт', NUMBER_FORMAT)
    extra_share_col = ('share_pct', 'Доля доп. услуг по метке, %', PERCENT_FORMAT)
    cosmo_share_col = ('share_pct', 'Доля продаж косметики, %', PERCENT_FORMAT)

    revenue_barber = boards.get('revenue_barber', [])
    revenue_admin = boards.get('revenue_admin', [])
    if any(
        boards.get(key)
        for key in (
            'extra_services_admin',
            'opz_admin',
            'revenue_admin',
        )
    ):
        base['notes'].append({
            'kind': 'info',
            'title': 'Область расчета показателей администраторов',
            'text': (
                'Личная выручка и косметика относятся к самому продавцу; '
                'ОПЗ — к записям под ответственностью администратора; '
                'допуслуги — ко всему филиалу во время его смен.'
            ),
        })
    if staff_id is not None:
        selected_revenue = (revenue_barber or revenue_admin)
        base['cards'] = [
            card(
                'Личная выручка выбранного сотрудника',
                selected_revenue[0]['value'] if selected_revenue else 0,
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
    if revenue_admin:
        charts.append(
            chart(
                'leaderboard_revenue_admin',
                'Топ по личной выручке — администраторы',
                'bar',
                [row['staff'] for row in revenue_admin],
                [{'label': 'Выручка', 'data': [row['value'] for row in revenue_admin], 'format': MONEY_FORMAT}],
            )
        )
    if charts:
        base['charts'] = charts
    base['tables'] = [
        ranking_table(
            'extra_services',
            'Топ по допуслугам — мастера',
            [staff_col, branch_col, qty_col, ('sum', 'Сумма', MONEY_FORMAT), ('pct', 'Доп. услуги, %', PERCENT_FORMAT), extra_share_col],
            boards.get('extra_services_barber_rankings', {}),
            'pct',
            [('qty', 'По количеству'), ('sum', 'По сумме'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        ranking_table(
            'extra_services_admin',
            'Топ по допуслугам филиала во время смен — администраторы',
            [staff_col, branch_col, qty_col, ('pct', 'Доп. услуги, %', PERCENT_FORMAT)],
            boards.get('extra_services_admin_rankings', {}),
            'pct',
            [('qty', 'По количеству'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        ranking_table(
            'cosmo_barber',
            'Топ по косметике — мастера',
            [staff_col, branch_col, qty_col, ('sum', 'Сумма', MONEY_FORMAT), ('pct', 'Косметика, %', PERCENT_FORMAT), cosmo_share_col],
            boards.get('cosmo_barber_rankings', {}),
            'sum',
            [('qty', 'По количеству'), ('sum', 'По сумме'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        ranking_table(
            'cosmo_admin',
            'Топ по косметике — админы',
            [staff_col, branch_col, qty_col, ('sum', 'Сумма', MONEY_FORMAT), ('pct', 'Косметика, %', PERCENT_FORMAT), cosmo_share_col],
            boards.get('cosmo_admin_rankings', {}),
            'sum',
            [('qty', 'По количеству'), ('sum', 'По сумме'), ('pct', 'По проценту')],
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
            'revenue_admin',
            'Топ по личной выручке — администраторы',
            [staff_col, branch_col, ('value', 'Выручка', MONEY_FORMAT)],
            revenue_admin,
            hide_when_empty=True,
        ),
        table(
            'avg_check_plan_branch',
            'Топ выполнения плана среднего чека — барбершопы',
            [('staff', 'Барбершоп', 'text'), ('plan', 'План', MONEY_FORMAT), ('fact', 'Факт', MONEY_FORMAT), ('pct', 'Выполнение, %', PERCENT_FORMAT)],
            boards.get('avg_check_plan_branch', []),
            hide_when_empty=True,
        ),
        table(
            'avg_check_plan_staff',
            'Топ выполнения плана среднего чека — мастера',
            [staff_col, branch_col, ('plan', 'План', MONEY_FORMAT), ('fact', 'Факт', MONEY_FORMAT), ('pct', 'Выполнение, %', PERCENT_FORMAT)],
            boards.get('avg_check_plan_staff', []),
            hide_when_empty=True,
        ),
    ]
    return base
