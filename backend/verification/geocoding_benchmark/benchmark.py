"""Bounded independent provider capture; cached raw responses prevent repeats.

Run from repo root: python -m backend.verification.geocoding_benchmark.benchmark
--limit 8 for feasibility, --limit 36 for the frozen benchmark. No DB/LLM calls.
"""
import argparse
import csv
from datetime import datetime,timezone
import json
from pathlib import Path
import time
import math
import statistics
from collections import Counter
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from pyproj import Geod

from backend.agent.location_models import AddressProposal
from backend.agent.user_geocoding import UserAddressGeocoder
from backend.agent.user_geocoding import interactive_street
from backend.census_addresses import census_street
from backend.agent.sangis_geocoding import SanGISAddressGeocoder,query_parameters,PLACEMENTS

DIRECTORY=Path(__file__).resolve().parent
LAYER='https://geo.sandag.org/server/rest/services/Hosted/Address_Points/FeatureServer/0'
CENSUS='https://geocoding.geo.census.gov/geocoder/locations/address'


def proposal(row):
    return AddressProposal(id='A',source_quote=row['input_address'],house_number=row['house_number'],
        street_name=row['street_name'],street_suffix=row['street_suffix'] or None,
        direction=row.get('direction') or None,post_direction=row.get('post_direction') or None,
        city=row['city'],state='CA',postal_code=row['postal_code'] or None)


def capture(row,provider):
    raw=DIRECTORY/'raw';raw.mkdir(exist_ok=True)
    target=raw/f"{row['id']}-{provider}.json"
    if target.exists():
        saved=json.loads(target.read_text(encoding='utf-8'))
        if saved['input_address']!=row['input_address']:raise ValueError('Frozen address differs from cached request')
        return saved
    street=' '.join(x for x in [row.get('direction'),row['street_name'],row['street_suffix'],row.get('post_direction')] if x)
    if provider=='census':
        params=dict(street=row['house_number']+' '+street,city=row['city'],state='CA',
                    benchmark='Public_AR_Current',format='json')
        if row['postal_code']:params['zip']=row['postal_code']
        endpoint=CENSUS
    else:
        params=query_parameters(proposal(row))
        endpoint=LAYER+'/query'
    url=endpoint+'?'+urlencode(params)
    record=dict(address_id=row['id'],provider=provider,input_address=row['input_address'],request_url=url,
                fetched_at=datetime.now(timezone.utc).isoformat(),http_attempts=1)
    started=time.monotonic()
    try:
        with urlopen(Request(url,headers={'User-Agent':'CityLens/0.3 (bounded provider comparison)'}),timeout=20) as response:
            record['payload']=json.load(response)
        record['transport_status']='ok'
    except Exception as exc:
        record.update(transport_status='provider_error',error_type=type(exc).__name__)
    record['latency_ms']=round((time.monotonic()-started)*1000,3)
    temporary=target.with_suffix('.tmp')
    temporary.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8');temporary.replace(target)
    return record


def percentile(values,p):
    if not values:return None
    values=sorted(values);index=(len(values)-1)*p;low=int(index);high=math.ceil(index)
    return values[low]+(values[high]-values[low])*(index-low)


def statistics_for(values):
    return dict(count=len(values),median=statistics.median(values) if values else None,
        mean=statistics.mean(values) if values else None,p90=percentile(values,.9),maximum=max(values) if values else None)


def normalized(resolution):
    valid=[c for c in resolution.candidates if not c.mismatch_reason]
    if resolution.status=='resolved':status='resolved'
    elif resolution.status=='needs_confirmation':status='ambiguous' if valid else 'invalid_result'
    elif resolution.status in {'no_match','provider_error'}:status=resolution.status
    else:status='invalid_result'
    return status,valid


