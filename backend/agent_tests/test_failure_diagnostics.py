"""Offline regressions for audited integration failures. No DB/network calls."""
import asyncio
import json
from unittest.mock import MagicMock,patch

import httpx
import openai
import psycopg
import pytest
from openai.lib._parsing._responses import type_to_text_format_param

from backend.agent.diagnostics import LOGGER
from backend.agent.llm import OpenAIModel,PLANNER_PROMPT
from backend.agent.models import Plan,PlanOutput
from backend.agent.settings import Settings
from backend.agent.tools import DatabaseTools,ReaderRoleMissing,ensure_reader
from backend.agent.workflow import Agent,validated_calls
from backend.agent_tests.helpers import FakeModel,FakeTools,request


def call():
    return validated_calls(request(),Plan(intent='location_summary',tools=['aggregate_traffic_collisions']),Settings())[0]


@pytest.mark.parametrize('error,expected',[
    (psycopg.OperationalError('connection refused'),'database_connection_failed'),
    (psycopg.OperationalError('password authentication failed for user fixture'),'database_authentication_failed'),
    (psycopg.OperationalError('no password supplied'),'database_authentication_failed'),
    (psycopg.errors.InvalidPassword('fixture'),'database_authentication_failed'),
    (psycopg.errors.InvalidAuthorizationSpecification('fixture'),'database_authentication_failed'),
])
def test_connect_failure_codes_and_no_error_message_leak(error,expected,caplog):
    with patch('backend.agent.tools.psycopg.connect',side_effect=error):
        result=DatabaseTools('postgresql://fake:supersecret@host/db').execute(call())
    assert result.error_code==expected and result.status=='error'
    assert not result.rows
    assert 'supersecret' not in caplog.text and 'postgresql://' not in caplog.text
    assert type(error).__name__ in caplog.text


@pytest.mark.parametrize('stage,error,expected',[
    ('reader_check',ReaderRoleMissing('missing'),'reader_role_missing'),
    ('reader_check',PermissionError('wrong role'),'reader_role_required'),
    ('reader_check',psycopg.errors.InsufficientPrivilege('private'),'database_permission_denied'),
    ('query',psycopg.errors.InsufficientPrivilege('private'),'database_permission_denied'),
    ('query',psycopg.errors.UndefinedFunction('private'),'database_function_missing'),
    ('query',psycopg.errors.SyntaxError('private'),'database_execution_failed'),
    ('query',psycopg.errors.QueryCanceled('private'),'tool_timeout'),
    ('query',psycopg.errors.LockNotAvailable('private'),'database_lock_timeout'),
    ('query',psycopg.OperationalError('server connection lost'),'database_connection_lost'),
])
def test_database_execution_codes(stage,error,expected,caplog):
    conn=MagicMock()
    target='ensure_reader' if stage=='reader_check' else 'execute_query'
    with patch('backend.agent.tools.psycopg.connect') as connect, \
         patch('backend.agent.tools.ensure_reader'),patch('backend.agent.tools.execute_query'), \
         patch('backend.agent.tools.'+target,side_effect=error):
        connect.return_value.__enter__.return_value=conn
        result=DatabaseTools('not-used').execute(call())
    assert result.error_code==expected
    assert result.status=='error'
    assert 'private' not in caplog.text
    assert stage in caplog.text


def test_missing_reader_group_is_checked_before_pg_has_role():
    conn=MagicMock()
    conn.execute.return_value.fetchone.return_value=(None,)
    with pytest.raises(ReaderRoleMissing):
        ensure_reader(conn)
    assert conn.execute.call_count==1
    assert 'to_regrole' in conn.execute.call_args.args[0]


def test_wire_schema_is_restricted_but_internal_validation_remains():
    wire=type_to_text_format_param(PlanOutput)['schema']
    def check(value):
        if isinstance(value,dict):
            assert not {'default','minLength','maxLength','maxItems','minimum','maximum'} & value.keys()
            if value.get('type')=='object':
                assert value['additionalProperties'] is False
                assert set(value['required'])==set(value['properties'])
            for nested in value.values():
                check(nested)
        elif isinstance(value,list):
            for nested in value:
                check(nested)
    check(wire)
    # Internal schema still has all existing field limits; it was not relaxed.
    internal=Plan.model_json_schema()
    assert internal['properties']['tools']['maxItems']==3
    assert internal['properties']['filter_quote']['anyOf'][0]['maxLength']==500
    for intent in ('location_summary','compare_locations','collision_trend',
                   'collision_drilldown','request_count','unsupported_dimension'):
        assert '- '+intent+':' in PLANNER_PROMPT
    assert 'Get It Done service-request ID counts ONLY' in PLANNER_PROMPT


