"""Scope, user-location and presentation regressions; no live geocoding/model calls."""
import asyncio
import json
from unittest.mock import AsyncMock
import pytest
from pydantic_core import to_jsonable_python

from backend.agent.location_models import AddressProposal,ChatTurn,ConfirmationEdits
from backend.agent.models import Plan,TimeRange,QueryResponse
from backend.agent.scope_resolution import resolve_scope,explicit_scope
from backend.agent.settings import Settings
from backend.agent.workflow import Agent
from backend.agent.user_geocoding import AddressResolution
from backend.agent.user_locations import UserLocationResolver,IndependentPOISearch,analytical_candidate
from backend.agent.places import PlacesDiscovery
from backend.agent.presentation import build_presentation
from backend.agent_tests.helpers import FakeTools,request
from backend.agent_tests.test_location_flow import InterpretingModel,Geocoder,interpretation,proposal,follow
from backend.agent_tests.test_sessions import patch as followup_patch


def entity(text,kind='building_or_poi',id='A'):
    return AddressProposal(id=id,source_quote=text,location_text=text,entity_type=kind,
        house_number=None,street_name=None,street_suffix=None,direction=None,city='San Diego',state='CA')


def conventional(text='9237 Regents Rd',id='A'):
    number,name,suffix=text.split()
    return proposal(id,number,name,suffix,text)


def make_agent(locations=None,geocoder=None,plan=None):
    locations=locations or [conventional()]
    plan=plan or Plan(intent='location_summary' if len(locations)==1 else 'compare_locations',tools=['aggregate_traffic_collisions'])
    output=interpretation(locations,plan=plan.model_dump(),time_range=dict(start='2026-01-01T00:00:00-08:00',end='2026-03-01T00:00:00-08:00',calendar_timezone='America/Los_Angeles'))
    return Agent(InterpretingModel(output),FakeTools(),Settings(),geocoder=geocoder or Geocoder())


def test_explicit_full_year_and_radius_override_old_frontend_and_model_defaults():
    async def scenario():
        current=make_agent()
        reply=await current.chat(ChatTurn(message='Traffic collisions near 9237 Regents Rd in 2026 within 500 meters.',
            time_range=TimeRange(start='2026-01-01T00:00:00-08:00',end='2026-03-01T00:00:00-08:00'),radius_meters=800,
            confirmation_policy='conditional'))
        assert reply.status=='complete'
        call=current.tools.calls[0]
        assert call.time_range.start.isoformat()=='2026-01-01T00:00:00-08:00'
        assert call.time_range.end.isoformat()=='2027-01-01T00:00:00-08:00'
        assert call.radius_meters==500
        assert reply.result.presentation['time_range']==call.time_range.model_dump(mode='json')
        assert 'Dec 31, 2026' in reply.result.presentation['timeframe_label']
    asyncio.run(scenario())


def test_followup_explicit_scope_beats_inherited_scope_even_when_model_omits_patch():
    from backend.agent.sessions import ConversationRunner,apply_followup
    original=request();plan=Plan(intent='location_summary',tools=['aggregate_traffic_collisions'])
    context=ConversationRunner.context(original,plan)
    updated,_,_=apply_followup(context,'Use traffic collisions in 2026 within 500 meters.',followup_patch(plan),Settings())
    assert updated.time_range.end.year==2027 and updated.time_range.end.month==1
    assert updated.radius_meters==500 and updated.locations==original.locations


def test_scope_defaults_and_conflicts():
    from datetime import datetime
    interval,radius=resolve_scope('Count reports',system_defaults=True,reference_time=datetime(2026,11,15))
    assert interval.start.year==2026 and interval.end.year==2027 and radius==800
    assert explicit_scope('in 2026')[0].end.year==2027
    assert explicit_scope('within 0.5 km')[1]==500
    with pytest.raises(ValueError):explicit_scope('within 400 meters and within 800 meters')


