"""Offline pilot selection/worker tests. No external provider or production writes."""
from unittest.mock import MagicMock,patch
import pytest

from backend.collision_geocoding import GeocodeResult,geocode_pending
from backend.scripts.collision_validation import (assess,block_range_overlap,
    numeric_range,point_segment_distance,road_distance_m)
from backend.scripts.geocoding_pilot import manifest,run,validate,write_json,read_json


def test_selected_worker_is_bounded_pending_and_parameterized():
    conn=MagicMock()
    conn.execute.return_value.fetchall.return_value=[('fixture','key',{})]
    provider=MagicMock()
    provider.geocode.return_value=GeocodeResult('no_match','fixture')
    with patch('backend.collision_geocoding.save_geocode',return_value=True) as save, \
         patch('backend.collision_geocoding.time.sleep'):
        assert geocode_pending(conn,provider,25,0,report_ids=['fixture'])=={'no_match':1}
    statement,params=conn.execute.call_args.args
    assert "geocode_status='pending'" in statement and 'report_id=ANY(%s)' in statement
    assert params==(['fixture'],25)
    assert save.call_args.kwargs=={'only_if_pending':True}


@pytest.mark.parametrize('ids',[[],['a','a'],['a','b'],[''],[None]])
def test_selection_rejects_bad_or_over_budget_ids(ids):
    conn=MagicMock()
    with pytest.raises(ValueError): geocode_pending(conn,MagicMock(),limit=1,report_ids=ids)
    conn.execute.assert_not_called()


def fixture_record(**kwargs):
    return dict(geocode_status='matched',longitude=-117.15,latitude=32.75,
        primary_street='N TEST ST',block_number='100',returned_components=dict(
            preDirection='N',streetName='TEST',suffixType='ST',state='CA',city='SAN DIEGO',
            fromAddress='100',toAddress='198'),**kwargs)


def fixture_road(id=1,longitude=-117.15,**attributes):
    a=dict(roadsegid=id,rd30full='N TEST ST',llowaddr=101,lhighaddr=199,
        rlowaddr=100,rhighaddr=198,segstat='M',dedstat='D',pending='N',ljurisdic='SD')
    a.update(attributes)
    return dict(attributes=a,geometry=dict(paths=[[[longitude,32.749],[longitude,32.751]]]))


def test_distances_and_zero_length_segments():
    assert point_segment_distance((3,4),(0,0),(0,0))==5
    assert point_segment_distance((3,4),(0,0),(6,0))==4
    assert point_segment_distance((9,4),(0,0),(6,0))==5
    assert road_distance_m(-117.15,32.75,fixture_road()['geometry']['paths'])<.01
    assert 90 < road_distance_m(-117.149,32.75,fixture_road()['geometry']['paths']) < 100


@pytest.mark.parametrize('low,high,expected',[(None,100,None),('bad',200,None),
    (0,0,None),(198,100,(100,198)),('100','198',(100,198))])
def test_ranges(low,high,expected):
    assert numeric_range(low,high)==expected


def test_anonymized_block_overlap_and_unknown_ranges():
    assert block_range_overlap('100',dict(llowaddr=101,lhighaddr=199)) is True
    assert block_range_overlap('200',dict(llowaddr=101,lhighaddr=199)) is False
    assert block_range_overlap('100',{}) is None
    assert block_range_overlap('101',dict(llowaddr=102,lhighaddr=199)) is False


def test_plausible_and_deterministic_compatible_candidates():
    actual=assess(fixture_record(),[fixture_road()])
    assert actual['assessment']=='plausible'
    assert actual['nearest_roadsegid']==1
    assert actual['plausible_candidates']==1
    ambiguous=assess(fixture_record(),[fixture_road(2),fixture_road(1)])
    assert ambiguous['assessment']=='needs_review'
    assert ambiguous['nearest_roadsegid']==1 and ambiguous['plausible_candidates']==2


def test_wrong_street_or_range_is_not_independent_confirmation():
    for road in [fixture_road(rd30full='S TEST ST'),fixture_road(llowaddr=501,lhighaddr=599,rlowaddr=500,rhighaddr=598),
                 fixture_road(llowaddr=None,lhighaddr=None,rlowaddr=0,rhighaddr=0)]:
        result=assess(fixture_record(),[road])
        assert result['assessment']=='unverified' and result['distance_m'] is None


@pytest.mark.parametrize('coordinates',[(float('nan'),32.7),(-117.1,float('inf')),(181,32.7),(-74,40.7)])
def test_invalid_or_geographically_wrong_points_rejected(coordinates):
    record=fixture_record()
    record.update(zip(('longitude','latitude'),coordinates))
    assert assess(record,[fixture_road()])['assessment']=='rejected'


