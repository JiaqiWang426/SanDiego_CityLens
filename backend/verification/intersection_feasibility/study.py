"""Read-only Stage A. Default --offline uses frozen data; --live captures missing
bounded REST responses only. No Google, Census geocoding, production tables or
status updates. Repeated road pairs and cache requests are deduplicated.
"""
import argparse
from collections import Counter,defaultdict,deque
from datetime import datetime,timezone
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode,parse_qs,urlparse
from urllib.request import Request,build_opener

import psycopg
from psycopg.rows import dict_row,tuple_row

from backend.agent.geocoding_http import NoRedirect
from backend.agent.settings import Settings
from backend.agent.tools import ensure_reader
from backend.census_addresses import census_street
from backend.collision_addresses import normalize_collision_address
from backend.scripts.audit_historical_locations import fingerprint,point_segment_distance
from .topology import (canonical,pair_key,invalid_source,street_matches,shared_nodes,
    classify_pair,geometry,projected,segment_crossing,identifier,endpoints)

HERE=Path(__file__).resolve().parent
ROADS='https://geo.sandag.org/server/rest/services/Hosted/Roads_All/FeatureServer/0'
NODES='https://geo.sandag.org/server/rest/services/Hosted/Roads_Intersection/FeatureServer/0'
TIGER='https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer'
SEED='citylens-intersections-stage-a-v1'
MAX_REQUESTS=64
ROAD_FIELDS='objectid,roadsegid,fnode,tnode,f_level,t_level,rd30full,rd30pred,rd30name,rd30sfx,rd30postd,segstat,segclass,funclass,oneway,ljurisdic,rjurisdic'
BOX=dict(xmin=-117.6,ymin=32.5,xmax=-116.8,ymax=33.2,spatialReference=dict(wkid=4326))


class EvidenceError(RuntimeError):pass


class OfflineEvidenceMissing(EvidenceError):pass


def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')


def digest(value):return hashlib.sha256((SEED+str(value)).encode()).hexdigest()


def groups_sample(rows,target=80,previous_features=()):
    if target!=80:raise ValueError('Stage A has a frozen 80-record scope')
    frequencies=Counter(pair_key(r['primary_street'],r['intersecting_street']) for r in rows)
    classes=defaultdict(set)
    for f in previous_features:
        a=f['attributes']
        for field in ['rd30full','rd20full']:
            if a.get(field):classes[canonical(a[field])].add(str(a.get('segclass') or 'unknown'))
    groups=defaultdict(list)
    for row in rows:
        primary,cross=row['primary_street'],row['intersecting_street'];joined=primary+' '+cross
        known=classes.get(canonical(primary),set())|classes.get(canonical(cross),set())
        road_class='+'.join(sorted(known)) if known else 'not_in_prior_cache'
        if re.search(r'\b(?:INTERSTATE|STATE ROUTE|RAMP|I-\d+)\b',joined):road_class='source_freeway_or_ramp_hint'
        complexity='description' if invalid_source(primary,cross) else 'complex_name' if max(len(primary.split()),len(cross.split()))>=4 else 'ordinary_name'
        directional=bool(re.search(r'\b(?:N|S|E|W|NE|NW|SE|SW|NORTH|SOUTH|EAST|WEST)\b',joined))
        beat=str(row.get('police_beat') or '')
        area=beat[0] if beat.isdigit() else 'unknown'
        additional=bool(str(row['raw_record'].get('ADDRESS_SFX_INTERSECTING') or '').strip())
        ambiguity='repeated_pair' if frequencies[pair_key(primary,cross)]>1 else 'single_report_pair'
        row.update(area_proxy=area,road_class_stratum=road_class,naming_complexity=complexity,
            directional_components=directional,ambiguity_proxy=ambiguity,additional_cross_suffix=additional,
            pair_population_frequency=frequencies[pair_key(primary,cross)])
        groups[(area,road_class,complexity,ambiguity,directional,additional)].append(row)
    queues=[deque(sorted(group,key=lambda r:digest(r['report_id'])))
        for key,group in sorted(groups.items(),key=lambda item:digest(item[0]))]
    selected=[]
    while queues and len(selected)<target:
        for queue in list(queues):
            if len(selected)==target:break
            selected.append(queue.popleft())
            if not queue:queues.remove(queue)
    if len(selected)!=target:raise EvidenceError('Insufficient source records for the requested sample')
    return selected


