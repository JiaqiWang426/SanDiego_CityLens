"""Read-only projections over existing CityLens tables; no DDL or ingestion."""
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import psycopg
from psycopg.rows import dict_row
from .agent.tools import ensure_reader
from .explorer_models import Bounds, ExplorerFilters

LA = ZoneInfo('America/Los_Angeles')
FACETS = ('category', 'status', 'community', 'zipcode')
HISTORICAL_CSV_RECORDS = 31667


class UnsupportedMapFilter(ValueError):
    pass


def clean(expression):
    return "NULLIF(regexp_replace(btrim(" + expression + "), '\\s+', ' ', 'g'), '')"


def projection(dataset):
    if dataset == 'service_requests':
        return 'citylens.service_requests', 'requested_at', {
            'id': 'service_request_id', 'requested': "requested_at AT TIME ZONE 'America/Los_Angeles'",
            'category': clean('service_name'), 'status': clean('status'),
            'community': clean("raw_record->>'comm_plan_name'"), 'zipcode': clean("raw_record->>'zipcode'"),
            'address': clean("raw_record->>'street_address'"), 'closed': clean("raw_record->>'date_closed'"),
            'parent_id': clean('parent_id')}
    return 'citylens.traffic_collisions', 'occurred_at', {
        'id': 'report_id', 'requested': "occurred_at AT TIME ZONE 'America/Los_Angeles'",
        'category': "'Traffic collision'::text", 'status': 'geocode_status',
        'community': 'NULL::text', 'zipcode': 'NULL::text', 'address': 'normalized_address',
        'closed': 'NULL::text', 'parent_id': 'NULL::text'}


def selection(scope, bounds=None):
    """User values are bound; expressions and identifiers come from allowlists."""
    scope = ExplorerFilters.model_validate(scope.model_dump())
    table, timestamp, fields = projection(scope.dataset)
    where = [f'{timestamp} >= %s', f'{timestamp} < %s']
    args = [datetime.combine(scope.start, time.min, LA),
            datetime.combine(scope.end + timedelta(days=1), time.min, LA)]
    if scope.dataset == 'service_requests':
        where.append("raw_record->>'council_district' = '1'")
    for field in FACETS:
        value = getattr(scope, field)
        if value == '__missing__':
            where.append(fields[field] + ' IS NULL')
        elif value is not None:
            where.append(fields[field] + ' = %s')
            args.append(value)
    if scope.exclude_linked:
        where.append(fields['parent_id'] + ' IS NULL')
    if bounds is not None:
        bounds = Bounds.model_validate(bounds.model_dump())
        envelope = 'ST_MakeEnvelope(%s,%s,%s,%s,4326)'
        # GiST on geography filters candidates; geometry intersects includes
        # the exact rectangular boundary, rather than a geodesic polygon.
        values = [bounds.west, bounds.south, bounds.east, bounds.north]
        if bounds.east - bounds.west < 180 and bounds.south > -90 and bounds.north < 90:
            where.append(f'location && {envelope}::geography')
            args += values
        where.append(f'ST_Intersects(location::geometry,{envelope})')
        args += values
    columns = ','.join(f'{expr} AS {name}' for name, expr in fields.items())
    columns += ',ST_Y(location::geometry) AS lat,ST_X(location::geometry) AS lng'
    return f'SELECT {columns} FROM {table} WHERE ' + ' AND '.join(where), args


