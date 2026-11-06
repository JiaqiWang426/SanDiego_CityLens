"""Catalog validation and category unions; synthetic PostGIS records only."""
import asyncio
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import psycopg
import pytest

from backend.agent.evidence import make_evidence, record_claim, verify_claims
from backend.agent.location_models import ChatTurn
from backend.agent.models import Plan, ServiceRequestAggregation
from backend.agent.service_categories import road_clarification
from backend.agent.sessions import ConversationRunner, apply_followup
from backend.agent.settings import Settings
from backend.agent.tools import DatabaseTools, ToolCall, query_spec
from backend.agent.workflow import Agent, validated_calls
from backend.agent_tests.helpers import FakeModel, FakeTools, request
from backend.agent_tests.test_integration import isolated_db
from backend.agent_tests.test_phase1_data import reliability_db, START, END, LAT, LNG, reference
from backend.agent_tests.test_sessions import patch
from backend.main import app, get_agent
from backend.service_request_queries import aggregate_service_requests


def call(**selection):
    return ToolCall(name='aggregate_service_requests',location={'id':'A','latitude':LAT,'longitude':LNG},
        radius_meters=800,time_range={'start':START,'end':END},period='month',limit=100,
        service_request_aggregation=selection)


@pytest.mark.parametrize('values', [dict(service_names=[]),dict(service_names=['Pothole'],service_name='Parking'),
    dict(service_names=[None]),dict(service_names=['x']*21)])
def test_category_selection_contract_rejects_ambiguous_or_unbounded_lists(values):
    with pytest.raises(ValueError):
        ServiceRequestAggregation(**values)


def test_multi_category_plan_and_followup_preserve_scope_and_use_array_binding():
    original=ConversationRunner.context(request(two=True),Plan(intent='compare_locations',tools=['count_service_requests']))
    message='Only Pothole and Pavement Maintenance.'
    selected=Plan(intent='compare_locations',tools=['aggregate_service_requests'],
        service_request_aggregation={'service_names':['Pothole','Pavement Maintenance']})
    updated,plan,sources=apply_followup(original,message,patch(selected,
        changed_plan_fields=['tools','service_request_aggregation.service_names'],change_quote=message),Settings())
    assert updated.locations==request(two=True).locations and updated.radius_meters==800
    assert updated.time_range==request().time_range
    calls=validated_calls(updated,plan,Settings())
    assert len(calls)==2
    statement,args=query_spec(calls[0])
    assert statement.count('%s')==10 and args[-1]==['Pothole','Pavement Maintenance']
    assert calls[0].model_dump(exclude={'location'})==calls[1].model_dump(exclude={'location'})
    context=ConversationRunner.context(updated,plan,sources)
    changed,kept,_=apply_followup(context,'Use 1,200 meters.',patch(radius_meters=1200,radius_quote='1,200 meters'),Settings())
    assert changed.radius_meters==1200 and kept.service_request_aggregation.service_names==selected.service_request_aggregation.service_names


def test_road_request_without_an_explicit_scope_cannot_execute_all_category_counts():
    with pytest.raises(ValueError):
        validated_calls(request(question='Only consider road-related issues'),
            Plan(intent='request_count',tools=['count_service_requests']),Settings())


def test_category_followup_compiles_required_tool_switch_when_model_omits_it():
    context=ConversationRunner.context(request(two=True),Plan(intent='compare_locations',tools=['count_service_requests']))
    selected=Plan(intent='compare_locations',tools=['aggregate_service_requests'],
        service_request_aggregation={'service_names':['Pothole','Pavement Maintenance']})
    message='Only Pothole and Pavement Maintenance.'
    updated,plan,_=apply_followup(context,message,patch(selected,
        changed_plan_fields=['service_request_aggregation.service_names'],change_quote=message),Settings())
    assert plan.tools==['aggregate_service_requests']
    assert all(c.name=='aggregate_service_requests' for c in validated_calls(updated,plan,Settings()))