@pytest.mark.parametrize('text',[
    'Collisions near 9237 Regents Rd from January to March in 2026.',
    'Collisions near 9237 Regents Rd for Q1 in 2026.',
    'Collisions near 9237 Regents Rd during the first half of 2026, i.e. in 2026 H1.',
    'Collisions near 9237 Regents Rd between 2026-02-01 and 2026-02-28 in 2026.',
    'Collisions near 9237 Regents Rd in the past six months, in 2026.',
    'Collisions near 9237 Regents Rd in 2026年3月.'])
def test_year_phrase_does_not_widen_a_finer_requested_interval(text):
    assert explicit_scope(text)[0] is None
    interval=TimeRange(start='2026-01-01T00:00:00-08:00',end='2026-04-01T00:00:00-07:00')
    assert resolve_scope(text,interval,800,system_defaults=True)[0]==interval


@pytest.mark.parametrize('text',[
    'Find traffic collisions near One Miramar Street Apartments in 2026.',
    'Compare 9237 Regents Rd and 3969 Miramar St in 2026 within 500 meters.',
    '2026年的交通事故'])
def test_whole_year_phrase_without_finer_interval_means_calendar_year(text):
    interval=explicit_scope(text)[0]
    assert interval.start.isoformat()=='2026-01-01T00:00:00-08:00'
    assert interval.end.isoformat()=='2027-01-01T00:00:00-08:00'


def test_finer_interval_end_to_end_keeps_model_interval_not_full_year():
    async def scenario():
        current=make_agent()
        reply=await current.chat(ChatTurn(message='Traffic collisions near 9237 Regents Rd from January to February in 2026.',
            confirmation_policy='conditional'))
        assert reply.status=='complete'
        assert current.tools.calls[0].time_range.end.isoformat()=='2026-03-01T00:00:00-08:00'
    asyncio.run(scenario())


def test_confirmed_current_scope_edits_have_highest_precedence():
    async def scenario():
        current=make_agent();pending=await current.chat(ChatTurn(message='Traffic collisions near 9237 Regents Rd in 2026.'))
        reply=await current.chat(follow(pending,confirmation_edits=ConfirmationEdits(start='2026-01-01',through='2026-01-31',radius_meters=400)))
        assert reply.status=='complete' and reply.radius_meters==400
        assert reply.time_range.start.month==1 and reply.time_range.end.month==2
        assert reply.confirmation_edits.through.isoformat()=='2026-01-31'
        assert current.tools.calls[0].time_range==reply.time_range
    asyncio.run(scenario())


@pytest.mark.parametrize('text,kind',[
    ('1 Miramar St','street_address'),('One Miramar Street','building_or_poi'),
    ('One Miramar Street Apartments','building_or_poi'),('Regents La Jolla','building_or_poi'),('9237 Regents Rd','street_address')])
def test_extracted_entities_preserve_original_expression_and_search_behavior(text,kind):
    class NoMatch:
        def __init__(self):self.calls=[]
        def resolve(self,address):self.calls.append(address);return AddressResolution('no_match',[])
    provider=NoMatch();location=conventional(text) if kind=='street_address' else entity(text)
    current=make_agent([location],provider)
    reply=asyncio.run(current.chat(ChatTurn(message='Find traffic collisions near '+text+' in 2026.',confirmation_policy='conditional')))
    assert reply.status=='geocoding_failed' and current.tools.calls==[]
    assert provider.calls[0].source_quote==text and provider.calls[0].entity_type==kind
    if kind=='building_or_poi':assert provider.calls[0].house_number is None
    # Extraction output is mocked here; this does not claim a live model match.


class StreetOnly:
    """Fake independent street geocoder (Census/SanGIS role)."""
    def __init__(self,status='resolved'):self.calls=[];self.status=status
    def resolve(self,address):
        self.calls.append(address)
        if self.status!='resolved':return AddressResolution(self.status,[],{'provider':'Fake street geocoder'})
        from backend.agent.location_models import GeocodedCandidate
        from backend.agent.models import Location
        return AddressResolution('resolved',[GeocodedCandidate(candidate_id='street_'+address.id,
            matched_address='1 MIRAMAR ST, San Diego, CA 92037',provider='SanGIS Address Points',precision='structure',
            location=Location(id=address.id,latitude=32.8466,longitude=-117.2553))],{'provider':'SanGIS Address Points'})


