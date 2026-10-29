import asyncio

from fastapi.testclient import TestClient
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError
import pytest

from backend.main import app,get_agent
from backend.agent.evidence import make_evidence,compare_pair,record_claim,verify_claims
from backend.agent.models import CollisionFilters,Plan
from backend.agent.settings import Settings
from backend.agent.tools import DatabaseTools,REGISTRY,ToolResult
from backend.agent.workflow import Agent,validated_calls
from backend.agent_tests.helpers import FakeModel,FakeTools,request


def run(coro):
    return asyncio.run(coro)


def client_for(model=None,tools=None,settings=None):
    agent=Agent(model or FakeModel(),tools or FakeTools(),settings or Settings())
    app.dependency_overrides[get_agent]=lambda:agent
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.parametrize('change',[
    {'radius_meters':-1},{'radius_meters':50001},{'locations':[]},
    {'locations':[{'id':'A','latitude':91,'longitude':-117}]},
    {'locations':[{'id':'A','latitude':40,'longitude':-117}]},
    {'time_range':{'start':'2026-01-01','end':'2026-02-01'}},
    {'time_range':{'start':'2026-02-01T00:00:00Z','end':'2026-01-01T00:00:00Z'}},
    {'time_range':{'start':'2026-01-01T00:00:00Z','end':'2026-02-01T00:00:00Z','calendar_timezone':'Mars/Unknown'}},
    {'time_range':{'start':'2000-01-01T00:00:00Z','end':'2026-02-01T00:00:00Z'}},
    {'sql':'DROP TABLE citylens.service_requests'},{'question':'   '},
])
def test_api_validation_without_llm(change):
    model=FakeModel()
    with client_for(model) as client:
        response=client.post('/api/v1/agent/query',json={**request().model_dump(mode='json'),**change})
    assert response.status_code==422
    assert model.plans==0


def test_api_single_location_evidence():
    tools=FakeTools()
    with client_for(tools=tools) as client:
        response=client.post('/api/v1/agent/query',json=request().model_dump(mode='json'))
    assert response.status_code==200
    body=response.json()
    assert body['status']=='partial'  # Coverage unknown is explicit.
    assert tools.calls[0].name=='aggregate_traffic_collisions'
    ev=body['evidence'][0]
    assert ev['value']==7 and len(ev['buckets'])==2
    assert ev['buckets'][1]['source_bucket_present'] is False
    assert ev['buckets'][1]['report_count']==0
    assert ev['provenance']['import_ids'] is None
    assert ev['quality']['completeness']=='unknown'
    assert ev['evidence_id'] in body['answer']
    assert body['claims'][0]['value']==7


def test_two_location_symmetric_comparisons_and_swapping():
    plan=Plan(intent='compare_locations',tools=['count_service_requests','aggregate_traffic_collisions'])
    tools=FakeTools()
    agent=Agent(FakeModel(plan),tools,Settings())
    req=request(two=True)
    response=run(agent.query(req))
    assert len(tools.calls)==4
    for a,b in zip(tools.calls[::2],tools.calls[1::2]):
        assert a.model_dump(exclude={'location'})==b.model_dump(exclude={'location'})
    assert len(response.comparison.metrics)==2
    for metric in response.comparison.metrics:
        assert metric.difference_b_minus_a==4
        assert metric.overlapping_circles
    swapped=req.model_copy(update={'locations':list(reversed(req.locations))})
    other=run(agent.query(swapped))
    assert all(m.difference_b_minus_a==-4 for m in other.comparison.metrics)


@pytest.mark.parametrize('question',['Which apartment has a lower crime rate?', 'Which is safer?', 'What is the commute time?',
                                    'How many pothole reports were submitted?', 'How many closed requests were repaired?'])
def test_unsupported_does_not_substitute_or_call_tools(question):
    model,tools=FakeModel(),FakeTools()
    result=run(Agent(model,tools,Settings()).query(request(question=question)))
    assert result.status=='unsupported'
    assert not result.claims and not result.evidence
    assert model.plans==0 and not tools.calls


def test_mixed_supported_and_unsupported():
    response=run(Agent(FakeModel(),FakeTools(),Settings()).query(
        request(question='Count traffic collisions and tell me the crime rate.')))
    assert response.status=='partial' and 'crime' in response.unsupported_dimensions
    assert response.claims


def test_failure_isolation_no_fabricated_zero():
    plan=Plan(intent='compare_locations',tools=['count_service_requests','aggregate_traffic_collisions'])
    response=run(Agent(FakeModel(plan),FakeTools(failed='aggregate_traffic_collisions'),Settings()).query(request(two=True)))
    assert response.status=='partial'
    failed=[e for e in response.evidence if e.tool=='aggregate_traffic_collisions']
    assert all(e.value is None and e.error_code=='tool_timeout' for e in failed)
    assert len(response.comparison.metrics)==1
    verify_claims(response.claims,response.evidence)


def test_empty_matches_are_not_missing_coverage():
    response=run(Agent(FakeModel(),FakeTools(empty=True),Settings()).query(request()))
    assert response.evidence[0].value==0
    assert response.evidence[0].quality.completeness=='unknown'
    assert response.status=='partial'
    assert 'do not establish absence' in response.answer