def connection():
    settings=Settings.from_env()
    if not settings.database_url:raise EvidenceError('Restricted reader configuration missing')
    conn=psycopg.connect(settings.database_url,connect_timeout=5,row_factory=dict_row)
    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
    conn.execute("SET LOCAL statement_timeout='60s'")
    with conn.cursor(row_factory=tuple_row) as cursor:
        class View:
            def execute(self,*args):cursor.execute(*args);return cursor
        ensure_reader(View())
    return conn


def prepare(directory=HERE):
    target=directory/'sample.json'
    if target.exists():return json.loads(target.read_text(encoding='utf-8'))
    with connection() as conn:
        before=fingerprint(conn)
        counts={r['geocode_status']:r['count'] for r in conn.execute('SELECT geocode_status,count(*) FROM citylens.traffic_collisions GROUP BY geocode_status')}
        rows=conn.execute("""SELECT report_id,occurred_at,police_beat,primary_street,intersecting_street,
            raw_record,address_key,validation_issues FROM citylens.traffic_collisions
            WHERE geocode_status='pending' AND primary_street<>'' AND intersecting_street<>'' ORDER BY report_id""").fetchall()
        for row in rows:
            row['occurred_at']=row['occurred_at'].isoformat() if row['occurred_at'] else None
            normalized=normalize_collision_address(row['raw_record'])
            row['source_components_consistent']=row['primary_street']==normalized.primary_street and row['intersecting_street']==normalized.intersecting_street
        previous=directory.parents[1]/'audits'/'2026-11-02-p1_2_2'/'roads-sample.json.gz'
        with gzip.open(previous,'rt',encoding='utf-8') as file:features=json.load(file)['features']
        selected=groups_sample(rows,previous_features=features)
        after=fingerprint(conn)
        if before!=after:raise EvidenceError('Source fingerprint changed inside snapshot')
    snapshot=dict(seed=SEED,created_at=datetime.now(timezone.utc).isoformat(),population_records=len(rows),
        population_unique_pairs=len({pair_key(r['primary_street'],r['intersecting_street']) for r in rows}),
        statuses=counts,before=before,after=after,records=selected,
        strata_limitations='Police district proxy; class is partial prior-cache evidence or a labelled lexical hint; ambiguity is report-frequency proxy, not observed provider success')
    save(target,snapshot)
    flat=[]
    for row in selected:
        record={k:v for k,v in row.items() if k not in {'raw_record','validation_issues'}}
        record.update({key:value for key,value in row['raw_record'].items() if key.startswith('ADDRESS_')})
        record['validation_issues']=json.dumps(row['validation_issues'])
        flat.append(record)
    write_csv(directory/'sample.csv',flat)
    return snapshot


def population_summary(rows,before,after):
    valid=[r for r in rows if not invalid_source(r['primary_street'],r['intersecting_street'])]
    names={canonical(street) for r in valid for street in [r['primary_street'],r['intersecting_street']]}
    variants={variant for r in valid for street in [r['primary_street'],r['intersecting_street']] for variant in road_names_variants(street)}
    return dict(records=len(rows),raw_normalized_field_pairs=len({tuple(sorted((r['primary_street'],r['intersecting_street']))) for r in rows}),
        existing_census_canonical_pairs=len({pair_key(r['primary_street'],r['intersecting_street']) for r in rows}),
        source_screen_passed_records=len(valid),source_screen_passed_unique_pairs=len({pair_key(r['primary_street'],r['intersecting_street']) for r in valid}),
        source_screen_passed_unique_road_names=len(names),query_name_variants=len(variants),
        minimum_same_size_name_query_groups=math.ceil(len(variants)/12),
        invalid_source_reasons=dict(Counter(invalid_source(r['primary_street'],r['intersecting_street']) for r in rows if invalid_source(r['primary_street'],r['intersecting_street']))),
        before=before,after=after,unchanged=before==after,
        limitation='Textual workload counts only; no provider coverage, validated intersection or recovery estimate. Query grouping excludes pagination, nodes and incident-segment queries.')


