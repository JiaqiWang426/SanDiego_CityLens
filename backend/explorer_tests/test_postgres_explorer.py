"""Opt-in isolated PostGIS tests, never a workstation database."""
import json
import psycopg
import pytest
from fastapi.testclient import TestClient
from backend.agent_tests.test_integration import isolated_db  # noqa: F401
from backend.main import app
from backend.explorer_api import get_store
from backend.explorer_models import Bounds, ExplorerFilters
from backend.explorer_store import ExplorerStore


@pytest.fixture(scope='module')
def prepared_db(isolated_db):
    admin_url, reader = isolated_db
    with psycopg.connect(admin_url) as conn:
        ingestion=conn.execute("""INSERT INTO citylens.ingestions(source_file,sha256,source_rows,distinct_requests)
            VALUES ('explorer_synthetic.csv','synthetic',707,707) RETURNING id""").fetchone()[0]
        rows=[(f'map_{i}',32.85,-117.25,'Pothole','New',None,'1',2026) for i in range(701)]
        rows += [('west',32.8,-117.3,'Trash','Closed',None,'1',2026),
                 ('east',32.9,-117.2,'Trash','Closed',None,'1',2026),
                 ('unlocated',None,None,None,'New',None,'1',2026),
                 ('linked',32.85,-117.25,'Pothole','New','parent','1',2026),
                 ('other_district',32.85,-117.25,'Pothole','New',None,'2',2026),
                 ('other_year',32.85,-117.25,'Pothole','New',None,'1',2025)]
        for id,lat,lng,category,status,parent,district,year in rows:
            raw=json.dumps(dict(council_district=district,zipcode='92037',comm_plan_name='La Jolla',street_address='TEST',date_closed='',public_description='untrusted private description'))
            conn.execute('''INSERT INTO citylens.service_requests(service_request_id,parent_id,requested_at,service_name,status,location,raw_record,ingestion_id)
                VALUES (%s,%s,%s,%s,%s,CASE WHEN %s::float8 IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography END,%s,%s)''',
                (id,parent,f'{year}-01-15T00:00:00-08:00',category,status,lat,lng,lat,raw,ingestion))
    yield admin_url,reader


def test_postgres_totals_filters_map_pagination_details_and_independent_sql(prepared_db):
    _,reader=prepared_db; store=ExplorerStore(reader); scope=ExplorerFilters()
    meta=store.metadata(); summary=store.summary(scope)
    assert meta['records'] == summary['total'] == 705
    assert meta['unlocated'] == 1
    assert sum(r['count'] or 0 for r in summary['months']) == 705
    assert sum(r['count'] for r in summary['categories']) == 705
    bounds=Bounds(west=-117.3,east=-117.2,south=32.8,north=32.9)
    mapped=store.map(scope,bounds,11)
    assert mapped['mode'] == 'clusters' and mapped['total'] == 704
    assert sum(r['count'] for r in mapped['items']) == 704
    assert len(mapped['items']) <= 961
    closed=ExplorerFilters(status='Closed')
    points=store.map(closed,bounds,11)
    assert points['mode'] == 'points' and {r['id'] for r in points['items']} == {'west','east'}
    assert store.summary(ExplorerFilters(category='Pothole',exclude_linked=True))['total'] == 701
    assert store.requests(closed,1,0)['total'] == 2
    assert store.requests(closed,1,0)['items'][0]['id'] != store.requests(closed,1,1)['items'][0]['id']
    assert store.detail('unlocated')['lat'] is None
    assert store.detail('other_district') is None
    assert 'public_description' not in str(store.detail('west'))
    with psycopg.connect(reader) as conn:
        count=conn.execute("""SELECT count(*) FROM citylens.service_requests WHERE requested_at >= '2026-01-01T00:00:00-08:00'
            AND requested_at < '2027-01-01T00:00:00-08:00' AND raw_record->>'council_district'='1' AND service_name='Pothole' AND parent_id IS NULL""").fetchone()[0]
    assert count == store.summary(ExplorerFilters(category='Pothole',exclude_linked=True))['total']
    assert store.summary(ExplorerFilters(category='pothole'),bounds,validate_labels=True)['applied_filters']['category'] == 'Pothole'
    with pytest.raises(ValueError):
        store.summary(ExplorerFilters(status='unresolved'),bounds,validate_labels=True)


