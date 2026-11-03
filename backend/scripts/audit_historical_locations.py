"""Reproducible read-only historical audit; no geocoder calls or data writes.

Optional cached Road Centerlines geometry is independent evidence, not accident
ground truth. --fetch-roads permits at most eight bounded exact-name GIS queries,
never geocoding or interpolation. Metadata must be inspected/archived first.
"""
import argparse
from collections import Counter,defaultdict,deque
import csv
from datetime import datetime,timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request,urlopen

import psycopg
from psycopg.rows import dict_row
from pyproj import Transformer

from backend.agent.settings import Settings
from backend.agent.tools import ensure_reader
from backend.agent.user_geocoding import interactive_street
from backend.collision_addresses import normalize_collision_address

OUTPUT=Path(__file__).resolve().parents[1]/'audits'/'2026-11-02-p1_2_2'
ROADS='https://geo.sandag.org/server/rest/services/Hosted/Roads_All/FeatureServer/0'
SEED='citylens-p1.2.2-v1'


def stable(value):return hashlib.sha256((SEED+str(value)).encode()).hexdigest()


def valid_number(row):
    number=str(row.get('block_number') or '').strip()
    return number.isdigit() and int(number)>0


def road_type(row):
    if not row['primary_street']:return 'missing_primary_street'
    if row['intersecting_street']:
        return 'intersection_with_block' if valid_number(row) else 'intersection_without_valid_block'
    street=row['primary_street'].upper()
    if any(token in street for token in ['INTERSTATE','FREEWAY','RAMP','I-5','I-8','I-15','I-805','SR-']):return 'freeway_or_ramp'
    return 'numbered_road_block' if valid_number(row) else 'invalid_or_missing_number'


def area(row):
    beat=str(row.get('police_beat') or '')
    return beat[0] if beat.isdigit() else 'unknown'


def offset_description(row):
    return bool(re.search(r'\b(?:FT|FEET|MILES?|MI)\b.*\bOF\b',row['primary_street']))


def stratified_sample(rows,target=160):
    if not 100<=target<=200:raise ValueError('Audit target must be 100..200')
    statuses=sorted({r['geocode_status'] for r in rows})
    chosen=[]
    for index,status in enumerate(statuses):
        quota=target//len(statuses)+(index<target%len(statuses))
        groups=defaultdict(list)
        for row in rows:
            if row['geocode_status']==status:
                groups[(road_type(row),area(row),bool(row['intersecting_street']),valid_number(row))].append(row)
        queues=[deque(sorted(group,key=lambda r:stable(r['report_id'])))
            for key,group in sorted(groups.items(),key=lambda item:stable(item[0]))]
        taken=0
        while queues and taken<quota:
            for group in list(queues):
                if taken>=quota:break
                chosen.append(group.popleft());taken+=1
                if not group:queues.remove(group)
    return chosen


def fingerprint(conn):
    r=conn.execute("SELECT count(*) AS records,count(location) AS located,md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id)) AS digest FROM citylens.traffic_collisions c").fetchone()
    return dict(r)


def road_cache(sample,output,fetch=False):
    target=output/'roads-sample.json.gz'
    if target.exists():
        with gzip.open(target,'rt',encoding='utf-8') as file:cached=json.load(file)
        if not {r['primary_street'] for r in sample if r['primary_street']}<=set(cached['names']):
            raise ValueError('Frozen road cache does not cover this sample; refusing false missing-road results')
        return cached
    if not fetch:return dict(status='not_run',reason='No cached independent road geometry; explicit --fetch-roads required',features=[])
    metadata=json.loads((output/'roads-layer-metadata.json').read_text(encoding='utf-8'))
    fields={f['name'] for f in metadata['fields']}
    needed={'objectid','rd30full','rd20full','llowaddr','lhighaddr','rlowaddr','rhighaddr','segclass'}
    if metadata.get('geometryType')!='esriGeometryPolyline' or not needed<=fields:raise ValueError('Road metadata changed')
    names=sorted({r['primary_street'] for r in sample if r['primary_street']})
    # Batch exact names only. Missing aliases are limitations, not invented roads.
    chunks=[names[i:i+20] for i in range(0,len(names),20)]
    if len(chunks)>8:raise ValueError('Road request cap exceeded')
    captures=[];features=[]
    for chunk in chunks:
        quoted=','.join("'"+name.replace("'","''")+"'" for name in chunk)
        params=dict(where=f'UPPER(rd30full) IN ({quoted}) OR UPPER(rd20full) IN ({quoted})',
            outFields=','.join(sorted(needed)),returnGeometry='true',outSR=4326,
            resultRecordCount=2000,f='json')
        url=ROADS+'/query?'+urlencode(params)
        with urlopen(Request(url,headers={'User-Agent':'CityLens/0.4 (bounded read-only road audit)'}),timeout=20) as response:
            raw=response.read(8_000_001)
        if len(raw)>8_000_000:raise ValueError('Road response too large')
        data=json.loads(raw)
        if data.get('error') or data.get('exceededTransferLimit') or data.get('spatialReference',{}).get('wkid')!=4326:
            raise ValueError('Incomplete/invalid road evidence; refusing partial quality assessment')
        features.extend(data['features'])
        captures.append(dict(request_url=url,features=len(data['features'])))
    result=dict(status='ok',source=ROADS,fetched_at=datetime.now(timezone.utc).isoformat(),requests=captures,
        names=names,features=list({f['attributes']['objectid']:f for f in features}.values()),
        limitation='Exact road-name sample only; no interpolation, download of full roads dataset, or accident ground truth')
    with gzip.open(target,'wt',encoding='utf-8') as file:json.dump(result,file,separators=(',',':'))
    return result


