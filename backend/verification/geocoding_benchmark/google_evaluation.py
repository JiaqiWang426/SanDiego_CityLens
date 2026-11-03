"""Selective Google experiment. Default is a network-free plan, never a live run.

Real Google content lives only in this process and is discarded at exit. Saved
outputs are operational counts/categories and aggregate diagnostics, not returned
addresses, place IDs, response bodies, coordinates or per-address distances.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime,timezone
import json
from pathlib import Path
import statistics

from backend.agent.google_geocoding import GoogleEvaluationGeocoder,GoogleRequestBudget,PRICE_PER_REQUEST_USD
from backend.agent.geocoding_review import distance_m,consistent_candidates
from backend.agent.sangis_geocoding import SanGISAddressGeocoder
from backend.agent.user_geocoding import UserAddressGeocoder
from .benchmark import proposal,percentile

HERE=Path(__file__).resolve().parent
OUTPUT=HERE/'google_evaluation'


def selected_addresses(directory=HERE):
    rows=list(csv.DictReader((directory/'addresses.csv').open(encoding='utf-8')))
    summary=json.loads((directory/'summary.json').read_text(encoding='utf-8'))
    results={r['id']:r for r in csv.DictReader((directory/'results.csv').open(encoding='utf-8'))}
    disagreement=set(summary['distances_m']['above_100m'])
    unresolved={id for id,r in results.items() if r['census_status']!='resolved' and r['sangis_status']!='resolved'}
    ids=disagreement|unresolved|{'01'}
    selected=[]
    for row in rows:
        if row['id'] not in ids:continue
        reasons=[]
        if row['id'] in disagreement:reasons.append('free_provider_distance_above_100m')
        if row['id'] in unresolved:reasons.append('neither_provider_unique')
        if row['id']=='01':reasons.append('9500_gilman_special_case')
        selected.append(dict(row,selection_reason=';'.join(reasons)))
    if len(selected)>15:raise ValueError('Frozen selection exceeds the authorized experiment cap')
    return selected


def classify(google,census,sangis):
    """Proximity is only a diagnostic after full component validation.

    Preserve the requested `corrobates` spelling in outcome codes. No nearest
    candidate is selected or written back. Distances remain transient.
    """
    g=consistent_candidates(google);a=consistent_candidates(census);b=consistent_candidates(sangis)
    distances=dict(google_census=[],google_sangis=[])
    if google.status=='provider_error':return ['provider_error'],distances
    if google.status=='no_match':return ['google_no_match'],distances
    if not g:return ['google_invalid'],distances
    distances['google_census']=[distance_m(candidate,c) for candidate in g for c in a]
    distances['google_sangis']=[distance_m(candidate,c) for candidate in g for c in b]
    if google.status!='resolved' or len(g)!=1:return ['still_ambiguous'],distances
    outcomes=[]
    if census.status!='resolved' and sangis.status!='resolved':outcomes.append('google_resolved_previous_failure')
    if any(d<=100 for d in distances['google_census']):outcomes.append('google_corrobates_census')
    if any(d<=100 for d in distances['google_sangis']):outcomes.append('google_corrobates_sangis')
    if a and b and all(d>100 for values in distances.values() for d in values):outcomes.append('google_disagrees_with_both')
    # Campus/free ambiguity is not adjudicated by nearest-to-Google selection.
    if any(r.status=='needs_confirmation' and len(consistent_candidates(r))>1 for r in (census,sangis)):
        outcomes.append('still_ambiguous')
    return outcomes or ['still_ambiguous'],distances


def run(*,live_google=False,max_requests=15,directory=HERE,output=OUTPUT,adapter=None):
    if not 1<=max_requests<=15:raise ValueError('Google request cap must be 1..15')
    rows=selected_addresses(directory)
    if live_google and len(rows)>max_requests:raise ValueError('Request cap is smaller than the frozen selection')
    output.mkdir(parents=True,exist_ok=True)
    with (output/'selected_addresses.csv').open('w',newline='',encoding='utf-8') as file:
        writer=csv.DictWriter(file,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    budget=GoogleRequestBudget(max_requests=max_requests)
    geocoder=adapter or GoogleEvaluationGeocoder.from_env(live_authorized=live_google,budget=budget)
    info=dict(created_at=datetime.now(timezone.utc).isoformat(),selected_addresses=len(rows),
        planned_google_requests=len(rows),hard_cap=max_requests,
        planned_list_cost_usd=round(len(rows)*PRICE_PER_REQUEST_USD,6),
        maximum_list_cost_usd=round(max_requests*PRICE_PER_REQUEST_USD,6),
        pricing_basis='USD 5/1000 before any available monthly free allowance; checked 2026-11-02',
        google_key_configured=bool(geocoder.api_key),live_authorized=live_google,
        executed_google_requests=0,run_status='not_run',actual_results=None,
        retention='Google content in RAM only; expired and discarded at process exit; no disk cache',
        primary_provider_unchanged=True)
    if not live_google or not geocoder.api_key:
        info['not_run_reason']='live_authorization_absent' if not live_google else 'google_key_missing'
    else:
        # An exclusive durable marker prevents accidental repeat paid runs. It
        # contains no key/content and remains even after interruption/failure.
        with (output/'live-run.lock').open('x',encoding='utf-8') as file:
            file.write('A capped live experiment was started; review usage before authorizing another.\n')
        diagnostics=[];outcomes=Counter();precision=Counter();status=Counter();distances=dict(google_census=[],google_sangis=[])
        for row in rows:
            address=proposal(row)
            free=[]
            for provider,parser in [('census',UserAddressGeocoder()),('sangis',SanGISAddressGeocoder())]:
                cached=json.loads((directory/'raw'/f"{row['id']}-{provider}.json").read_text(encoding='utf-8'))
                if cached['transport_status']!='ok':raise ValueError('Cached free-provider error requires explicit handling')
                free.append(parser.parse_response(address,cached['payload']))
            google=geocoder.resolve(address)
            codes,pairs=classify(google,*free)
            outcomes.update(codes);status[google.status]+=1
            precision.update(c.precision for c in google.candidates)
            for key,values in pairs.items():distances[key].extend(values)
            diagnostics.append(dict(address_id=row['id'],google_status=google.status,
                valid_candidate_count=len(consistent_candidates(google)),
                partial_matches=sum(bool(c.metadata.get('partial_match')) for c in google.candidates),
                rejections=google.metadata.get('rejections',{}),outcomes=codes,
                error_code=google.metadata.get('reason')))
            if google.metadata.get('reason') in {'google_quota_exhausted','google_daily_limit_or_billing_error','google_request_denied','google_request_cap_exhausted','google_rate_limited'}:break
        info.update(run_status='completed' if len(diagnostics)==len(rows) else 'stopped',
            executed_google_requests=geocoder.budget.requests,
            actual_results=dict(evaluated_addresses=len(diagnostics),status_counts=dict(status),
                unattempted_addresses=len(rows)-len(diagnostics),validated_unique_matches=status['resolved'],
                validated_unique_rate_over_planned_sample=status['resolved']/len(rows),
                validated_unique_rate_among_evaluated=status['resolved']/len(diagnostics) if diagnostics else None,
                outcome_counts=dict(outcomes),precision_counts=dict(precision),
                distance_population='All component-consistent Google candidates versus all component-consistent free candidates; pair counts are not address counts',
                pairwise_distance_statistics_m={key:dict(count=len(values),
                    median=statistics.median(values) if values else None,mean=statistics.mean(values) if values else None,
                    p90=percentile(values,.9),maximum=max(values) if values else None) for key,values in distances.items()},
                cases=diagnostics),usage=geocoder.budget.snapshot())
        # No response/candidate objects or per-address distances are serialized.
        del google,free,distances
    # A dry plan must not overwrite results from an earlier authorized live run.
    filename='summary.json' if info['run_status']!='not_run' else 'plan.json'
    (output/filename).write_text(json.dumps(info,indent=2)+'\n',encoding='utf-8')
    return info


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-google',action='store_true',help='Explicit authorization for at most 15 paid HTTP attempts')
    parser.add_argument('--max-requests',type=int,default=15)
    args=parser.parse_args()
    try:
        result=run(live_google=args.live_google,max_requests=args.max_requests)
        print(json.dumps({k:result[k] for k in ['selected_addresses','planned_list_cost_usd','maximum_list_cost_usd','executed_google_requests','run_status']}))
    except Exception as exc:
        raise SystemExit('Google experiment stopped: '+type(exc).__name__+'; details/credentials withheld') from None


if __name__=='__main__':main()
