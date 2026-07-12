"""Shared plan/fact metric definitions and staff category helpers."""

from __future__ import annotations

from collections.abc import Mapping
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

MONEY_METRICS: tuple[dict[str, Any], ...] = (
    {
        'code': 'revenue',
        'label': 'Выручка',
        'summary': ('revenue',),
        'plan': ('revenue',),
        'leaderboard': ('revenue_top', 'revenue_barber', 'revenue_admin'),
    },
    {
        'code': 'avg_check',
        'label': 'Средний чек',
        'summary': ('average_check', 'average_check_source_status'),
        'plan': ('avg_check_total',),
        'leaderboard': ('avg_check_top',),
    },
    {
        'code': 'cosmo_sum',
        'label': 'Космо, сумма',
        'summary': (),
        'plan': ('cosmo_sum', 'cosmo_price'),
        'leaderboard': ('cosmo_barber', 'cosmo_admin'),
    },
)

ALL_MONEY_CODES: frozenset[str] = frozenset(metric['code'] for metric in MONEY_METRICS)

# Roles allowed to configure subordinate visibility always see every money metric.
DEFAULT_ROLE_MONEY_CODES: dict[str, frozenset[str]] = {
    'platform_admin': ALL_MONEY_CODES,
    'owner': ALL_MONEY_CODES,
    'branch_admin': ALL_MONEY_CODES,
    'manager': frozenset(),
    'viewer': frozenset(),
}

# Roles whose money visibility is configurable per tenant (rest are fixed at ALL).
CONFIGURABLE_MONEY_ROLES: tuple[str, ...] = ('branch_admin', 'manager', 'viewer')


def default_money_codes_for_role(role: str | None) -> frozenset[str]:
    return DEFAULT_ROLE_MONEY_CODES.get(role or '', frozenset())


def money_payload_keys(hidden_codes: frozenset[str], group: str) -> set[str]:
    """Payload keys (summary/plan/leaderboard) governed by the given hidden money codes."""
    keys: set[str] = set()
    for metric in MONEY_METRICS:
        if metric['code'] in hidden_codes:
            keys.update(metric.get(group, ()))
    return keys


RAW_PLAN_FACT_CODES = {
    'revenue',
    'clients',
    'avg_check_denominator',
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


def has_zero_clients_plan(plan_values: Mapping[str, Any]) -> bool:
    return (
        'clients' in plan_values
        and float(plan_values.get('clients') or 0.0) == 0.0
    )


def has_positive_plan_values(plan_values: Mapping[str, Any]) -> bool:
    return any(float(value or 0.0) > 0.0 for value in plan_values.values())


def is_visible_staff_plan(plan_values: Mapping[str, Any]) -> bool:
    return (
        not has_zero_clients_plan(plan_values)
        and (not plan_values or has_positive_plan_values(plan_values))
    )


def is_non_working_staff_plan(plan_values: Mapping[str, Any]) -> bool:
    return has_zero_clients_plan(plan_values) or not has_positive_plan_values(plan_values)
