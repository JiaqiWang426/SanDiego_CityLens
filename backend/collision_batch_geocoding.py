"""Sequential, resumable Census batches for eligible pending collisions only.

Run with python -m backend.collision_batch_geocoding. A new run first writes
and verifies 100 reports; only then processes the frozen remaining selection.
No migrations, source imports, role changes, SanGIS or LLM calls occur here.
"""
import argparse
from collections import Counter
from dataclasses import asdict,replace
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict,make_conninfo

from .census_addresses import census_street,supported
from .census_batch import CensusBatchClient,BatchProtocolError,ProviderUnavailable,input_csv,parse_batch
from .collision_addresses import normalize_collision_address
from .collision_geocoding import GeocodeResult,save_geocode
from .config import load_environment

ROOT=Path(__file__).resolve().parent
DEFAULT_DIR=ROOT/'audits/2026-11-02-census-batch'
ELIGIBLE="""geocode_status='pending' AND location IS NULL AND primary_street<>''
    AND intersecting_street='' AND btrim(raw_record->>'ADDRESS_NO_PRIMARY') ~ '^[1-9][0-9]*$'"""
GEO_FIELDS=['location','geocode_status','geocode_provider','geocode_precision','geocoded_at','geocode_details']


def utcnow(): return datetime.now(timezone.utc).isoformat()


def read(path): return json.loads(path.read_text(encoding='utf-8'))


def write(path,data):
    """Atomic checkpoint replacement; a crash cannot leave half-written JSON."""
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,indent=2,default=str,allow_nan=False)+'\n',encoding='utf-8')
    temporary.replace(path)


def pilot_hashes():
    return {str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted((ROOT/'audits/2026-11-01-geocoding-pilot').rglob('*')) if f.is_file()}


def fingerprint(conn,protected_ids):
    source=conn.execute('''SELECT count(*),md5(string_agg(md5((to_jsonb(c)-%s::text[])::text),'' ORDER BY report_id))
        FROM citylens.traffic_collisions c''',(GEO_FIELDS,)).fetchone()
    protected=conn.execute('''SELECT count(*),md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id))
        FROM citylens.traffic_collisions c WHERE report_id=ANY(%s)''',(protected_ids,)).fetchone()
    history={}
    for table,order in [('collision_imports','id'),('collision_source_rows','import_id,row_number')]:
        # Identifiers are fixed literals above, never caller-supplied SQL.
        history[table]=list(conn.execute(f"SELECT count(*),md5(string_agg(md5(to_jsonb(t)::text),'' ORDER BY {order})) FROM citylens.{table} t").fetchone())
    return dict(total_records=source[0],source_digest=source[1],protected_count=protected[0],
                protected_digest=protected[1],history=history,pilot_artifacts=pilot_hashes())


def prepare(conn,directory,config):
    state_file=directory/'state.json'
    if state_file.exists():
        state=read(state_file)
        if state['config']!=config:
            raise ValueError('Resume with original batch size/benchmark/transport configuration')
        return state,read(directory/'plan.json')
    if any(directory.iterdir()):
        raise ValueError('New run directory must be empty')
    protected=[r[0] for r in conn.execute("SELECT report_id FROM citylens.traffic_collisions WHERE geocode_status<>'pending'").fetchall()]
    state=dict(run_id='census_batch_'+uuid.uuid4().hex,started=utcnow(),config=config,
               protected_ids=protected,smoke_verified=False,status='running',batches=[],requests=[],active_seconds=0)
    baseline=fingerprint(conn,protected)
    rows=conn.execute(f'SELECT report_id,address_key,raw_record FROM citylens.traffic_collisions WHERE {ELIGIBLE} ORDER BY md5(report_id)').fetchall()
    groups,records={},[]
    for report_id,key,raw in rows:
        address=normalize_collision_address(raw)
        if not supported(address) or address.address_key!=key:
            raise ValueError('Eligibility or source address key differs from existing normalizer')
        primary=census_street(address.primary_street)
        number=str(int(address.block_number))
        street=number+' '+primary
        token='u'+hashlib.sha256(street.encode()).hexdigest()[:20]
        group=dict(street=street,number=number,primary_street=primary)
        if token in groups and groups[token]!=group:
            raise ValueError('Unique request identifier collision')
        groups[token]=group
        records.append(dict(report_id=report_id,address_key=key,request_id=token))
    plan=dict(records=records,requests=groups,eligible_records=len(records),unique_addresses=len(groups))
    write(directory/'before.json',baseline)
    write(directory/'plan.json',plan)
    write(state_file,state)
    return state,plan


