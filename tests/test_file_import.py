import pytest

from file_import import FileImportSyncClient, ImportPayload
from data_sources import normalize_source_type
from mapping_profiles import (
    AREA_SALON_PROFILE,
    LocaleFormat,
    parse_amount,
    parse_iso_date,
    parse_iso_datetime,
)
from sync_source import SyncSource

IT = LocaleFormat(decimal_comma=True, day_first=True)
US = LocaleFormat(decimal_comma=False, day_first=False)


def test_parse_amount_italian_decimals():
    assert parse_amount('1.234,56', IT) == 1234.56
    assert parse_amount('12,50', IT) == 12.5
    assert parse_amount('€ 8,00', IT) == 8.0
    assert parse_amount('10', IT) == 10.0
    assert parse_amount('', IT) is None
    assert parse_amount(None, IT) is None


def test_parse_amount_us_style():
    assert parse_amount('1,234.56', US) == 1234.56


def test_parse_iso_date_day_first():
    assert parse_iso_date('31/12/2024', IT) == '2024-12-31'
    assert parse_iso_date('1/2/24', IT) == '2024-02-01'
    assert parse_iso_date('2024-12-31', IT) == '2024-12-31'
    assert parse_iso_date('31/12/2024 14:30', IT) == '2024-12-31'
    assert parse_iso_date('not-a-date', IT) is None
    assert parse_iso_date('', IT) is None


def test_parse_iso_datetime_preserves_time():
    assert parse_iso_datetime('31/12/2024 14:30', IT) == '2024-12-31T14:30:00'
    assert parse_iso_datetime('2024-12-31T09:15:30', IT) == '2024-12-31T09:15:30'
    assert parse_iso_datetime('not-a-date', IT) is None


def test_client_mapping_uses_source_id():
    rows = [{'ID': '10', 'Nome': 'Mario Rossi', 'Telefono': '333', 'Email': 'm@r.it',
             'Data di nascita': '05/06/1990'}]
    client = FileImportSyncClient(AREA_SALON_PROFILE, ImportPayload(clients=rows))
    result = client.get_clients('1')
    assert len(result) == 1
    assert result[0]['id'] != 10
    assert 1 <= result[0]['id'] <= 2_147_483_647
    assert result[0]['name'] == 'Mario Rossi'
    assert result[0]['phone'] == '333'
    assert result[0]['email'] == 'm@r.it'
    assert result[0]['birth_date'] == '1990-06-05'


def test_client_without_id_gets_stable_synthetic_id():
    rows = [{'ID': '', 'Nome': 'Ada', 'Telefono': '111', 'Email': 'a@a.it', 'Data di nascita': ''}]
    first = FileImportSyncClient(AREA_SALON_PROFILE, ImportPayload(clients=rows)).get_clients('1')
    second = FileImportSyncClient(AREA_SALON_PROFILE, ImportPayload(clients=rows)).get_clients('1')
    assert first[0]['id'] == second[0]['id']  # idempotent across re-imports
    assert 1 <= first[0]['id'] <= 2_147_483_647


def test_file_import_ids_are_namespaced_by_company():
    rows = [{'ID': '10', 'Nome': 'Ada', 'Telefono': '111', 'Email': 'a@a.it', 'Data di nascita': ''}]
    first = FileImportSyncClient(AREA_SALON_PROFILE, ImportPayload(clients=rows)).get_clients('1')
    second = FileImportSyncClient(AREA_SALON_PROFILE, ImportPayload(clients=rows)).get_clients('2')
    assert first[0]['id'] != second[0]['id']


def test_records_group_service_lines_into_appointments():
    rows = [
        {'ID Appuntamento': '500', 'ID Cliente': '10', 'ID Operatore': '3',
         'Data': '31/12/2024', 'Data e ora': '31/12/2024 14:30',
         'ID Servizio': '7', 'Servizio': 'Taglio', 'Prezzo': '25,00', 'Quantità': '1'},
        {'ID Appuntamento': '500', 'ID Cliente': '10', 'ID Operatore': '3',
         'Data': '31/12/2024', 'Data e ora': '31/12/2024 14:30',
         'ID Servizio': '9', 'Servizio': 'Piega', 'Prezzo': '15,50', 'Quantità': '1'},
    ]
    client = FileImportSyncClient(AREA_SALON_PROFILE, ImportPayload(records=rows))
    records = client.get_records('1')

    assert len(records) == 1
    rec = records[0]
    expected_client_id = FileImportSyncClient(
        AREA_SALON_PROFILE,
        ImportPayload(clients=[{'ID': '10', 'Nome': 'Mario', 'Telefono': '', 'Email': '', 'Data di nascita': ''}]),
    ).get_clients('1')[0]['id']
    assert rec['id'] != 500
    assert rec['date'] == '2024-12-31'
    assert rec['datetime'] == '2024-12-31T14:30:00'
    assert rec['client'] == {'id': expected_client_id}
    assert rec['staff_id'] != 3
    assert {(s['title'], s['cost']) for s in rec['services']} == {('Taglio', 25.0), ('Piega', 15.5)}


def test_records_respect_date_window():
    rows = [
        {'ID Appuntamento': '1', 'Data': '01/01/2024', 'ID Servizio': '1',
         'Servizio': 'A', 'Prezzo': '10,00', 'Quantità': '1'},
        {'ID Appuntamento': '2', 'Data': '15/06/2024', 'ID Servizio': '1',
         'Servizio': 'B', 'Prezzo': '10,00', 'Quantità': '1'},
    ]
    client = FileImportSyncClient(AREA_SALON_PROFILE, ImportPayload(records=rows))
    windowed = client.get_records('1', start_date='2024-06-01', end_date='2024-06-30')
    assert len(windowed) == 1
    assert windowed[0]['date'] == '2024-06-15'


def test_file_import_client_conforms_to_sync_source():
    client = FileImportSyncClient(AREA_SALON_PROFILE, ImportPayload())
    assert isinstance(client, SyncSource)


def test_file_import_is_not_public_credential_source_until_upload_flow_exists():
    with pytest.raises(Exception) as exc:
        normalize_source_type('file_import')
    assert getattr(exc.value, 'status_code', None) == 400
