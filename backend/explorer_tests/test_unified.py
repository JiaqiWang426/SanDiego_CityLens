import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from backend.main import app, get_agent
from backend.explorer_api import get_store
from backend.explorer_models import Bounds, ExplorerFilters, ViewportContext
from backend.explorer_store import ExplorerStore, selection
from backend.agent.location_models import ChatTurn
from backend.agent.map_analysis import ViewportPlanOutput, ViewportAnalysis, radius_visualization
from backend.agent.models import Plan
from backend.agent.sessions import ConversationRunner
from backend.agent.settings import Settings
from backend.agent.workflow import Agent
from backend.agent_tests.helpers import FakeModel, FakeTools, request


def context(**filters):
    return ViewportContext(bounds=Bounds(west=-117.3, east=-117.2, south=32.8, north=32.9), filters=ExplorerFilters(**filters))


def plan(**updates):
    values = {key: None for key in ViewportPlanOutput.model_fields}
    values.update({'mode':'viewport', **updates})
    return ViewportPlanOutput(**values)


def summary(total=3):
    return dict(total=total, categories=[dict(label='Pothole',count=total)],
                statuses=[dict(label='New',count=total)], months=[dict(month='2026-01',count=total)],
                main_categories=[dict(label='Roads & Sidewalks',count=total)],
                sub_categories=[dict(main_category='Roads & Sidewalks',sub_category='Pothole',count=total)],
                provenance={'source_table':'citylens.service_requests'})


def agent():
    model = FakeModel()
    model.plan_viewport = AsyncMock(return_value=plan())
    return Agent(model, FakeTools(), Settings())


def test_unified_routes_and_missing_database():
    with TestClient(app) as client:
        assert 'chat-form' in client.get('/').text
        assert 'Ask. Inspect. Verify.' in client.get('/agent').text
        for path in ['/assets/explorer.js','/assets/app.js','/assets/vendor/leaflet.js']:
            assert client.get(path).status_code == 200
        response = client.get('/api/v1/explorer/summary')
        assert response.status_code == 503 and 'database_not_configured' in response.json()['detail']


@pytest.mark.parametrize('params', [
    {'west':1,'east':0}, {'south':32,'north':32}, {'west':'nan'}, {'north':91},
    {'zoom':20}, {'start':'2025-01-01'}, {'start':'2026-02-01','end':'2026-01-01'},
    {'dataset':'traffic_collisions','community':'La Jolla'}, {'dataset':'arbitrary_table'},
])
def test_invalid_map_parameters(params):
    with TestClient(app) as client:
        assert client.get('/api/v1/explorer/map',params=params).status_code == 422


def test_sql_binds_values_and_uses_existing_gist_column():
    malicious = "Pothole'; DROP TABLE citylens.service_requests; --"
    sql, values = selection(ExplorerFilters(category=malicious), context().bounds)
    assert malicious not in sql and malicious in values
    assert 'location && ST_MakeEnvelope' in sql and 'ST_Intersects' in sql
    assert 'raw_record' in sql and "council_district' = '1'" in sql
    assert values[0].utcoffset().total_seconds() == -8 * 3600
    assert values[1].year == 2027


def test_viewport_ignored_for_unrelated_radius_request():
    current = agent()
    reply, saved = asyncio.run(ViewportAnalysis(current).run(ChatTurn(message='Compare two apartments within 800 meters.',viewport_context=context())))
    assert reply is None and saved is None
    current.model.plan_viewport.assert_not_called()


def test_explicit_viewport_requires_context_and_rejects_mixed_scopes():
    current = agent()
    for turn, code in [(ChatTurn(message='Summarize my current map view.'),'viewport_context_required'),
                       (ChatTurn(message='Current map view within 800 meters.',viewport_context=context()),'conflicting_spatial_scopes')]:
        reply, _ = asyncio.run(ViewportAnalysis(current).run(turn))
        assert reply.status == 'needs_clarification' and code in reply.diagnostic_codes
    current.model.plan_viewport.assert_not_called()


def test_viewport_summary_and_visualization_are_derived_from_sql():
    current = agent()
    with patch.object(ExplorerStore,'summary',return_value=summary()) as execute:
        reply, saved = asyncio.run(ViewportAnalysis(current).run(ChatTurn(message='Summarize Get It Done in my current map view.',viewport_context=context())))
    assert reply.viewport_analysis['value'] == 3
    assert reply.visualization.bounds == context().bounds
    assert reply.visualization.evidence_ids == [reply.viewport_analysis['evidence_id']]
    assert saved['viewport']['bounds'] == context().bounds.model_dump()
    assert execute.call_args.args == (context().filters,context().bounds)


@pytest.mark.parametrize('output', [
    plan(category='Pothole',change_quote='fabricated'),
    plan(category='Pothole',change_quote='Summarize my current map view.'),
    plan(start='2025-01-01',change_quote='Summarize my current map view.'),
    plan(dataset='traffic_collisions',change_quote='Summarize my current map view.'),
])
def test_unquoted_or_invalid_filter_changes_execute_no_sql(output):
    current = agent(); current.model.plan_viewport.return_value = output
    with patch.object(ExplorerStore,'summary') as execute:
        reply, _ = asyncio.run(ViewportAnalysis(current).run(ChatTurn(message='Summarize my current map view.',viewport_context=context())))
    assert reply.status == 'needs_clarification'
    execute.assert_not_called()