def apply_results(conn,records,results,chunk_size=500):
    """Bounded atomic commits using the existing key + pending guarded saver.

    Replaying a saved response after interruption skips already completed rows.
    There is no HTTP call inside a database transaction.
    """
    applied,skipped=Counter(),0
    for offset in range(0,len(records),chunk_size):
        with conn.transaction():
            for record in records[offset:offset+chunk_size]:
                result=results[record['request_id']]
                if save_geocode(conn,record['report_id'],record['address_key'],result,only_if_pending=True):
                    applied[result.status]+=1
                else:
                    skipped+=1
    return dict(applied),skipped


def process_batch(conn,directory,state,plan,records,batch_id,cache,client):
    batch_dir=directory/batch_id
    batch_dir.mkdir(exist_ok=True)
    manifest_file=batch_dir/'manifest.json'
    if manifest_file.exists():
        manifest=read(manifest_file)
        if manifest['records']!=records:
            raise ValueError('Batch checkpoint selection changed')
    else:
        needed={r['request_id']:plan['requests'][r['request_id']] for r in records if r['request_id'] not in cache}
        manifest=dict(records=records,requests=needed,started=utcnow())
        write(manifest_file,manifest)
    needed=manifest['requests']
    if batch_id not in state['batches']:
        state['batches'].append(batch_id)
        write(directory/'state.json',state)
    output_file=batch_dir/'response.csv'
    results_file=batch_dir/'results.json'
    if results_file.exists():
        results={id:GeocodeResult(**value) for id,value in read(results_file).items()}
    elif needed:
        (batch_dir/'input.csv').write_bytes(input_csv(needed))
        def event(value):
            state['requests'].append(dict(batch_id=batch_id,at=utcnow(),**value))
            write(directory/'state.json',state)
        if not output_file.exists():
            output=client.submit(needed,event=event)
            # Save before parsing or writing; resume reuses a valid saved response.
            temporary=output_file.with_suffix('.csv.tmp')
            temporary.write_text(output,encoding='utf-8',newline='')
            temporary.replace(output_file)
        output=output_file.read_text(encoding='utf-8')
        results=parse_batch(output,needed,client.benchmark,batch_id)
        if any(r.details.get('reason') in {'invalid_coordinates','outside_san_diego_screen'} for r in results.values()):
            raise BatchProtocolError('Suspicious coordinate output; review saved batch before continuing')
        digest=hashlib.sha256(output.encode()).hexdigest()
        results={id:replace(r,details=dict(r.details,run_id=state['run_id'],response_sha256=digest))
                 for id,r in results.items()}
        errors=sum(r.status=='error' for r in results.values())
        if errors>max(3,len(needed)*.2):
            raise BatchProtocolError('Systemic provider invalid/error responses; batch not written')
        write(results_file,{id:asdict(value) for id,value in results.items()})
    else:
        results={}
        write(results_file,{})
    cache.update(results)
    if any(r['request_id'] not in cache for r in records):
        raise BatchProtocolError('Missing cached request mapping')
    if (batch_dir/'committed.json').exists():
        return
    applied,skipped=apply_results(conn,records,cache)
    write(batch_dir/'committed.json',dict(at=utcnow(),applied=applied,skipped_by_guard=skipped))
    print(json.dumps(dict(batch=batch_id,reports=len(records),submitted_addresses=len(needed),
                          applied=applied,skipped_by_guard=skipped)),flush=True)


