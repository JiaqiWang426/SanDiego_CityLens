"""Focused batch tests only: no real provider requests or production writes."""
import csv
import io
from dataclasses import replace
from unittest.mock import MagicMock,patch
from urllib.error import HTTPError

import psycopg
import pytest

from backend.census_addresses import census_street,match_reason
from backend.census_batch import (BatchProtocolError,CensusBatchClient,ProviderUnavailable,
    input_csv,parse_batch)
from backend.collision_addresses import normalize_collision_address
from backend.collision_geocoding import CensusGeocoder,GeocodeResult
from backend.collision_batch_geocoding import apply_results,fingerprint,process_batch,read
from backend.agent_tests.test_integration import isolated_db


def requests():
    return {'a':dict(street='800 4TH AVE',number='800',primary_street='4TH AVE'),
            'b':dict(street='9400 CAM RUIZ',number='9400',primary_street='CAM RUIZ')}


def response(*rows):
    stream=io.StringIO(newline='')
    csv.writer(stream).writerows(rows)
    return stream.getvalue()


def matched(id='a',**changes):
    address=requests()[id]
    row=[id,address['street']+', San Diego, CA,','Match','Exact',
         address['street']+', SAN DIEGO, CA, 92101','-117.16,32.72','123','R']
    for index,value in changes.items():row[int(index)]=value
    return row


@pytest.mark.parametrize('source,expected',[
    ('04TH avenue','4TH AVE'),('08TH ST','8TH ST'),('101ST ST','101ST ST'),
    ('CAMINO RUIZ','CAM RUIZ'),('CAM DE LA REINA','CAM DE LA REINA'),
    ('North Camino RUIZ','N CAM RUIZ'),('TEST STREET SOUTH','TEST ST S'),
    ('EAST TEST ROAD','E TEST RD'),('OLD TEST RD','OLD TEST RD'),
    ('TEST CAMINO DR','TEST CAMINO DR')])
def test_geocoder_only_normalization(source,expected):
    assert census_street(source)==expected


def test_source_normalization_hash_stays_unchanged():
    raw=dict(ADDRESS_NO_PRIMARY='800',ADDRESS_ROAD_PRIMARY='04TH',ADDRESS_SFX_PRIMARY='AVENUE')
    before=normalize_collision_address(raw)
    assert census_street(before.primary_street)=='4TH AVE'
    assert normalize_collision_address(raw)==before and before.primary_street=='04TH AVE'


@pytest.mark.parametrize('name,street',[('DE LA REINA','CAM DE LA REINA'),('RUIZ','CAMINO RUIZ')])
def test_single_provider_pretype_is_preserved_and_compared(name,street):
    raw=dict(ADDRESS_NO_PRIMARY='2200',ADDRESS_ROAD_PRIMARY=street)
    address=normalize_collision_address(raw)
    match=dict(matchedAddress='2200 CAM '+name+', SAN DIEGO, CA, 92108',
        addressComponents=dict(preType='CAM',streetName=name,state='CA',city='SAN DIEGO'),
        coordinates=dict(x=-117.15,y=32.75))
    assert CensusGeocoder().parse_response(address,dict(result=dict(addressMatches=[match]))).status=='matched'
    match['addressComponents']['suffixDirection']='N'
    assert CensusGeocoder().parse_response(address,dict(result=dict(addressMatches=[match]))).status=='ambiguous'


def test_required_csv_fields_and_quoting_no_header():
    data=input_csv({'x':dict(street='100 MAIN, EAST ST')})
    assert list(csv.reader(io.StringIO(data.decode())))==[['x','100 MAIN, EAST ST','San Diego','CA','']]
    with pytest.raises(ValueError):input_csv({})
    with pytest.raises(ValueError):input_csv({str(i):dict(street='TEST') for i in range(10001)})
    with pytest.raises(ValueError):input_csv({'x':dict(street='X'*5_000_000)})


def test_reordered_output_maps_by_identifier_and_preserves_raw_diagnostics():
    results=parse_batch(response(matched('b'),matched('a')),requests(),batch_id='test')
    assert results['a'].details['matched_address'].startswith('800 4TH AVE,')
    assert results['b'].details['matched_address'].startswith('9400 CAM RUIZ,')
    assert results['a'].precision=='block_approximate'
    assert results['a'].details['address_range_available'] is False


@pytest.mark.parametrize('rows',[
    [matched('a'),matched('a')], [matched('a')],
    [matched('a'),matched('b',**{'0':'unknown'})],
    [matched('a',**{'1':'9400 CAM RUIZ, San Diego, CA,'}),matched('b')],
    [matched('a',**{'2':'Surprise'}),matched('b')],
    [matched('a')[:6],matched('b')],
    [matched('a',**{'5':'invalid'}),matched('b')]])
def test_untrusted_mapping_or_format_stops_before_results(rows):
    with pytest.raises(BatchProtocolError):parse_batch(response(*rows),requests())


@pytest.mark.parametrize('flag,status',[('No_Match','no_match'),('Tie','ambiguous'),
    ('Invalid','error'),('Error','error')])
def test_unmatched_tie_invalid_failed_keep_nulls(flag,status):
    results=parse_batch(response(['a',requests()['a']['street']+', San Diego, CA,',flag]),{'a':requests()['a']})
    r=results['a']
    assert r.status==status and r.longitude is None and r.latitude is None and r.precision is None


