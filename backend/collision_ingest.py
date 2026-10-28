"""Batch-import traffic collisions into the existing CityLens database.

No geocoding/network requests occur during import. Missing/invalid counts stay
NULL, with explicit issues; every source row is archived before ID upsert.
The caller owns the transaction. main() commits the whole import or rolls back.
"""
import argparse
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re

import psycopg
from psycopg.types.json import Jsonb

from collision_addresses import clean_text, normalize_collision_address

FIELDS = ('REPORT_ID DATE_TIME POLICE_BEAT ADDRESS_NO_PRIMARY ADDRESS_PD_PRIMARY '
          'ADDRESS_ROAD_PRIMARY ADDRESS_SFX_PRIMARY ADDRESS_PD_INTERSECTING '
          'ADDRESS_NAME_INTERSECTING ADDRESS_SFX_INTERSECTING VIOLATION_SECTION '
          'VIOLATION_TYPE CHARGE_DESC INJURED KILLED HIT_RUN_LVL').split()
DEFAULT_CSV = Path(__file__).resolve().parent / 'Raw Data' / 'Traffic collision' / 'pd_collisions_datasd.csv'


def parse_collision(record):
    """Return typed fields and issues; missing report IDs cannot enter main table.

    IDs remain strings. Invalid dates/counts become NULL, not fabricated values.
    Date parsing is strict ISO local wall time; SQL applies Los Angeles timezone.
    """
    issues = []
    report_id = record.get('REPORT_ID', '').strip()
    if not report_id:
        issues.append('REPORT_ID: missing; excluded from normalized table')
    date = None
    text = record.get('DATE_TIME', '').strip()
    if text:
        try:
            date = datetime.fromisoformat(text)
            if date.tzinfo is not None or not 1900 <= date.year <= 9998:
                raise ValueError('Expected local wall time')
        except ValueError:
            date = None
            issues.append('DATE_TIME: invalid; stored NULL')
    else:
        issues.append('DATE_TIME: missing; stored NULL')
    def count(field):
        value = record.get(field, '').strip()
        if not value:
            issues.append(f'{field}: missing; stored NULL')
            return None
        if not re.fullmatch(r'\d+', value) or int(value) > 2147483647:
            issues.append(f'{field}: invalid nonnegative integer; stored NULL')
            return None
        return int(value)
    injured, killed = count('INJURED'), count('KILLED')
    level = clean_text(record.get('HIT_RUN_LVL')) or None
    hit = False if level is None else True if level in {'MISDEMEANOR', 'FELONY'} else None
    if level is not None and hit is None:
        issues.append('HIT_RUN_LVL: unknown label; hit_and_run stored NULL')
    address = normalize_collision_address(record)
    if not address.primary_street:
        issues.append('ADDRESS_ROAD_PRIMARY: missing; location unresolved')
    return (report_id, date, clean_text(record.get('POLICE_BEAT')) or None,
            address.primary_street, address.intersecting_street,
            address.normalized_address, address.address_key,
            clean_text(record.get('VIOLATION_SECTION')) or None,
            clean_text(record.get('VIOLATION_TYPE')) or None,
            record.get('CHARGE_DESC', '').strip() or None,
            injured, killed, level, hit, issues)


