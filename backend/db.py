"""Initialize PostGIS, import a City Get It Done CSV, or count nearby requests.

Data flow: CSV -> temporary JSON staging table -> normalized request table.
DATABASE_URL selects the database; sql/get_it_done.sql defines storage, while
sql/tools/*.sql defines callable query functions.
This module performs basic structural validation, not a source-quality audit.
It does not establish citywide coverage, real-world issue counts, or repairs.
"""
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
    """Create database structure, then query functions, on an open connection.

    The caller owns the transaction. IF NOT EXISTS makes object creation
    repeatable; it does not migrate an existing table to a changed structure.
    sql/traffic_collisions.sql adds collision tables after sql/get_it_done.sql.
    Tool files then execute in alphabetical filename order.
    CREATE OR REPLACE updates existing query function definitions. Tools should
    depend on the schema rather than each other; if dependencies are introduced,
    filenames must explicitly order their creation (for example, 010_ then 020_).
    Removing a tool file does not drop an already installed database function.
    """
    sql_dir = Path(__file__).resolve().parent / 'sql'
    # The caller commits all definitions together or rolls back on failure.
    # Resolve from the script location so init works from any working directory.
    conn.execute((sql_dir / 'get_it_done.sql').read_text(encoding='utf-8'))
    conn.execute((sql_dir / 'traffic_collisions.sql').read_text(encoding='utf-8'))
    for tool_file in sorted((sql_dir / 'tools').glob('*.sql')):
        conn.execute(tool_file.read_text(encoding='utf-8'))


def load_csv(conn, path):
    """Validate and upsert a CSV snapshot, printing ingestion metadata as JSON.

    Args:
        conn: Open psycopg connection with permission to write CityLens tables.
        path: pathlib.Path pointing to the source CSV.

    Returns:
        None. The command prints ingestion ID, row count, and distinct ID count.

    Errors propagate to the caller. In main(), the connection context rolls
    back all writes on failure and commits on success. Do not catch an import
    error and commit the partially completed transaction in another caller.

    Conflicting IDs keep their final CSV occurrence; this is a deterministic
    rule, not evidence that this occurrence has the newest source update.
    Records absent from this import are retained from earlier imports.
    """
    # Stage the entire snapshot in one transaction. Last occurrence of a repeated
    # source ID wins; reimporting updates IDs rather than inflating counts.
    conn.execute('CREATE TEMP TABLE incoming (ordinal bigint GENERATED ALWAYS AS IDENTITY, record jsonb) ON COMMIT DROP')
    # ordinal records CSV order. The staging table disappears at commit and
    # JSON preserves source fields that are not promoted to queryable columns.
    # utf-8-sig accepts an optional UTF-8 BOM; newline='' lets csv parse quoted
    # descriptions containing newlines correctly.
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        required = {'service_request_id', 'date_requested', 'lat', 'lng',
                    'service_request_parent_id', 'service_name', 'status'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f'Missing CSV columns: {required - set(reader.fieldnames or [])}')
        # Column existence is separate from value validation. Empty category
        # or status fields are currently allowed; no category vocabulary audit
        # or whitespace normalization is performed here.
        # COPY streams records without issuing one INSERT round trip per row.
        with conn.cursor().copy('COPY incoming (record) FROM STDIN') as copy:
            for line, record in enumerate(reader, 2):
                if not record['service_request_id'] or not record['date_requested']:
                    raise ValueError(f'Missing ID or date at CSV record {line}')
                # line is a logical CSV record number, not necessarily a file
                # line number when a quoted description spans several lines.
                lat, lng = record['lat'], record['lng']
                # A complete pair must be numeric, finite, and globally valid.
                # If either component is empty, retain the raw record with no
                # spatial point. This does not verify San Diego boundaries or
                # validate a lone nonempty component of an incomplete pair.
                if lat and lng:
                    if not (math.isfinite(float(lat)) and math.isfinite(float(lng))
                            and -90 <= float(lat) <= 90 and -180 <= float(lng) <= 180):
                        raise ValueError(f'Invalid coordinates at CSV record {line}')
                copy.write_row((Jsonb(record),))
    # Hash the file bytes for provenance, not for authentication or freshness.
    # The source file must remain unchanged during parsing and hashing.
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    # Source row count includes repeated IDs; distinct_requests does not.
    # Only successful imports retain this entry after transaction commit.
    run = conn.execute('''INSERT INTO citylens.ingestions
        (source_file, sha256, source_rows, distinct_requests)
        SELECT %s, %s, count(*), count(DISTINCT record->>'service_request_id')
        FROM incoming RETURNING id, source_rows, distinct_requests''',
        (path.name, digest)).fetchone()
    # Normalization performed below:
    # - Empty parent IDs become SQL NULL (no explicit parent relationship).
    # - Source wall times are assumed to be America/Los_Angeles. PostgreSQL
    #   converts them to instants; this assumption needs a source metadata audit.
    # - ST_MakePoint expects longitude first, latitude second. SRID 4326 means
    #   WGS84 coordinates; geography enables ellipsoidal distances in meters.
    # - DISTINCT ON plus descending ordinal selects the last occurrence per ID.
    # - ON CONFLICT replaces the current record; old normalized versions are
    #   not archived. raw_record preserves the current source row only.
    # Date conversion errors abort the transaction. Dates are not checked for
    # implausible future values or missing geographic/temporal source coverage.
    # %s placeholders bind values separately from SQL text.
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
    # Refresh planner statistics after bulk loading so PostgreSQL can choose
    # between the spatial index, time index, and other execution strategies.
    conn.execute('ANALYZE citylens.service_requests')
    print(json.dumps(dict(ingestion_id=run[0], source_rows=run[1], distinct_requests=run[2])))


