"""Bounded read-only explorer queries. Charts summarize all matching records.

Only the map is limited by viewport. A server grid represents every located
match without shipping tens of thousands of markers to the browser.
"""
import json
import math
from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from . import explorer_data as data

router = APIRouter(prefix='/api/v1/explorer', tags=['Service request explorer'])


def filters(start: date = date(2026, 1, 1), end: date = date(2026, 12, 31),
            category: str | None = Query(None, max_length=200), status: str | None = Query(None, max_length=100),
            community: str | None = Query(None, max_length=200), zipcode: str | None = Query(None, max_length=20),
            exclude_linked: bool = False):
    if start > end or start.year != 2026 or end.year != 2026:
        raise HTTPException(422, 'Choose an increasing date range within 2026.')
    clauses, args = ['requested >= ?', 'requested < ?'], [start.isoformat(), (end + timedelta(days=1)).isoformat()]
    for field, value in [('category', category), ('status', status), ('community', community), ('zipcode', zipcode)]:
        if value == '__missing__':
            clauses.append(field + ' IS NULL')
        elif value is not None:
            clauses.append(field + ' = ?')
            args.append(value)
    if exclude_linked:
        clauses.append('parent_id IS NULL')
    return ' AND '.join(clauses), args


@router.get('/metadata')
def metadata():
    with data.connect() as db:
        result = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
        result.pop('signature', None)
        result['facets'] = {field: [dict(r) for r in db.execute(
            f'SELECT {field} AS label,count(*) AS count FROM requests GROUP BY {field} ORDER BY count DESC')]
            for field in ['category', 'status', 'community', 'zipcode']}
        return result


@router.get('/summary')
def summary(scope=Depends(filters)):
    where, args = scope
    with data.connect() as db:
        source_end = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])['last_date'][:7]
        totals = dict(db.execute(f'''SELECT count(*) AS total, sum(status='Closed') AS closed,
            sum(lat IS NULL) AS unlocated, sum(parent_id IS NOT NULL) AS linked,
            count(DISTINCT community) AS communities FROM requests WHERE {where}''', args).fetchone())
        totals = {k: v or 0 for k, v in totals.items()}
        monthly = {r['month']: r['count'] for r in db.execute(f"SELECT substr(requested,6,2) AS month,count(*) AS count FROM requests WHERE {where} GROUP BY month", args)}
        totals['months'] = [{'month': f'2026-{i:02}',
                             'count': monthly.get(f'{i:02}', 0) if f'2026-{i:02}' <= source_end else None}
                            for i in range(1, 13)]
        totals['categories'] = [dict(r) for r in db.execute(f'SELECT category AS label,count(*) AS count FROM requests WHERE {where} GROUP BY category ORDER BY count DESC,category', args)]
        return totals


@router.get('/map')
def map_points(west: float = Query(-180, ge=-180, le=180), south: float = Query(-90, ge=-90, le=90),
               east: float = Query(180, ge=-180, le=180), north: float = Query(90, ge=-90, le=90),
               zoom: int = Query(11, ge=0, le=19), scope=Depends(filters)):
    if not all(math.isfinite(x) for x in [west, south, east, north]) or west >= east or south >= north:
        raise HTTPException(422, 'Invalid map bounds.')
    where, args = scope
    where += ' AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?'
    args = [*args, south, north, west, east]
    with data.connect() as db:
        count = db.execute(f'SELECT count(*) FROM requests WHERE {where}', args).fetchone()[0]
        if count <= 700:
            return {'mode': 'points', 'total': count, 'items': [dict(r) for r in db.execute(
                f'SELECT id,lat,lng,category,status FROM requests WHERE {where} ORDER BY requested DESC,id LIMIT 700', args)]}
        # Adapt cell width to viewport too: the response remains bounded even
        # for a wide viewport at an unusual zoom. Each request counts once.
        cell = max(360 / (2 ** zoom) * .12, (east-west)/30, (north-south)/30)
        rows = db.execute(f'''SELECT count(*) AS count,avg(lat) AS lat,avg(lng) AS lng
            FROM requests WHERE {where} GROUP BY CAST((lng+180)/? AS INTEGER),CAST((lat+90)/? AS INTEGER)''', [*args, cell, cell])
        return {'mode': 'clusters', 'total': count, 'items': [dict(r) for r in rows]}


@router.get('/requests')
def requests(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), scope=Depends(filters)):
    where, args = scope
    with data.connect() as db:
        return {'total': db.execute(f'SELECT count(*) FROM requests WHERE {where}', args).fetchone()[0],
                'items': [dict(r) for r in db.execute(f'SELECT * FROM requests WHERE {where} ORDER BY requested DESC,id LIMIT ? OFFSET ?', [*args, limit, offset])]}


@router.get('/requests/{request_id}')
def detail(request_id: str):
    with data.connect() as db:
        row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Request not found in District 1, 2026.')
        return dict(row)
