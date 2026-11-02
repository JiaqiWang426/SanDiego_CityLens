"""Guarded 25-record audit; run from repo root with python -m backend.scripts.geocoding_pilot.

--run is opt-in and one-shot. Other modes never geocode or write database rows.
Credentials stay in memory. This is an audit runner, not a production scheduler.
"""
import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import csv
import json
import os
from pathlib import Path
import statistics
import time
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from backend.config import load_environment
from backend.collision_addresses import normalize_collision_address
from backend import collision_geocoding as worker
from backend.agent.tools import DatabaseTools, MetadataCall, ToolCall, ensure_reader
from backend.scripts.collision_validation import assess

DEFAULT_DIR = Path(__file__).resolve().parents[1]/'audits/2026-11-01-geocoding-pilot'
ROAD_LAYER = 'https://geo.sandag.org/server/rest/services/Hosted/Roads_All/FeatureServer/0'
GEO_FIELDS = ['location','geocode_status','geocode_provider','geocode_precision','geocoded_at','geocode_details']


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False)+'\n', encoding='utf-8')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def manifest(directory):
    data = read_json(directory/'sample.json')
    ids = data['report_ids']
    if not ids or len(ids)>25 or len(ids)!=len(set(ids)) or set(ids)!={r['report_id'] for r in data['records']}:
        raise ValueError('Invalid or over-budget manifest')
    for row in data['records']:
        address = normalize_collision_address(row['raw_record'])
        if (address.address_key!=row['address_key'] or not address.block_number.isdigit()
                or int(address.block_number)<=0 or not address.primary_street or address.intersecting_street):
            raise ValueError('Manifest is not a supported, unchanged block-address selection')
    return data


def snapshot(conn, ids):
    """Database-side digests include every original collision field, ordered by ID.

    No source data is exported. Nonselected geocodes and completed geocodes
    get separate digests, so changes outside the six authorized fields show up.
    """
    source = conn.execute('''SELECT count(*),md5(string_agg(
        md5((to_jsonb(c)-%s::text[])::text),'' ORDER BY report_id))
        FROM citylens.traffic_collisions c''',(GEO_FIELDS,)).fetchone()
    geo_expr = "md5(jsonb_build_array(report_id,ST_AsEWKT(location::geometry),geocode_status,geocode_provider,geocode_precision,geocoded_at,geocode_details)::text)"
    nonselected = conn.execute(f'''SELECT md5(string_agg({geo_expr},'' ORDER BY report_id))
        FROM citylens.traffic_collisions WHERE NOT (report_id=ANY(%s))''',(ids,)).fetchone()[0]
    completed = conn.execute(f'''SELECT count(*),md5(string_agg({geo_expr},'' ORDER BY report_id))
        FROM citylens.traffic_collisions WHERE geocode_status<>'pending' AND NOT (report_id=ANY(%s))''',(ids,)).fetchone()
    counts = conn.execute('''SELECT geocode_status,count(*),count(location),count(geocoded_at)
        FROM citylens.traffic_collisions GROUP BY 1 ORDER BY 1''').fetchall()
    return dict(records=source[0],source_digest=source[1],nonselected_geocodes_digest=nonselected,
                completed_nonselected_count=completed[0],completed_nonselected_digest=completed[1],
                statuses=[dict(status=s,records=n,located=l,attempted=a) for s,n,l,a in counts])