def test_failed_query_or_inconsistent_evidence_has_no_map_or_count():
    for result in [RuntimeError('private database details'), {**summary(),'total':99}]:
        kwargs = {'side_effect':result} if isinstance(result,Exception) else {'return_value':result}
        with patch.object(ExplorerStore,'summary',**kwargs):
            reply, _ = asyncio.run(ViewportAnalysis(agent()).run(ChatTurn(message='Summarize my current map view.',viewport_context=context())))
        assert reply.status == 'error' and reply.visualization is None and reply.viewport_analysis is None
        assert 'private' not in reply.model_dump_json()


def test_radius_visualization_uses_verified_evidence_and_preserves_radius():
    current = Agent(FakeModel(Plan(intent='compare_locations',tools=['count_service_requests'])),FakeTools(),Settings())
    reply = asyncio.run(current.query(request(two=True,question='Compare Get It Done reports near A and B.')))
    assert reply.visualization.mode == 'radius'
    assert len(reply.visualization.circles) == 2
    for circle in reply.visualization.circles:
        expected = next(e for e in reply.evidence if e.location_id == circle.location_id)
        assert (circle.latitude,circle.longitude,circle.radius_meters) == (expected.scope.latitude,expected.scope.longitude,800)
        assert expected.evidence_id in circle.evidence_ids
    assert radius_visualization([]) is None


def test_viewport_multi_turn_retains_bounds_after_restart_and_queries_again():
    async def scenario():
        saver=InMemorySaver(); current=agent()
        config={'configurable':{'thread_id':'a'*32}}
        graph=ConversationRunner(current).builder.compile(checkpointer=saver)
        with patch.object(ExplorerStore,'summary',return_value=summary()) as execute:
            first=await graph.ainvoke({'session_id':'a'*32,'kind':'chat','payload':ChatTurn(message='Summarize my current map view.',viewport_context=context()).model_dump(mode='json')},config)
            assert first['query_context']['mode'] == 'viewport'
            second=agent(); second.model.plan_viewport.return_value=plan(category='Pothole',change_quote='Only Pothole')
            graph=ConversationRunner(second).builder.compile(checkpointer=saver)
            moved=context(); moved.bounds.west=-117.4
            last=await graph.ainvoke({'session_id':'a'*32,'kind':'chat','payload':ChatTurn(message='Only Pothole',viewport_context=moved).model_dump(mode='json')},config)
            assert execute.call_count == 2
            assert execute.call_args.args[1] == context().bounds
            assert execute.call_args.args[0].category == 'Pothole'
            assert len(last['messages']) == 4
            assert last['reply']['session_id'] == 'a'*32
            assert last['reply']['visualization']['bounds'] == context().bounds.model_dump()
    asyncio.run(scenario())


def test_saved_viewport_cannot_override_a_new_explicit_radius():
    current=agent()
    saved={'mode':'viewport','viewport':context().model_dump(mode='json')}
    reply, after=asyncio.run(ViewportAnalysis(current).run(ChatTurn(message='Compare 9500 Gilman Drive and 9000 Regents Drive within 800 meters.'),saved))
    assert reply is None and after is None
    current.model.plan_viewport.assert_not_called()


def test_map_clarification_retains_initial_bounds_for_correction():
    current=agent()
    current.model.plan_viewport.return_value=plan(mode='clarify',clarification='Please name an exact source status.')
    reply, saved=asyncio.run(ViewportAnalysis(current).run(ChatTurn(message='Summarize unresolved cases in my current map view.',viewport_context=context())))
    assert reply.status == 'needs_clarification'
    assert saved['mode'] == 'viewport' and saved['viewport']['bounds'] == context().bounds.model_dump()
    current.model.plan_viewport.return_value=plan(status='New',change_quote='Use New')
    with patch.object(ExplorerStore,'summary',return_value=summary()):
        reply, saved=asyncio.run(ViewportAnalysis(current).run(ChatTurn(action='clarify',message='Use New'),saved))
    assert reply.status == 'complete' and saved['viewport']['filters']['status'] == 'New'


def test_viewport_pending_addresses_are_not_silently_discarded():
    from backend.agent_tests.test_location_flow import agent as address_agent, QUESTION
    from fastapi import HTTPException
    async def scenario():
        runner=ConversationRunner(address_agent())
        first=await runner.run({'session_id':'a'*32,'kind':'chat','payload':ChatTurn(message=QUESTION).model_dump(mode='json')})
        with pytest.raises(HTTPException) as exc:
            await runner.run({**first,'session_id':'a'*32,'kind':'chat','payload':ChatTurn(message='Summarize my current map view.',viewport_context=context()).model_dump(mode='json')})
        assert exc.value.status_code == 409
    asyncio.run(scenario())


def test_api_validates_structured_bounds_before_agent_execution():
    current=agent(); app.dependency_overrides[get_agent]=lambda:current
    try:
        with TestClient(app) as client:
            body={'message':'Summarize my current map view.','viewport_context':context().model_dump(mode='json')}
            body['viewport_context']['bounds']['west']=200
            assert client.post('/api/v1/agent/chat',json=body).status_code == 422
        current.model.plan_viewport.assert_not_called()
    finally:
        app.dependency_overrides.clear()