def _wire_response(mode):
    plan=Plan(intent='location_summary',tools=['aggregate_traffic_collisions']).model_dump()
    status='completed'
    content=[{'type':'output_text','text':json.dumps(plan),'annotations':[]}]
    if mode=='incomplete':
        status='incomplete'
        content=[]
    elif mode=='refusal':
        content=[{'type':'refusal','refusal':'fixture refusal'}]
    elif mode=='no_output':
        content=[]
    elif mode=='invalid_json':
        content=[{'type':'output_text','text':'{broken','annotations':[]}]
    elif mode=='invalid_internal_plan':
        plan['filter_quote']='x'*501
        content=[{'type':'output_text','text':json.dumps(plan),'annotations':[]}]
    elif mode=='rejected_intent':
        plan['intent']='request_count'
        content=[{'type':'output_text','text':json.dumps(plan),'annotations':[]}]
    body=dict(id='resp_test',object='response',created_at=0,status=status,model='fixture-model',
              output=[dict(id='msg',type='message',role='assistant',status='completed',content=content)])
    if mode=='incomplete':
        body['incomplete_details']={'reason':'max_output_tokens'}
    return body


@pytest.mark.parametrize('mode,expected,intent',[
    (400,'planning_provider_error:400','undetermined'),
    (401,'planning_provider_error:401','undetermined'),
    (403,'planning_provider_error:403','undetermined'),
    (404,'planning_provider_error:404','undetermined'),
    (429,'planning_provider_error:429','undetermined'),
    (500,'planning_provider_error:500','undetermined'),
    ('sdk_timeout','planning_timeout','undetermined'),
    ('network','planning_provider_connection_failed','undetermined'),
    ('incomplete','planning_output_incomplete:max_output_tokens','undetermined'),
    ('refusal','planning_output_refused','undetermined'),
    ('no_output','planning_no_structured_output','undetermined'),
    ('invalid_json','planning_structured_output_invalid','undetermined'),
    ('invalid_internal_plan','plan_rejected:invalid_schema','undetermined'),
    ('rejected_intent','plan_rejected:request_count_requires_service_counter','request_count'),
])
def test_real_sdk_and_graph_failure_modes_without_network(mode,expected,intent,caplog):
    requests=[]
    secret='sk-fixture-secret-never-log'
    def handler(req):
        requests.append(req)
        if isinstance(mode,int):
            return httpx.Response(mode,json={'error':{'message':'fixture '+secret,
                'param':'text.format.schema','code':'fixture_error','type':'fixture_type'}})
        if mode=='sdk_timeout':
            raise httpx.ReadTimeout('private provider message',request=req)
        if mode=='network':
            raise httpx.ConnectError('private provider message',request=req)
        return httpx.Response(200,json=_wire_response(mode))
    def factory(**kwargs):
        return openai.AsyncOpenAI(**kwargs,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    settings=Settings(openai_api_key=secret,model='fixture-model')
    tools=FakeTools()
    with patch('backend.agent.llm.AsyncOpenAI',side_effect=factory):
        response=asyncio.run(Agent(OpenAIModel(settings),tools,settings).query(request()))
    assert response.status=='error' and response.intent==intent
    assert response.limitation_codes==[expected]
    assert not response.unsupported_dimensions and not tools.calls
    assert len(requests)==1
    assert expected in caplog.text
    assert secret not in caplog.text and 'private provider message' not in caplog.text
    if isinstance(mode,int):
        assert 'text.format.schema' in caplog.text and '[redacted]' in caplog.text


def test_composition_provider_failure_keeps_verified_claims(caplog):
    class BadComposer(FakeModel):
        async def compose(self,claims):
            raise openai.APIStatusError('do not log',response=httpx.Response(429,
                request=httpx.Request('POST','https://example.invalid')),body={'message':'quota fixture'})
    response=asyncio.run(Agent(BadComposer(),FakeTools(),Settings()).query(request()))
    assert response.claims and response.status=='partial'
    assert 'composition_provider_error:429' in response.limitation_codes
    assert 'composition_fallback' in response.limitation_codes
    assert 'composition_provider_error:429' in caplog.text


def test_zero_geocoding_has_an_explicit_diagnostic():
    class Unlocated(FakeTools):
        def execute(self,call):
            result=super().execute(call)
            result.metadata['dataset_located_records']=0
            result.rows=[]
            return result
    response=asyncio.run(Agent(FakeModel(),Unlocated(),Settings()).query(request()))
    assert response.evidence[0].value==0
    assert 'collision_geocoding_incomplete' in response.limitation_codes
    assert any('No stored collision report' in text for text in response.limitations)


def test_smoke_requires_approval_without_constructing_a_model(capsys):
    from backend.scripts.smoke_plan import main
    with patch('backend.scripts.smoke_plan.OpenAIModel') as factory:
        assert main([])==2
        factory.assert_not_called()
    assert 'No API request made' in capsys.readouterr().out


def test_controlled_smoke_is_one_planner_only(capsys):
    from backend.scripts.smoke_plan import smoke
    calls=[]
    class Model:
        def __init__(self,settings):
            pass
        async def plan(self,request):
            calls.append('plan')
            return PlanOutput.model_validate(Plan(intent='location_summary',tools=['aggregate_traffic_collisions']).model_dump())
    with patch('backend.scripts.smoke_plan.OpenAIModel',Model):
        assert asyncio.run(smoke(Settings()))==0
    assert calls==['plan']
    assert '"database_tools_executed": 0' in capsys.readouterr().out
