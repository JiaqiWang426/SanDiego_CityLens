"""Course explorer tests use a disposable cache and read-only real CSV cache."""
import csv
from fastapi.testclient import TestClient

from backend import explorer_data
from backend.explorer_app import app


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


def test_real_csv_api_totals_filters_map_and_detail():
    with TestClient(app) as client:
        meta = client.get('/api/v1/explorer/metadata').json()
        summary = client.get('/api/v1/explorer/summary').json()
        assert meta['records'] == summary['total']
        assert sum(row['count'] or 0 for row in summary['months']) == summary['total']
        assert summary['months'][-1]['count'] is None  # Source snapshot ends in October.
        assert sum(row['count'] for row in summary['categories']) == summary['total']
        assert summary['linked'] == meta['linked_requests']

        category = next(x['label'] for x in meta['facets']['category'] if x['label'])
        scoped = client.get('/api/v1/explorer/summary', params={'category': category}).json()
        assert scoped['total'] == next(x['count'] for x in meta['facets']['category'] if x['label'] == category)
        page = client.get('/api/v1/explorer/requests', params={'category': category, 'limit': 5}).json()
        assert page['total'] == scoped['total']
        assert all(x['category'] == category and 'public_description' not in x for x in page['items'])
        item = client.get('/api/v1/explorer/requests/' + page['items'][0]['id']).json()
        assert item['id'] == page['items'][0]['id']

        map_data = client.get('/api/v1/explorer/map').json()
        assert map_data['total'] == meta['records'] - meta['unlocated']
        assert sum(x.get('count', 1) for x in map_data['items']) == map_data['total']
        assert len(map_data['items']) <= 900
        assert client.get('/api/v1/explorer/summary', params={'start': '2025-01-01'}).status_code == 422
        assert client.get('/api/v1/explorer/map', params={'west': 1, 'east': 0}).status_code == 422
