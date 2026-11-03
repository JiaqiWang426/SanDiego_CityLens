"""Offline P1.2.2 regressions; no paid provider or historical database calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from unittest.mock import MagicMock,patch

import pytest

from backend.agent.geocoding_http import GeocodingRequestError,NoRedirect
from backend.agent.google_geocoding import GoogleEvaluationGeocoder,GoogleRequestBudget
from backend.agent.geocoding_review import SelectiveReviewGeocoder,distance_m
from backend.agent.location_models import ChatTurn
from backend.agent.user_geocoding import AddressResolution
from backend.agent_tests.test_location_flow import agent,proposal,follow,QUESTION,Geocoder
from backend.verification.geocoding_benchmark.google_evaluation import selected_addresses,classify,run
from backend.scripts.audit_historical_locations import stratified_sample,road_type,road_evidence,point_segment_distance,road_cache


def address():return proposal(number='9500',name='Gilman',suffix='Dr',quote='9500 Gilman Dr').model_copy(update={'postal_code':'92093','city':'La Jolla'})


def result(*,number='9500',route='Gilman Drive',precision='ROOFTOP',lat=32.877,lng=-117.236,partial=False,id='mock-place'):
    def component(kind,long,short=None):return dict(long_name=long,short_name=short or long,types=[kind])
    return dict(formatted_address=f'{number} {route}, La Jolla, CA 92093, USA',place_id=id,
        partial_match=partial,address_components=[component('street_number',number),component('route',route),
            component('locality','San Diego'),component('administrative_area_level_1','California','CA'),
            component('country','United States','US'),component('postal_code','92093')],
        geometry=dict(location=dict(lat=lat,lng=lng),location_type=precision))


def parse(*results):return GoogleEvaluationGeocoder().parse_response(address(),dict(status='OK',results=list(results)))


@pytest.mark.parametrize('precision,status',[('ROOFTOP','resolved'),('RANGE_INTERPOLATED','needs_confirmation'),
    ('GEOMETRIC_CENTER','needs_confirmation'),('APPROXIMATE','needs_confirmation')])
def test_precision_and_metadata_preserved_without_ground_truth_claim(precision,status):
    r=parse(result(precision=precision))
    assert r.status==status
    c=r.candidates[0]
    assert c.precision==precision.lower() and c.metadata['location_type']==precision
    assert c.metadata['place_id']=='mock-place' and c.metadata['attribution']=='Google Maps'
    assert c.location.latitude==32.877 and c.location.longitude==-117.236


@pytest.mark.parametrize('changes',[dict(number='9400'),dict(route='Gilman Street'),dict(route='N Gilman Drive'),
    dict(route='Gilman Drive S')])
def test_complete_house_street_suffix_and_direction_conflicts_rejected(changes):
    r=parse(result(**changes))
    assert r.status=='invalid_or_out_of_scope' and not r.candidates


@pytest.mark.parametrize('kind,value',[('country','CA'),('administrative_area_level_1','AZ'),
    ('locality','Vista'),('postal_code','92037')])
def test_locality_country_state_and_zip_conflicts_rejected(kind,value):
    data=result()
    for c in data['address_components']:
        if kind in c['types']:c.update(long_name=value,short_name=value)
    assert parse(data).status=='invalid_or_out_of_scope'


def test_partial_match_requires_confirmation_and_does_not_count_as_validated():
    r=parse(result(partial=True))
    assert r.status=='needs_confirmation' and r.candidates[0].mismatch_reason=='partial_match'
    assert classify(r,AddressResolution('no_match',[]),AddressResolution('no_match',[]))[0]==['google_invalid']


def test_multiple_results_not_first_match_and_exact_duplicate_handled():
    r=parse(result(),result(id='mock-other',lat=32.88))
    assert r.status=='needs_confirmation' and len(r.candidates)==2
    assert all(c.requires_confirmation for c in r.candidates)
    assert parse(result(),result()).status=='resolved'
    assert parse(result(number='1'),result()).status=='resolved'


@pytest.mark.parametrize('data',[None,[],{},dict(status='OK'),dict(status='OK',results=[]),
    dict(status={},results=[]),dict(status=[],results=[]),
    dict(status='OK',results=[None]),dict(status='OK',results=[dict(geometry=None)]),
    dict(status='ZERO_RESULTS',results=[result()])])
def test_malformed_or_missing_results_not_silent_no_match(data):
    assert GoogleEvaluationGeocoder().parse_response(address(),data).status in {'provider_error','invalid_or_out_of_scope'}


@pytest.mark.parametrize('changes',[dict(lat=float('nan')),dict(lng=float('inf')),dict(lat=True),dict(lng=None),
    dict(lat=-117.2,lng=32.8),dict(lat=40.7,lng=-74),dict(precision='OTHER')])
def test_invalid_geometry_or_precision_rejected(changes):
    assert parse(result(**changes)).status=='invalid_or_out_of_scope'


@pytest.mark.parametrize('field,value',[('address_components',None),('geometry',[]),('partial_match','true'),('place_id',None)])
def test_malformed_nested_response_is_safe(field,value):
    data=result();data[field]=value
    assert parse(data).status=='invalid_or_out_of_scope'


@pytest.mark.parametrize('status,code',[('OVER_QUERY_LIMIT','google_quota_exhausted'),
    ('OVER_DAILY_LIMIT','google_daily_limit_or_billing_error'),('REQUEST_DENIED','google_request_denied'),
    ('UNKNOWN_ERROR','google_unknown_error'),('INVALID_REQUEST','google_invalid_request')])
def test_provider_errors_remain_distinct_from_zero_results(status,code):
    r=GoogleEvaluationGeocoder().parse_response(address(),dict(status=status,error_message='DO NOT LOG THIS',results=[]))
    assert r.status=='provider_error' and r.metadata['reason']==code
    assert 'DO NOT LOG THIS' not in json.dumps(r.metadata)
    assert GoogleEvaluationGeocoder().parse_response(address(),dict(status='ZERO_RESULTS',results=[])).status=='no_match'


def test_live_gate_missing_key_and_default_feature_switch_prevent_requests():
    transport=MagicMock()
    assert GoogleEvaluationGeocoder('test-key',transport=transport).resolve(address()).metadata['reason']=='google_live_not_authorized'
    assert GoogleEvaluationGeocoder(live_authorized=True,transport=transport).resolve(address()).metadata['reason']=='google_key_missing'
    transport.assert_not_called()
    primary=MagicMock();sangis=MagicMock();google=MagicMock()
    primary.resolve.return_value=parse(result())
    SelectiveReviewGeocoder(primary,sangis,google).resolve(address())
    sangis.resolve.assert_not_called();google.resolve.assert_not_called()


def test_attempt_caps_include_errors_retries_and_stop_at_quota(caplog,capsys):
    key='private-offline-test-key'
    transport=MagicMock(side_effect=GeocodingRequestError(dict(request_errors=[dict(error_type='TimeoutError',http_status=None)])))
    budget=GoogleRequestBudget(max_requests=1)
    adapter=GoogleEvaluationGeocoder(key,live_authorized=True,budget=budget,transport=transport,attempts=2)
    with patch('backend.agent.google_geocoding.time.sleep'):
        r=adapter.resolve(address())
    assert r.metadata['reason']=='google_request_cap_exhausted' and transport.call_count==1
    assert budget.snapshot()['requests']==1 and budget.snapshot()['estimated_list_cost_usd']==.005
    transport=MagicMock(return_value=(dict(status='OVER_QUERY_LIMIT',error_message=key),{}))
    adapter=GoogleEvaluationGeocoder(key,live_authorized=True,transport=transport)
    assert adapter.resolve(address()).metadata['reason']=='google_quota_exhausted'
    assert adapter.resolve(address()).metadata['reason']=='google_quota_exhausted' and transport.call_count==1
    assert key not in caplog.text+capsys.readouterr().out


def test_rate_limit_is_shared_and_window_expires():
    clock=MagicMock(return_value=1)
    b=GoogleRequestBudget(max_requests=3,per_minute=1,clock=clock)
    assert b.acquire() is None and b.acquire()=='google_rate_limited'
    clock.return_value=62
    assert b.acquire() is None and b.requests==2
    with pytest.raises(ValueError):GoogleRequestBudget(max_requests=16)


def test_google_http_is_one_attempt_without_automatic_redirects():
    transport=MagicMock(return_value=(dict(status='ZERO_RESULTS',results=[]),{}))
    adapter=GoogleEvaluationGeocoder('mock-key',live_authorized=True,transport=transport)
    assert adapter.resolve(address()).status=='no_match'
    assert transport.call_args.kwargs==dict(timeout=15,attempts=1,follow_redirects=False)
    assert NoRedirect().redirect_request(None,None,302,'redirect',{},'https://example.test') is None


def test_resolve_malformed_status_remains_invalid_not_a_crash():
    transport=MagicMock(return_value=(dict(status={},results=[]),{}))
    r=GoogleEvaluationGeocoder('offline-key',live_authorized=True,transport=transport).resolve(address())
    assert r.status=='invalid_or_out_of_scope' and r.metadata['reason']=='malformed_status'


@pytest.mark.parametrize('code,expected',[(403,'google_http_error'),(429,'google_quota_exhausted'),(500,'google_http_error')])
def test_http_failures_are_not_address_no_match(code,expected):
    transport=MagicMock(side_effect=GeocodingRequestError(dict(request_errors=[dict(error_type='HTTPError',http_status=code)])))
    adapter=GoogleEvaluationGeocoder('offline-key',live_authorized=True,transport=transport)
    r=adapter.resolve(address())
    assert r.status=='provider_error' and r.metadata['reason']==expected
    assert adapter.budget.requests==1


def test_concurrent_attempts_cannot_exceed_shared_cap():
    budget=GoogleRequestBudget(max_requests=3)
    transport=MagicMock(return_value=(dict(status='ZERO_RESULTS',results=[]),{}))
    adapter=GoogleEvaluationGeocoder('offline-key',live_authorized=True,budget=budget,transport=transport)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:adapter.resolve(address()),range(20)))
    assert transport.call_count==budget.requests==3
    assert sum(r.status=='no_match' for r in results)==3
    assert sum(r.metadata.get('reason')=='google_request_cap_exhausted' for r in results)==17


def free(provider,lat=32.877,precision='interpolated_address',status='resolved'):
    r=parse(result(lat=lat));c=r.candidates[0]
    c.provider=provider;c.precision=precision
    return AddressResolution(status,[c] if status in {'resolved','needs_confirmation'} else [])


@pytest.mark.parametrize('lat,google_called,expected',[(32.8771,False,'resolved'),(32.879,True,'needs_confirmation')])
def test_disagreement_100m_is_review_not_nearest_wins(lat,google_called,expected):
    a,b,g=MagicMock(),MagicMock(),MagicMock()
    a.resolve.return_value=free('US Census Geocoder')
    b.resolve.return_value=free('SanGIS Address Points',lat,'structure')
    g.resolve.return_value=parse(result(lat=32.878))
    r=SelectiveReviewGeocoder(a,b,g,enabled=True).resolve(address())
    assert r.status==expected and g.resolve.called==google_called
    if google_called:assert len(r.candidates)==3 and r.metadata['free_provider_distance_m']>100


def test_adequate_single_provider_avoids_google_but_centroid_needs_review():
    a,b,g=MagicMock(),MagicMock(),MagicMock()
    a.resolve.return_value=AddressResolution('no_match',[])
    b.resolve.return_value=free('SanGIS Address Points',precision='structure')
    review=SelectiveReviewGeocoder(a,b,g,enabled=True)
    assert review.resolve(address()).status=='resolved';g.resolve.assert_not_called()
    b.resolve.return_value=free('SanGIS Address Points',precision='parcel_centroid')
    g.resolve.return_value=parse(result())
    assert review.resolve(address()).status=='needs_confirmation'


def test_google_no_match_error_and_ambiguity_do_not_erase_free_candidates():
    a,b,g=MagicMock(),MagicMock(),MagicMock()
    a.resolve.return_value=free('US Census Geocoder')
    b.resolve.return_value=free('SanGIS Address Points',32.88,'structure')
    policy=SelectiveReviewGeocoder(a,b,g,enabled=True)
    for status in ['no_match','provider_error']:
        g.resolve.return_value=AddressResolution(status,[])
        assert len(policy.resolve(address()).candidates)==2
    g.resolve.return_value=parse(result(),result(lat=32.881,id='other'))
    assert len(policy.resolve(address()).candidates)==4


def test_google_only_resolution_diagnostics_are_serializable():
    a,b,g=MagicMock(),MagicMock(),MagicMock()
    a.resolve.return_value=b.resolve.return_value=AddressResolution('no_match',[])
    g.resolve.return_value=parse(result())
    r=SelectiveReviewGeocoder(a,b,g,enabled=True).resolve(address())
    assert r.status=='resolved' and r.candidates[0].provider=='Google Maps'
    assert json.loads(json.dumps(r.metadata))['google_status']=='resolved'


def test_selection_deduplicates_seven_disagreements_five_failures_and_gilman():
    rows=selected_addresses()
    assert len(rows)==12 and len({r['id'] for r in rows})==12
    assert sum('free_provider_distance_above_100m' in r['selection_reason'] for r in rows)==7
    assert sum('neither_provider_unique' in r['selection_reason'] for r in rows)==5
    assert [r['id'] for r in rows if '9500 Gilman' in r['input_address']]==['01']


def test_benchmark_default_is_offline_and_does_not_overwrite_live_summary(tmp_path,monkeypatch):
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY','offline-key')
    (tmp_path/'summary.json').write_text('existing live summary')
    with patch('backend.agent.google_geocoding.get_json') as http:
        info=run(output=tmp_path)
    http.assert_not_called()
    assert info['executed_google_requests']==0 and info['actual_results'] is None
    assert (tmp_path/'summary.json').read_text()=='existing live summary'
    with pytest.raises(ValueError):run(max_requests=16,output=tmp_path)
    with pytest.raises(ValueError):run(live_google=True,max_requests=5,output=tmp_path)


def test_benchmark_live_mock_output_excludes_google_content_and_repeat_runs(tmp_path):
    class MockEvaluation(GoogleEvaluationGeocoder):
        def resolve(self,a):
            self.budget.acquire()
            c=free('Google Maps').candidates[0]
            c.matched_address='returned-private-address';c.metadata['place_id']='returned-private-place-id'
            c.location.latitude=32.87654321098765
            return AddressResolution('resolved',[c])
    adapter=MockEvaluation('private-key')
    info=run(live_google=True,output=tmp_path,adapter=adapter)
    output=(tmp_path/'summary.json').read_text()
    assert info['executed_google_requests']==12
    assert all(text not in output for text in ['returned-private','private-key','32.87654321098765','formatted_address','place_id'])
    with pytest.raises(FileExistsError):run(live_google=True,output=tmp_path,adapter=adapter)
    assert adapter.budget.requests==12


def test_ambiguous_sangis_compares_all_candidates_without_selecting_nearest():
    g=parse(result())
    sangis=AddressResolution('needs_confirmation',[free('SanGIS Address Points',32.876).candidates[0],free('SanGIS Address Points',32.88).candidates[0]])
    codes,pairs=classify(g,AddressResolution('no_match',[]),sangis)
    assert len(pairs['google_sangis'])==2 and 'still_ambiguous' in codes
    assert len(sangis.candidates)==2


def test_multiple_google_results_are_all_compared_without_resolution():
    google=parse(result(),result(lat=32.88,id='mock-second'))
    codes,pairs=classify(google,free('US Census Geocoder'),free('SanGIS Address Points'))
    assert codes==['still_ambiguous']
    assert len(pairs['google_census'])==len(pairs['google_sangis'])==2


def test_google_candidate_selection_preserves_mixed_provider_filters_and_sql_gate():
    async def exercise():
        class Resolver(Geocoder):
            def resolve(self,a):
                r=super().resolve(a)
                if a.id=='B':
                    r.status='needs_confirmation';r.candidates[0].provider='Google Maps'
                    r.candidates[0].precision='rooftop';r.candidates[0].requires_confirmation=True
                return r
        a=agent(geocoder=Resolver())
        pending=await a.chat(ChatTurn(message=QUESTION))
        assert not a.tools.calls
        candidates=await a.chat(follow(pending))
        assert candidates.status=='needs_candidate_confirmation' and not a.tools.calls
        final=await a.chat(follow(candidates,'choose_candidates',candidate_choices={'B':candidates.candidates['B'][0].candidate_id}))
        assert final.status=='complete' and len(a.tools.calls)==2
        assert [c.provider for c in final.resolved_locations]==['US Census Geocoder','Google Maps']
        x,y=a.tools.calls
        assert x.model_dump(exclude={'location'})==y.model_dump(exclude={'location'})
    asyncio.run(exercise())


def test_historical_stratification_and_road_geometry_are_deterministic():
    rows=[dict(report_id=str(i),geocode_status=['matched','pending','ambiguous','no_match'][i%4],
        police_beat=str(100+i%9*100),primary_street='GILMAN DR',intersecting_street='' if i%3 else 'VOIGT DR',
        block_number='9500' if i%4 else '0') for i in range(240)]
    first=stratified_sample(rows)
    assert [r['report_id'] for r in first]==[r['report_id'] for r in stratified_sample(list(reversed(rows)))]
    assert len(first)==160 and all(sum(r['geocode_status']==s for r in first)==40 for s in ['matched','pending','ambiguous','no_match'])
    assert road_type(dict(primary_street='A ST',intersecting_street='B ST',block_number='0'))=='intersection_without_valid_block'
    assert point_segment_distance((1,1),(0,0),(2,0))==1
    assert point_segment_distance((1,1),(0,0),(0,0))==pytest.approx(2**.5)
    row=dict(primary_street='GILMAN DR',block_number='9500',longitude=-117.236,latitude=32.877)
    f=dict(attributes=dict(rd30full='GILMAN DR',rd20full='GILMAN DR',llowaddr=9400,lhighaddr=9600,rlowaddr=0,rhighaddr=0),
        geometry=dict(paths=[[[-117.236,32.87],[-117.236,32.88]]]))
    evidence=road_evidence(row,[f])
    assert evidence['number_range_supported_segments']==1 and evidence['distance_to_named_road_m']<1


def test_compressed_road_cache_rejects_unqueried_names_without_network(tmp_path):
    cached=dict(status='ok',names=['GILMAN DR'],features=[])
    with gzip.open(tmp_path/'roads-sample.json.gz','wt',encoding='utf-8') as file:json.dump(cached,file)
    with patch('backend.scripts.audit_historical_locations.urlopen') as http:
        assert road_cache([dict(primary_street='GILMAN DR')],tmp_path)==cached
        with pytest.raises(ValueError):road_cache([dict(primary_street='UNQUERIED ST')],tmp_path)
    http.assert_not_called()
