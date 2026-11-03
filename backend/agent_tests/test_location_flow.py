"""Focused P1.2 tests: external models/geocoder mocked; SQL smoke is read-only."""
import asyncio
from copy import deepcopy
from unittest.mock import MagicMock
from unittest.mock import AsyncMock

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from backend.agent.location_models import (AddressProposal,AddressCorrectionOutput,ChatTurn,
    GeocodedCandidate,InterpretationOutput)
from backend.agent.user_geocoding import AddressResolution,UserAddressGeocoder
from backend.agent.workflow import Agent
from backend.agent.settings import Settings
from backend.agent.models import Plan,Location,Composition
from backend.agent.tools import ToolResult
from backend.agent.evidence import verify_claims
from backend.agent_tests.helpers import FakeModel,FakeTools
from backend.main import app,get_agent
from backend.agent.llm import OpenAIModel,ModelNotConfigured


QUESTION='Compare Pothole complaints near 9000 Regents Driev and 9500 Gilman Drive within 800 meters over the past six months.'
START='2026-05-02T00:00:00-07:00'
END='2026-11-02T00:00:00-08:00'


def proposal(id='A',number='9000',name='Regents',suffix='Dr',quote='9000 Regents Driev'):
    return AddressProposal(id=id,source_quote=quote,house_number=number,street_name=name,
        street_suffix=suffix,direction=None,city='San Diego',state='CA')


def interpretation(locations=None,**updates):
    plan=Plan(intent='compare_locations',tools=['aggregate_service_requests'],filter_quote='Pothole',
              service_request_aggregation=dict(service_name='Pothole',group_by='status'))
    payload=dict(locations=[a.model_dump() for a in (locations or [proposal(),proposal('B','9500','Gilman','Dr','9500 Gilman Drive')])],
        time_range=dict(start=START,end=END,calendar_timezone='America/Los_Angeles'),radius_meters=800,
        plan=plan.model_dump(),clarification=None,unsupported_filters=[])
    payload.update(updates)
    return InterpretationOutput.model_validate(payload)


class InterpretingModel(FakeModel):
    def __init__(self,output=None):
        super().__init__()
        self.output=output or interpretation()
        self.interpretations=0
        self.corrections=0
        self.correction_output=None
    async def interpret_locations(self,question,defaults,reference_time):
        self.interpretations+=1
        self.reference_time=reference_time
        return self.output
    async def correct_locations(self,message,current):
        self.corrections+=1
        return self.correction_output


class Groups(FakeTools):
    def execute(self,call):
        self.calls.append(call)
        return ToolResult(call,rows=[dict(service_name='POTHOLE',status='NEW',request_id_count=7 if call.location.id=='A' else 11)],
                          metadata=dict(dataset_records=100,dataset_located_records=100))


class Geocoder:
    def __init__(self,status='resolved'):
        self.calls=[]
        self.status=status
    def resolve(self,address):
        self.calls.append(address)
        candidate=GeocodedCandidate(candidate_id='candidate_'+address.id,matched_address=address.display,
            location=Location(id=address.id,label=address.display,latitude=32.84 if address.id=='A' else 32.85,longitude=-117.25),
            requires_confirmation=self.status=='needs_confirmation')
        return AddressResolution(self.status,[candidate] if self.status in {'resolved','needs_confirmation'} else [])


def agent(output=None,geocoder=None):
    return Agent(InterpretingModel(output),Groups(),Settings(),geocoder=geocoder or Geocoder())


def follow(reply,action='confirm',**kwargs):
    return ChatTurn(action=action,conversation_id=reply.conversation_id,revision=reply.revision,**kwargs)


def test_confirmation_gate_and_symmetric_filters_evidence_and_idempotence():
    async def run():
        a=agent()
        pending=await a.chat(ChatTurn(message=QUESTION))
        assert pending.status=='needs_confirmation' and a.tools.calls==[] and a.conversations.geocoder.calls==[]
        assert pending.time_range.start.isoformat()==START and pending.original_question==QUESTION
        result=await a.chat(follow(pending))
        assert result.status=='complete' and result.result.status=='partial'
        assert a.model.plans==0 and a.model.interpretations==1
        assert len(a.tools.calls)==2 and len(a.conversations.geocoder.calls)==2
        x,y=a.tools.calls
        assert x.model_dump(exclude={'location'})==y.model_dump(exclude={'location'})
        assert x.service_request_aggregation.service_name=='Pothole'
        assert result.result.comparison.metrics[0].difference_b_minus_a==4
        assert 'service_name=Pothole' in result.result.answer and 'Regents' in result.result.answer
        verify_claims(result.result.claims,result.result.evidence)
        repeat=await a.chat(follow(pending))
        assert repeat.result==result.result and len(a.tools.calls)==2
    asyncio.run(run())