def summary(conn,directory,state,plan):
    statuses=dict(conn.execute('SELECT geocode_status,count(*) FROM citylens.traffic_collisions GROUP BY 1').fetchall())
    run=conn.execute('''SELECT geocode_status,count(*),count(*) FILTER(WHERE geocode_details->>'validation_rejected'='true')
        FROM citylens.traffic_collisions WHERE geocode_details->>'run_id'=%s GROUP BY 1''',(state['run_id'],)).fetchall()
    counts={s:n for s,n,_ in run}
    rejected=sum(n for _,_,n in run)
    eligible=conn.execute(f'SELECT count(*) FROM citylens.traffic_collisions WHERE {ELIGIBLE}').fetchone()[0]
    located=conn.execute('SELECT count(*) FROM citylens.traffic_collisions WHERE location IS NOT NULL').fetchone()[0]
    attempted=set()
    batches=[]
    for id in state['batches']:
        manifest=read(directory/id/'manifest.json')
        attempted.update(r['report_id'] for r in manifest['records'])
        batches.append(dict(id=id,records=len(manifest['records']),unique_requests=len(manifest['requests']),
                            committed=(directory/id/'committed.json').exists()))
    total=sum(statuses.values())
    report=dict(run_id=state['run_id'],status=state['status'],started=state['started'],updated=utcnow(),
        selected_records=plan['eligible_records'],unique_normalized_addresses=plan['unique_addresses'],
        total_attempted=len(attempted),total_processed=sum(counts.values()),matched=counts.get('matched',0),
        no_match=counts.get('no_match',0),ambiguous=counts.get('ambiguous',0)-rejected,
        rejected_by_validation=rejected,failed_provider_records=counts.get('error',0),
        failed_network_requests=sum(e['outcome']=='request_error' for e in state['requests']),
        still_pending=statuses.get('pending',0),eligible_still_pending=eligible,
        total_records=total,total_with_usable_coordinates=located,database_statuses=statuses,
        match_rate=counts.get('matched',0)/sum(counts.values()) if counts else None,
        coordinate_coverage=located/total if total else None,
        actual_batches=batches,http_requests=len(state['requests']),
        active_runtime_seconds=round(state['active_seconds'],3),last_error=state.get('last_error'))
    write(directory/'summary.json',report)
    return report


def verify_preservation(conn,directory,state):
    before=read(directory/'before.json')
    after=fingerprint(conn,state['protected_ids'])
    write(directory/'after.json',after)
    checks={k:before[k]==after[k] for k in before}
    write(directory/'preservation.json',checks)
    if not all(checks.values()):
        raise RuntimeError('Source/history/completed-geocode/pilot preservation check failed')


def verify_final(conn,directory,state):
    verify_preservation(conn,directory,state)
    invalid=conn.execute('''SELECT count(*) FILTER(WHERE geocode_status='matched' AND
        (location IS NULL OR ST_IsEmpty(location::geometry) OR ST_SRID(location::geometry)<>4326
         OR GeometryType(location::geometry)<>'POINT' OR geocode_precision IS DISTINCT FROM 'block_approximate'
         OR NOT(ST_X(location::geometry) BETWEEN -117.6 AND -116.8)
         OR NOT(ST_Y(location::geometry) BETWEEN 32.5 AND 33.2))),
        count(*) FILTER(WHERE geocode_status<>'matched' AND location IS NOT NULL)
        FROM citylens.traffic_collisions''').fetchone()
    if any(invalid):
        raise RuntimeError('Invalid matched/unresolved coordinate invariant')
    from .agent.tools import DatabaseTools,MetadataCall,ToolCall
    adapter=DatabaseTools(os.environ['AGENT_DATABASE_URL'])
    metadata=adapter.execute(MetadataCall(domain='traffic_collisions'))
    call=ToolCall.model_validate(dict(name='aggregate_traffic_collisions',
        location=dict(id='sanity',latitude=32.84,longitude=-117.25),radius_meters=5000,
        time_range=dict(start='2020-01-01T00:00:00-08:00',end='2026-11-03T00:00:00-08:00'),
        period='year',limit=100))
    spatial=adapter.execute(call)
    outputs=[]
    for result in (metadata,spatial):
        value=asdict(result)
        value['call']=result.call.model_dump(mode='json')
        outputs.append(value)
    write(directory/'final-tools.json',dict(invalid_matched=invalid[0],unresolved_with_coordinates=invalid[1],tools=outputs))
    if any(r.status!='ok' for r in (metadata,spatial)):
        raise RuntimeError('Final read-only metadata/spatial tool failed')