def test_road_scope_clarification_uses_only_available_values_and_no_llm_or_analysis():
    class CatalogTools(FakeTools):
        def service_categories(self):
            return ['POTHOLE','PAVEMENT MAINTENANCE','TRAFFIC SIGNAL ISSUE','PARKING',None]
    async def scenario():
        model=FakeModel();model.followup=AsyncMock(side_effect=AssertionError('No model needed for catalog-backed ambiguity'))
        tools=CatalogTools();agent=Agent(model,tools,Settings())
        context=ConversationRunner.context(request(two=True),Plan(intent='compare_locations',tools=['count_service_requests']))
        reply,after,_=await ConversationRunner(agent).followup('e'*32,ChatTurn(message='What if we only consider road-related issues?'),context,None)
        assert reply.status=='needs_clarification' and after==context and tools.calls==[]
        assert 'POTHOLE' in reply.message and 'PAVEMENT MAINTENANCE' in reply.message
        assert 'TRAFFIC SIGN MAINTENANCE' not in reply.message
        assert reply.time_range==request().time_range and reply.radius_meters==800
        model.followup.assert_not_called()
    asyncio.run(scenario())
    assert road_clarification('Road-related issues (Pothole)', ['POTHOLE']) is None


@pytest.mark.parametrize('group_by',['service_name','status','both'])
@pytest.mark.parametrize('exclude',[False,True])
def test_union_reuses_existing_counts_without_duplicate_ids(reliability_db,group_by,exclude):
    _,reader=reliability_db
    # Independent reference calculations for disjoint stored categories. Repeated
    # normalized list entries must never join/multiply an ID's contribution.
    expected={}
    for category in ('Pothole','Parking'):
        for key,value in reference(group_by,exclude=exclude,service_name=category).items():
            expected[key]=expected.get(key,0)+value
    with psycopg.connect(reader) as conn:
        found=aggregate_service_requests(conn,LAT,LNG,800,START,END,group_by=group_by,
            exclude_linked_duplicates=exclude,service_names=[' Pothole ','POTHOLE','Parking'])
    assert {(r['service_name'],r['status']):r['request_id_count'] for r in found}==expected


def test_union_unknown_status_and_old_scalar_signature(reliability_db):
    _,reader=reliability_db
    with psycopg.connect(reader) as conn:
        old=aggregate_service_requests(conn,LAT,LNG,800,START,END,group_by='both',service_name=' Pothole ',status='closed')
    with psycopg.connect(reader) as conn:
        new=aggregate_service_requests(conn,LAT,LNG,800,START,END,group_by='both',service_names=['Pothole'],status='closed')
    assert old==new
    with psycopg.connect(reader) as conn:
        unknown=aggregate_service_requests(conn,LAT,LNG,800,START,END,group_by='both',service_names=['','UNKNOWN'])
    expected={**reference('both',service_name=''),**reference('both',service_name='UNKNOWN')}
    assert {(r['service_name'],r['status']):r['request_id_count'] for r in unknown}==expected


@pytest.mark.parametrize('selection',[[],['not present'],[None]])
def test_sql_union_rejects_empty_unknown_or_null_element_selection(reliability_db,selection):
    _,reader=reliability_db
    with psycopg.connect(reader) as conn,pytest.raises(psycopg.errors.InvalidParameterValue):
        aggregate_service_requests(conn,LAT,LNG,800,START,END,service_names=selection)


def test_catalog_api_and_agent_filters_validate_against_actual_values(reliability_db):
    _,reader=reliability_db
    tools=DatabaseTools(reader)
    catalog=tools.service_categories()
    assert 'POTHOLE' in catalog and 'PARKING' in catalog and None in catalog
    rejected=tools.execute(call(service_name='Invented Road Supercategory'))
    assert rejected.status=='error' and rejected.error_code=='unsupported_service_category' and rejected.rows==[]
    assert make_evidence(rejected).value is None
    result=tools.execute(call(service_names=['Pothole','Parking'],group_by='both'))
    assert result.status=='ok'
    evidence=make_evidence(result)
    verify_claims([record_claim(evidence)],[evidence])
    assert evidence.filters['service_names']==['Pothole','Parking'] and evidence.query_id==result.query_id
    current=Agent(FakeModel(),tools,Settings())
    app.dependency_overrides[get_agent]=lambda:current
    try:
        with TestClient(app) as client:
            response=client.get('/api/v1/tools/service-categories')
        assert response.status_code==200 and 'POTHOLE' in response.json()['service_names']
        assert response.json()['unknown_category_available'] is True
    finally:
        app.dependency_overrides.clear()