TRANSFORM=Transformer.from_crs(4326,26911,always_xy=True)  # Local UTM meters.


def point_segment_distance(point,a,b):
    dx,dy=b[0]-a[0],b[1]-a[1]
    denominator=dx*dx+dy*dy
    t=max(0,min(1,((point[0]-a[0])*dx+(point[1]-a[1])*dy)/denominator)) if denominator else 0
    return math.hypot(point[0]-a[0]-t*dx,point[1]-a[1]-t*dy)


def road_evidence(row,features):
    expected=interactive_street(row['primary_street'])
    roads=[f for f in features if expected and expected in {
        interactive_street(f['attributes'].get('rd30full','')),interactive_street(f['attributes'].get('rd20full',''))}]
    in_range=[]
    for feature in roads:
        a=feature['attributes']
        if valid_number(row) and any(isinstance(a.get(lo),(int,float)) and isinstance(a.get(hi),(int,float))
            and a[lo]>0 and a[hi]>0 and min(a[lo],a[hi])<=int(row['block_number'])<=max(a[lo],a[hi])
            for lo,hi in [('llowaddr','lhighaddr'),('rlowaddr','rhighaddr')]):in_range.append(feature)
    distances=[];range_distances=[]
    if row.get('longitude') is not None and row.get('latitude') is not None:
        point=TRANSFORM.transform(row['longitude'],row['latitude'])
        for feature in roads:
            segments=[]
            for path in feature.get('geometry',{}).get('paths',[]):
                projected=[TRANSFORM.transform(*xy[:2]) for xy in path]
                segments.extend(point_segment_distance(point,a,b) for a,b in zip(projected,projected[1:]))
            if segments:
                distances.append(min(segments))
                if feature in in_range:range_distances.append(min(segments))
    return dict(exact_named_road_segments=len(roads),number_range_supported_segments=len(in_range),
        distance_to_named_road_m=round(min(distances),3) if distances else None,
        distance_to_number_range_road_m=round(min(range_distances),3) if range_distances else None)


