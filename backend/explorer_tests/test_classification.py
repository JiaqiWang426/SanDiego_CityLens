"""Classification, comparison and cache migration regression tests."""
import csv
import hashlib
import json
import os
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
import pytest

from backend import category_analysis as audit
from backend import explorer_api, explorer_data
from backend.category_rules import MAPPING, load_category_rules
from backend.explorer_app import app

PREFIX = '/api/v1/explorer'
COMPARISONS = ['/comparisons/monthly', '/comparisons/community']
DERIVED = ['main_category', 'sub_category', 'classification_method']
EXPECTED_COUNTS = {
    'Sanitation & Waste': 9317, 'Traffic & Mobility': 2917, 'Parking & Vehicles': 7657,
    'Community & Property Services': 3603, 'Parks & Vegetation': 2632,
    'Roads & Sidewalks': 2293, 'Water & Drainage': 860, 'Graffiti': 915, 'Needs Review': 1473,
}


def write_source(path, rows):
    fields = sorted(explorer_data.REQUIRED)
    base = dict.fromkeys(fields, '')
    base.update(date_requested='2026-01-01 12:00:00', council_district='1', lat='32.8', lng='-117.2')
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({**base, **row} for row in rows)


@pytest.fixture(scope='module')
def client():
    with TestClient(app) as instance:
        yield instance


def test_metadata_hierarchy_and_real_record_assignments_match_independent_audit(client):
    meta = client.get(PREFIX + '/metadata').json()
    assert meta['records'] == 31667
    rules = load_category_rules()
    assert meta['category_hierarchy'] == rules.hierarchy()
    assert len(meta['category_hierarchy']) == 9
    assert sum(len(row['subcategories']) for row in meta['category_hierarchy']) == 66
    assert meta['mapping_sha256'] == hashlib.sha256(MAPPING.read_bytes()).hexdigest()
    assert {row['label']: row['count'] for row in meta['facets']['main_category']} == EXPECTED_COUNTS
    assert sum(row['count'] for row in meta['facets']['sub_category']) == 31667

    raw = pd.read_csv(explorer_data.SOURCE, dtype=str, keep_default_na=False)
    scoped, _ = audit.scope_and_checks(raw)
    scoped['_service'] = audit.normalized(scoped.service_name)
    scoped['_detail'] = audit.normalized(scoped.service_name_detail)
    mapping = pd.read_csv(MAPPING, dtype=str, keep_default_na=False)
    expected = audit.classify(scoped, mapping)
    expected_records = {explorer_data.text(row.service_request_id):
                        (explorer_data.text(row.service_name), row.main_category, row.sub_category, row.classification_method)
                        for row in expected.itertuples()}
    with explorer_data.connect() as db:
        actual = {row['id']: (row['category'], *(row[field] for field in DERIVED))
                  for row in db.execute('SELECT id,category,main_category,sub_category,classification_method FROM requests')}
    assert len(actual) == 31667
    assert actual == expected_records


def test_every_mapping_condition_and_default_agree_with_pandas_audit():
    mapping = pd.read_csv(MAPPING, dtype=str, keep_default_na=False)
    samples = []
    for row in mapping.itertuples():
        details = row.detail_values.split('|') if row.detail_match == 'in' else ['', 'Unseen detail for regression']
        for detail in details:
            samples.append({'service_name': ' ' + row.service_name + ' ',
                            'service_name_detail': '\t' + detail + '  '})
    sample = pd.DataFrame(samples)
    sample['_service'] = audit.normalized(sample.service_name)
    sample['_detail'] = audit.normalized(sample.service_name_detail)
    expected = audit.classify(sample, mapping)
    rules = load_category_rules()
    for row in expected.itertuples():
        assert rules.classify(row.service_name, row.service_name_detail) == tuple(getattr(row, field) for field in DERIVED)
    with pytest.raises(ValueError, match='Unmapped service_name'):
        rules.classify('Unknown future service', '')


@pytest.mark.parametrize('params', [
    {}, {'main_category': 'Graffiti'}, {'sub_category': 'Park graffiti'},
    {'main_category': 'Parking & Vehicles', 'sub_category': 'Parking obstruction'},
    {'category': 'Parks Issue', 'main_category': 'Graffiti'},
    {'start': '2026-03-01', 'end': '2026-03-31', 'main_category': 'Parking & Vehicles'},
    {'start': '2026-10-26', 'end': '2026-10-26'},
    {'community': 'LA JOLLA', 'zipcode': '92037', 'status': 'Closed', 'exclude_linked': True},
    {'community': '__missing__'}, {'category': '__missing__'},
    {'main_category': 'Graffiti', 'sub_category': 'Pothole'},
    {'main_category': "Graffiti' OR 1=1 --"},
])
def test_comparison_totals_equal_filtered_summary_and_request_totals(client, params):
    summary_response = client.get(PREFIX + '/summary', params=params)
    assert summary_response.status_code == 200
    total = summary_response.json()['total']
    assert client.get(PREFIX + '/requests', params={**params, 'limit': 1}).json()['total'] == total
    for endpoint in COMPARISONS:
        response = client.get(PREFIX + endpoint, params=params)
        assert response.status_code == 200
        rows = response.json()['data']
        assert sum(row['count'] for row in rows) == total
        dimension = 'month' if endpoint.endswith('monthly') else 'community'
        assert len({(row[dimension], row['main_category'], row['sub_category']) for row in rows}) == len(rows)
        assert all(isinstance(row['count'], int) and row['count'] > 0 for row in rows)
        for row in rows:
            for field in ('main_category', 'sub_category'):
                if field in params:
                    assert row[field] == params[field]
            if dimension == 'month':
                assert len(row['month']) == 7
                assert params.get('start', '2026-01-01')[:7] <= row['month'] <= params.get('end', '2026-12-31')[:7]