@pytest.mark.parametrize('text,source,suffix',[
    ('How many Pothole complaints near 9000 Regents Drive?','9000 Regents Drive','Dr'),
    ('How many Pothole complaints near 9000 Regents Dr?','9000 Regents Dr','Dr'),
    ('Please count Pothole complaints around 9000 Regents Driev, thank you.','9000 Regents Driev','Dr'),
    ('Pothole reports near 9000   regents DRIVE please','9000   regents DRIVE','Dr')])
def test_proposed_input_patterns_are_structured_and_require_confirmation(text,source,suffix):
    output=interpretation([proposal(quote=source,suffix=suffix)],plan=Plan(intent='request_count',tools=['aggregate_service_requests'],
        service_request_aggregation=dict(service_name='Pothole'),filter_quote='Pothole').model_dump())
    a=agent(output)
    pending=asyncio.run(a.chat(ChatTurn(message=text)))
    assert pending.status=='needs_confirmation' and '9000 Regents Dr' in pending.locations[0].display
    assert not a.tools.calls and not a.conversations.geocoder.calls


def test_correction_requires_new_confirmation_and_preserves_analytical_scope():
    async def run():
        a=agent()
        p=await a.chat(ChatTurn(message=QUESTION))
        changed=proposal('B','9600','Gilman','Dr','9600 Gilman Dr')
        a.model.correction_output=AddressCorrectionOutput(locations=[proposal(),changed],clarification=None)
        revised=await a.chat(follow(p,'correct',message='A is correct, but B should be 9600 Gilman Dr.'))
        assert revised.status=='needs_confirmation' and revised.revision>p.revision
        assert revised.time_range==p.time_range and revised.proposed_plan==p.proposed_plan and revised.radius_meters==800
        assert revised.original_question==QUESTION and not a.tools.calls and not a.conversations.geocoder.calls
        with pytest.raises(HTTPException): await a.chat(follow(p))
        final=await a.chat(follow(revised))
        assert final.status=='complete' and a.conversations.geocoder.calls[1].house_number=='9600'
        assert a.model.interpretations==1 and a.model.corrections==1
    asyncio.run(run())


def test_cancel_and_unbound_yes_never_geocode_or_query():
    async def run():
        a=agent()
        with pytest.raises(HTTPException): await a.chat(ChatTurn(action='confirm',message='yes',revision=1))
        p=await a.chat(ChatTurn(message=QUESTION))
        cancelled=await a.chat(follow(p,'cancel'))
        assert cancelled.status=='cancelled' and (await a.chat(follow(p))).status=='cancelled'
        assert not a.tools.calls and not a.conversations.geocoder.calls
    asyncio.run(run())


@pytest.mark.parametrize('status',['no_match','provider_error','invalid_or_out_of_scope'])
def test_geocoder_failures_do_not_execute_analysis(status):
    async def run():
        a=agent(geocoder=Geocoder(status))
        p=await a.chat(ChatTurn(message=QUESTION))
        failed=await a.chat(follow(p))
        assert failed.status=='geocoding_failed' and not a.tools.calls and failed.result is None
    asyncio.run(run())


def test_ambiguous_candidates_need_explicit_selection_for_every_location():
    async def run():
        a=agent(geocoder=Geocoder('needs_confirmation'))
        p=await a.chat(ChatTurn(message=QUESTION))
        choices=await a.chat(follow(p))
        assert choices.status=='needs_candidate_confirmation' and not a.tools.calls
        with pytest.raises(HTTPException):await a.chat(follow(choices,'choose_candidates',candidate_choices={'A':'candidate_A'}))
        with pytest.raises(HTTPException):await a.chat(follow(choices,'choose_candidates',candidate_choices={'A':'wrong','B':'candidate_B'}))
        final=await a.chat(follow(choices,'choose_candidates',candidate_choices={'A':'candidate_A','B':'candidate_B'}))
        assert final.status=='complete' and len(a.tools.calls)==2
    asyncio.run(run())


