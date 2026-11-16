"""Course explorer tests use a disposable cache and read-only real CSV cache."""
import csv
import pytest

from backend import explorer_data


def test_csv_projection_deduplicates_and_respects_final_scope(tmp_path):
    source, cache = tmp_path / 'requests.csv', tmp_path / 'cache.sqlite3'
    fields = sorted(explorer_data.REQUIRED)
    base = dict.fromkeys(fields, '')
    rows = [
        {**base, 'service_request_id': 'A', 'date_requested': '2026-01-02 12:00:00',
         'council_district': '1', 'lat': '32.8', 'lng': '-117.2', 'service_name': 'Pothole'},
        {**base, 'service_request_id': 'B', 'date_requested': '2026-01-03 12:00:00',
         'council_district': '1', 'lat': '', 'lng': '', 'status': 'New'},
        {**base, 'service_request_id': 'A', 'date_requested': '2026-01-04 12:00:00',
         'council_district': '2', 'lat': '32.8', 'lng': '-117.2'},
    ]
    with source.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    meta = explorer_data.build_cache(source, cache)
    assert (meta['records'], meta['unlocated']) == (1, 1)
    assert explorer_data.ensure_cache(source, cache) == meta
    with explorer_data.connect(cache) as db:
        assert [r['id'] for r in db.execute('SELECT id FROM requests')] == ['B']


def test_failed_rebuild_preserves_cache_and_removes_temp_file(tmp_path):
    source, cache = tmp_path / 'requests.csv', tmp_path / 'cache.sqlite3'
    with source.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=sorted(explorer_data.REQUIRED))
        writer.writeheader()
    explorer_data.build_cache(source, cache)
    previous_cache = cache.read_bytes()

    source.write_text('service_request_id\nA\n', encoding='utf-8')
    with pytest.raises(ValueError, match='Missing required CSV columns'):
        explorer_data.build_cache(source, cache)

    assert cache.read_bytes() == previous_cache
    assert not list(tmp_path.glob('explorer-*.sqlite3*'))


def test_legacy_csv_projection_documents_historical_snapshot():
    meta = explorer_data.ensure_cache()
    assert meta['records'] == 31667
    assert meta['source_rows'] == 303226
    with explorer_data.connect() as db:
        assert db.execute('SELECT count(*) FROM requests').fetchone()[0] == meta['records']
        assert db.execute('SELECT count(*) FROM requests WHERE lat IS NULL').fetchone()[0] == meta['unlocated']
