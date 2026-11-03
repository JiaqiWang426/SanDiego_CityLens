"""Read-only post-deployment verification of the live CityLens database.

    .\\.venv\\Scripts\\python.exe -m backend.verification.historical_geocoding_final.deployment_checks

Uses only the restricted Agent reader. Writes deployment-verification.json.
Exits nonzero if any integrity check fails.
"""
import csv
import json
from collections import Counter
from pathlib import Path
import sys

import psycopg
from psycopg.rows import dict_row

from backend.agent.evidence import make_evidence
from backend.agent.models import Plan, QueryRequest
from backend.agent.settings import Settings
from backend.agent.tools import DatabaseTools, MetadataCall
from backend.agent.workflow import validated_calls
from backend.sangis_topology import batch, database, rollout
from backend.sangis_topology.acquire import now, read, save
from backend.verification.intersection_feasibility.study import connection

HERE = Path(__file__).resolve().parent
STAGE_C = HERE.parent / 'intersection_stage_c'
WINDOWS = [('2017-01-01T00:00:00-08:00', '2027-01-01T00:00:00-08:00'), ('2023-01-01T00:00:00-08:00', '2024-01-01T00:00:00-08:00')]
RADII = (400, 800, 1000)


def ids(path):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        return {r['report_id'] for r in csv.DictReader(stream)}