@pytest.mark.parametrize('output',[interpretation([proposal(number=None)]),
    interpretation(clarification='Which Regents street do you mean?'),
    interpretation(unsupported_filters=['freeway distance']),
    interpretation([proposal(number='9999')])])
def test_missing_ambiguous_invented_or_unsupported_inputs_need_clarification(output):
    async def run():
        a=agent(output)
        response=await a.chat(ChatTurn(message=QUESTION))
        assert response.status=='needs_clarification' and not a.tools.calls and not a.conversations.geocoder.calls
        with pytest.raises(HTTPException):await a.chat(follow(response))
    asyncio.run(run())


def test_new_endpoint_keeps_confirmation_server_side():
    a=agent()
    app.dependency_overrides[get_agent]=lambda:a
    try:
        with TestClient(app) as client:
            pending=client.post('/api/v1/agent/chat',json={'message':QUESTION}).json()
            assert pending['status']=='needs_confirmation' and not a.tools.calls
            assert client.post('/api/v1/agent/chat',json={'action':'confirm'}).status_code==409
            final=client.post('/api/v1/agent/chat',json={'action':'confirm','conversation_id':pending['conversation_id'],
                'revision':pending['revision']}).json()
            assert final['status']=='complete' and len(final['result']['tool_calls'])==2
    finally: app.dependency_overrides.clear()


def census_payload(number='9000',name='REGENTS',direction='',longitude=-117.25,multiple=False):
    match=dict(matchedAddress=f'{number} {direction+" " if direction else ""}{name} DR, SAN DIEGO, CA, 92122',
        addressComponents=dict(streetName=name,suffixType='DR',preDirection=direction,city='SAN DIEGO',state='CA'),
        coordinates=dict(x=longitude,y=32.84))
    return dict(result=dict(input=dict(benchmark=dict(benchmarkName='Public_AR_Current')),
        addressMatches=[match,deepcopy(match)] if multiple else [match]))


@pytest.mark.parametrize('changes,status',[({},'resolved'),({'name':'GILMAN'},'needs_confirmation'),
    ({'number':'9500'},'needs_confirmation'),({'direction':'N'},'needs_confirmation'),
    ({'multiple':True},'needs_confirmation'),({'longitude':-74},'invalid_or_out_of_scope')])
def test_interactive_census_validation_and_precision(changes,status):
    payload=census_payload(**changes)
    provider=MagicMock()
    provider.geocode.return_value=MagicMock(status='ambiguous',details=dict(response=payload))
    result=UserAddressGeocoder(provider).resolve(proposal())
    assert result.status==status
    if result.candidates:
        assert all(c.precision=='interpolated_address' for c in result.candidates)
        assert all(c.requires_confirmation for c in result.candidates)==(status=='needs_confirmation')


@pytest.mark.parametrize('change',[dict(street_name='Imagined'),dict(direction='N'),dict(street_suffix=None)])
def test_missing_or_invented_components_cannot_be_silently_added_or_dropped(change):
    async def run():
        invalid=proposal().model_copy(update=change)
        a=agent(interpretation([invalid]))
        reply=await a.chat(ChatTurn(message=QUESTION))
        assert reply.status=='needs_clarification' and not a.tools.calls and not a.conversations.geocoder.calls
    asyncio.run(run())


def test_missing_time_expiry_and_stale_confirmation_do_not_query():
    async def run():
        a=agent(interpretation(time_range=None))
        p=await a.chat(ChatTurn(message=QUESTION))
        assert p.status=='needs_clarification'
        a.conversations.pending[p.conversation_id].updated-=2000
        with pytest.raises(HTTPException):await a.chat(follow(p))
        assert not a.tools.calls
    asyncio.run(run())


def test_parallel_confirmation_does_not_duplicate_geocoding_or_sql():
    async def run():
        a=agent()
        pending=await a.chat(ChatTurn(message=QUESTION))
        results=await asyncio.gather(a.chat(follow(pending)),a.chat(follow(pending)))
        assert all(r.status=='complete' for r in results)
        assert len(a.tools.calls)==2 and len(a.conversations.geocoder.calls)==2
    asyncio.run(run())


@pytest.mark.parametrize('exc,code',[(ModelNotConfigured(),'openai_not_configured'),
                                  (TimeoutError(),'location_interpretation_timeout')])
