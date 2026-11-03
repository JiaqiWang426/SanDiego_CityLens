"""Deterministic provider/fallback tests; no external calls or database writes."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import MagicMock,patch
from urllib.error import HTTPError

import pytest

from backend.agent.geocoding_http import get_json,GeocodingRequestError
from backend.agent.location_models import ChatTurn,GeocodedCandidate
from backend.agent.models import Location
from backend.agent.user_geocoding import AddressResolution
from backend.agent.user_geocoding import interactive_street,UserAddressGeocoder
from backend.agent.sangis_geocoding import SanGISAddressGeocoder,FallbackAddressGeocoder,query_parameters
from backend.agent_tests.test_location_flow import agent,proposal,follow,QUESTION,Geocoder


def address():return proposal(number='9500',name='Gilman',quote='9500 Gilman Dr').model_copy(update={'postal_code':'92093','city':'La Jolla'})


def feature(id=1,x=-117.23618,y=32.87728,**attributes):
    fields=dict(objectid=id,addrnmbr=9500,addrname='GILMAN',addrsfx='DR',addrpdir=None,
        addrpostd=None,addrfrac=None,addrunit=None,addrzip='92093',community='La Jolla',
        addrjur='SD',state='CA',placement_location='S',asource='S',apn='test-apn',roadsegid=50)
    fields.update(attributes)
    return dict(attributes=fields,geometry=dict(x=x,y=y))


def payload(*features):return dict(spatialReference=dict(wkid=4326),features=list(features))


def parse(*features):return SanGISAddressGeocoder().parse_response(address(),payload(*features))


def test_unique_situs_point_has_valid_longitude_latitude_and_source_metadata():
    result=parse(feature())
    assert result.status=='resolved'
    c=result.candidates[0]
    assert c.location.longitude==-117.23618 and c.location.latitude==32.87728
    assert c.provider=='SanGIS Address Points' and c.precision=='structure'
    assert c.metadata['source_features'][0]['roadsegid']==50
    assert c.metadata['source_features'][0]['apn']=='test-apn'
    assert c.metadata['asource']==['S'] and c.metadata['placement_location']==['S']
    assert 'SITUS' in c.metadata['precision_basis']


def test_empty_is_no_match_but_malformed_and_arcgis_errors_are_not():
    adapter=SanGISAddressGeocoder()
    assert adapter.parse_response(address(),payload()).status=='no_match'
    assert adapter.parse_response(address(),dict(error=dict(code=503,message='unavailable'))).status=='provider_error'
    assert adapter.parse_response(address(),dict(spatialReference=dict(wkid=4326),features=None)).status=='invalid_or_out_of_scope'
    assert adapter.parse_response(address(),[]).status=='invalid_or_out_of_scope'


def test_distinct_locations_ambiguous_and_exact_coincident_duplicates_aggregate():
    result=parse(feature(1),feature(2,x=-117.24))
    assert result.status=='needs_confirmation' and len(result.candidates)==2
    assert all(c.requires_confirmation for c in result.candidates)
    duplicate=parse(feature(1),feature(2,apn='different-apn'))
    assert duplicate.status=='resolved' and len(duplicate.candidates)==1
    assert duplicate.candidates[0].metadata['feature_count']==2
    assert len(duplicate.candidates[0].metadata['source_features'])==2


@pytest.mark.parametrize('changes',[
    dict(addrnmbr=9400),dict(addrname='REGENTS'),dict(addrsfx='ST'),dict(addrpdir='N'),dict(addrpostd='S'),
    dict(addrzip='92037'),dict(addrzip=None),dict(addrjur='VS'),dict(state='AZ'),dict(addrfrac='1/2')])
def test_conflicting_components_are_not_silently_accepted(changes):
    result=parse(feature(**changes))
    assert result.status=='invalid_or_out_of_scope' and not result.candidates
    assert result.metadata['rejections']


@pytest.mark.parametrize('geometry',[None,{},'invalid',dict(x=None,y=32.8),dict(x=-117.2,y=float('nan')),
    dict(x=float('inf'),y=32.8),dict(x=32.8,y=-117.2),dict(x=True,y=32.8),dict(x=-74,y=40.7)])
def test_missing_invalid_reversed_out_of_scope_geometry_fails_closed(geometry):
    f=feature();f['geometry']=geometry
    result=parse(f)
    assert result.status=='invalid_or_out_of_scope' and not result.candidates


def test_unconfirmed_crs_and_truncated_candidates_are_invalid():
    adapter=SanGISAddressGeocoder()
    data=payload(feature());data['spatialReference']['wkid']=2230
    assert adapter.parse_response(address(),data).status=='invalid_or_out_of_scope'
    data=payload(feature());data['exceededTransferLimit']=True
    assert adapter.parse_response(address(),data).status=='invalid_or_out_of_scope'


@pytest.mark.parametrize('data',[
    dict(spatialReference='invalid',features=[]),payload(None),payload('invalid'),
    payload(dict(attributes='invalid',geometry={})),
])
def test_malformed_sangis_nested_fields_fail_closed(data):
    result=SanGISAddressGeocoder().parse_response(address(),data)
    assert result.status=='invalid_or_out_of_scope' and not result.candidates


@pytest.mark.parametrize('matches',[[None],[dict(addressComponents='invalid',coordinates={})]])
def test_malformed_census_candidates_do_not_trigger_fallback(matches):
    result=UserAddressGeocoder().parse_response(address(),dict(result=dict(addressMatches=matches)))
    assert result.status=='invalid_or_out_of_scope' and not result.candidates


@pytest.mark.parametrize('code,precision',[('S','structure'),('E','entrance'),('C','parcel_centroid'),
    ('R','review_required'),('U','unknown'),('OTHER','unknown')])
def test_placement_is_preserved_without_rooftop_claims(code,precision):
    result=parse(feature(placement_location=code,asource='M' if code=='C' else 'S'))
    assert result.status=='resolved' and result.candidates[0].precision==precision
    assert result.candidates[0].metadata['placement_location']==[code]


def test_units_and_source_placement_conflicts_require_review():
    assert parse(feature(addrunit='100')).status=='needs_confirmation'
    c=parse(feature(asource='M',placement_location='S')).candidates[0]
    assert c.precision=='unknown' and c.requires_confirmation and c.metadata['placement_source_conflict']


def test_query_is_structured_escaped_bounded_and_requests_wgs84():
    params=query_parameters(address())
    assert 'addrnmbr=9500' in params['where'] and "'GILMAN'" in params['where']
    assert params['outSR']==4326 and params['resultRecordCount']==200
    params=query_parameters(address().model_copy(update={'street_name':"O'HARE"}))
    assert "'O''HARE'" in params['where']


@pytest.mark.parametrize('status',['resolved','needs_confirmation','provider_error','invalid_or_out_of_scope'])
def test_census_success_ambiguity_or_errors_do_not_fall_through(status):
    primary=MagicMock();fallback=MagicMock()
    primary.resolve.return_value=AddressResolution(status,[])
    result=FallbackAddressGeocoder(primary,fallback).resolve(address())
    assert result.status==status
    fallback.resolve.assert_not_called()


@pytest.mark.parametrize('status',['resolved','no_match','needs_confirmation','provider_error'])
def test_only_census_no_match_invokes_sangis_and_keeps_its_status(status):
    primary=MagicMock();fallback=MagicMock()
    primary.resolve.return_value=AddressResolution('no_match',[])
    fallback.resolve.return_value=AddressResolution(status,[])
    result=FallbackAddressGeocoder(primary,fallback).resolve(address())
    assert result.status==status and len(result.metadata['provider_attempts'])==2
    fallback.resolve.assert_called_once()


def test_transient_timeout_is_bounded_and_http_failure_remains_provider_error():
    good=MagicMock();good.__enter__.return_value.read.return_value=json.dumps(payload(feature())).encode()
    with patch('backend.agent.geocoding_http.urlopen',side_effect=[TimeoutError(),good]) as http,patch('backend.agent.geocoding_http.time.sleep'):
        data,info=get_json('https://example.test',attempts=2)
        assert info['http_attempts']==2 and http.call_count==2 and data['features']
    with patch('backend.agent.geocoding_http.urlopen',side_effect=HTTPError('https://example.test',403,'blocked',{},None)) as http:
        result=SanGISAddressGeocoder().resolve(address())
        assert result.status=='provider_error' and http.call_count==1
    with patch('backend.agent.geocoding_http.urlopen',side_effect=TimeoutError()) as http,patch('backend.agent.geocoding_http.time.sleep'):
        with pytest.raises(GeocodingRequestError):get_json('https://example.test',attempts=2)
        assert http.call_count==2


def test_mixed_provider_locations_share_filters_and_wait_for_every_resolution():
    async def run():
        class Primary(Geocoder):
            def resolve(self,a):return super().resolve(a) if a.id=='A' else AddressResolution('no_match',[])
        class Fallback(Geocoder):
            def resolve(self,a):
                r=super().resolve(a)
                for c in r.candidates:c.provider='SanGIS Address Points';c.precision='structure'
                return r
        primary,fallback=Primary(),Fallback()
        a=agent(geocoder=FallbackAddressGeocoder(primary,fallback))
        pending=await a.chat(ChatTurn(message=QUESTION))
        assert not a.tools.calls and not primary.calls and not fallback.calls
        final=await a.chat(follow(pending))
        assert final.status=='complete' and len(a.tools.calls)==2
        assert [c.provider for c in final.resolved_locations]==['US Census Geocoder','SanGIS Address Points']
        first,second=a.tools.calls
        assert first.model_dump(exclude={'location'})==second.model_dump(exclude={'location'})
        failed=agent(geocoder=FallbackAddressGeocoder(Primary(),Geocoder('no_match')))
        p=await failed.chat(ChatTurn(message=QUESTION))
        assert (await failed.chat(follow(p))).status=='geocoding_failed' and not failed.tools.calls
    asyncio.run(run())


@pytest.mark.parametrize('left,right',[('CALLE MAR DE MARIPOSA','CLL MAR DE MARIPOSA'),
    ('VISTA SORRENTO PKY','VISTA SORRENTO PKWY'),('PARK PLAZA','PARK PLZ'),('PARK PLZA','PARK PLZ')])
def test_documented_exact_usps_aliases(left,right):
    assert interactive_street(left)==interactive_street(right)
    assert interactive_street('N '+left)!=interactive_street('S '+right)
    assert interactive_street('PARK PLZ')!=interactive_street('PARK BLVD')


def test_census_calle_prefix_alias_does_not_trigger_unnecessary_fallback():
    a=address().model_copy(update={'house_number':'10615','street_name':'Calle Mar De Mariposa','street_suffix':None,'postal_code':'92130'})
    raw=dict(result=dict(addressMatches=[dict(matchedAddress='10615 CLL MAR DE MARIPOSA, SAN DIEGO, CA, 92130',
        addressComponents=dict(preType='CLL',streetName='MAR DE MARIPOSA',city='SAN DIEGO',state='CA',zip='92130'),
        coordinates=dict(x=-117.2,y=32.9))]))
    assert UserAddressGeocoder().parse_response(a,raw).status=='resolved'


def test_census_error_body_is_not_a_legitimate_no_match():
    assert UserAddressGeocoder().parse_response(address(),dict(error='unavailable')).status=='provider_error'


def test_post_direction_and_zip_are_preserved_and_not_invented():
    a=address().model_copy(update={'street_name':'Scholars','house_number':'9625','post_direction':'N'})
    f=feature(addrnmbr=9625,addrname='SCHOLARS',addrpostd='N')
    assert SanGISAddressGeocoder().parse_response(a,payload(f)).status=='resolved'
    assert SanGISAddressGeocoder().parse_response(a,payload(feature(addrnmbr=9625,addrname='SCHOLARS',addrpostd='S'))).status=='invalid_or_out_of_scope'
    controller=agent().conversations
    quoted=proposal(number='9625',name='Scholars',quote='9625 Scholars Drive North').model_copy(update={'post_direction':'N','postal_code':'92093'})
    assert controller.check_addresses([quoted],'9625 Scholars Drive North, La Jolla, CA 92093')
    with pytest.raises(ValueError):controller.check_addresses([quoted],'9625 Scholars Drive North')
