from report_payload import MONEY_FORMAT, NUMBER_FORMAT, card, chart, ranking_table, table, without_empty_columns


def test_chart_always_states_its_x_axis_kind():
    assert chart('c', 'T', 'bar', ['a'], [])['x_kind'] == 'category'
    assert chart('c', 'T', 'line', ['2025-01-01'], [], x_kind='time')['x_kind'] == 'time'
    flags = chart('c', 'T', 'bar', [], [], stacked=True, wide=True)
    assert flags['stacked'] is True and flags['wide'] is True


def test_table_emits_row_key_only_when_given():
    columns = [('period', 'Период', 'text'), ('revenue', 'Выручка', MONEY_FORMAT)]
    assert 'row_key' not in table('t', 'T', columns, [])
    assert table('t', 'T', columns, [], row_key='period')['row_key'] == 'period'
    ranking = ranking_table('r', 'R', columns, {'a': [{'period': 'x'}]}, 'a', [('a', 'A')])
    assert ranking['rows'] == [{'period': 'x'}]
    assert ranking['ranking']['default_metric'] == 'a'


def test_card_and_without_empty_columns():
    assert card('Выручка', 5, MONEY_FORMAT) == {'label': 'Выручка', 'value': 5, 'format': MONEY_FORMAT}
    assert card('Записи', 1)['format'] == NUMBER_FORMAT
    columns = [('a', 'A', 'text'), ('b', 'B', 'money')]
    assert without_empty_columns(columns, [{'b': 0}], {'b'}) == [columns[0]]
    assert without_empty_columns(columns, [{'b': 3}], {'b'}) == columns
