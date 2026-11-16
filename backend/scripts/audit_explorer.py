"""Read-only PostgreSQL/CSV parity audit; writes only an ignored local report."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from fastapi.encoders import jsonable_encoder

from ..agent.settings import Settings
from ..agent.tools import database_failure_code
from ..explorer_data import SOURCE, normalize
from ..explorer_models import ExplorerFilters
from ..explorer_store import ExplorerStore, selection


def audit(store, source=SOURCE):
    fields = ['id', 'requested', 'category', 'status', 'zipcode', 'community', 'lat', 'lng', 'address', 'closed', 'parent_id']
    expected = {}
    source_rows = 0
    with Path(source).open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            source_rows += 1
            record = normalize(row)
            if record:
                expected[record[0]] = dict(zip(fields, record))
            else:
                expected.pop(' '.join((row.get('service_request_id') or '').split()), None)
    with Path(source).open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    base, args = selection(ExplorerFilters())
    with store.session() as cursor:
        current = {row['id']: row for row in store.rows(cursor, base, args)}
        districts = store.rows(cursor, """SELECT raw_record->>'council_district' AS district,
            count(*) AS records FROM citylens.service_requests GROUP BY district ORDER BY records DESC""")
        missing = store.rows(cursor, """SELECT count(*) FILTER (WHERE raw_record->>'council_district' IS NULL) AS missing_district,
            count(*) FILTER (WHERE NULLIF(btrim(raw_record->>'zipcode'),'') IS NULL) AS missing_zipcode,
            count(*) FILTER (WHERE NULLIF(btrim(raw_record->>'comm_plan_name'),'') IS NULL) AS missing_planning_area
            FROM citylens.service_requests""")[0]
    differences = dict.fromkeys(fields[1:], 0)
    changed_ids = []
    for key in expected.keys() & current.keys():
        changed = False
        for field in fields[1:]:
            left, right = expected[key][field], current[key][field]
            if field == 'requested':
                right = right.isoformat(sep=' ', timespec='seconds')
            if field in {'lat', 'lng'} and left is not None and right is not None:
                different = abs(left-right) > 1e-7
            else:
                different = left != right
            if different:
                differences[field] += 1
                changed = True
        if changed:
            changed_ids.append(key)
    return dict(status='verified', csv=dict(source_name=Path(source).name, sha256=digest,
        source_rows=source_rows, records=len(expected)), postgres=store.metadata(),
        collisions=store.metadata('traffic_collisions'), district_counts=districts, missing_source_fields=missing,
        comparison=dict(csv_only_count=len(expected.keys()-current.keys()), postgres_only_count=len(current.keys()-expected.keys()),
            changed_record_count=len(changed_ids), differing_fields=differences,
            csv_only_sample=sorted(expected.keys()-current.keys())[:20],
            postgres_only_sample=sorted(current.keys()-expected.keys())[:20], changed_id_sample=sorted(changed_ids)[:20]),
        semantics=['Service requests: current upsert rows, LA-local 2026, raw council_district exactly 1.',
            'Collisions: imported 2026 records; no District 1 assignment is inferred.',
            'A matching count alone does not establish row-level parity. No source data was modified.'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('.local/explorer-postgres-audit.json'))
    args = parser.parse_args()
    try:
        report = audit(ExplorerStore(Settings.from_env().database_url))
    except Exception as exc:
        code = 'database_not_configured' if isinstance(exc, RuntimeError) else database_failure_code(exc, 'connect')
        report = dict(status='not_verified', diagnostic_code=code,
            reason='No existing database comparison completed. No writes or ingestion were attempted.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(jsonable_encoder(report), indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'report': str(args.output)}))
    return 0 if report['status'] == 'verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