def audit_population(directory=HERE):
    with connection() as conn:
        before=fingerprint(conn)
        rows=conn.execute("SELECT report_id,primary_street,intersecting_street FROM citylens.traffic_collisions WHERE geocode_status='pending' AND primary_street<>'' AND intersecting_street<>''").fetchall()
        after=fingerprint(conn)
    if before!=after:raise EvidenceError('Source fingerprint changed inside snapshot')
    result=population_summary(rows,before,after)
    save(directory/'population-workload.json',result)
    return result


def write_csv(path,rows):
    if not rows:rows=[dict(status='none')]
    fields=list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w',newline='',encoding='utf-8') as file:
        writer=csv.DictWriter(file,fieldnames=fields);writer.writeheader()
        writer.writerows({k:json.dumps(v) if isinstance(v,(list,dict)) else v for k,v in row.items()} for row in rows)


class Capture:
    def __init__(self,directory=HERE,live=False):
        self.directory=directory;self.live=live
        self.path=directory/'requests.json';(directory/'cache').mkdir(exist_ok=True)
        if self.path.exists():self.ledger=json.loads(self.path.read_text())
        else:
            entries=[]
            for name,url in [('Roads_All',ROADS),('Roads_Intersection',NODES),
                    ('TIGER-Transportation',TIGER),('TIGER-local',TIGER+'/8')]:
                if (directory/f'{name}-metadata.json').exists():entries.append(dict(kind='metadata',url=url+'?f=json',status='captured_before_query_design'))
            self.ledger=dict(hard_cap=MAX_REQUESTS,attempts=entries);save(self.path,self.ledger)
        self.new_requests=0

    def query(self,endpoint,params,kind):
        url=endpoint+'?'+urlencode(params)
        key=hashlib.sha256(url.encode()).hexdigest()
        target=self.directory/'cache'/f'{kind}-{key[:20]}.json.gz'
        if target.exists():
            with gzip.open(target,'rt',encoding='utf-8') as file:record=json.load(file)
        else:
            if not self.live:raise OfflineEvidenceMissing('Offline cache entry missing: '+kind)
            if len(self.ledger['attempts'])>=MAX_REQUESTS:raise EvidenceError('Stage A REST request cap exhausted')
            entry=dict(kind=kind,url=url,started_at=datetime.now(timezone.utc).isoformat(),status='started')
            self.ledger['attempts'].append(entry);save(self.path,self.ledger);self.new_requests+=1
            record=dict(url=url,kind=kind)
            try:
                with build_opener(NoRedirect()).open(Request(url,headers={'User-Agent':'CityLens/0.5 (bounded intersection feasibility)'}),timeout=25) as response:
                    raw=response.read(8_000_001)
                if len(raw)>8_000_000:raise ValueError('Oversized evidence')
                record['response']=json.loads(raw);entry['status']='ok'
            except (HTTPError,URLError,TimeoutError,OSError,ValueError) as exc:
                record['error_type']=type(exc).__name__;entry['status']='provider_error'
            with gzip.open(target,'wt',encoding='utf-8') as file:json.dump(record,file,separators=(',',':'))
            save(self.path,self.ledger)
        if record.get('error_type'):raise EvidenceError('Cached transport failure: '+record['error_type'])
        payload=record.get('response')
        if not isinstance(payload,dict) or payload.get('error') or payload.get('exceededTransferLimit'):
            raise EvidenceError('Provider error, malformed or truncated response: '+kind)
        return payload


def features(payload):
    sr=payload.get('spatialReference')
    if not isinstance(sr,dict) or sr.get('wkid')!=4326 or not isinstance(payload.get('features'),list):raise EvidenceError('WGS84 feature response required')
    for feature in payload['features']:
        if not isinstance(feature,dict) or not isinstance(feature.get('attributes'),dict):raise EvidenceError('Malformed feature')
    return payload['features']


def chunks(values,size):
    values=sorted(set(values))
    return [values[i:i+size] for i in range(0,len(values),size)]


def road_names_variants(street):
    variants={street.upper(),canonical(street)}
    # Inverse of the existing verified CAMINO/CAM prefix equivalence only.
    for value in list(variants):
        if value.startswith('CAM '):variants.add('CAMINO '+value[4:])
    return variants