def named_place_agent(locations,street=None):
    street=street or StreetOnly()
    return make_agent(locations,UserLocationResolver(Settings(),address_resolver=street)),street


def test_spelled_building_number_is_not_forced_into_house_number():
    # The live model may label "One Miramar Street" a street address with house 1.
    wrong=proposal(number='1',name='Miramar',suffix='St',quote='One Miramar Street')
    current,street=named_place_agent([wrong])
    reply=asyncio.run(current.chat(ChatTurn(message='Traffic collisions near One Miramar Street in 2026.',confirmation_policy='conditional')))
    assert reply.status=='needs_candidate_confirmation' and not current.tools.calls
    assert reply.locations[0].entity_type=='building_or_poi' and reply.locations[0].house_number is None
    assert street.calls[0].house_number=='1' and street.calls[0].street_name=='Miramar'
    right=proposal(number='1',name='Main',suffix='St',quote='One Main Street')
    assert current.conversations.check_addresses([right],'Use the street address One Main Street.')


@pytest.mark.parametrize('text,expected',[
    ('One Miramar Street Apartments',('1','Miramar','Street',None)),
    ('One Miramar Street',('1','Miramar','Street',None)),
    ('Twenty-One Jump Street',('21','Jump','Street',None)),
    ('Five North Harbor Drive Tower',('5','Harbor','Drive','North')),
    ('Regents La Jolla',None),('One Paseo',None),('1 Miramar St',None)])
def test_spelled_address_interpretation_uses_only_user_text(text,expected):
    from backend.agent.user_locations import spelled_address_interpretation
    derived=spelled_address_interpretation(entity(text))
    if expected is None:
        assert derived is None
    else:
        assert (derived.house_number,derived.street_name,derived.street_suffix,derived.direction)==expected
        assert derived.source_quote==text and derived.entity_type=='street_address'


def test_building_name_street_reading_requires_explicit_confirmation_and_keeps_provenance():
    async def scenario():
        current,street=named_place_agent([entity('One Miramar Street Apartments')])
        pending=await current.chat(ChatTurn(message='Traffic collisions near One Miramar Street Apartments in 2026 within 300 meters.',
            confirmation_policy='conditional'))
        assert pending.status=='needs_candidate_confirmation' and not current.tools.calls
        candidate=pending.candidates['A'][0]
        assert candidate.requires_confirmation and candidate.metadata['derived_from']=='user_text'
        assert candidate.metadata['derived_address']=='1 Miramar Street'
        done=await current.chat(follow(pending,'choose_candidates',candidate_choices={'A':candidate.candidate_id}))
        assert done.status=='complete' and current.tools.calls[0].radius_meters==300
        meta=done.resolved_locations[0].metadata
        assert meta['user_confirmed'] is True and meta['original_text']=='One Miramar Street Apartments'
        assert done.resolved_locations[0].provider=='SanGIS Address Points'
    asyncio.run(scenario())


def test_named_place_without_any_independent_match_offers_fallbacks_not_fabrication():
    current,street=named_place_agent([entity('Regents La Jolla')])
    reply=asyncio.run(current.chat(ChatTurn(message='Traffic collisions near Regents La Jolla in 2026.',confirmation_policy='conditional')))
    assert reply.status=='geocoding_failed' and not reply.candidates and not current.tools.calls
    assert street.calls==[]  # no numbered reading exists for this name


def test_model_invented_components_for_named_place_are_discarded_not_fatal():
    invented=entity('One Miramar Street').model_copy(update={'street_name':'One Miramar','street_suffix':'Street'})
    current,_=named_place_agent([invented])
    reply=asyncio.run(current.chat(ChatTurn(message='Traffic collisions near One Miramar Street in 2026.',confirmation_policy='conditional')))
    assert reply.status=='needs_candidate_confirmation'
    assert reply.locations[0].street_name is None and reply.locations[0].location_text=='One Miramar Street'