def stored_records(conn, ids):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT report_id,raw_record,address_key,primary_street,geocode_status,
            geocode_provider,geocode_precision,geocoded_at,geocode_details,
            ST_X(location::geometry) AS longitude,ST_Y(location::geometry) AS latitude
            FROM citylens.traffic_collisions WHERE report_id=ANY(%s) ORDER BY report_id''',(ids,))
        return cur.fetchall()


def verify_tools(directory, filename):
    """Actual adapter and ASGI endpoint, no mocked tools or model requests."""
    from fastapi.testclient import TestClient
    from backend.main import app, get_agent
    calls = [ToolCall.model_validate(dict(name='aggregate_service_requests',
        location=dict(id='A',label='Pilot check',latitude=32.84,longitude=-117.25),radius_meters=800,
        period='month',limit=100,
        time_range=dict(start='2026-01-01T00:00:00-08:00',end='2026-03-01T00:00:00-08:00',calendar_timezone='America/Los_Angeles'),
        service_request_aggregation=dict(group_by='both'))),
        MetadataCall(domain='service_requests'), MetadataCall(domain='traffic_collisions')]
    url = os.environ['AGENT_DATABASE_URL']
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute('BEGIN READ ONLY')
        ensure_reader(conn)
        identity = conn.execute('''SELECT current_user,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
            FROM pg_roles WHERE rolname=current_user''').fetchone()
        conn.execute('ROLLBACK')
    adapter = []
    for call in calls:
        result=DatabaseTools(url).execute(call)
        serialized=asdict(result)
        serialized['call']=call.model_dump(mode='json')
        adapter.append(serialized)
    endpoints = []
    get_agent.cache_clear()
    try:
        with TestClient(app) as client:
            for call in calls:
                response = client.post('/api/v1/tools/run',json=call.model_dump(mode='json'))
                endpoints.append(dict(http_status=response.status_code,body=response.json()))
    finally:
        get_agent.cache_clear()
    passed = all(r['status']=='ok' for r in adapter) and all(
        r['http_status']==200 and r['body']['tool_call']['status']=='ok' for r in endpoints)
    result = dict(at=now(),identity=list(identity),non_administrator=not any(identity[1:]),
                  all_tools_passed=passed,adapter_results=adapter,endpoint_results=endpoints)
    write_json(directory/filename,result)
    if not passed or not result['non_administrator']:
        raise RuntimeError('Read-only tool preflight failed; details recorded')
    return result


def run(directory):
    data = manifest(directory)
    marker = directory/'RUN_STARTED.json'
    if marker.exists():
        raise RuntimeError('One-shot pilot already started; validate saved results without rerunning')
    verify_tools(directory,'live-preflight.json')
    agent_info = conninfo_to_dict(os.environ['AGENT_DATABASE_URL'])
    # Reuse the configured Compose maintenance identity on the SAME DB only.
    maintenance = os.environ.get('DATABASE_URL')
    if not maintenance:
        maintenance = make_conninfo(os.environ['AGENT_DATABASE_URL'],user='citylens',
                                    password=os.environ['CITYLENS_DB_PASSWORD'])
    admin_info = conninfo_to_dict(maintenance)
    if any(agent_info.get(k)!=admin_info.get(k) for k in ('host','port','dbname')):
        raise RuntimeError('Maintenance and Agent database targets differ')
    ids = data['report_ids']
    with psycopg.connect(maintenance,connect_timeout=5,autocommit=True) as conn:
        conn.execute("SET statement_timeout='30s'")
        live = stored_records(conn,ids)
        expected = {r['report_id']:r['address_key'] for r in data['records']}
        if len(live)!=len(ids) or any(r['geocode_status']!='pending' or r['longitude'] is not None
                or r['geocoded_at'] is not None or r['address_key']!=expected[r['report_id']] for r in live):
            raise RuntimeError('Selected records changed or already attempted; refusing pilot')
        before = snapshot(conn,ids)
        write_json(directory/'before.json',before)
        started, start_tick, journal, requests = now(),time.monotonic(),[],[]
        # Exclusive marker prevents accidental repeats even after interruption.
        with marker.open('x',encoding='utf-8') as file:
            json.dump(dict(started=started,maximum_records=25,report_ids=ids),file,indent=2)
        original_open = worker.urlopen
        def counted_open(request, *args, **kwargs):
            if len(requests)>=25 or urlparse(request.full_url).hostname!='geocoding.geo.census.gov':
                raise RuntimeError('Census request budget or hostname guard')
            requests.append(dict(at=now(),url=request.full_url))
            write_json(directory/'census-requests.json',requests)
            return original_open(request,*args,**kwargs)
        class RecordingProvider:
            def __init__(self):
                self.census = worker.CensusGeocoder()
            def geocode(self,address):
                tick = time.monotonic()
                result = self.census.geocode(address)
                journal.append(dict(address=asdict(address),result=asdict(result),
                                    finished=now(),seconds=round(time.monotonic()-tick,3)))
                write_json(directory/'census-responses.json',journal)
                return result
        try:
            with patch.object(worker,'urlopen',counted_open):
                stats = worker.geocode_pending(conn,RecordingProvider(),limit=25,pause=1.0,report_ids=ids)
        finally:
            after = snapshot(conn,ids)
            write_json(directory/'after.json',after)
            records = stored_records(conn,ids)
            # Keep the raw provider evidence, but export only address source fields.
            for r in records:
                r['raw_record'] = {k:v for k,v in r['raw_record'].items() if k.startswith('ADDRESS_')}
            write_json(directory/'stored-results.json',records)
            safety = dict(source_records_unchanged=before['source_digest']==after['source_digest'],
                nonselected_geocodes_unchanged=before['nonselected_geocodes_digest']==after['nonselected_geocodes_digest'],
                completed_geocodes_unchanged=before['completed_nonselected_digest']==after['completed_nonselected_digest'])
            write_json(directory/'run.json',dict(started=started,finished=now(),
                seconds=round(time.monotonic()-start_tick,3),census_requests=len(requests),safety=safety,
                stats=locals().get('stats'),selected=len(ids)))
        if not all(safety.values()):
            raise RuntimeError('Postflight preservation digest changed; review audit')
    verify_tools(directory,'live-postflight.json')


def fetch_roads(directory):
    """Official schema first, then filtered road geometry, no permanent GIS table."""
    data = manifest(directory)
    log = []
    def get(url):
        entry = dict(at=now(),url=url)
        log.append(entry)
        try:
            with urlopen(url,timeout=30) as response:
                payload=json.load(response)
            if 'error' in payload:
                raise ValueError('ArcGIS service returned an error')
            return payload
        except Exception as exc:
            entry['error_type']=type(exc).__name__
            raise
        finally:
            write_json(directory/'sangis-requests.json',log)
    schema=get(ROAD_LAYER+'?f=pjson')
    write_json(directory/'sangis-layer-schema.json',schema)
    needed={'roadsegid','rd30full','rd20full','llowaddr','lhighaddr','rlowaddr','rhighaddr',
            'segstat','dedstat','pending','ljurisdic','rjurisdic'}
    if schema.get('geometryType')!='esriGeometryPolyline' or not needed<={f['name'] for f in schema.get('fields',[])}:
        raise RuntimeError('Actual road schema does not support expected diagnostics')
    names=sorted({r['normalized']['primary_street'] for r in data['records']})
    quoted=','.join("'"+s.replace("'","''")+"'" for s in names)
    where=f'UPPER(rd30full) IN ({quoted}) OR UPPER(rd20full) IN ({quoted})'
    features=[]
    complete=False
    for page in range(10):
        params=dict(where=where,outFields='*',returnGeometry='true',outSR=4326,
            orderByFields='objectid',resultRecordCount=2000,resultOffset=page*2000,f='json')
        payload=get(ROAD_LAYER+'/query?'+urlencode(params))
        if payload.get('spatialReference',{}).get('wkid')!=4326:
            raise RuntimeError('Road geometry CRS not confirmed as 4326')
        features.extend(payload.get('features',[]))
        if not payload.get('exceededTransferLimit',False):
            complete=True
            break
    write_json(directory/'sangis-roads.json',dict(source=ROAD_LAYER,retrieved=now(),
        complete=complete,spatialReference=dict(wkid=4326),where=where,features=features))


def validate(directory):
    """Reproducible offline artifacts; no network or DB access."""
    stored=read_json(directory/'stored-results.json')
    road_file=directory/'sangis-roads.json'
    roads=read_json(road_file) if road_file.exists() else dict(features=[],complete=False)
    records=[]
    for item in stored:
        address=normalize_collision_address(item['raw_record'])
        details=item.get('geocode_details') or {}
        response=details.get('response') or {}
        matches=response.get('result',{}).get('addressMatches',[])
        match=matches[0] if len(matches)==1 else {}
        r=dict(report_id=item['report_id'],original_address_components=item['raw_record'],
            block_number=address.block_number,primary_street=address.primary_street,
            submitted_address=address.normalized_address,geocode_status=item['geocode_status'],
            matched_address=match.get('matchedAddress'),longitude=item['longitude'],latitude=item['latitude'],
            returned_components=match.get('addressComponents'),provider=item['geocode_provider'],
            benchmark_requested=details.get('benchmark_requested',details.get('benchmark')),
            benchmark_returned=response.get('result',{}).get('input',{}).get('benchmark'),
            candidate_count=len(matches) if 'result' in response else None,
            provider_reason=details.get('reason') or details.get('error') or
                ('No address candidates' if not matches else 'Multiple candidates' if len(matches)>1 else None),
            geocoded_at=item['geocoded_at'],precision=item['geocode_precision'])
        r.update(assess(r,roads['features'],complete=roads['complete']))
        records.append(r)
    write_json(directory/'validation-details.json',records)
    with (directory/'results.csv').open('w',newline='',encoding='utf-8') as file:
        writer=csv.DictWriter(file,fieldnames=list(records[0]))
        writer.writeheader()
        for record in records:
            writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in record.items()})
    provider=Counter(r['geocode_status'] for r in records)
    independent=Counter(r['assessment'] for r in records)
    distances=[r['distance_m'] for r in records if r['distance_m'] is not None]
    matched=provider['matched']
    run_info=read_json(directory/'run.json')
    summary=dict(selected=len(records),attempted=sum(r['geocoded_at'] is not None for r in records),
        census_statuses={k:provider[k] for k in ['matched','no_match','ambiguous','error','unsupported','pending']},
        census_status_scope='Existing adapter classifications; ambiguous also includes a single Census candidate rejected by conservative address checks',
        native_census_candidate_records=sum((r['candidate_count'] or 0)>0 for r in records),
        native_census_multiple_candidate_records=sum((r['candidate_count'] or 0)>1 for r in records),
        failed_status_mapping='failed means stored error; no separate failed status exists',
        independent_assessments={k:independent[k] for k in ['plausible','needs_review','rejected','unverified']},
        provider_match_rate=dict(numerator=matched,denominator=len(records),value=matched/len(records)),
        independent_plausibility_rate=dict(numerator=independent['plausible'],denominator=len(records),value=independent['plausible']/len(records)),
        plausible_among_provider_matches=dict(numerator=independent['plausible'],denominator=matched,value=independent['plausible']/matched if matched else None),
        road_distances_m=dict(count=len(distances),minimum=min(distances) if distances else None,
            median=statistics.median(distances) if distances else None,maximum=max(distances) if distances else None,
            at_most_10m=sum(d<=10 for d in distances),between_10_and_50m=sum(10<d<=50 for d in distances),
            above_50m=sum(d>50 for d in distances),ordered=sorted(distances)),
        roads_retrieved=len(roads['features']),road_retrieval_complete=roads['complete'],
        census_external_requests=run_info['census_requests'],
        sangis_validation_external_requests=len(read_json(directory/'sangis-requests.json')) if (directory/'sangis-requests.json').exists() else 0,
        external_request_scope='Census worker and reproducible SanGIS validation fetch only; earlier portal/schema browsing was not fully metered, so entire-session HTTP total is unknown',
        runtime_seconds=run_info['seconds'],safety=run_info['safety'],
        provider_errors=[dict(report_id=r['report_id'],reason=r['provider_reason']) for r in records if r['geocode_status']=='error'],
        review_examples=[dict(report_id=r['report_id'],address=r['submitted_address'],
            distance_m=r['distance_m'],reasons=r['reasons']) for r in records if r['assessment']=='needs_review'],
        note='Exploratory purposive sample; rates must not be extrapolated to the dataset')
    write_json(directory/'summary.json',summary)
    write_json(directory/'pilot_points.geojson',dict(type='FeatureCollection',features=[
        dict(type='Feature',geometry=dict(type='Point',coordinates=[r['longitude'],r['latitude']]),
             properties=dict(report_id=r['report_id'],address=r['submitted_address'],precision=r['precision'],
                 assessment=r['assessment'],distance_m=r['distance_m'],reasons=r['reasons']))
        for r in records if r['geocode_status']=='matched']))
    print(json.dumps(summary,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit-dir',type=Path,default=DEFAULT_DIR)
    parser.add_argument('mode',choices=['run','fetch-roads','validate','verify-tools'])
    args=parser.parse_args()
    try:
        if args.mode in {'run','verify-tools'}:
            load_environment()
        if args.mode=='run': run(args.audit_dir)
        elif args.mode=='fetch-roads': fetch_roads(args.audit_dir)
        elif args.mode=='validate': validate(args.audit_dir)
        else: verify_tools(args.audit_dir,'live-postflight.json')
    except Exception as exc:
        # Database exception messages can contain connection strings; do not echo.
        write_json(args.audit_dir/'failure.json',dict(at=now(),mode=args.mode,error_type=type(exc).__name__))
        raise SystemExit(f'Pilot {args.mode} stopped: {type(exc).__name__}; inspect safe audit artifacts') from None


if __name__=='__main__':
    main()
