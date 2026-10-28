"""Initialize PostGIS, import a City Get It Done CSV, or count nearby requests."""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


def initialize(conn):
    conn.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))


def load_csv(conn, path):
    # Stage the entire snapshot in one transaction. Last occurrence of a repeated
    # source ID wins; reimporting updates IDs rather than inflating counts.
    conn.execute('CREATE TEMP TABLE incoming (ordinal bigint GENERATED ALWAYS AS IDENTITY, record jsonb) ON COMMIT DROP')
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        required = {'service_request_id', 'date_requested', 'lat', 'lng',
                    'service_request_parent_id', 'service_name', 'status'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f'Missing CSV columns: {required - set(reader.fieldnames or [])}')
        with conn.cursor().copy('COPY incoming (record) FROM STDIN') as copy:
            for line, record in enumerate(reader, 2):
                if not record['service_request_id'] or not record['date_requested']:
                    raise ValueError(f'Missing ID or date at CSV record {line}')
                lat, lng = record['lat'], record['lng']
                if lat and lng:
                    if not (math.isfinite(float(lat)) and math.isfinite(float(lng))
                            and -90 <= float(lat) <= 90 and -180 <= float(lng) <= 180):
                        raise ValueError(f'Invalid coordinates at CSV record {line}')
                copy.write_row((Jsonb(record),))
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    run = conn.execute('''INSERT INTO citylens.ingestions
        (source_file, sha256, source_rows, distinct_requests)
        SELECT %s, %s, count(*), count(DISTINCT record->>'service_request_id')
        FROM incoming RETURNING id, source_rows, distinct_requests''',
        (path.name, digest)).fetchone()
    conn.execute('''INSERT INTO citylens.service_requests
        SELECT DISTINCT ON (record->>'service_request_id')
            record->>'service_request_id',
            nullif(record->>'service_request_parent_id',''),
            (record->>'date_requested')::timestamp AT TIME ZONE 'America/Los_Angeles',
            record->>'service_name', record->>'status',
            CASE WHEN nullif(record->>'lat','') IS NOT NULL
                      AND nullif(record->>'lng','') IS NOT NULL
                 THEN ST_SetSRID(ST_MakePoint((record->>'lng')::float8,
                                             (record->>'lat')::float8),4326)::geography END,
            record, %s
        FROM incoming ORDER BY record->>'service_request_id', ordinal DESC
        ON CONFLICT (service_request_id) DO UPDATE SET
            parent_id=EXCLUDED.parent_id, requested_at=EXCLUDED.requested_at,
            service_name=EXCLUDED.service_name, status=EXCLUDED.status,
            location=EXCLUDED.location, raw_record=EXCLUDED.raw_record,
            ingestion_id=EXCLUDED.ingestion_id''', (run[0],))
    conn.execute('ANALYZE citylens.service_requests')
    print(json.dumps(dict(ingestion_id=run[0], source_rows=run[1], distinct_requests=run[2])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    load = sub.add_parser('load')
    load.add_argument('csv', type=Path)
    count = sub.add_parser('count')
    count.add_argument('--lat', type=float, required=True)
    count.add_argument('--lng', type=float, required=True)
    count.add_argument('--radius', type=float, default=800)
    count.add_argument('--start', required=True)
    count.add_argument('--end', required=True)
    count.add_argument('--exclude-linked-duplicates', action='store_true')
    args = parser.parse_args()
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
        if args.command == 'init':
            initialize(conn)
        elif args.command == 'load':
            load_csv(conn, args.csv)
        else:
            with conn.transaction():
                conn.execute('SET TRANSACTION READ ONLY')
                conn.execute("SET LOCAL statement_timeout = '10s'")
                # Require explicit offsets; date-only values should not silently use UTC.
                from datetime import datetime
                start, end = (datetime.fromisoformat(s) for s in (args.start, args.end))
                if start.utcoffset() is None or end.utcoffset() is None:
                    raise ValueError('Use ISO timestamps with explicit UTC offsets')
                result = conn.execute('SELECT citylens.count_service_requests(%s,%s,%s,%s,%s,%s)',
                    (args.lat, args.lng, args.radius, start, end,
                     args.exclude_linked_duplicates)).fetchone()[0]
                print(json.dumps({'count': result}))


if __name__ == '__main__':
    main()
