"""Shared plan/fact metric definitions and staff category helpers."""

from __future__ import annotations

from typing import Any


REVIEWS_QTY_CODE = 'reviews_qty'

PLAN_FACT_METRICS: tuple[dict[str, str], ...] = (
    {'code': 'revenue', 'label': 'Выручка', 'format': 'money'},
    {'code': 'avg_check_total', 'label': 'СЧ общий', 'format': 'money'},
    {'code': 'clients', 'label': 'Кол-во клиентов', 'format': 'number'},
    {'code': 'wax_qty', 'label': 'Воск, шт', 'format': 'number'},
    {'code': 'camouflage_qty', 'label': 'Камуфляж, шт', 'format': 'number'},
    {'code': 'face_care_qty', 'label': 'Уход лицо, шт', 'format': 'number'},
    {'code': 'head_care_qty', 'label': 'Уход голова, шт', 'format': 'number'},
    {'code': 'cosmo_qty', 'label': 'Космо, шт', 'format': 'number'},
    {'code': 'cosmo_sum', 'label': 'Космо сумм.', 'format': 'money'},
    {'code': 'opz_qty', 'label': 'ОПЗ, шт', 'format': 'number'},
    {'code': 'opz_pct', 'label': 'ОПЗ,%', 'format': 'percent'},
    {'code': 'extra_services_pct', 'label': '% доп.услуг', 'format': 'percent'},
    {'code': REVIEWS_QTY_CODE, 'label': 'Отзывы', 'format': 'number'},
)

RAW_PLAN_FACT_CODES = {
    'revenue',
    'clients',
    'wax_qty',
    'camouflage_qty',
    'face_care_qty',
    'head_care_qty',
    'cosmo_qty',
    'cosmo_sum',
    'opz_qty',
    REVIEWS_QTY_CODE,
}

BARBER_METRIC_CODES = tuple(
    metric['code']
    for metric in PLAN_FACT_METRICS
    if metric['code'] != REVIEWS_QTY_CODE
)
ADMIN_METRIC_CODES = (
    'revenue',
    'avg_check_total',
    'clients',
    'cosmo_qty',
    'cosmo_sum',
    'opz_qty',
    'opz_pct',
    REVIEWS_QTY_CODE,
)

STAFF_CATEGORY_METRIC_CODES = {
    'barber': BARBER_METRIC_CODES,
    'administrator': ADMIN_METRIC_CODES,
}

STAFF_CATEGORY_LABELS = {
    'barber': 'Барберы',
    'administrator': 'Администраторы',
    'unknown': 'Без категории',
}

STAFF_CATEGORY_ALIASES = {
    'barber': 'barber',
    'barbers': 'barber',
    'master': 'barber',
    'masters': 'barber',
    'барбер': 'barber',
    'барберы': 'barber',
    'мастер': 'barber',
    'мастера': 'barber',
    'administrator': 'administrator',
    'administrators': 'administrator',
    'admin': 'administrator',
    'admins': 'administrator',
    'администратор': 'administrator',
    'администраторы': 'administrator',
    'админ': 'administrator',
    'админы': 'administrator',
}


def normalize_plan_text(value: Any) -> str:
    return str(value or '').strip().lower().replace('ё', 'е')


def normalize_staff_category(value: Any) -> str | None:
    text = normalize_plan_text(value)
    if not text:
        return None
    for alias, category in STAFF_CATEGORY_ALIASES.items():
        if alias in text:
            return category
    return None


def metrics_for_category(category: str | None) -> tuple[dict[str, str], ...]:
    codes = STAFF_CATEGORY_METRIC_CODES.get(category or '', BARBER_METRIC_CODES)
    allowed = set(codes)
    return tuple(metric for metric in PLAN_FACT_METRICS if metric['code'] in allowed)