def integrity():
    snapshot = rollout.load_snapshot(HERE / 'before-row-digests.json.gz', read(HERE / 'validation-manifest.json')['files']['before-row-digests.json.gz'])
    allowed = ids(STAGE_C / 'deployment_allowlist.csv') | ids(HERE / 'deployment_allowlist.csv')
    excluded = ids(HERE / 'excluded_candidates.csv') | ids(STAGE_C / 'excluded_candidates.csv')
    with connection() as c:
        audit = batch.audit(c)
        drift = rollout.check(c, snapshot, audit)
        journal = c.execute("""SELECT run_id, outcome, count(*) AS n FROM citylens_ref.geocode_apply_history GROUP BY 1,2 ORDER BY 1,2""").fetchall()
        runs = c.execute("SELECT run_id, approval->>'reviewer' AS reviewer, approval->>'approved_at' AS approved_at, created_at FROM citylens_ref.geocode_apply_runs ORDER BY created_at").fetchall()
        updated = [r['report_id'] for r in c.execute("SELECT report_id FROM citylens_ref.geocode_apply_history WHERE outcome='updated'")]
        located = {r['report_id'] for r in c.execute('SELECT report_id FROM citylens.traffic_collisions WHERE location IS NOT NULL')}
        dupes = c.execute('SELECT count(*) - count(DISTINCT report_id) AS n FROM citylens.traffic_collisions').fetchone()['n']
        geometry = c.execute("""SELECT count(*) FILTER (WHERE NOT ST_IsValid(location::geometry) OR ST_SRID(location::geometry)<>4326
                  OR ST_X(location::geometry) NOT BETWEEN -117.6 AND -116.8 OR ST_Y(location::geometry) NOT BETWEEN 32.5 AND 33.2) AS invalid,
                  count(*) FILTER (WHERE geocode_precision IS NULL OR geocode_provider IS NULL OR geocode_details IS NULL) AS no_provenance,
                  count(*) FILTER (WHERE (location IS NULL) <> (geocode_status<>'matched')) AS status_location_mismatch
                  FROM citylens.traffic_collisions WHERE location IS NOT NULL OR geocode_status='matched'""").fetchone()
        new_provenance = c.execute("""SELECT count(*) AS n FROM citylens.traffic_collisions WHERE report_id=ANY(%s) AND NOT (
                  (geocode_provider='SanGIS local topology' AND geocode_precision='intersection_approximate'
                   AND coalesce(geocode_details->'stage_c'->>'validation_status', geocode_details->'final_geocoding'->>'validation_status')='pass')
                  OR (geocode_provider='SanGIS road centerline' AND geocode_precision='block_approximate'
                   AND geocode_details->'final_geocoding'->>'validation_status'='pass'))""", (updated,)).fetchone()['n']
        prior_kept = c.execute("""SELECT count(*) FILTER (WHERE geocode_details->'prior_geocoding'->'geocode_status' IS NULL) AS missing,
                  count(*) FILTER (WHERE (geocode_details->'prior_geocoding'->>'geocode_status')='ambiguous'
                                   AND geocode_details->'prior_geocoding'->'geocode_details'->>'reason' IS NULL) AS ambiguous_reason_lost
                  FROM citylens.traffic_collisions WHERE report_id=ANY(%s)""", (updated,)).fetchone()
        distribution = c.execute("""SELECT geocode_provider AS provider, geocode_precision AS precision, count(*) AS n
                  FROM citylens.traffic_collisions WHERE location IS NOT NULL GROUP BY 1,2 ORDER BY 1,2""").fetchall()
        guard = database.reference_reader_guard(c)
        writes = c.execute("""SELECT has_table_privilege(current_user,'citylens.traffic_collisions','UPDATE')
                  OR has_table_privilege(current_user,'citylens.traffic_collisions','INSERT')
                  OR has_table_privilege(current_user,'citylens.traffic_collisions','DELETE') AS w""").fetchone()['w']
        constraint = c.execute("SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conname='traffic_collisions_geocode_precision_check'").fetchone()['d']
    original = {i for i in snapshot['rows'] if i in located and i not in set(updated)}
    checks = {
        'records': audit['records'], 'statuses': audit['statuses'], 'located': audit['located'], 'unlocated': audit['records'] - audit['located'],
        'coverage_percent': round(100 * audit['located'] / audit['records'], 4),
        'no_deleted_or_duplicated_records': audit['records'] == snapshot['collision_audit']['records'] and dupes == 0,
        'original_source_fields_unchanged': audit['source_fields'] == snapshot['collision_audit']['source_fields'],
        'all_unjournaled_rows_byte_identical_including_census_matches': True,  # rollout.check raised otherwise
        'original_census_matched_still_located': len(original),
        'journaled_updates': len(updated), 'duplicate_journal_updates': len(updated) - len(set(updated)),
        'journal_reconciles_with_located': audit['located'] - snapshot['collision_audit']['located'] == len(set(updated)),
        'new_rows_outside_approved_allowlists': len(set(updated) - allowed),
        'excluded_candidates_located': len(excluded & located),
        'invalid_wgs84_geometry': geometry['invalid'], 'missing_precision_or_provenance': geometry['no_provenance'],
        'status_location_mismatch': geometry['status_location_mismatch'], 'new_rows_bad_provenance': new_provenance,
        'prior_evidence_missing': prior_kept['missing'], 'ambiguous_census_reason_lost': prior_kept['ambiguous_reason_lost'],
        'provider_precision': [dict(r) for r in distribution], 'journal': [dict(r) for r in journal],
        'runs': [{k: str(v) for k, v in r.items()} for r in runs], 'rollout': drift,
        'agent_reference_permissions': guard, 'agent_collision_write_privilege': writes, 'precision_constraint': constraint}
    ok = (checks['no_deleted_or_duplicated_records'] and checks['original_source_fields_unchanged'] and checks['original_census_matched_still_located'] == 59627
          and not checks['duplicate_journal_updates'] and checks['journal_reconciles_with_located'] and not checks['new_rows_outside_approved_allowlists']
          and not checks['excluded_candidates_located'] and not geometry['invalid'] and not geometry['no_provenance'] and not geometry['status_location_mismatch']
          and not new_provenance and not prior_kept['missing'] and not prior_kept['ambiguous_reason_lost'] and not any(guard.values()) and not writes)
    return ok, checks