def test_empty_dataset_unavailable_not_zero():
    class Empty(FakeTools):
        def execute(self,call):
            return ToolResult(call,status='unavailable',error_code='dataset_empty')
    result=run(Agent(FakeModel(),Empty(),Settings()).query(request()))
    assert result.status=='error'
    assert result.evidence[0].value is None and not result.claims


def test_source_prompt_injection_is_not_composer_input_or_answer():
    plan=Plan(intent='collision_drilldown',tools=['get_traffic_collisions'])
    response=run(Agent(FakeModel(plan),FakeTools(),Settings()).query(request()))
    serialized=response.model_dump_json()
    assert '999999' not in serialized and 'DROP TABLE' not in serialized and 'INSTRUCTIONS' not in serialized
    assert response.evidence[0].records[0]['injured'] is None
    assert response.evidence[0].metric=='retrieved_collision_page_size'
    assert 'not total matches' in response.answer


@pytest.mark.parametrize('change',[
    {'tools':['filter_traffic_collisions']},{'tools':['execute_sql']},
    {'collision_filters':{'min_injured':-1}},
    {'collision_filters':{'min_killed':2,'max_killed':1}},
])
def test_plan_schema_forbids_unsafe_or_invalid_arguments(change):
    with pytest.raises(ValidationError):
        Plan.model_validate({'intent':'location_summary','tools':[],**change})


def test_tool_budgets_and_quoted_filters():
    req=request(two=True)
    plan=Plan(intent='compare_locations',tools=list(REGISTRY))
    with pytest.raises(ValueError):
        validated_calls(req,plan,Settings(max_tool_calls=4))
    filtered=Plan(intent='location_summary',tools=['aggregate_traffic_collisions'],
                  collision_filters=CollisionFilters(min_injured=1))
    with pytest.raises(ValueError):
        validated_calls(req,filtered,Settings())
    filtered.filter_quote='traffic collisions'
    assert len(validated_calls(req,filtered,Settings()))==2


def test_all_service_count_cannot_replace_requested_category():
    req=request(question='Count pothole reports and traffic collisions.')
    with pytest.raises(ValueError):
        validated_calls(req,Plan(intent='location_summary',tools=['count_service_requests']),Settings())


def test_claim_tampering_references_and_arithmetic():
    call=validated_calls(request(),Plan(intent='location_summary',tools=['aggregate_traffic_collisions']),Settings())[0]
    ev=make_evidence(FakeTools().execute(call))
    claim=record_claim(ev)
    verify_claims([claim],[ev])
    for update in [{'value':999},{'evidence_ids':['invented']},{'text':'A is safer.'}]:
        with pytest.raises(ValueError):
            verify_claims([claim.model_copy(update=update)],[ev])
    other=ev.model_copy(update={'location_id':'B','scope':ev.scope.model_copy(update={'radius_meters':900})})
    with pytest.raises(ValueError):
        compare_pair(ev,other)


def test_bad_composer_uses_verified_template():
    response=run(Agent(FakeModel(broken_composer=True),FakeTools(),Settings()).query(request()))
    assert 'composition_fallback' in response.limitation_codes
    assert response.claims and 'invented' not in response.answer


def test_no_database_configuration_explicit_error():
    response=run(Agent(FakeModel(),DatabaseTools(None),Settings()).query(request()))
    assert response.status=='error' and not response.claims
    assert response.evidence[0].error_code=='database_not_configured'


def test_planner_failure_and_timeout_controlled():
    class Broken(FakeModel):
        async def plan(self,req):
            raise TimeoutError()
    response=run(Agent(Broken(),FakeTools(),Settings()).query(request()))
    assert response.status=='error' and 'planning_timeout' in response.limitation_codes


def test_daily_buckets_across_dst():
    req=request().model_copy(update={'time_range':request().time_range.model_validate(dict(
        start='2026-03-07T00:00:00-08:00',end='2026-03-10T00:00:00-07:00'))})
    call=validated_calls(req,Plan(intent='collision_trend',tools=['aggregate_traffic_collisions'],period='day'),Settings())[0]
    ev=make_evidence(ToolResult(call,rows=[]))
    assert len(ev.buckets)==3
    assert ev.buckets[1].period_end.utcoffset().total_seconds()==-7*3600
    assert ev.buckets[1].period_start.utcoffset().total_seconds()==-8*3600


def test_openai_plan_schema_is_strict():
    schema=to_strict_json_schema(Plan)
    assert schema['additionalProperties'] is False
    assert set(schema['required'])==set(schema['properties'])


def test_final_validation_rejects_modified_answer():
    req=request()
    agent=Agent(FakeModel(),FakeTools(),Settings())
    response=run(agent.query(req))
    changed=response.model_copy(update={'answer':'A is safer and has 999 collisions.'})
    final=agent.final_validate(dict(request=req,response=changed,claims=response.claims,
        evidence=response.evidence,unsupported=[],codes=[],trace_id=response.tool_trace_id))['response']
    assert final.status=='error' and not final.claims
    assert 'final_validation_failed' in final.limitation_codes


def test_endpoint_request_deadline():
    class TimedOut:
        settings=Settings()
        async def query(self,req):
            raise TimeoutError()
    app.dependency_overrides[get_agent]=lambda:TimedOut()
    with TestClient(app) as client:
        response=client.post('/api/v1/agent/query',json=request().model_dump(mode='json'))
    assert response.status_code==200
    assert response.json()['limitation_codes']==['request_timeout']