def execute(args):
    load_environment()
    agent=os.environ['AGENT_DATABASE_URL']
    maintenance=os.environ.get('DATABASE_URL') or make_conninfo(agent,user='citylens',password=os.environ['CITYLENS_DB_PASSWORD'])
    a,m=conninfo_to_dict(agent),conninfo_to_dict(maintenance)
    if any(a.get(k)!=m.get(k) for k in ('host','port','dbname')):
        raise ValueError('Agent and maintenance database targets differ')
    directory=args.run_dir.resolve()
    directory.mkdir(parents=True,exist_ok=True)
    if directory== (ROOT/'audits/2026-11-01-geocoding-pilot').resolve():
        raise ValueError('Never write into the historical pilot directory')
    config=dict(batch_size=args.batch_size,benchmark=args.benchmark,timeout=args.timeout,attempts=args.attempts,backoff=args.backoff)
    with psycopg.connect(maintenance,connect_timeout=5,autocommit=True) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(hashtext('citylens_census_batch_v1'))").fetchone()[0]:
            raise RuntimeError('Another Census batch worker owns the lock')
        conn.execute("SET statement_timeout='60s'")
        state,plan=prepare(conn,directory,config)
        tick=time.monotonic()
        client=CensusBatchClient(args.benchmark,args.timeout,args.attempts,args.backoff)
        cache={}
        try:
            state.update(status='running',last_error=None)
            smoke=plan['records'][:100]
            if smoke:
                process_batch(conn,directory,state,plan,smoke,'0000-smoke',cache,client)
            if not state['smoke_verified']:
                verify_preservation(conn,directory,state)
                processed=conn.execute("SELECT count(*) FROM citylens.traffic_collisions WHERE geocode_details->>'run_id'=%s",(state['run_id'],)).fetchone()[0]
                if processed!=len(smoke):
                    raise RuntimeError('Smoke report mapping/write count differs from selection')
                state['smoke_verified']=True
                write(directory/'smoke-verification.json',dict(passed=True,records=processed,
                    preservation=read(directory/'preservation.json'),batch_size=len(smoke)))
                write(directory/'state.json',state)
            if not args.smoke_only:
                # Group by unique request, so no address is split across bulk batches.
                grouped={}
                for record in plan['records'][100:]:
                    grouped.setdefault(record['request_id'],[]).append(record)
                tokens=list(grouped)
                for index,start in enumerate(range(0,len(tokens),args.batch_size),1):
                    records=[r for token in tokens[start:start+args.batch_size] for r in grouped[token]]
                    process_batch(conn,directory,state,plan,records,f'{index:04d}-bulk',cache,client)
                    state['active_seconds']+=time.monotonic()-tick
                    tick=time.monotonic()
                    report=summary(conn,directory,state,plan)
                    print(json.dumps({k:report[k] for k in ['total_processed','matched','no_match','ambiguous','rejected_by_validation','eligible_still_pending','http_requests']}),flush=True)
                verify_final(conn,directory,state)
                state['status']='complete' if conn.execute(f'SELECT count(*) FROM citylens.traffic_collisions WHERE {ELIGIBLE}').fetchone()[0]==0 else 'partial'
            else:
                state['status']='smoke_verified'
        except (Exception,KeyboardInterrupt) as exc:
            state.update(status='partial',last_error=dict(type=type(exc).__name__,at=utcnow()))
            # No connection/error text is printed; diagnostics contain no DSN.
            raise
        finally:
            state['active_seconds']+=time.monotonic()-tick
            write(directory/'state.json',state)
            report=summary(conn,directory,state,plan)
            print(json.dumps({k:report[k] for k in ['status','total_processed','matched','no_match','ambiguous','rejected_by_validation','failed_provider_records','eligible_still_pending','http_requests','coordinate_coverage']}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,default=DEFAULT_DIR)
    parser.add_argument('--batch-size',type=int,default=2000)
    parser.add_argument('--benchmark',default='Public_AR_Current')
    parser.add_argument('--timeout',type=float,default=180)
    parser.add_argument('--attempts',type=int,default=3)
    parser.add_argument('--backoff',type=float,default=5)
    parser.add_argument('--smoke-only',action='store_true')
    args=parser.parse_args()
    if not 100<=args.batch_size<=10000:
        parser.error('batch-size must be 100..10000 unique addresses')
    try:
        execute(args)
    except (Exception,KeyboardInterrupt) as exc:
        raise SystemExit(f'Batch run stopped safely: {type(exc).__name__}. See checkpoint summary; credentials withheld.') from None


if __name__=='__main__': main()