def fetch_roads(snapshot,capture):
    names={variant for row in snapshot['records'] for street in [row['primary_street'],row['intersecting_street']]
        if not invalid_source(row['primary_street'],row['intersecting_street']) for variant in road_names_variants(street)}
    roads=[]
    for group in chunks(names,12):
        quoted=','.join("'"+name.replace("'","''")+"'" for name in group)
        params=dict(where=f'UPPER(rd30full) IN ({quoted})',outFields=ROAD_FIELDS,outSR=4326,
            returnGeometry='true',resultRecordCount=2000,f='json',geometry=json.dumps(BOX),
            geometryType='esriGeometryEnvelope',inSR=4326,spatialRel='esriSpatialRelIntersects')
        roads.extend(features(capture.query(ROADS+'/query',params,'road_names')))
    unique={}
    for feature in roads:
        key=feature['attributes']['objectid']
        if key in unique and unique[key]!=feature:raise EvidenceError('Conflicting repeated road feature')
        unique[key]=feature
    return list(unique.values())


def node_evidence(pairs,roads,capture):
    ids=set()
    for a,b in pairs:
        if invalid_source(a,b):continue
        first=[f for f in roads if street_matches(f,a)];second=[f for f in roads if street_matches(f,b)]
        ids.update(shared_nodes(first,second))
    nodes=defaultdict(list);incidence={id:[] for id in ids}
    # Complete cached query scopes can satisfy a subset without replaying a
    # differently batched URL. This also reuses initial broader bounded captures.
    cached={};covered={}
    for kind in ['nodes','incidence']:
        cached[kind]=[];covered[kind]=set()
        for path in sorted((capture.directory/'cache').glob(kind+'-*.json.gz')):
            with gzip.open(path,'rt',encoding='utf-8') as file:record=json.load(file)
            if record.get('error_type'):continue
            payload=record.get('response') or {}
            if payload.get('error') or payload.get('exceededTransferLimit'):continue
            where=parse_qs(urlparse(record['url']).query).get('where',[''])[0]
            covered[kind].update(int(id) for id in re.findall(r'\b\d+\b',where))
            cached[kind].extend(features(payload))
    if ids<=covered['nodes'] and ids<=covered['incidence']:
        unique_nodes={};unique_roads={}
        for feature in cached['nodes']:
            key=feature['attributes']['objectid']
            if key in unique_nodes and unique_nodes[key]!=feature:raise EvidenceError('Conflicting cached node record')
            unique_nodes[key]=feature
        for feature in unique_nodes.values():
            if feature['attributes']['interid'] in ids:nodes[feature['attributes']['interid']].append(feature)
        for feature in cached['incidence']:
            key=feature['attributes']['objectid']
            if key in unique_roads and unique_roads[key]!=feature:raise EvidenceError('Conflicting cached incident segment')
            unique_roads[key]=feature
        for feature in unique_roads.values():
            for node in {identifier(feature['attributes'].get('fnode')),identifier(feature['attributes'].get('tnode'))}&ids:
                incidence[node].append(feature)
        return dict(nodes),incidence
    for group in chunks(ids,120):
        listed=','.join(map(str,group))
        payload=capture.query(NODES+'/query',dict(where=f'interid IN ({listed})',
            outFields='objectid,interid,type',outSR=4326,returnGeometry='true',resultRecordCount=2000,f='json'),'nodes')
        for feature in features(payload):nodes[feature['attributes']['interid']].append(feature)
        payload=capture.query(ROADS+'/query',dict(where=f'fnode IN ({listed}) OR tnode IN ({listed})',
            outFields=ROAD_FIELDS,outSR=4326,returnGeometry='true',resultRecordCount=2000,f='json'),'incidence')
        for feature in features(payload):
            for node in {identifier(feature['attributes'].get('fnode')),identifier(feature['attributes'].get('tnode'))}&set(group):
                incidence[node].append(feature)
    return dict(nodes),incidence


def independent_selection(results,limit=20):
    # Ensure grade conflicts/ambiguity/uncertainty are included when present, then
    # fill with unique candidates across area-independent deterministic ordering.
    eligible=[r for r in results if any(c.get('longitude') is not None for c in r['candidates'])]
    groups=defaultdict(list)
    for r in eligible:groups[('grade_conflict' if r['grade_separated_conflicts'] else r['status'])].append(r)
    selected=[]
    for kind,rows in sorted(groups.items(),key=lambda item:(item[0]=='accepted_candidate',item[0])):
        for r in sorted(rows,key=lambda r:digest(r['pair_id']))[:4]:selected.append(r)
    for r in sorted(eligible,key=lambda r:digest(r['pair_id'])):
        if len(selected)>=limit:break
        if r not in selected:selected.append(r)
    return selected[:limit]