def test_new_filters_and_derived_fields_preserve_legacy_endpoints(client):
    params = {'main_category': 'Graffiti', 'sub_category': 'Park graffiti'}
    summary = client.get(PREFIX + '/summary', params=params).json()
    page = client.get(PREFIX + '/requests', params={**params, 'limit': 3}).json()
    assert summary['total'] == page['total'] == 152
    assert summary['categories'] == [{'label': 'Parks Issue', 'count': 152}]
    for row in page['items']:
        assert row['category'] == 'Parks Issue'
        assert tuple(row[field] for field in DERIVED) == ('Graffiti', 'Park graffiti', 'detail_match')
        detail = client.get(PREFIX + '/requests/' + row['id']).json()
        assert detail == row
        assert 'public_description' not in detail
    points = client.get(PREFIX + '/map', params=params).json()
    assert points['mode'] == 'points' and points['total'] == 152
    assert all(row['main_category'] == 'Graffiti' and row['sub_category'] == 'Park graffiti'
               and row['category'] == 'Parks Issue' for row in points['items'])
    assert client.get(PREFIX + '/summary', params={'main_category': 'Sanitation & Waste',
                                                 'sub_category': 'Park litter and bins'}).json()['total'] == 53
    assert client.get(PREFIX + '/requests/nonexistent-id').status_code == 404


@pytest.mark.parametrize('endpoint', COMPARISONS)
def test_comparison_validation_and_empty_results(client, endpoint):
    assert client.get(PREFIX + endpoint, params={'start': '2025-01-01'}).status_code == 422
    assert client.get(PREFIX + endpoint, params={'start': '2026-03-02', 'end': '2026-03-01'}).status_code == 422
    assert client.get(PREFIX + endpoint, params={'main_category': 'x' * 201}).status_code == 422
    assert client.get(PREFIX + endpoint, params={'main_category': 'Graffiti', 'sub_category': 'Pothole'}).json() == {'data': []}


def test_missing_communities_coordinates_and_inclusive_end_date(tmp_path, monkeypatch):
    source, cache = tmp_path / 'source.csv', tmp_path / 'cache.sqlite3'
    write_source(source, [
        {'service_request_id': 'A', 'service_name': 'Parks Issue', 'service_name_detail': 'Graffiti'},
        {'service_request_id': 'B', 'service_name': 'Parks Issue', 'service_name_detail': 'Trash/Litter',
         'date_requested': '2026-01-31 23:59:59', 'comm_plan_name': 'Known'},
        {'service_request_id': 'C', 'service_name': 'Pothole', 'date_requested': '2026-02-01', 'lat': 'nan'},
        {'service_request_id': 'D', 'date_requested': '2026-02-01'},
        {'service_request_id': 'E', 'service_name': 'Other', 'date_requested': '2026-02-02',
         'comm_plan_name': 'Unknown', 'service_request_parent_id': 'B'},
    ])
    explorer_data.build_cache(source, cache)
    monkeypatch.setattr(explorer_data, 'CACHE', cache)
    synthetic_app = FastAPI()
    synthetic_app.include_router(explorer_api.router)
    with TestClient(synthetic_app) as synthetic:
        rows = synthetic.get(PREFIX + '/comparisons/community').json()['data']
        assert sum(row['count'] for row in rows) == 5
        assert sum(row['count'] for row in rows if row['community'] is None) == 3
        assert sum(row['count'] for row in rows if row['community'] == 'Unknown') == 1
        missing = synthetic.get(PREFIX + '/comparisons/community', params={'community': '__missing__'}).json()['data']
        assert sum(row['count'] for row in missing) == 3
        assert all(row['community'] is None for row in missing)
        monthly = synthetic.get(PREFIX + '/comparisons/monthly').json()['data']
        assert sum(row['count'] for row in monthly if row['month'] == '2026-01') == 2
        assert sum(row['count'] for row in monthly if row['month'] == '2026-02') == 3
        for endpoint in COMPARISONS:
            assert sum(row['count'] for row in synthetic.get(PREFIX + endpoint, params={
                'start': '2026-01-31', 'end': '2026-01-31', 'main_category': 'Sanitation & Waste'}).json()['data']) == 1
            assert sum(row['count'] for row in synthetic.get(PREFIX + endpoint, params={'exclude_linked': True}).json()['data']) == 4
        assert synthetic.get(PREFIX + '/summary').json()['unlocated'] == 1
        unknown = synthetic.get(PREFIX + '/requests/D').json()
        assert unknown['category'] is None
        assert tuple(unknown[field] for field in DERIVED) == ('Needs Review', 'Missing service name', 'needs_review')