def test_postgres_api_contracts_and_collision_scope(prepared_db):
    _,reader=prepared_db; store=ExplorerStore(reader)
    app.dependency_overrides[get_store]=lambda:store
    try:
        with TestClient(app) as client:
            meta=client.get('/api/v1/explorer/metadata').json()
            assert meta['records'] == 705 and meta['storage'] == 'postgresql'
            assert client.get('/api/v1/explorer/requests/west').json()['category'] == 'Trash'
            assert client.get('/api/v1/explorer/requests/other_year').status_code == 404
            collisions=client.get('/api/v1/explorer/metadata',params={'dataset':'traffic_collisions'}).json()
            assert collisions['records'] == 4 and collisions['unlocated'] == 1 and collisions['district'] is None
            assert client.get('/api/v1/explorer/map',params={'dataset':'traffic_collisions'}) .json()['total'] == 3
    finally:
        app.dependency_overrides.clear()


def test_postgres_explorer_rejects_admin_credentials(prepared_db):
    admin,_=prepared_db
    with pytest.raises(PermissionError):
        ExplorerStore(admin).summary(ExplorerFilters())


def test_postgres_classification_filters_comparisons_hierarchy_and_area(prepared_db):
    admin,reader=prepared_db
    from backend import service_classification
    with psycopg.connect(admin) as conn:
        result=service_classification.apply(conn)
        assert service_classification.apply(conn)['changed_rows'] == 0
    assert result['unclassified'] == 0
    store=ExplorerStore(reader)
    meta=store.metadata()
    # 'Trash' is not a source label in the approved mapping: explicit, not guessed.
    assert {r['label']: r['count'] for r in meta['facets']['main_category']} == {'Roads & Sidewalks': 702, 'Needs Review': 3}
    assert meta['classification']['unmapped'] == 2 and meta['classification']['mapping_current'] is True
    review=next(r for r in meta['category_hierarchy'] if r['main_category'] == 'Needs Review')
    assert review['subcategories'][-1] == 'Unmapped service name'
    assert len(meta['category_hierarchy']) == 9
    total=store.summary(ExplorerFilters())
    assert sum(r['count'] for r in total['main_categories']) == sum(r['count'] for r in total['sub_categories']) == 705
    pothole=ExplorerFilters(main_category='Roads & Sidewalks', sub_category='Pothole')
    summary=store.summary(pothole)
    assert summary['total'] == 702 and summary['categories'] == [{'label': 'Pothole', 'count': 702}]
    for dimension in ('monthly', 'community'):
        rows=store.comparison(pothole, dimension)
        assert sum(r['count'] for r in rows) == 702
    assert store.comparison(ExplorerFilters(main_category='Graffiti', sub_category='Pothole'), 'monthly') == []
    assert store.requests(pothole, 1, 0)['items'][0]['classification_method'] == 'service_default'
    # Explicit analysis area: unlocated records cannot match; without it they count.
    area=Bounds(west=-117.31, east=-117.29, south=32.79, north=32.81)
    scoped=store.summary(ExplorerFilters(), area=area)
    assert scoped['total'] == 1 and scoped['unlocated'] == 0 and scoped['analysis_area'] is not None
    assert store.summary(ExplorerFilters())['unlocated'] == 1
    assert store.requests(ExplorerFilters(), 10, 0, area=area)['total'] == 1
    assert sum(r['count'] for r in store.comparison(ExplorerFilters(), 'community', area=area)) == 1
    wide=Bounds(west=-117.3, east=-117.2, south=32.8, north=32.9)
    assert store.map(ExplorerFilters(), wide, 11, area=area)['total'] == 1
    with psycopg.connect(reader) as conn:
        independent=conn.execute("""SELECT count(*) FROM citylens.service_requests WHERE requested_at >= '2026-01-01T00:00:00-08:00'
            AND requested_at < '2027-01-01T00:00:00-08:00' AND raw_record->>'council_district'='1'
            AND main_category='Roads & Sidewalks' AND sub_category='Pothole'""").fetchone()[0]
    assert independent == 702
    # The Agent's label resolution also accepts derived categories.
    resolved=store.summary(ExplorerFilters(main_category='roads & sidewalks'), wide, validate_labels=True)
    assert resolved['applied_filters']['main_category'] == 'Roads & Sidewalks'
    with pytest.raises(ValueError):
        store.summary(ExplorerFilters(sub_category='Not a subcategory'), wide, validate_labels=True)