class ExplorerStore:
    def __init__(self, database_url):
        self.database_url = database_url

    @contextmanager
    def session(self):
        if not self.database_url:
            raise RuntimeError('database_not_configured')
        with psycopg.connect(self.database_url, connect_timeout=3) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            conn.execute("SET LOCAL statement_timeout='10s'")
            conn.execute("SET LOCAL lock_timeout='2s'")
            ensure_reader(conn)
            with conn.cursor(row_factory=dict_row) as cursor:
                yield cursor

    @staticmethod
    def rows(cursor, statement, args=()):
        cursor.execute(statement, args)
        return [dict(row) for row in cursor.fetchall()]

    def metadata(self, dataset='service_requests'):
        base, args = selection(ExplorerFilters(dataset=dataset))
        table, timestamp, _ = projection(dataset)
        with self.session() as cursor:
            result = self.rows(cursor, f'''WITH records AS ({base})
                SELECT count(*) AS records,min(requested) AS first_date,max(requested) AS last_date,
                count(*) FILTER (WHERE lat IS NULL) AS unlocated,
                count(*) FILTER (WHERE parent_id IS NOT NULL) AS linked_requests FROM records''', args)[0]
            result['facets'] = {field: self.rows(cursor, f'''WITH records AS ({base})
                SELECT {field} AS label,count(*) AS count FROM records
                GROUP BY {field} ORDER BY count DESC,{field} NULLS LAST''', args) for field in FACETS}
            result['dataset_coverage'] = self.rows(cursor, f'''SELECT count(*) AS records,
                count(location) AS located,min({timestamp}) AS first_date,max({timestamp}) AS last_date FROM {table}''')[0]
            imports = 'citylens.ingestions' if dataset == 'service_requests' else 'citylens.collision_imports'
            result['imports'] = self.rows(cursor, f'''SELECT id,source_file,sha256,imported_at
                FROM {imports} ORDER BY id DESC LIMIT 100''')
            result['imports_truncated'] = self.rows(cursor, f'SELECT count(*) > 100 AS truncated FROM {imports}')[0]['truncated']
        result.update(dataset=dataset, source_name=table, storage='postgresql', year=2026,
                      district=1 if dataset == 'service_requests' else None,
                      snapshot_policy='upsert_current_records_not_historical_snapshot',
                      source_freshness_verified=False,
                      historical_csv_records=HISTORICAL_CSV_RECORDS if dataset == 'service_requests' else None,
                      historical_count_difference=result['records'] - HISTORICAL_CSV_RECORDS if dataset == 'service_requests' else None)
        result['limitations'] = [
            'Counts describe current imported records; source completeness and freshness are unknown.',
            'Closed is a source status and does not establish that an issue was repaired.',
            'Planning areas and ZIP values are source fields, not verified geographic boundaries.',
        ] if dataset == 'service_requests' else [
            'Collision locations are approximate stored geocoding results; unlocated reports cannot appear on the map.',
            'Collision scope is the imported 2026 dataset, not a verified District 1 boundary.',
            'Collision status means geocoding status, not whether a case is open or closed.',
        ]
        return result

    def summary(self, scope, bounds=None, *, validate_labels=False):
        base, args = selection(scope, bounds)
        all_base, all_args = selection(ExplorerFilters(dataset=scope.dataset))
        with self.session() as cursor:
            if validate_labels:
                table, _, fields = projection(scope.dataset)
                canonical = {}
                for field in FACETS:
                    value = getattr(scope, field)
                    if value is None or value == '__missing__':
                        continue
                    labels = self.rows(cursor, f'''SELECT DISTINCT {fields[field]} AS label FROM {table}
                        WHERE lower({fields[field]})=lower(%s)''', [value])
                    if len(labels) != 1:
                        raise UnsupportedMapFilter('Unknown or ambiguous source filter label: ' + field)
                    canonical[field] = labels[0]['label']
                scope = ExplorerFilters.model_validate({**scope.model_dump(), **canonical})
                base, args = selection(scope, bounds)
            totals = self.rows(cursor, f'''WITH records AS ({base}) SELECT count(*) AS total,
                count(*) FILTER (WHERE status='Closed') AS closed,
                count(*) FILTER (WHERE lat IS NULL) AS unlocated,
                count(*) FILTER (WHERE parent_id IS NOT NULL) AS linked,
                count(DISTINCT community) AS communities FROM records''', args)[0]
            months = self.rows(cursor, f'''WITH records AS ({base}) SELECT to_char(requested,'MM') AS month,
                count(*) AS count FROM records GROUP BY month ORDER BY month''', args)
            source_end = self.rows(cursor, f'WITH records AS ({all_base}) SELECT max(requested) AS last_date FROM records', all_args)[0]['last_date']
            monthly = {r['month']: r['count'] for r in months}
            totals['months'] = [{'month': f'2026-{i:02}',
                'count': monthly.get(f'{i:02}', 0) if source_end and i <= source_end.month else None} for i in range(1, 13)]
            totals['categories'] = self.rows(cursor, f'''WITH records AS ({base}) SELECT category AS label,
                count(*) AS count FROM records GROUP BY category ORDER BY count DESC,category NULLS LAST''', args)
            totals['statuses'] = self.rows(cursor, f'''WITH records AS ({base}) SELECT status AS label,
                count(*) AS count FROM records GROUP BY status ORDER BY count DESC,status NULLS LAST''', args)
            imports = 'citylens.ingestions' if scope.dataset == 'service_requests' else 'citylens.collision_imports'
            totals['provenance'] = self.rows(cursor, f'''SELECT transaction_timestamp() AS executed_at,
                txid_current_snapshot()::text AS database_snapshot,
                max(imported_at) AS last_successful_local_ingestion_at FROM {imports}''')[0]
            totals['provenance'].update(source_table='citylens.' + scope.dataset,
                source_freshness_verified=False, snapshot_policy='upsert_current_records_not_historical_snapshot')
            totals['applied_filters'] = scope.model_dump(mode='json')
            totals['verification_query'] = dict(parameterized_sql=f'SELECT count(*) AS total FROM ({base}) AS scoped_records',
                                                parameters=args)
        return totals

    def map(self, scope, bounds, zoom):
        base, args = selection(scope, bounds)
        with self.session() as cursor:
            count = self.rows(cursor, f'WITH records AS ({base}) SELECT count(*) AS total FROM records', args)[0]['total']
            if count <= 700:
                items = self.rows(cursor, f'''WITH records AS ({base}) SELECT id,lat,lng,category,status
                    FROM records ORDER BY requested DESC,id LIMIT 700''', args)
                return dict(mode='points', total=count, items=items)
            cell = max(360 / (2 ** zoom) * .12, (bounds.east-bounds.west)/30, (bounds.north-bounds.south)/30)
            items = self.rows(cursor, f'''WITH records AS ({base})
                SELECT count(*) AS count,avg(lat) AS lat,avg(lng) AS lng FROM records
                GROUP BY floor((lng+180)/%s),floor((lat+90)/%s)''', [*args, cell, cell])
            return dict(mode='clusters', total=count, items=items)

    def requests(self, scope, limit, offset):
        base, args = selection(scope)
        with self.session() as cursor:
            total = self.rows(cursor, f'WITH records AS ({base}) SELECT count(*) AS total FROM records', args)[0]['total']
            items = self.rows(cursor, f'''WITH records AS ({base}) SELECT * FROM records
                ORDER BY requested DESC,id LIMIT %s OFFSET %s''', [*args, limit, offset])
            return dict(total=total, items=items)

    def detail(self, request_id, dataset='service_requests'):
        base, args = selection(ExplorerFilters(dataset=dataset))
        with self.session() as cursor:
            rows = self.rows(cursor, f'WITH records AS ({base}) SELECT * FROM records WHERE id=%s', [*args, request_id])
            return rows[0] if rows else None
