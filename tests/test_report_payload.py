from report_payload import without_empty_columns


def test_without_empty_columns_drops_only_optional_columns_with_no_value():
    columns = [('a', 'A', 'text'), ('b', 'B', 'money')]
    assert without_empty_columns(columns, [{'b': 0}], {'b'}) == [columns[0]]
    assert without_empty_columns(columns, [{'b': 3}], {'b'}) == columns
