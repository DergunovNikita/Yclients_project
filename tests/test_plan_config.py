from plan_config import (
    has_positive_plan_values,
    has_zero_clients_plan,
    is_non_working_staff_plan,
    is_visible_staff_plan,
)


def test_plan_value_predicates_treat_zero_clients_as_non_working():
    values = {'clients': 0.0, 'revenue': 9000.0}

    assert has_zero_clients_plan(values) is True
    assert has_positive_plan_values(values) is True
    assert is_non_working_staff_plan(values) is True
    assert is_visible_staff_plan(values) is False


def test_plan_value_predicates_allow_empty_dashboard_staff_plan():
    assert has_zero_clients_plan({}) is False
    assert has_positive_plan_values({}) is False
    assert is_non_working_staff_plan({}) is True
    assert is_visible_staff_plan({}) is True


def test_plan_value_predicates_treat_blank_clients_key_as_zero_clients():
    values = {'clients': None, 'revenue': 0.0, 'wax_qty': ''}

    assert has_zero_clients_plan(values) is True
    assert has_positive_plan_values(values) is False
    assert is_non_working_staff_plan(values) is True
    assert is_visible_staff_plan(values) is False
