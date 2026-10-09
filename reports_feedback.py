"""Builders for the customer feedback reports."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import and_, case, exists, func, select
from sqlalchemy.orm import aliased

from dashboard_service import _company_scope_clause, day_window, reporting_window_clause
from models import Comment, Company, Staff
from report_payload import DECIMAL_FORMAT, NUMBER_FORMAT, PERCENT_FORMAT, ReportRequest, card, chart, table

MAX_RATING = 5
LOW_RATING = 3
NO_MASTER = 'Без мастера'


def _review_conditions(req: ReportRequest) -> list[Any]:
    """Rated reviews of the period, each review once.

    YClients sometimes returns one review twice: once tied to a master and once without one (same visit,
    same moment). The anonymous twin is dropped; a review with no twin stays, even without a master.
    """
    twin = aliased(Comment)
    anonymous_twin = and_(
        Comment.master_id.is_(None),
        Comment.record_id.is_not(None),
        exists(
            select(1).where(
                twin.company_id == Comment.company_id,
                twin.record_id == Comment.record_id,
                twin.date == Comment.date,
                twin.master_id.is_not(None),
            )
        ),
    )
    conditions = [
        Comment.rating > 0,
        *day_window(Comment.date, req.start, req.end),
        reporting_window_clause(Comment.company_id, Comment.date),
        ~anonymous_twin,
    ]
    scope = _company_scope_clause(Comment.company_id, req.company_id, req.allowed_company_ids)
    if scope is not None:
        conditions.append(scope)
    if req.staff_id is not None:
        conditions.append(Comment.master_id == req.staff_id)
    return conditions


def _has_text():
    return func.length(func.trim(func.coalesce(Comment.text, ''))) > 0


async def build_nps_dashboard(req: ReportRequest) -> dict[str, Any]:
    """YClients reviews: volume, average rating, low-rating share, distribution and the negative ones."""
    base = req.base
    conditions = _review_conditions(req)
    totals = (
        await req.db.execute(
            select(
                func.count(Comment.id),
                func.avg(Comment.rating),
                func.coalesce(func.sum(case((Comment.rating < MAX_RATING, 1), else_=0)), 0),
                func.coalesce(func.sum(case((_has_text(), 1), else_=0)), 0),
            ).where(*conditions)
        )
    ).one()
    reviews, avg_rating, below_max, with_text = int(totals[0]), totals[1], int(totals[2]), int(totals[3])

    distribution: dict[int, int] = defaultdict(int)
    rating_stmt = select(Comment.rating, func.count(Comment.id)).where(*conditions).group_by(Comment.rating)
    rating_rows = (await req.db.execute(rating_stmt)).all()
    for rating, count in rating_rows:
        distribution[min(max(int(round(float(rating))), 1), MAX_RATING)] += int(count)

    negative_stmt = (
        select(
            Comment.id,
            Comment.date,
            Comment.rating,
            Comment.text,
            Comment.master_id,
            Staff.name.label('staff_name'),
            Company.title.label('company_title'),
        )
        .outerjoin(Staff, Staff.id == Comment.master_id)
        .outerjoin(Company, Company.id == Comment.company_id)
        .where(*conditions, Comment.rating <= LOW_RATING)
        .order_by(Comment.date.desc(), Comment.id.desc())
    )
    negative_rows = [
        {
            'id': row.id,
            'date': row.date.isoformat() if row.date else None,
            'staff_name': row.staff_name or NO_MASTER,
            'company_title': row.company_title,
            'rating': float(row.rating),
            'comment': row.text,
        }
        for row in (await req.db.execute(negative_stmt)).all()
    ]

    base['cards'] = [
        card('Отзывов', reviews, NUMBER_FORMAT),
        card('Средняя оценка', float(avg_rating) if avg_rating is not None else None, DECIMAL_FORMAT),
        card('Доля оценок ниже 5', round(below_max / reviews * 100, 2) if reviews else None, PERCENT_FORMAT),
        card('Отзывов с текстом', with_text, NUMBER_FORMAT),
    ]
    ratings = list(range(1, MAX_RATING + 1))
    base['charts'] = [
        chart(
            'ratings',
            'Распределение оценок',
            'bar',
            [str(rating) for rating in ratings],
            [{'label': 'Отзывов', 'data': [distribution[rating] for rating in ratings], 'format': NUMBER_FORMAT}],
        )
    ]
    columns = [('date', 'Дата', 'date'), ('staff_name', 'Мастер', 'text')]
    if len(req.allowed_company_ids or []) > 1:
        columns.append(('company_title', 'Филиал', 'text'))
    columns += [('rating', 'Оценка', DECIMAL_FORMAT), ('comment', 'Комментарий', 'text')]
    base['tables'] = [
        table(
            'negative_reviews',
            f'Оценки {LOW_RATING} и ниже',
            columns,
            negative_rows,
        )
    ]
    if req.staff_id is not None:
        base['notes'].append({
            'kind': 'info',
            'title': 'Фильтр по сотруднику',
            'text': 'Показаны отзывы, оставленные этому мастеру; отзывы о филиале без мастера не входят.',
        })
    return base