@pytest.mark.parametrize('start,end,offsets',[
    ('2026-01-01T00:00:00','2027-01-01T00:00:00',('-08:00','-08:00')),
    ('2026-07-01T00:00:00','2026-08-01T00:00:00',('-07:00','-07:00')),
    ('2026-01-01T00:00:00-08:00','2026-03-01T00:00:00-08:00',('-08:00','-08:00'))])
def test_offsetless_model_timestamps_use_calendar_timezone(start,end,offsets):
    from backend.agent.location_models import ExtractedTimeRange
    value=ExtractedTimeRange(start=start,end=end,calendar_timezone='America/Los_Angeles')
    interval=TimeRange.model_validate(value.model_dump())
    assert (interval.start.isoformat()[-6:],interval.end.isoformat()[-6:])==offsets


def test_conditional_ambiguous_candidates_wait_for_selection():
    async def scenario():
        current=make_agent(geocoder=Geocoder('needs_confirmation'))
        pending=await current.chat(ChatTurn(message='Traffic collisions near 9237 Regents Rd in 2026.',confirmation_policy='conditional'))
        assert pending.status=='needs_candidate_confirmation' and not current.tools.calls
        done=await current.chat(follow(pending,'choose_candidates',candidate_choices={'A':pending.candidates['A'][0].candidate_id}))
        assert done.status=='complete' and done.resolved_locations[0].metadata['user_confirmed'] is True
    asyncio.run(scenario())


def test_user_coordinates_are_parsed_from_user_quote_without_a_geocoder():
    location=entity('32.84, -117.25','coordinates')
    current=make_agent([location],UserLocationResolver(Settings()))
    reply=asyncio.run(current.chat(ChatTurn(message='Traffic collisions near 32.84, -117.25 in 2026.',confirmation_policy='conditional')))
    assert reply.status=='complete'
    point=reply.resolved_locations[0]
    assert point.location.latitude==32.84 and point.provider=='User coordinates'
    assert point.metadata['coordinate_source']=='user_coordinates'


def test_no_match_manual_fallback_keeps_provenance_and_scope():
    class Missing:
        def resolve(self,address):return AddressResolution('no_match',[])
    async def scenario():
        current=make_agent([entity('One Miramar Street Apartments')],Missing())
        pending=await current.chat(ChatTurn(message='Traffic collisions near One Miramar Street Apartments in 2026 within 500 meters.',confirmation_policy='conditional'))
        assert pending.status=='geocoding_failed' and not current.tools.calls
        selected=await current.chat(follow(pending,'select_manual_locations',manual_points={'A':dict(latitude=32.84,longitude=-117.25,source='manual_open_map',confirmed=True)}))
        assert selected.status=='complete' and selected.radius_meters==500
        assert selected.time_range.end.year==2027
        assert selected.resolved_locations[0].precision=='user_selected'
        assert selected.resolved_locations[0].metadata['original_text']=='One Miramar Street Apartments'
        assert current.tools.calls[0].location.latitude==32.84
    asyncio.run(scenario())


@pytest.mark.parametrize('updates',[{'source':'google_maps'},{'confirmed':False},{'latitude':float('nan')}])
def test_manual_input_cannot_claim_google_or_unconfirmed_coordinates(updates):
    from backend.agent.location_models import ManualPoint
    with pytest.raises(ValueError):ManualPoint(**{**dict(latitude=32.84,longitude=-117.25,source='manual_open_map',confirmed=True),**updates})


