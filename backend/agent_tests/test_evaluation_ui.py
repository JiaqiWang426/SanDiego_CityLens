from fastapi.testclient import TestClient

from backend.main import app,get_agent
from backend.agent.models import Plan
from backend.agent.settings import Settings
from backend.agent.workflow import Agent,validated_calls
from backend.agent.tools import ToolCall
from backend.agent_tests.helpers import FakeModel,FakeTools,request


def test_frontend_and_static_files():
    with TestClient(app) as client:
        response=client.get('/')
        assert response.status_code==200 and 'Ask. Inspect. Verify.' in response.text
        assert client.get('/assets/app.js').status_code==200
        assert client.get('/assets/styles.css').status_code==200


def test_trace_replay_does_not_use_model_and_retains_scope():
    model,tools=FakeModel(),FakeTools()
    agent=Agent(model,tools,Settings())
    app.dependency_overrides[get_agent]=lambda:agent
    try:
        with TestClient(app) as client:
            result=client.post('/api/v1/agent/query',json=request().model_dump(mode='json')).json()
            assert len(result['tool_calls'])==1
            log=result['tool_calls'][0]
            assert log['query_id']==result['evidence'][0]['query_id']
            assert log['arguments']==result['execution_plan'][0]
            assert 'READ ONLY' in log['read_only_sql']
            assert 'aggregate_traffic_collisions' in log['parameterized_sql']
            assert log['result_rows'][0]['collision_count']==7
            replay=client.post('/api/v1/tools/run',json=log['arguments'])
        assert replay.status_code==200
        assert replay.json()['evidence']['value']==7
        assert model.plans==1  # Replay made no second planning call.
        assert tools.calls[0]==tools.calls[1]
    finally:
        app.dependency_overrides.clear()


def test_manual_runner_rejects_arbitrary_sql_and_budget_overrides():
    agent=Agent(FakeModel(),FakeTools(),Settings(max_rows=10))
    app.dependency_overrides[get_agent]=lambda:agent
    args=validated_calls(request(),Plan(intent='location_summary',tools=['count_service_requests']),agent.settings)[0].model_dump(mode='json')
    try:
        with TestClient(app) as client:
            for update in [{'name':'execute_sql'},{'sql':'DROP TABLE citylens.ingestions'},{'limit':11}]:
                assert client.post('/api/v1/tools/run',json={**args,**update}).status_code==422
            status=client.get('/api/v1/status').json()
            assert 'database_configured' in status
            assert 'database_url' not in status and 'openai_api_key' not in status
    finally:
        app.dependency_overrides.clear()


def test_copyable_sql_escapes_street_text():
    from backend.agent.models import CollisionFilters
    from backend.agent.tools import tool_log,ToolResult
    args=validated_calls(request(),Plan(intent='location_summary',tools=['aggregate_traffic_collisions']),Settings())[0]
    args=args.model_copy(update={'collision_filters':CollisionFilters(street_name="O'BRIEN ST")})
    exported=tool_log(ToolResult(args)).read_only_sql
    assert "O''BRIEN ST" in exported
    assert exported.startswith('BEGIN TRANSACTION READ ONLY;')
    assert exported.endswith('ROLLBACK;')
