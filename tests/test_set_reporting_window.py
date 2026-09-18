"""Argument parsing for the reporting-window setter.

A mis-parsed argument silently removes history from every metric of the wrong branch,
so the guards matter more than the usual CLI plumbing.
"""
from datetime import date, timedelta

import pytest

import scripts.set_reporting_window as set_reporting_window


def test_parses_dates_and_clear_keywords():
    parsed = set_reporting_window.parse_assignments(
        ['11=2022-05-01', '22=none', '33=CLEAR']
    )

    assert parsed == {11: date(2022, 5, 1), 22: None, 33: None}


def test_rejects_a_future_reporting_start():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    with pytest.raises(SystemExit) as excinfo:
        set_reporting_window.parse_assignments([f'1={tomorrow}'])

    assert 'future' in str(excinfo.value)


def test_accepts_today_as_a_reporting_start():
    today = date.today()

    assert set_reporting_window.parse_assignments([f'1={today.isoformat()}']) == {1: today}


def test_accepts_a_future_reporting_end():
    """A branch can be scheduled to leave; only a start in the future is a typo."""
    tomorrow = date.today() + timedelta(days=1)

    parsed = set_reporting_window.parse_assignments([f'1={tomorrow.isoformat()}'], 'end')

    assert parsed == {1: tomorrow}


@pytest.mark.parametrize('argument', ['11', '11:2022-05-01'])
def test_rejects_arguments_without_an_assignment(argument):
    with pytest.raises(SystemExit):
        set_reporting_window.parse_assignments([argument])


def test_rejects_a_non_integer_company_id():
    with pytest.raises(SystemExit) as excinfo:
        set_reporting_window.parse_assignments(['branch=2022-05-01'])

    assert 'integer' in str(excinfo.value)


def test_rejects_an_unparseable_date():
    with pytest.raises(SystemExit) as excinfo:
        set_reporting_window.parse_assignments(['1=01.05.2022'])

    assert 'ISO date' in str(excinfo.value)


def test_rejects_the_same_company_twice():
    """Silently taking the last value would apply a date the operator did not check."""
    with pytest.raises(SystemExit) as excinfo:
        set_reporting_window.parse_assignments(['1=2022-05-01', '1=2023-05-01'])

    assert 'more than once' in str(excinfo.value)


class _Branch:
    def __init__(self, company_id, start, end):
        self.id = company_id
        self.reporting_start_date = start
        self.reporting_end_date = end


def test_resulting_window_merges_assignments_with_stored_values():
    """An end is checked against the start the branch will have, not only the one passed in."""
    branch = _Branch(1, date(2022, 5, 1), None)

    window = set_reporting_window._resulting_window(
        branch,
        {'start': {}, 'end': {1: date(2026, 8, 31)}},
    )

    assert window == (date(2022, 5, 1), date(2026, 8, 31))
