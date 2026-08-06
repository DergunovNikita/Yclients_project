"""Argument parsing for the reporting-start setter.

A mis-parsed argument silently removes history from every metric of the wrong branch,
so the guards matter more than the usual CLI plumbing.
"""
from datetime import date, timedelta

import pytest

import scripts.set_reporting_start as set_reporting_start


def test_parses_dates_and_clear_keywords():
    parsed = set_reporting_start.parse_assignments(
        ['11=2022-05-01', '22=none', '33=CLEAR']
    )

    assert parsed == {11: date(2022, 5, 1), 22: None, 33: None}


def test_rejects_a_future_reporting_start():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    with pytest.raises(SystemExit) as excinfo:
        set_reporting_start.parse_assignments([f'1={tomorrow}'])

    assert 'future' in str(excinfo.value)


def test_accepts_today_as_a_reporting_start():
    today = date.today()

    assert set_reporting_start.parse_assignments([f'1={today.isoformat()}']) == {1: today}


@pytest.mark.parametrize('argument', ['11', '11:2022-05-01'])
def test_rejects_arguments_without_an_assignment(argument):
    with pytest.raises(SystemExit):
        set_reporting_start.parse_assignments([argument])


def test_rejects_a_non_integer_company_id():
    with pytest.raises(SystemExit) as excinfo:
        set_reporting_start.parse_assignments(['branch=2022-05-01'])

    assert 'integer' in str(excinfo.value)


def test_rejects_an_unparseable_date():
    with pytest.raises(SystemExit) as excinfo:
        set_reporting_start.parse_assignments(['1=01.05.2022'])

    assert 'ISO date' in str(excinfo.value)


def test_rejects_the_same_company_twice():
    """Silently taking the last value would apply a date the operator did not check."""
    with pytest.raises(SystemExit) as excinfo:
        set_reporting_start.parse_assignments(['1=2022-05-01', '1=2023-05-01'])

    assert 'more than once' in str(excinfo.value)