def independent_check(result,capture):
    candidates=[c for c in result['candidates'] if c.get('longitude') is not None]
    # Validate every candidate of an ambiguous pair; no closest-candidate choice.
    checks=[]
    for candidate in candidates:
        x,y=candidate['longitude'],candidate['latitude']
        bbox=dict(xmin=x-.002,ymin=y-.002,xmax=x+.002,ymax=y+.002,spatialReference=dict(wkid=4326))
        params=dict(geometry=json.dumps(bbox),geometryType='esriGeometryEnvelope',sr=4326,
            layers='all:2,6,8',tolerance=0,mapExtent=f'{x-.002},{y-.002},{x+.002},{y+.002}',
            imageDisplay='800,800,96',returnGeometry='true',returnFieldName='true',f='json')
        try:
            payload=capture.query(TIGER+'/identify',params,'independent_tiger')
            matches=payload.get('results')
            if not isinstance(matches,list):raise EvidenceError('Malformed independent road evidence')
            observed=[]
            for f in matches:
                a=f.get('attributes') or {}
                # NAME is a full road name on these official TIGER layers.
                name=a.get('NAME') or a.get('Name')
                if not isinstance(name,str):continue
                if f.get('geometryType')!='esriGeometryPolyline':continue
                observed.append(dict(name=canonical(name),feature=f))
            point=projected([x,y]);road_results={}
            for street in [result['primary'],result['cross']]:
                lines=[f['feature'] for f in observed if f['name']==canonical(street)]
                distances=[]
                for f in lines:
                    points=geometry(f)
                    if points:distances.extend(point_segment_distance(point,a,b) for a,b in zip(points,points[1:]))
                road_results[street]=dict(matching_features=len(lines),distance_m=round(min(distances),3) if distances else None)
            spatial_support=all(v['distance_m'] is not None and v['distance_m']<=30 for v in road_results.values())
            checks.append(dict(node_id=candidate['node_id'],status='both_roads_spatially_corroborated' if spatial_support else 'not_corroborated',
                road_checks=road_results,independent_features=len(matches),
                grade_or_historical_ground_truth='not_verified',source=TIGER))
        except OfflineEvidenceMissing:
            raise
        except EvidenceError as exc:
            checks.append(dict(node_id=candidate['node_id'],status='provider_error',reason=str(exc)))
    return dict(pair_id=result['pair_id'],primary=result['primary'],cross=result['cross'],
        study_status=result['status'],grade_conflicts=result['grade_separated_conflicts'],checks=checks)