UPSERT = '''
INSERT INTO citylens.traffic_collisions AS old
    (report_id,occurred_at,police_beat,primary_street,intersecting_street,
     normalized_address,address_key,violation_section,violation_type,charge_desc,
     injured,killed,hit_run_level,hit_and_run,raw_record,validation_issues,import_id)
SELECT DISTINCT ON (report_id)
    report_id,local_time AT TIME ZONE 'America/Los_Angeles',police_beat,
    primary_street,intersecting_street,normalized_address,address_key,
    violation_section,violation_type,charge_desc,injured,killed,hit_run_level,
    hit_and_run,raw_record,issues,%s
FROM collision_stage WHERE report_id <> '' ORDER BY report_id,ordinal DESC
ON CONFLICT (report_id) DO UPDATE SET
    occurred_at=EXCLUDED.occurred_at,police_beat=EXCLUDED.police_beat,
    primary_street=EXCLUDED.primary_street,intersecting_street=EXCLUDED.intersecting_street,
    normalized_address=EXCLUDED.normalized_address,address_key=EXCLUDED.address_key,
    violation_section=EXCLUDED.violation_section,violation_type=EXCLUDED.violation_type,
    charge_desc=EXCLUDED.charge_desc,injured=EXCLUDED.injured,killed=EXCLUDED.killed,
    hit_run_level=EXCLUDED.hit_run_level,hit_and_run=EXCLUDED.hit_and_run,
    raw_record=EXCLUDED.raw_record,validation_issues=EXCLUDED.validation_issues,
    import_id=EXCLUDED.import_id,
    location=CASE WHEN old.address_key=EXCLUDED.address_key THEN old.location END,
    geocode_status=CASE WHEN old.address_key=EXCLUDED.address_key THEN old.geocode_status ELSE 'pending' END,
    geocode_provider=CASE WHEN old.address_key=EXCLUDED.address_key THEN old.geocode_provider END,
    geocode_precision=CASE WHEN old.address_key=EXCLUDED.address_key THEN old.geocode_precision END,
    geocoded_at=CASE WHEN old.address_key=EXCLUDED.address_key THEN old.geocoded_at END,
    geocode_details=CASE WHEN old.address_key=EXCLUDED.address_key THEN old.geocode_details END
'''


def load_collisions(conn, path, batch_size=2000):
    """Stage bounded COPY batches, archive all rows, then upsert distinct IDs.

    Last occurrence wins for a repeated REPORT_ID. Rows absent from a new file
    remain in the database. Malformed CSV structure aborts the entire import;
    invalid field values are retained with issues and nullable typed fields.
    """
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    path = Path(path)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    run = conn.execute('''INSERT INTO citylens.collision_imports(source_file,sha256)
        VALUES (%s,%s) RETURNING id''', (path.name,digest)).fetchone()[0]
    conn.execute('''CREATE TEMP TABLE collision_stage (
        ordinal bigint,report_id text,local_time timestamp,police_beat text,
        primary_street text,intersecting_street text,normalized_address text,address_key text,
        violation_section text,violation_type text,charge_desc text,injured integer,
        killed integer,hit_run_level text,hit_and_run boolean,issues jsonb,raw_record jsonb
    ) ON COMMIT DROP''')
    def flush(batch):
        with conn.cursor().copy('COPY collision_stage FROM STDIN') as copy:
            for row in batch:
                copy.write_row(row)
    batch = []
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames or []
        if not set(FIELDS).issubset(columns) or len(columns) != len(set(columns)):
            raise ValueError('Missing required or duplicate CSV column names')
        for ordinal, record in enumerate(reader, 1):
            if None in record or any(value is None for value in record.values()):
                raise ValueError(f'Malformed CSV record {ordinal}: column count mismatch')
            *values, issues = parse_collision(record)
            batch.append((ordinal,*values,Jsonb(issues),Jsonb(record)))
            if len(batch) >= batch_size:
                flush(batch)
                batch.clear()
        if batch:
            flush(batch)
    # Detect concurrent source-file changes rather than claiming a wrong hash.
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise ValueError('CSV changed during import')
    conn.execute('''INSERT INTO citylens.collision_source_rows
        SELECT %s,ordinal,raw_record,issues FROM collision_stage''', (run,))
    conn.execute(UPSERT, (run,))
    stats = conn.execute('''SELECT count(*),count(*) FILTER (WHERE report_id <> ''),
        count(*) FILTER (WHERE report_id = ''),count(DISTINCT nullif(report_id,'')),
        count(*) FILTER (WHERE issues <> '[]'::jsonb) FROM collision_stage''').fetchone()
    conn.execute('''UPDATE citylens.collision_imports SET source_rows=%s,
        accepted_rows=%s,rejected_rows=%s WHERE id=%s''', (*stats[:3],run))
    conn.execute('DROP TABLE collision_stage')
    conn.execute('ANALYZE citylens.traffic_collisions')
    return dict(import_id=run,source_rows=stats[0],accepted_rows=stats[1],
                rejected_rows=stats[2],distinct_reports=stats[3],rows_with_issues=stats[4])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path, nargs='?', default=DEFAULT_CSV)
    parser.add_argument('--batch-size', type=int, default=2000)
    args = parser.parse_args()
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
        stats = load_collisions(conn,args.csv,args.batch_size)
    print(json.dumps(stats))


if __name__ == '__main__':
    main()