def agent_queries():
    settings = Settings.from_env()
    tools = DatabaseTools(settings.database_url)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as c:
        c.execute('SET TRANSACTION READ ONLY')
        pick = lambda provider: c.execute("""SELECT report_id, ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lng FROM citylens.traffic_collisions
                   WHERE geocode_provider=%s ORDER BY md5(report_id) LIMIT 1""", (provider,)).fetchone()
        pins = {'sangis_road_block': pick('SanGIS road centerline'), 'sangis_intersection': pick('SanGIS local topology'),
                'census_block': pick('US Census Geocoder Batch')}
        cases = []
        for label, p in pins.items():
            for radius in RADII:
                for start, end in WINDOWS:
                    req = QueryRequest(question='How many traffic collisions were recorded?', radius_meters=radius,
                                       locations=[dict(id='A', latitude=p['lat'], longitude=p['lng'])],
                                       time_range=dict(start=start, end=end))
                    calls = validated_calls(req, Plan(intent='collision_drilldown', tools=['aggregate_traffic_collisions', 'get_traffic_collisions']), settings)
                    results = [tools.execute(call) for call in calls]
                    again = [tools.execute(call) for call in calls]
                    agg, page = results
                    evidence = make_evidence(agg)
                    direct = c.execute("""SELECT count(*) AS n, count(*) FILTER (WHERE geocode_provider LIKE 'US Census%%') AS census,
                           count(*) FILTER (WHERE geocode_provider='SanGIS road centerline') AS sangis_block,
                           count(*) FILTER (WHERE geocode_provider='SanGIS local topology') AS sangis_intersection
                           FROM citylens.traffic_collisions WHERE location IS NOT NULL AND occurred_at>=%s AND occurred_at<%s
                           AND ST_DWithin(location, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s)""",
                                       (start, end, p['lng'], p['lat'], radius)).fetchone()
                    page_ids = [r['report_id'] for r in page.rows]
                    outside = c.execute("""SELECT count(*) AS n FROM citylens.traffic_collisions WHERE report_id=ANY(%s) AND (location IS NULL
                           OR NOT ST_DWithin(location, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s) OR occurred_at<%s OR occurred_at>=%s)""",
                                        (page_ids, p['lng'], p['lat'], radius, start, end)).fetchone()['n']
                    cases.append({'pin': label, 'radius_m': radius, 'window': [start, end], 'tool_status': agg.status,
                                  'tool_count': evidence.value, 'direct_postgis_count': direct['n'],
                                  'by_source': {k: direct[k] for k in ('census', 'sangis_block', 'sangis_intersection')},
                                  'deterministic': make_evidence(again[0]).value == evidence.value and [r['report_id'] for r in again[1].rows] == page_ids,
                                  'page_ids_unique': len(page_ids) == len(set(page_ids)), 'page_rows_outside_scope_or_unlocated': outside,
                                  'coordinate_precision_label': evidence.quality.coordinate_precision,
                                  'caveats': evidence.caveats})
        two = QueryRequest(question='Compare traffic collisions at these two places.', radius_meters=800,
                           locations=[dict(id='A', latitude=pins['sangis_road_block']['lat'], longitude=pins['sangis_road_block']['lng']),
                                      dict(id='B', latitude=pins['sangis_intersection']['lat'], longitude=pins['sangis_intersection']['lng'])],
                           time_range=dict(start=WINDOWS[0][0], end=WINDOWS[0][1]))
        compare = [make_evidence(tools.execute(call)).value for call in
                   validated_calls(two, Plan(intent='compare_locations', tools=['aggregate_traffic_collisions']), settings)]
        metadata = tools.execute(MetadataCall(domain='traffic_collisions'))
    ok = all(x['tool_status'] == 'ok' and x['tool_count'] == x['direct_postgis_count'] and x['deterministic'] and x['page_ids_unique']
             and not x['page_rows_outside_scope_or_unlocated'] for x in cases)
    return ok, {'pins': {k: {'report_id': v['report_id']} for k, v in pins.items()}, 'cases': cases, 'two_location_800m': compare,
                'metadata': metadata.rows[0] if metadata.rows else {'status': metadata.status}}


def main():
    ok_integrity, checks = integrity()
    ok_agent, agent = agent_queries()
    result = {'checked_at': now(), 'integrity_passed': ok_integrity, 'integrity': checks, 'agent_passed': ok_agent, 'agent': agent}
    save(HERE / 'deployment-verification.json', result)
    print(json.dumps({'integrity_passed': ok_integrity, 'agent_passed': ok_agent, 'statuses': checks['statuses'], 'located': checks['located'],
                      'coverage_percent': checks['coverage_percent']}))
    return 0 if ok_integrity and ok_agent else 1


if __name__ == '__main__':
    sys.exit(main())