def analyze(snapshot,roads,nodes,incidence,capture):
    pair_reports=defaultdict(list)
    for row in snapshot['records']:pair_reports[pair_key(row['primary_street'],row['intersecting_street'])].append(row['report_id'])
    results=[]
    source_rows={row['report_id']:row for row in snapshot['records']}
    for (primary,cross),reports in sorted(pair_reports.items()):
        r=classify_pair(primary,cross,roads,nodes,incidence,
            source_consistent=all(source_rows[id]['source_components_consistent'] for id in reports))
        r.update(pair_id=hashlib.sha256((primary+' & '+cross).encode()).hexdigest()[:16],report_ids=reports)
        results.append(r)
    save(capture.directory/'pair-results.json',results)
    candidate_rows=[]
    for r in results:
        if not r['candidates']:
            candidate_rows.append({k:r[k] for k in ['pair_id','primary','cross','status','reason','report_ids']})
        else:
            for c in r['candidates']:candidate_rows.append(dict(pair_id=r['pair_id'],primary=r['primary'],cross=r['cross'],
                status=r['status'],reason=r['reason'],report_ids=r['report_ids'],**{'candidate_'+k:v for k,v in c.items()}))
    write_csv(capture.directory/'candidates.csv',candidate_rows)
    plan_path=capture.directory/'independent-plan.json'
    if plan_path.exists():
        plan=json.loads(plan_path.read_text())
        by_id={r['pair_id']:r for r in results}
        selected=[by_id[id] for id in plan['pair_ids']]
    else:
        selected=independent_selection(results)
        save(plan_path,dict(pair_ids=[r['pair_id'] for r in selected],method='Frozen before independent evidence capture'))
    independent=[independent_check(r,capture) for r in selected]
    save(capture.directory/'independent-validation.json',independent)
    write_csv(capture.directory/'independent-validation.csv',[
        dict(pair_id=r['pair_id'],primary=r['primary'],cross=r['cross'],study_status=r['study_status'],
            grade_conflicts=r['grade_conflicts'],**check) for r in independent for check in r['checks']])
    flat_checks=[c for r in independent for c in r['checks']]
    summary=dict(seed=SEED,sample_size=len(snapshot['records']),unique_pairs=len(results),
        duplicate_report_pairs=len(snapshot['records'])-len(results),population_records=snapshot['population_records'],
        population_unique_pairs=snapshot['population_unique_pairs'],pair_statuses=dict(Counter(r['status'] for r in results)),
        candidate_nodes=sum(len(r['candidates']) for r in results),
        supported_nodes=sum(c['status']=='supported' for r in results for c in r['candidates']),
        grade_conflict_pairs=sum(r['grade_separated_conflicts']>0 for r in results),
        geometric_crossing_pairs=sum(r['geometric_crossings']>0 for r in results),
        node_types=dict(Counter(str(c['node_type']) for r in results for c in r['candidates'])),
        independent_validation=dict(pairs=len(independent),candidate_checks=len(flat_checks),
            outcomes=dict(Counter(c['status'] for c in flat_checks)),
            limitation='Independent TIGER named-road spatial comparison only; not independent surveyed topology, grade or historical crash ground truth'),
        external_requests=dict(total_recorded_attempts=len(capture.ledger['attempts']),new_this_run=capture.new_requests,
            hard_cap=MAX_REQUESTS,by_kind=dict(Counter(e['kind'] for e in capture.ledger['attempts'])),
            failures=sum(e['status']=='provider_error' for e in capture.ledger['attempts'])),
        source_statuses=snapshot['statuses'],database_fingerprint=snapshot['before'],
        acceptance_means='One present-day supported road intersection candidate, never a verified historical crash point',
        extrapolated_recovery_rate=None,google_requests=0,collision_updates=0)
    save(capture.directory/'summary.json',summary)
    return summary


def run(live=False,directory=HERE):
    directory.mkdir(exist_ok=True)
    if not (directory/'sample.json').exists() and not live:raise EvidenceError('Frozen sample missing; --live permits read-only preparation')
    snapshot=prepare(directory)
    capture=Capture(directory,live)
    pairs={pair_key(r['primary_street'],r['intersecting_street']) for r in snapshot['records']}
    roads=fetch_roads(snapshot,capture)
    nodes,incidence=node_evidence(pairs,roads,capture)
    result=analyze(snapshot,roads,nodes,incidence,capture)
    if live:
        with connection() as conn:after=fingerprint(conn)
        save(directory/'database-final-check.json',dict(before=snapshot['before'],after=after,
            unchanged=after==snapshot['before'],mode='restricted-reader read-only; no update SQL'))
        if after!=snapshot['before']:raise EvidenceError('Database changed during study; inspect independent writers')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Allow read-only source selection and missing bounded free REST captures')
    parser.add_argument('--audit-population',action='store_true',help='Read-only textual workload counts; no external REST requests')
    args=parser.parse_args()
    try:
        if args.audit_population:
            result=audit_population()
            print(json.dumps({k:result[k] for k in ['records','source_screen_passed_records','source_screen_passed_unique_pairs','query_name_variants','minimum_same_size_name_query_groups','unchanged']}))
        else:
            result=run(live=args.live)
            print(json.dumps({k:result[k] for k in ['sample_size','unique_pairs','pair_statuses','independent_validation','external_requests']}))
    except Exception as exc:raise SystemExit('Stage A stopped: '+type(exc).__name__+'; '+str(exc) if isinstance(exc,EvidenceError) else 'Stage A stopped: '+type(exc).__name__+'; database details/credentials withheld') from None


if __name__=='__main__':main()