def test_interpretation_failures_are_classified_without_queries(exc,code):
    a=agent()
    a.model.interpret_locations=AsyncMock(side_effect=exc)
    response=asyncio.run(a.chat(ChatTurn(message=QUESTION)))
    assert code in response.diagnostic_codes and not a.tools.calls and not a.conversations.geocoder.calls


def test_existing_openai_parser_uses_required_structured_schema_without_coordinates():
    async def run():
        model=OpenAIModel(Settings())
        model._parse=AsyncMock(return_value=interpretation())
        output=await model.interpret_locations(QUESTION,{},'2026-11-02T00:00:00-08:00')
        assert output.locations[0].street_suffix=='Dr'
        prompt,content,schema=model._parse.call_args.args
        assert 'Driev' in prompt and QUESTION in content
        assert schema is InterpretationOutput
        properties=schema.model_json_schema()['$defs']['AddressProposal']['properties']
        assert 'latitude' not in properties and 'longitude' not in properties
    asyncio.run(run())


def test_incomplete_address_can_be_completed_then_explicitly_confirmed():
    async def run():
        output=interpretation([proposal(),proposal('B',None,'Gilman','Dr','Gilman Drive')])
        a=agent(output)
        pending=await a.chat(ChatTurn(message=QUESTION.replace('9500 Gilman Drive','Gilman Drive')))
        assert pending.status=='needs_clarification'
        a.model.correction_output=AddressCorrectionOutput(locations=[proposal(),proposal('B','9500','Gilman','Dr','9500 Gilman Dr')],clarification=None)
        corrected=await a.chat(follow(pending,'clarify',message='B should be 9500 Gilman Dr.'))
        assert corrected.status=='needs_confirmation' and not a.tools.calls
        assert corrected.time_range==pending.time_range and corrected.proposed_plan==pending.proposed_plan
        assert a.model.interpretations==1 and a.model.corrections==1
        assert (await a.chat(follow(corrected))).status=='complete'
    asyncio.run(run())


def test_materially_different_census_address_is_confirmed_before_its_pin_is_used():
    async def run():
        output=interpretation([proposal()],plan=Plan(intent='request_count',tools=['aggregate_service_requests'],
            filter_quote='Pothole',service_request_aggregation=dict(service_name='Pothole')).model_dump())
        provider=MagicMock()
        provider.geocode.return_value=MagicMock(status='ambiguous',details=dict(response=census_payload(name='GILMAN')))
        a=agent(output,UserAddressGeocoder(provider))
        p=await a.chat(ChatTurn(message=QUESTION))
        candidate_reply=await a.chat(follow(p))
        assert candidate_reply.status=='needs_candidate_confirmation' and not a.tools.calls
        c=candidate_reply.candidates['A'][0]
        assert 'GILMAN' in c.matched_address and c.requires_confirmation
        done=await a.chat(follow(candidate_reply,'choose_candidates',candidate_choices={'A':c.candidate_id}))
        assert done.status=='complete' and 'GILMAN' in a.tools.calls[0].location.label
    asyncio.run(run())


def test_unrelated_yes_does_not_acquire_addresses_or_execute_tools():
    a=agent()
    response=asyncio.run(a.chat(ChatTurn(message='yes')))
    assert response.status=='needs_clarification' and not a.tools.calls and not a.conversations.geocoder.calls


def test_shared_prefix_normalization_does_not_change_source_name_or_invent_suffix():
    a=agent()
    proposed=proposal(number='9400',name='CAM RUIZ',suffix=None,quote='9400 CAMINO RUIZ')
    assert a.conversations.check_addresses([proposed],'9400 CAMINO RUIZ')
    with pytest.raises(ValueError):
        a.conversations.check_addresses([proposal(quote='9000 Regents')],'9000 Regents')


def test_rejected_saved_plan_is_an_error_not_a_successful_analysis():
    async def run():
        invalid=interpretation(plan=Plan(intent='request_count',tools=['count_service_requests']).model_dump())
        a=agent(invalid)
        proposed=await a.chat(ChatTurn(message=QUESTION))
        final=await a.chat(follow(proposed))
        assert final.status=='error' and final.result.status=='error' and not a.tools.calls
        assert any(code.startswith('plan_rejected:') for code in final.result.limitation_codes)
    asyncio.run(run())