@pytest.mark.parametrize('matched_address,coordinates',[
    ('900 4TH AVE, SAN DIEGO, CA, 92101','-117.16,32.72'),
    ('800 4TH ST, SAN DIEGO, CA, 92101','-117.16,32.72'),
    ('800 N 4TH AVE, SAN DIEGO, CA, 92101','-117.16,32.72'),
    ('800 4TH AVE, LOS ANGELES, CA, 90001','-117.16,32.72'),
    ('800 4TH AVE, SAN DIEGO, CA, 92101','nan,32.72'),
    ('800 4TH AVE, SAN DIEGO, CA, 92101','-74,40.7')])
def test_rejected_matches_never_store_a_point(matched_address,coordinates):
    r=parse_batch(response(matched('a',**{'4':matched_address,'5':coordinates})),{'a':requests()['a']})['a']
    assert r.status=='ambiguous' and r.longitude is None and r.details['validation_rejected']


def test_retry_is_bounded_and_request_is_official_multipart():
    good=MagicMock()
    good.__enter__.return_value.read.return_value=b'CSV RESPONSE'
    events=[]
    with patch('backend.census_batch.urlopen',side_effect=[TimeoutError(),good]) as http, \
         patch('backend.census_batch.time.sleep') as sleep:
        assert CensusBatchClient(backoff=1).submit(requests(),event=events.append)=='CSV RESPONSE'
    assert http.call_count==2 and sleep.call_args.args==(1,)
    request=http.call_args.args[0]
    assert request.full_url.endswith('/locations/addressbatch')
    assert b'name="addressFile"' in request.data and b'name="benchmark"' in request.data
    assert [e['outcome'] for e in events]==['request_error','response']
    with patch('backend.census_batch.urlopen',side_effect=TimeoutError()) as http,patch('backend.census_batch.time.sleep'):
        with pytest.raises(ProviderUnavailable): CensusBatchClient(attempts=2).submit(requests())
        assert http.call_count==2


def test_permanent_http_failure_not_retried():
    with patch('backend.census_batch.urlopen',side_effect=HTTPError('https://example.test',400,'bad',{},None)) as http:
        with pytest.raises(ProviderUnavailable):CensusBatchClient().submit(requests())
        assert http.call_count==1


def test_atomic_chunk_writes_always_require_key_and_pending():
    conn=MagicMock()
    records=[dict(report_id='r1',address_key='old',request_id='a')]
    result=GeocodeResult('no_match','fixture')
    with patch('backend.collision_batch_geocoding.save_geocode',return_value=False) as save:
        assert apply_results(conn,records,{'a':result})==({},1)
        assert save.call_args.args[1:3]==('r1','old') and save.call_args.kwargs==dict(only_if_pending=True)
        conn.transaction.assert_called_once()


def test_real_database_guarded_writes_and_resume_are_idempotent(isolated_db):
    admin,_=isolated_db
    result=GeocodeResult('matched','fixture',-117.15,32.75,'block_approximate')
    with psycopg.connect(admin,autocommit=True) as conn:
        protected=['a1','a2','b1']
        before=fingerprint(conn,protected)
        wrong=[dict(report_id='unknown',address_key='obsolete',request_id='a')]
        assert apply_results(conn,wrong,{'a':result})==({},1)
        selected=[dict(report_id='unknown',address_key='synthetic',request_id='a')]
        assert apply_results(conn,selected,{'a':result})==({'matched':1},0)
        assert apply_results(conn,selected,{'a':replace(result,status='no_match',longitude=None,latitude=None,precision=None)})==({},1)
        assert fingerprint(conn,protected)==before
        assert conn.execute("SELECT geocode_status FROM citylens.traffic_collisions WHERE report_id='unknown'").fetchone()[0]=='matched'


def test_interrupted_commit_resumes_saved_response_without_second_request(tmp_path):
    record=dict(report_id='report',address_key='key',request_id='a')
    plan=dict(requests={'a':requests()['a']})
    state=dict(run_id='fixture',batches=[],requests=[])
    client=MagicMock(benchmark='Public_AR_Current')
    client.submit.return_value=response(matched('a'))
    with patch('backend.collision_batch_geocoding.apply_results',side_effect=RuntimeError('interrupted')):
        with pytest.raises(RuntimeError):process_batch(MagicMock(),tmp_path,state,plan,[record],'batch',{},client)
    assert (tmp_path/'batch/results.json').exists()
    assert not (tmp_path/'batch/committed.json').exists()
    with patch('backend.collision_batch_geocoding.apply_results',return_value=({},1)) as apply:
        process_batch(MagicMock(),tmp_path,state,plan,[record],'batch',{},client)
        assert apply.call_args.args[1]==[record]
    assert client.submit.call_count==1
    assert read(tmp_path/'batch/committed.json')['skipped_by_guard']==1


def test_bad_realistic_mapping_never_calls_persistence(tmp_path):
    record=dict(report_id='report',address_key='key',request_id='a')
    state=dict(run_id='fixture',batches=[],requests=[])
    client=MagicMock(benchmark='Public_AR_Current')
    client.submit.return_value=response(matched('a',**{'1':'900 WRONG ST, San Diego, CA,'}))
    with patch('backend.collision_batch_geocoding.apply_results') as apply:
        with pytest.raises(BatchProtocolError):
            process_batch(MagicMock(),tmp_path,state,dict(requests={'a':requests()['a']}),[record],'batch',{},client)
        apply.assert_not_called()
