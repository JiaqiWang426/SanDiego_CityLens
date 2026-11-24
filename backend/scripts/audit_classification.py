"""Read-only verification of classification and Explorer analytics on live data.

    python -m backend.scripts.audit_classification

Uses AGENT_DATABASE_URL (restricted reader) from .env. Compares:
1. every stored classification with category_rules.CategoryRules (Python port of
   the reference implementation) over ALL service_requests rows;
2. Explorer API responses (via the FastAPI app) with independent, hand-written
   SQL for representative filter combinations, including an analysis area;
3. comparison endpoints and record pages with the equally filtered summary.

Writes backend/audits/2026-11-23-explorer-integration/live-verification.json.
No data is modified; credentials are never printed.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from ..category_rules import load_category_rules
from ..config import load_environment

OUT = Path(__file__).resolve().parents[1] / 'audits' / '2026-11-23-explorer-integration' / 'live-verification.json'
SCOPE = """raw_record->>'council_district'='1' AND requested_at >= %s AND requested_at < %s"""
YEAR = ('2026-01-01T00:00:00-08:00', '2027-01-01T00:00:00-08:00')
AREA = dict(area_west=-117.26, area_south=32.79, area_east=-117.22, area_north=32.82)  # Pacific Beach-ish box

CASES = [
    ('all', {}, '', ()),
    ('main Graffiti', {'main_category': 'Graffiti'}, ' AND main_category=%s', ('Graffiti',)),
    ('main+sub', {'main_category': 'Parking & Vehicles', 'sub_category': 'Parking obstruction'},
     ' AND main_category=%s AND sub_category=%s', ('Parking & Vehicles', 'Parking obstruction')),
    ('sub only', {'sub_category': 'Park graffiti'}, ' AND sub_category=%s', ('Park graffiti',)),
    ('original+main', {'category': 'Parks Issue', 'main_category': 'Graffiti'},
     ' AND service_name=%s AND main_category=%s', ('Parks Issue', 'Graffiti')),
    ('incompatible pair', {'main_category': 'Graffiti', 'sub_category': 'Pothole'},
     ' AND main_category=%s AND sub_category=%s', ('Graffiti', 'Pothole')),
    ('status+community+zip+linked', {'status': 'Closed', 'community': 'LA JOLLA', 'zipcode': '92037', 'exclude_linked': 'true'},
     " AND status=%s AND raw_record->>'comm_plan_name'=%s AND raw_record->>'zipcode'=%s AND parent_id IS NULL",
     ('Closed', 'LA JOLLA', '92037')),
    ('march parking', {'start': '2026-03-01', 'end': '2026-03-31', 'main_category': 'Parking & Vehicles'},
     ' AND requested_at >= %s AND requested_at < %s AND main_category=%s',
     ('2026-03-01T00:00:00-08:00', '2026-04-01T00:00:00-07:00', 'Parking & Vehicles')),
    ('area', AREA, ' AND ST_Intersects(location::geometry, ST_MakeEnvelope(%s,%s,%s,%s,4326))',
     (AREA['area_west'], AREA['area_south'], AREA['area_east'], AREA['area_north'])),
    ('area+main', {**AREA, 'main_category': 'Sanitation & Waste'},
     ' AND ST_Intersects(location::geometry, ST_MakeEnvelope(%s,%s,%s,%s,4326)) AND main_category=%s',
     (AREA['area_west'], AREA['area_south'], AREA['area_east'], AREA['area_north'], 'Sanitation & Waste')),
]


def main():
    load_environment()
    url = os.environ['AGENT_DATABASE_URL']
    report = dict(executed_at=datetime.now(timezone.utc).isoformat(), checks=[], failures=[])

    def check(name, expected, actual):
        ok = expected == actual
        report['checks'].append(dict(name=name, expected=expected, actual=actual, ok=ok))
        if not ok:
            report['failures'].append(name)

    rules = load_category_rules()
    with psycopg.connect(url) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        rows = conn.execute('''SELECT service_request_id, service_name, raw_record->>'service_name_detail',
            main_category, sub_category, classification_method, classification_version FROM citylens.service_requests''').fetchall()
        mismatches = [r[0] for r in rows if (*rules.classify_or_unmapped(r[1], r[2]), rules.sha256) != tuple(r[3:])]
        check('all rows: stored classification == Python rules', 0, len(mismatches))
        report['rows_checked'] = len(rows)
        report['mismatch_examples'] = mismatches[:10]
        independent = {}
        for name, _, extra, args in CASES:
            base = SCOPE
            total = conn.execute(f'SELECT count(*) FROM citylens.service_requests WHERE {base}{extra}', (*YEAR, *args)).fetchone()[0]
            mains = dict(conn.execute(f'''SELECT main_category, count(*) FROM citylens.service_requests
                WHERE {base}{extra} GROUP BY 1''', (*YEAR, *args)).fetchall())
            months = dict(conn.execute(f'''SELECT to_char(requested_at AT TIME ZONE 'America/Los_Angeles','YYYY-MM'), count(*)
                FROM citylens.service_requests WHERE {base}{extra} GROUP BY 1''', (*YEAR, *args)).fetchall())
            independent[name] = (total, mains, months)

    from ..main import app
    from ..explorer_api import get_store
    from ..explorer_store import ExplorerStore
    app.dependency_overrides[get_store] = lambda: ExplorerStore(url)
    try:
        # No lifespan: Explorer routes do not use the async session pool.
        client = TestClient(app)
        for name, params, _, _ in CASES:
            total, mains, months = independent[name]
            summary = client.get('/api/v1/explorer/summary', params=params).json()
            check(f'{name}: summary.total', total, summary['total'])
            check(f'{name}: main categories', mains, {r['label']: r['count'] for r in summary['main_categories']})
            check(f'{name}: sub categories sum', total, sum(r['count'] for r in summary['sub_categories']))
            check(f'{name}: months', months, {r['month']: r['count'] for r in summary['months'] if r['count']})
            monthly = client.get('/api/v1/explorer/comparisons/monthly', params=params).json()['data']
            community = client.get('/api/v1/explorer/comparisons/community', params=params).json()['data']
            check(f'{name}: monthly comparison sum', total, sum(r['count'] for r in monthly))
            check(f'{name}: community comparison sum', total, sum(r['count'] for r in community))
            page = client.get('/api/v1/explorer/requests', params={**params, 'limit': 5}).json()
            check(f'{name}: requests.total', total, page['total'])
        meta = client.get('/api/v1/explorer/metadata').json()
        check('metadata: mapping current', True, meta['classification']['mapping_current'])
        check('metadata: hierarchy size', (9, 66), (len(meta['category_hierarchy']),
              sum(len(r['subcategories']) for r in meta['category_hierarchy'])))
        # Viewport must not change summary totals (no area): the map endpoint is separate.
        view = client.get('/api/v1/explorer/map', params=dict(west=-117.26, south=32.79, east=-117.22, north=32.82, zoom=14)).json()
        check('map viewport total == area summary total', independent['area'][0], view['total'])
        check('summary ignores viewport', independent['all'][0], client.get('/api/v1/explorer/summary').json()['total'])
    finally:
        app.dependency_overrides.clear()
    report['passed'] = not report['failures']
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    print(json.dumps(dict(passed=report['passed'], checks=len(report['checks']), failures=report['failures'],
                          rows_checked=report['rows_checked']), indent=2))


if __name__ == '__main__':
    main()