def main():
    """Dispatch init/load/count; read credentials only from DATABASE_URL."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    load = sub.add_parser('load')
    # Default source is relative to this script, independent of the shell's
    # working directory. An explicit path selects another Get It Done CSV.
    load.add_argument(
        'csv', type=Path, nargs='?',
        default=Path(__file__).resolve().parent / 'Raw Data' / 'Get it done'
        / 'get_it_done_requests_CD1_datasd.csv',
        help='Get It Done CSV (default: backend/Raw Data/Get it done/get_it_done_requests_CD1_datasd.csv)',
    )
    count = sub.add_parser('count')
    count.add_argument('--lat', type=float, required=True)
    count.add_argument('--lng', type=float, required=True)
    count.add_argument('--radius', type=float, default=800)
    count.add_argument('--start', required=True)
    count.add_argument('--end', required=True)
    count.add_argument('--exclude-linked-duplicates', action='store_true')
    args = parser.parse_args()
    if __package__:
        from .config import load_environment
    else:
        from config import load_environment
    load_environment()
    # Connection timeout bounds connection establishment; statement_timeout
    # below bounds SQL execution. They address different sources of delay.
    # The connection context commits on normal exit and rolls back on errors.
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
        if args.command == 'init':
            initialize(conn)
        elif args.command == 'load':
            load_csv(conn, args.csv)
        else:
            # Read-only applies to this transaction, not to account permissions.
            # A deployed agent still needs a separately restricted DB account.
            with conn.transaction():
                conn.execute('SET TRANSACTION READ ONLY')
                conn.execute("SET LOCAL statement_timeout = '10s'")
                # Require explicit offsets; date-only values should not silently use UTC.
                from datetime import datetime
                start, end = (datetime.fromisoformat(s) for s in (args.start, args.end))
                if start.utcoffset() is None or end.utcoffset() is None:
                    raise ValueError('Use ISO timestamps with explicit UTC offsets')
                # fetchone() returns a one-column tuple; [0] extracts the count.
                # The SQL function validates radius/coordinate/time bounds.
                result = conn.execute('SELECT citylens.count_service_requests(%s,%s,%s,%s,%s,%s)',
                    (args.lat, args.lng, args.radius, start, end,
                     args.exclude_linked_duplicates)).fetchone()[0]
                print(json.dumps({'count': result}))


if __name__ == '__main__':
    main()