def analyze(rows):
    providers=dict(census=UserAddressGeocoder(),sangis=SanGISAddressGeocoder())
    results=[];details=[]
    for row in rows:
        item=dict(id=row['id'],input_address=row['input_address'],category=row['category'],neighborhood=row['neighborhood'],source_url=row['source_url'])
        audit=dict(input=row,providers={})
        for name,adapter in providers.items():
            record=json.loads((DIRECTORY/'raw'/f"{row['id']}-{name}.json").read_text())
            if record['transport_status']!='ok':
                status,candidates='provider_error',[]
                metadata=dict(error_type=record.get('error_type'))
                mismatches=[]
            else:
                resolution=adapter.parse_response(proposal(row),record['payload'])
                status,candidates=normalized(resolution)
                metadata=resolution.metadata
                mismatches=[c.mismatch_reason for c in resolution.candidates if c.mismatch_reason]
            item[name+'_status']=status
            item[name+'_candidate_count']=len(candidates)
            item[name+'_latency_ms']=record['latency_ms']
            unique=candidates[0] if status=='resolved' else None
            item[name+'_candidate_coordinates']=[[c.location.longitude,c.location.latitude] for c in candidates]
            item[name+'_coordinates']=[unique.location.longitude,unique.location.latitude] if unique else None
            item[name+'_precision']=unique.precision if unique else None
            item[name+'_address_mismatch']=bool(mismatches or metadata.get('rejections'))
            if name=='sangis':item['sangis_placement']=unique.metadata.get('placement_location') if unique else None
            audit['providers'][name]=dict(status=status,candidates=[c.model_dump(mode='json') for c in candidates],
                rejected_census_reasons=mismatches,diagnostics=metadata)
        a,b=item['census_coordinates'],item['sangis_coordinates']
        item['coordinate_distance_m']=round(Geod(ellps='WGS84').inv(*a,*b)[2],3) if a and b else None
        results.append(item);details.append(audit)
    (DIRECTORY/'details.json').write_text(json.dumps(details,indent=2)+'\n',encoding='utf-8')
    with (DIRECTORY/'results.csv').open('w',encoding='utf-8',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=list(results[0]));writer.writeheader()
        writer.writerows({k:json.dumps(v) if isinstance(v,(list,dict)) else v for k,v in item.items()} for item in results)
    statuses={name:dict(Counter(r[name+'_status'] for r in results)) for name in providers}
    distances=[r['coordinate_distance_m'] for r in results if r['coordinate_distance_m'] is not None]
    coverage=Counter(('both' if r['census_status']=='resolved' and r['sangis_status']=='resolved' else
        'census_only' if r['census_status']=='resolved' else 'sangis_only' if r['sangis_status']=='resolved' else 'neither_unique') for r in results)
    placement=Counter('+'.join(str(code) if code is not None else 'not_recorded' for code in (r['sangis_placement'] or ['not_recorded'])) for r in results if r['sangis_status']=='resolved')
    mismatch_cases={key:[] for key in ['sangis_multiple_valid_candidates','sangis_house_number','sangis_street_components',
        'sangis_directions','sangis_zip_missing','sangis_zip_conflict','sangis_community_missing','sangis_jurisdiction_or_state']}
    for row,result in zip(rows,results):
        if result['sangis_candidate_count']>1:mismatch_cases['sangis_multiple_valid_candidates'].append(row['id'])
        raw=json.loads((DIRECTORY/'raw'/f"{row['id']}-sangis.json").read_text()).get('payload',{})
        flags=set();address=proposal(row)
        for f in raw.get('features',[]):
            a=f.get('attributes') or {}
            if a.get('addrnmbr')!=int(row['house_number']):flags.add('sangis_house_number')
            street=' '.join(str(a.get(k) or '') for k in ['addrpdir','addrname','addrsfx','addrpostd'])
            if interactive_street(street)!=interactive_street(address.street):flags.add('sangis_street_components')
            if (census_street(a.get('addrpdir') or '')!=census_street(address.direction or '') or
                    census_street(a.get('addrpostd') or '')!=census_street(address.post_direction or '')):flags.add('sangis_directions')
            if row['postal_code']:
                if not a.get('addrzip'):flags.add('sangis_zip_missing')
                elif a['addrzip']!=row['postal_code'][:5]:flags.add('sangis_zip_conflict')
            if not a.get('community'):flags.add('sangis_community_missing')
            if a.get('addrjur')!='SD' or a.get('state')!='CA':flags.add('sangis_jurisdiction_or_state')
        for flag in flags:mismatch_cases[flag].append(row['id'])
    rejections=Counter();census_rejections=Counter()
    for d in details:
        rejections.update(d['providers']['sangis']['diagnostics'].get('rejections',{}))
        census_rejections.update(d['providers']['census']['rejected_census_reasons'])
    summary=dict(sample_size=len(rows),normalization_policy='interactive exact token aliases including USPS CALLE/CLL, PKY/PKWY, PLAZA/PLZA/PLZ; strict number/direction/ZIP/geometry',categories=dict(Counter(r['category'] for r in rows)),
        neighborhood_counts=dict(Counter(r['neighborhood'] for r in rows)),
        status_counts=statuses,match_rates={name:dict(validated_unique_matches=statuses[name].get('resolved',0),
            denominator=len(rows),rate=statuses[name].get('resolved',0)/len(rows),provider_errors=statuses[name].get('provider_error',0)) for name in providers},
        coverage={key:coverage[key] for key in ['both','census_only','sangis_only','neither_unique']},
        automatic_fallback_additional=sum(r['census_status']=='no_match' and r['sangis_status']=='resolved' for r in results),
        fallback_offers_ambiguous=sum(r['census_status']=='no_match' and r['sangis_status']=='ambiguous' for r in results),
        distances_m=dict(**statistics_for(distances),above_50m=[r['id'] for r in results if (r['coordinate_distance_m'] or 0)>50],
            above_100m=[r['id'] for r in results if (r['coordinate_distance_m'] or 0)>100]),
        latency_ms={name:statistics_for([r[name+'_latency_ms'] for r in results]) for name in providers},
        sangis_unique_match_placement_signatures=dict(placement),placement_domains={k:v[1] for k,v in PLACEMENTS.items()},
        sangis_rejected_feature_reasons=dict(rejections),census_rejected_candidate_reasons=dict(census_rejections),
        candidate_diagnostic_cases={key:dict(count=len(ids),address_ids=ids) for key,ids in mismatch_cases.items()},
        category_match_rates={category:{name:dict(matches=sum(r['category']==category and r[name+'_status']=='resolved' for r in results),denominator=sum(r['category']==category for r in results)) for name in providers} for category in sorted({r['category'] for r in results})},
        request_counts={name:sum(json.loads((DIRECTORY/'raw'/f"{r['id']}-{name}.json").read_text())['http_attempts'] for r in rows) for name in providers},
        special_9500_gilman=next(r for r in results if r['id']=='01'),
        limitations=['Purposive independently sourced convenience sample; no population estimates',
            'Civic/large-property addresses are overrepresented; no ground-truth coordinate accuracy measured',
            'Fixed Census-then-SanGIS sequential order, small sample; weak latency inference',
            'Ambiguous or invalid is not counted as a validated unique match',
            'Existing feasibility captures reused, with no repeat provider queries'])
    (DIRECTORY/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit',type=int,default=36)
    parser.add_argument('--offline',action='store_true',help='Reparse cached raw responses only; no network')
    args=parser.parse_args()
    if not 1<=args.limit<=50:parser.error('limit must be 1..50')
    rows=list(csv.DictReader((DIRECTORY/'addresses.csv').open(encoding='utf-8')))[:args.limit]
    for row in rows:
        if args.offline:continue
        census=capture(row,'census');sangis=capture(row,'sangis')
        cp=census.get('payload',{}).get('result',{}).get('addressMatches',[])
        sp=sangis.get('payload',{})
        print(json.dumps(dict(id=row['id'],input=row['input_address'],census_candidates=len(cp),
            sangis_candidates=len(sp.get('features',[])),census_transport=census['transport_status'],
            sangis_transport=sangis['transport_status'],sangis_error=sp.get('error'),
            )),flush=True)
        time.sleep(.1)
    summary=analyze(rows)
    print(json.dumps({key:summary[key] for key in ['sample_size','status_counts','coverage','automatic_fallback_additional','request_counts']}))


if __name__=='__main__':main()