def test_directions_suffixes_locality_checked():
    for components in [dict(preDirection='S'),dict(suffixType='RD'),dict(city='LOS ANGELES'),
                       dict(state='AZ'),dict(suffixDirection='N')]:
        record=fixture_record()
        record['returned_components'].update(components)
        assert assess(record,[fixture_road()])['assessment']=='rejected'


def test_incomplete_or_distant_or_unconstructed_evidence_needs_review():
    assert assess(fixture_record(),[fixture_road()],complete=False)['assessment']=='needs_review'
    assert assess(fixture_record(),[fixture_road(longitude=-117.14)])['assessment']=='needs_review'
    assert assess(fixture_record(),[fixture_road(segstat='T')])['assessment']=='needs_review'
    record=fixture_record()
    record['returned_components']['fromAddress']='101'
    assert assess(record,[fixture_road()])['assessment']=='needs_review'
    assert assess(fixture_record(),[])['assessment']=='unverified'


def test_unmatched_has_no_spatial_assessment_or_status_mutation():
    record=fixture_record()
    record.update(geocode_status='ambiguous',longitude=None,latitude=None)
    original=record.copy()
    assert assess(record,[fixture_road()])['assessment']=='unverified'
    assert record==original


def test_one_shot_guard_precedes_credentials_or_network(tmp_path):
    from backend.collision_addresses import normalize_collision_address
    address=dict(ADDRESS_NO_PRIMARY='100',ADDRESS_ROAD_PRIMARY='TEST',ADDRESS_SFX_PRIMARY='ST')
    a=normalize_collision_address(address)
    write_json(tmp_path/'sample.json',dict(report_ids=['test'],records=[
        dict(report_id='test',raw_record=address,address_key=a.address_key)]))
    write_json(tmp_path/'RUN_STARTED.json',{})
    with patch('backend.scripts.geocoding_pilot.verify_tools') as verify:
        with pytest.raises(RuntimeError): run(tmp_path)
        verify.assert_not_called()


def test_manifest_over_budget_and_source_changes_refused(tmp_path):
    write_json(tmp_path/'sample.json',dict(report_ids=list(map(str,range(26))),records=[]))
    with pytest.raises(ValueError): manifest(tmp_path)
    write_json(tmp_path/'sample.json',dict(report_ids=['a'],records=[dict(report_id='a',raw_record={},address_key='wrong')]))
    with pytest.raises(ValueError): manifest(tmp_path)


def test_offline_artifacts_reconcile_and_keep_null_coordinates(tmp_path,capsys):
    address=dict(ADDRESS_NO_PRIMARY='100',ADDRESS_PD_PRIMARY='N',
                 ADDRESS_ROAD_PRIMARY='TEST',ADDRESS_SFX_PRIMARY='ST')
    match=dict(matchedAddress='100 N TEST ST, SAN DIEGO, CA',
               addressComponents=fixture_record()['returned_components'])
    stored=[]
    for id,status in [('a','matched'),('b','no_match'),('c','error')]:
        response=dict(result=dict(addressMatches=[match] if status=='matched' else [],
            input=dict(benchmark=dict(benchmarkName='fixture')))) if status!='error' else {}
        stored.append(dict(report_id=id,raw_record=address,geocode_status=status,
            geocode_provider='fixture',geocode_precision='block_approximate' if status=='matched' else None,
            geocoded_at='2026-11-01T00:00:00Z',longitude=-117.15 if status=='matched' else None,
            latitude=32.75 if status=='matched' else None,
            geocode_details=dict(response=response,benchmark_requested='fixture')))
    write_json(tmp_path/'stored-results.json',stored)
    write_json(tmp_path/'sangis-roads.json',dict(features=[fixture_road()],complete=True))
    write_json(tmp_path/'run.json',dict(census_requests=3,seconds=1,safety={}))
    validate(tmp_path)
    summary=read_json(tmp_path/'summary.json')
    assert summary['provider_match_rate']==dict(numerator=1,denominator=3,value=1/3)
    assert summary['independent_assessments']==dict(plausible=1,needs_review=0,rejected=0,unverified=2)
    assert len(read_json(tmp_path/'pilot_points.geojson')['features'])==1
    details=read_json(tmp_path/'validation-details.json')
    assert details[1]['longitude'] is None and details[1]['precision'] is None
    assert details[0]['benchmark_returned']==dict(benchmarkName='fixture')
    assert summary['road_distances_m']['count']==1