def test_google_discovery_coordinates_and_addresses_never_enter_saved_state(monkeypatch):
    from backend.agent import places
    source=PlacesDiscovery();monkeypatch.setattr(places,'discovery',source)
    results=source.present({'places':[{'id':'ChIJ_example','displayName':{'text':'GOOGLE_ONLY_NAME'},
        'formattedAddress':'GOOGLE_ONLY_ADDRESS','location':{'latitude':1,'longitude':2}}]},'One Miramar Street Apartments')
    assert 'location' not in results['candidates'][0]
    assert not source.validate_choice(results['search_token'],'ChIJ_example','different original query')
    class Missing:
        def resolve(self,address):return AddressResolution('no_match',[])
    async def scenario():
        current=make_agent([entity('One Miramar Street Apartments')],Missing())
        pending=await current.chat(ChatTurn(message='Traffic collisions near One Miramar Street Apartments in 2026.',confirmation_policy='conditional'))
        selected=await current.chat(follow(pending,'choose_place',place_choices={'A':dict(place_id='ChIJ_example',search_token=results['search_token'])}))
        assert selected.status=='geocoding_failed' and not current.tools.calls
        state=current.conversations.pending[selected.conversation_id]
        serialized=json.dumps(to_jsonable_python(state,exclude={'lock','updated'}))
        assert 'ChIJ_example' in serialized
        assert 'GOOGLE_ONLY_ADDRESS' not in serialized and 'GOOGLE_ONLY_NAME' not in serialized
    asyncio.run(scenario())


def test_independent_poi_source_is_optional_and_always_requires_confirmation():
    address=entity('Regents La Jolla')
    assert IndependentPOISearch(Settings()).resolve(address).status=='no_match'
    source=IndependentPOISearch(Settings(independent_poi_attribution='Test independent source'))
    result=source.parse(address,[dict(lat='32.84',lon='-117.25',display_name='Independent source name',osm_type='way',osm_id=1)])
    assert result.status=='needs_confirmation' and result.candidates[0].requires_confirmation
    assert result.candidates[0].metadata['coordinate_source']=='independent_poi'
    assert analytical_candidate(result.candidates[0])


def test_presentation_counts_differences_and_requested_dates_match_evidence():
    current=make_agent([conventional(),conventional('3969 Miramar St','B')])
    reply=asyncio.run(current.chat(ChatTurn(message='Compare traffic collisions near 9237 Regents Rd and 3969 Miramar St in 2026.',confirmation_policy='conditional')))
    result=reply.result;view=result.presentation
    assert view['metrics'][0]['value']==result.evidence[0].value
    assert view['metrics'][1]['value']==result.evidence[1].value
    assert result.comparison.metrics[0].difference_b_minus_a==4
    assert '4 more' in view['summary'] and 'evidence_' not in view['summary']
    assert view['time_range']['end'].startswith('2027-01-01')
    assert view['spatial_scope']['radius_meters']==current.tools.calls[0].radius_meters
    assert view['metrics'][0]['evidence_ids']==[result.evidence[0].evidence_id]
    assert 'safety' in view['interpretation']


def test_failed_evidence_cannot_generate_kpi_zero_or_comparison_conclusion():
    current=Agent(InterpretingModel(),FakeTools(failed='aggregate_traffic_collisions'),Settings())
    result=asyncio.run(current.query(request()))
    view=build_presentation(result,request())
    assert all(m.value is None and not m.available for m in view.metrics)
    assert not re_has_count(view.summary)


def re_has_count(text):
    import re
    return bool(re.search(r'\b\d+\b',text))


def test_process_local_followup_preserves_scope_when_persistence_is_unconfigured():
    async def scenario():
        current=make_agent()
        first=await current.chat(ChatTurn(message='Traffic collisions near 9237 Regents Rd in 2026 within 500 meters.',confirmation_policy='conditional'))
        plan=Plan(intent='location_summary',tools=['aggregate_traffic_collisions'])
        current.model.followup=AsyncMock(return_value=followup_patch(plan))
        second=await current.chat(ChatTurn(message='Run that again.',conversation_id=first.conversation_id,confirmation_policy='conditional'))
        assert second.status=='complete' and len(current.tools.calls)==2
        assert current.tools.calls[0]==current.tools.calls[1]
        assert second.time_range.end.year==2027 and second.radius_meters==500
    asyncio.run(scenario())