def run(output=OUTPUT,target=160,fetch_roads=False):
    output.mkdir(parents=True,exist_ok=True)
    settings=Settings.from_env()
    if not settings.database_url:raise RuntimeError('Restricted reader configuration missing')
    with psycopg.connect(settings.database_url,row_factory=dict_row,connect_timeout=5) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout='60s'")
        # ensure_reader expects positional rows; temporarily use its existing convention.
        with conn.cursor(row_factory=psycopg.rows.tuple_row) as cursor:
            class View:
                def execute(self,*args):cursor.execute(*args);return cursor
            ensure_reader(View())
        before=fingerprint(conn)
        rows=conn.execute("""SELECT report_id,geocode_status,police_beat,primary_street,intersecting_street,
            trim(coalesce(raw_record->>'ADDRESS_NO_PRIMARY','')) AS block_number,address_key,
            geocode_details->>'reason' AS reason,geocode_details->>'provider_status' AS provider_status
            FROM citylens.traffic_collisions ORDER BY report_id""").fetchall()
        sample=stratified_sample(rows,target)
        data=conn.execute("""SELECT report_id,raw_record,geocode_details,geocode_provider,geocode_precision,
            ST_X(location::geometry) AS longitude,ST_Y(location::geometry) AS latitude
            FROM citylens.traffic_collisions WHERE report_id=ANY(%s)""",([r['report_id'] for r in sample],)).fetchall()
        lookup={r['report_id']:r for r in data}
        for row in sample:
            row.update(lookup[row['report_id']])
            original=row.pop('raw_record')
            row['source_address_components']={k:v for k,v in original.items() if k.startswith('ADDRESS_')}
            normalized=normalize_collision_address(original)
            row['stored_components_consistent']=row['primary_street']==normalized.primary_street and row['intersecting_street']==normalized.intersecting_street
        after=fingerprint(conn)
    assert before==after
    # GIS read requests happen after releasing the database snapshot.
    roads=road_cache(sample,output,fetch_roads)
    for row in sample:row.update(road_evidence(row,roads['features']))
    statuses=sorted({r['geocode_status'] for r in rows})
    population={};selected={}
    for status in statuses:
        group=[r for r in rows if r['geocode_status']==status]
        subset=[r for r in sample if r['geocode_status']==status]
        eligible=[r for r in group if valid_number(r) and r['primary_street'] and not r['intersecting_street']]
        population[status]=dict(records=len(group),road_types=dict(Counter(road_type(r) for r in group)),
            records_with_intersection_information=sum(bool(r['intersecting_street']) for r in group),
            unique_complete_intersection_pairs=len({tuple(sorted((r['primary_street'],r['intersecting_street']))) for r in group if r['primary_street'] and r['intersecting_street']}),
            records_with_missing_primary_street=sum(not r['primary_street'] for r in group),
            records_with_invalid_or_nonpositive_number=sum(not valid_number(r) for r in group),
            records_with_offset_descriptions=sum(offset_description(r) for r in group),
            police_district_proxy=dict(Counter(area(r) for r in group)),
            recorded_reasons=dict(Counter(r.get('reason') or 'not_recorded' for r in group)),
            unique_source_address_keys=len({r['address_key'] for r in group}),
            unique_primary_block_queries=len({(r['block_number'],r['primary_street']) for r in group}),
            existing_census_eligible_records=len(eligible),
            unique_eligible_queries=len({(r['block_number'],r['primary_street']) for r in eligible}))
        selected[status]=dict(records=len(subset),road_types=dict(Counter(road_type(r) for r in subset)),
            police_district_proxy=dict(Counter(area(r) for r in subset)),
            road_name_evidence=sum(r['exact_named_road_segments']>0 for r in subset),
            road_number_range_evidence=sum(r['number_range_supported_segments']>0 for r in subset),
            stored_component_inconsistencies=sum(not r['stored_components_consistent'] for r in subset),
            located_distance_to_named_road_m=[r['distance_to_named_road_m'] for r in subset if r['distance_to_named_road_m'] is not None],
            located_distance_to_number_range_road_m=[r['distance_to_number_range_road_m'] for r in subset if r['distance_to_number_range_road_m'] is not None])
    unresolved=[r for r in rows if r['geocode_status']!='matched']
    unique_unresolved=len({(r['block_number'],r['primary_street']) for r in unresolved})
    unique_source_keys=len({r['address_key'] for r in unresolved})
    result=dict(created_at=datetime.now(timezone.utc).isoformat(),seed=SEED,target_sample_size=target,
        actual_sample_size=len(sample),before=before,after=after,collision_fingerprint_unchanged=before==after,
        population=population,sample=selected,roads_status=roads['status'],road_query_count=len(roads.get('requests',[])),
        matched_coverage=before['located']/before['records'],unresolved_unique_primary_block_queries=unique_unresolved,
        unresolved_unique_source_address_keys=unique_source_keys,
        google_historical_requests=0,recoverable_records_estimate=None,
        hypothetical_google_cost_not_authorized=dict(requests_if_unresolved_records_submitted=len(unresolved),
            list_cost_before_free_allowance_usd=round(len(unresolved)*.005,3),
            deduplicated_full_source_address_queries=unique_source_keys,
            deduplicated_list_cost_usd=round(unique_source_keys*.005,3),
            permitted_plan=False,reason='No Google historical enrichment; not appropriate accident ground truth or permanent reusable data'),
        limitations=['Stratified quota sample, not a simple random sample; no unweighted population extrapolation',
            'Police-beat first digit is a district proxy, not an independently verified neighborhood',
            'Road-name/range support is plausibility evidence, not accident-location verification',
            'No geocoding-provider query, interpolation, repair, or status update performed',
            'Unresolved recovery success cannot be estimated from textual eligibility alone'])
    (output/'summary.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    (output/'sample.json').write_text(json.dumps(sample,indent=2)+'\n',encoding='utf-8')
    simple=[{k:v for k,v in r.items() if k not in {'geocode_details','source_address_components'}} for r in sample]
    with (output/'sample.csv').open('w',newline='',encoding='utf-8') as file:
        writer=csv.DictWriter(file,fieldnames=list(simple[0]));writer.writeheader();writer.writerows(simple)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample-size',type=int,default=160)
    parser.add_argument('--fetch-roads',action='store_true')
    args=parser.parse_args()
    try:
        result=run(target=args.sample_size,fetch_roads=args.fetch_roads)
        print(json.dumps({key:result[key] for key in ['actual_sample_size','matched_coverage','collision_fingerprint_unchanged','road_query_count']}))
    except Exception as exc:raise SystemExit('Read-only historical audit stopped: '+type(exc).__name__+'; credentials withheld') from None


if __name__=='__main__':main()