def test_mapping_content_change_invalidates_cache_even_with_same_size_and_mtime(tmp_path):
    source, cache, mapping = tmp_path / 'source.csv', tmp_path / 'cache.sqlite3', tmp_path / 'mapping.csv'
    write_source(source, [{'service_request_id': 'A', 'service_name': 'Pothole'}])
    mapping.write_bytes(MAPPING.read_bytes())
    before = explorer_data.ensure_cache(source, cache, mapping)
    stat = mapping.stat()
    content = mapping.read_text().replace('Pothole,Roads & Sidewalks,Pothole,any',
                                          'Pothole,Roads & Sidewalks,PotholE,any')
    mapping.write_text(content)
    os.utime(mapping, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert mapping.stat().st_size == stat.st_size and mapping.stat().st_mtime_ns == stat.st_mtime_ns
    after = explorer_data.ensure_cache(source, cache, mapping)
    assert before['mapping_sha256'] != after['mapping_sha256']
    with explorer_data.connect(cache) as db:
        assert db.execute('SELECT sub_category FROM requests').fetchone()[0] == 'PotholE'
    assert explorer_data.ensure_cache(source, cache, mapping) == after


def test_version_one_cache_is_rebuilt_with_derived_columns(tmp_path):
    source, cache = tmp_path / 'source.csv', tmp_path / 'cache.sqlite3'
    write_source(source, [{'service_request_id': 'A', 'service_name': 'Pothole'}])
    old_signature = explorer_data.signature(source)
    old_signature['version'] = 1
    with sqlite3.connect(cache) as db:
        db.execute('CREATE TABLE requests (id TEXT PRIMARY KEY, category TEXT)')
        db.execute('CREATE TABLE metadata (value TEXT NOT NULL)')
        db.execute('INSERT INTO metadata VALUES (?)', (json.dumps({'signature': old_signature}),))
    metadata = explorer_data.ensure_cache(source, cache)
    assert metadata['signature']['version'] == explorer_data.VERSION == 2
    with explorer_data.connect(cache) as db:
        row = db.execute('SELECT * FROM requests').fetchone()
        assert row['id'] == 'A' and row['category'] == 'Pothole'
        assert tuple(row[field] for field in DERIVED) == ('Roads & Sidewalks', 'Pothole', 'service_default')


@pytest.mark.parametrize('failure', ['unknown_service', 'malformed_source', 'overlapping_mapping'])
def test_failed_rebuild_preserves_old_cache_and_removes_temporary_files(tmp_path, failure):
    source, cache, mapping = tmp_path / 'source.csv', tmp_path / 'cache.sqlite3', tmp_path / 'mapping.csv'
    write_source(source, [{'service_request_id': 'A', 'service_name': 'Pothole'}])
    mapping.write_bytes(MAPPING.read_bytes())
    explorer_data.build_cache(source, cache, mapping)
    before = cache.read_bytes()
    if failure == 'unknown_service':
        write_source(source, [{'service_request_id': 'A', 'service_name': 'Unknown future service'}])
    elif failure == 'malformed_source':
        with source.open('a') as stream:
            stream.write('ragged,row\n')
    else:
        row = next(line for line in mapping.read_text().splitlines() if line.startswith('Parks Issue,Graffiti,Park graffiti,'))
        with mapping.open('a') as stream:
            stream.write(row + '\n')
    with pytest.raises(ValueError):
        explorer_data.ensure_cache(source, cache, mapping)
    assert cache.read_bytes() == before
    assert not list(tmp_path.glob('explorer-*.sqlite3'))


def test_mapping_change_during_build_cannot_replace_existing_cache(tmp_path, monkeypatch):
    source, cache, mapping = tmp_path / 'source.csv', tmp_path / 'cache.sqlite3', tmp_path / 'mapping.csv'
    write_source(source, [{'service_request_id': 'A', 'service_name': 'Pothole'}])
    mapping.write_bytes(MAPPING.read_bytes())
    explorer_data.build_cache(source, cache, mapping)
    before = cache.read_bytes()
    original_loader = explorer_data.load_category_rules

    def load_then_change(path):
        compiled = original_loader(path)
        with mapping.open('a') as stream:
            stream.write('\n')
        return compiled

    monkeypatch.setattr(explorer_data, 'load_category_rules', load_then_change)
    with pytest.raises(ValueError, match='Source or mapping changed during processing'):
        explorer_data.build_cache(source, cache, mapping)
    assert cache.read_bytes() == before
    assert not list(tmp_path.glob('explorer-*.sqlite3'))
