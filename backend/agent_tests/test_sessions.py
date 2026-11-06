"""Session regressions use mocked models and an owned disposable PostGIS DB."""
import asyncio
import secrets
import sys
from unittest.mock import AsyncMock
from pathlib import Path
import os
import socket
import subprocess
import time

import httpx

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from backend import main
from backend.agent.evidence import verify_claims
from backend.agent.location_models import ChatTurn, AddressCorrectionOutput
from backend.agent.models import Plan, QueryRequest, Claim, EvidenceRecord
from backend.agent.session_models import FollowupOutput
from backend.agent.sessions import (ConversationRunner, SessionStore, apply_followup,
                                    restore_pending)
from backend.agent.settings import Settings
from backend.agent.tools import DatabaseTools
from backend.agent.workflow import Agent
from backend.agent_tests.helpers import FakeModel, FakeTools, request
from backend.agent_tests.test_integration import isolated_db  # noqa: F401
from backend.agent_tests.test_location_flow import agent, interpretation, proposal, QUESTION
from backend.scripts.setup_sessions import setup


def client():
    return TestClient(main.app, backend_options={'loop_factory': asyncio.SelectorEventLoop} if sys.platform == 'win32' else {})


def run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop if sys.platform == 'win32' else None)


def patch(plan=None, **updates):
    value = dict(mode='followup', radius_meters=None, radius_quote=None,
        time_range=None, time_quote=None,
        plan=(plan or Plan(intent='compare_locations', tools=['count_service_requests'])).model_dump(),
        changed_plan_fields=[], change_quote=None, clarification=None)
    value.update(updates)
    return FollowupOutput.model_validate(value)


def base_context():
    return ConversationRunner.context(request(two=True), Plan(intent='compare_locations', tools=['count_service_requests']))


def test_followup_provider_schema_restricts_plan_fields_to_supported_leaves():
    field = FollowupOutput.model_json_schema()['properties']['changed_plan_fields']['items']
    assert 'radius_meters' not in field['enum'] and 'time_range' not in field['enum']
    assert 'service_request_aggregation.service_name' in field['enum']
    with pytest.raises(ValueError):
        patch(changed_plan_fields=['radius_meters'], radius_meters=1200, radius_quote='1,200 meters')


def test_followup_merges_only_explicit_scope_and_filter_leaves():
    original = base_context()
    plan = Plan(intent='compare_locations', tools=['aggregate_service_requests'],
                service_request_aggregation={'service_name': 'Pothole'})
    message = 'Only consider road-related complaints (Pothole).'
    updated, selected, sources = apply_followup(original, message,
        patch(plan, changed_plan_fields=['tools', 'service_request_aggregation.service_name'], change_quote=message), Settings())
    assert updated.locations == request(two=True).locations
    assert updated.time_range == request().time_range and updated.radius_meters == 800
    assert selected.service_request_aggregation.service_name == 'Pothole'
    retained = ConversationRunner.context(updated, selected, sources)
    changed, retained_plan, _ = apply_followup(retained, 'Use 1,200 meters.',
        patch(radius_meters=1200, radius_quote='1,200 meters'), Settings())
    assert changed.radius_meters == 1200 and changed.locations == updated.locations
    assert changed.time_range == updated.time_range
    assert retained_plan.service_request_aggregation.service_name == 'Pothole'
    assert original == base_context()


@pytest.mark.parametrize('updates', [
    {'radius_meters': 1200, 'radius_quote': 'invented'},
    {'radius_meters': -1, 'radius_quote': 'Only'},
    {'changed_plan_fields': ['locations'], 'change_quote': 'Only'},
    {'changed_plan_fields': ['tools'], 'change_quote': 'invented'},
])
def test_followup_rejects_unquoted_invalid_or_geographic_patches(updates):
    with pytest.raises(ValueError):
        apply_followup(base_context(), 'Only change an explicit parameter.', patch(**updates), Settings())


def test_pending_confirmation_roundtrip_and_isolation_without_network():
    async def scenario():
        first = agent()
        saver = InMemorySaver()
        graph = ConversationRunner(first).builder.compile(checkpointer=saver)
        config = {'configurable': {'thread_id': 'a'*32}}
        state = await graph.ainvoke({'session_id': 'a'*32, 'kind': 'chat',
            'payload': ChatTurn(message=QUESTION).model_dump(mode='json')}, config)
        assert state['reply']['status'] == 'needs_confirmation'
        assert 'lock' not in state['pending'] and 'updated' not in state['pending']
        restored = restore_pending(state['pending'])
        assert restored.time_range and restored.plan and restored.locations
        assert not restored.lock.locked()
        second = agent()
        graph = ConversationRunner(second).builder.compile(checkpointer=saver)
        result = await graph.ainvoke({'session_id': 'a'*32, 'kind': 'chat',
            'payload': ChatTurn(action='confirm', revision=1).model_dump(mode='json')}, config)
        assert result['reply']['status'] == 'complete'
        assert len(result['messages']) == 4 and second.model.interpretations == 0
        assert (await graph.aget_state({'configurable': {'thread_id': 'b'*32}})).values == {}
    run(scenario())


def test_ambiguous_followup_preserves_context_and_executes_no_tools():
    async def scenario():
        current = agent()
        current.model.followup = AsyncMock(return_value=patch(mode='clarify', clarification='Which exact service category do you mean?'))
        context = base_context()
        reply, after, _ = await ConversationRunner(current).followup('a'*32,
            ChatTurn(message='Only road-related complaints'), context, None)
        assert reply.status == 'needs_clarification' and after == context
        assert current.tools.calls == []
        sent = current.model.followup.call_args.args[1]
        assert set(sent) == {'request', 'plan', 'filter_sources'}
        cancelled = await ConversationRunner(current).run({'session_id': 'a'*32, 'kind': 'chat',
            'query_context': context, 'payload': ChatTurn(action='cancel').model_dump(mode='json')})
        assert cancelled['reply']['status'] == 'cancelled' and cancelled['query_context'] == context
        assert current.tools.calls == []
    run(scenario())


def test_failed_full_query_does_not_reuse_an_older_active_scope():
    async def scenario():
        current = Agent(FakeModel(), FakeTools(), Settings())
        graph = ConversationRunner(current).builder.compile(checkpointer=InMemorySaver())
        config = {'configurable': {'thread_id': 'd'*32}}
        state = await graph.ainvoke({'session_id': 'd'*32, 'kind': 'query',
            'payload': request().model_dump(mode='json')}, config)
        assert state['query_context']
        current.model.plan_value = Plan(intent='request_count', tools=['aggregate_traffic_collisions'])
        state = await graph.ainvoke({'payload': request(two=True).model_dump(mode='json')}, config)
        assert state['reply']['status'] == 'error' and state['query_context'] is None
        assert len(state['messages']) == 4 and len(current.tools.calls) == 1
    run(scenario())


def test_address_followup_reuses_correction_and_preserves_other_location_and_scope():
    async def scenario():
        current = agent()
        current.model.followup = AsyncMock(return_value=patch(mode='new_locations'))
        current.model.correction_output = AddressCorrectionOutput(locations=[proposal(),
            proposal('B', '9600', 'Gilman', 'Dr', '9600 Gilman Dr')], clarification=None)
        graph = ConversationRunner(current).builder.compile(checkpointer=InMemorySaver())
        config = {'configurable': {'thread_id': 'c'*32}}
        await graph.ainvoke({'session_id': 'c'*32, 'kind': 'chat',
            'payload': ChatTurn(message=QUESTION).model_dump(mode='json')}, config)
        completed = await graph.ainvoke({'payload': ChatTurn(action='confirm', revision=1).model_dump(mode='json')}, config)
        before = completed['query_context']
        revised = await graph.ainvoke({'payload': ChatTurn(message='B should be 9600 Gilman Dr').model_dump(mode='json')}, config)
        assert revised['reply']['status'] == 'needs_confirmation'
        assert revised['reply']['locations'][0] == completed['reply']['locations'][0]
        assert revised['reply']['locations'][1]['house_number'] == '9600'
        assert revised['reply']['radius_meters'] == before['request']['radius_meters']
        assert revised['reply']['time_range'] == before['request']['time_range']
        assert revised['reply']['proposed_plan'] == before['plan']
        assert current.model.interpretations == 1 and current.model.corrections == 1
        assert len(current.tools.calls) == 2, 'A changed address must wait for confirmation'
    run(scenario())


def test_session_endpoints_report_disabled_persistence(monkeypatch):
    monkeypatch.setattr(Settings, 'from_env', lambda: Settings())
    with client() as api:
        assert api.post('/api/v1/sessions', json={}).status_code == 503
        assert api.get('/api/v1/sessions').status_code == 503
        assert api.post('/api/v1/agent/chat', json={'session_id': 'a'*32, 'message': 'Question'}).status_code == 503


@pytest.fixture(scope='module')
def session_db(isolated_db):
    admin_url, reader_url = isolated_db
    with psycopg.connect(admin_url) as conn:
        exists = conn.execute("SELECT to_regclass('citylens.sessions')").fetchone()[0]
        if exists and conn.execute('SELECT count(*) FROM citylens.sessions').fetchone()[0]:
            raise RuntimeError('Refusing a test database containing existing conversations')
        # This fixture owns synthetic data in an isolated *_test database. The
        # category follow-up must target a label that actually exists there.
        conn.execute('''INSERT INTO citylens.service_requests
            (service_request_id,service_name,requested_at,location,raw_record,ingestion_id)
            SELECT 'session_category_fixture','Pothole','2026-01-15T00:00:00-08:00',
                ST_SetSRID(ST_MakePoint(-117.25,32.84),4326)::geography,'{}',id
            FROM citylens.ingestions ORDER BY id LIMIT 1''')
    run(setup(admin_url))
    run(setup(admin_url))  # Repeatability includes official migrations.
    login, password = 'session_test_'+secrets.token_hex(5), secrets.token_hex(24)
    with psycopg.connect(admin_url) as conn:
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(login), sql.Literal(password)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA citylens,citylens_checkpoints TO {}').format(sql.Identifier(login)))
        conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON citylens.sessions TO {}').format(sql.Identifier(login)))
        conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA citylens_checkpoints TO {}').format(sql.Identifier(login)))
    return admin_url, reader_url, make_conninfo(admin_url, user=login, password=password)


def configure_api(monkeypatch, url, current):
    monkeypatch.setattr(Settings, 'from_env', lambda: Settings(session_database_url=url))
    main.app.dependency_overrides[main.get_agent] = lambda: current


def test_postgres_graph_recreation(session_db):
    async def scenario():
        store = SessionStore(session_db[2])
        await store.open()
        try:
            session_id = (await store.create())['session_id']
            current = Agent(FakeModel(), FakeTools(), Settings())
            await store.turn(session_id, 'query', request(), current)
            other_id = (await store.create())['session_id']
            other = request(two=True).model_copy(update={'question': 'An independent comparison', 'radius_meters': 2500})
            current.model.plan_value = Plan(intent='compare_locations', tools=['aggregate_traffic_collisions'])
            await store.turn(other_id, 'query', other, current)
            saved = await store.retrieve(session_id, Agent(FakeModel(), FakeTools(), Settings()))
            assert saved['messages'] and saved['query_context']['request']['radius_meters'] == 800
            separate = await store.retrieve(other_id, current)
            assert separate['query_context']['request']['radius_meters'] == 2500
            assert len(separate['query_context']['request']['locations']) == 2
            assert separate['messages'][0]['content'] == 'An independent comparison'
            assert saved['messages'][0]['content'] != separate['messages'][0]['content']
            await store.delete(session_id)
            assert (await store.retrieve(other_id, current))['messages'] == separate['messages']
            await store.delete(other_id)
        finally:
            await store.close()
    run(scenario())


def test_postgres_api_restart_followups_isolation_and_delete(session_db, monkeypatch):
    admin_url, reader_url, session_url = session_db
    initial = agent(interpretation(plan=Plan(intent='compare_locations', tools=['count_service_requests']).model_dump(),
                                  time_range=request().time_range.model_dump(mode='json')))
    initial.tools = DatabaseTools(reader_url)
    configure_api(monkeypatch, session_url, initial)
    session_id = other_id = None
    try:
        with client() as api:
            session_id = api.post('/api/v1/sessions', json={}).json()['session_id']
            other_id = api.post('/api/v1/sessions', json={}).json()['session_id']
            proposed = api.post('/api/v1/agent/chat', json={'session_id': session_id,
                'message': 'Compare 9000 Regents Driev and 9500 Gilman Drive within 800 meters in January to March 2026.'})
            assert proposed.status_code == 200
            assert proposed.json()['status'] == 'needs_confirmation'
        # New application lifespan, pool, Agent and compiled conversation graph.
        restarted = agent()
        restarted.tools = DatabaseTools(reader_url)
        plan = Plan(intent='compare_locations', tools=['aggregate_service_requests'],
                    service_request_aggregation={'service_name': 'Pothole'})
        restarted.model.followup = AsyncMock(side_effect=[patch(plan,
            changed_plan_fields=['tools', 'service_request_aggregation.service_name'],
            change_quote='Only road-related complaints (Pothole)'), patch(radius_meters=1200, radius_quote='1,200 meters')])
        configure_api(monkeypatch, session_url, restarted)
        with client() as api:
            loaded = api.get('/api/v1/sessions/'+session_id).json()
            assert len(loaded['messages']) == 2 and loaded['latest_response']['revision'] == 1
            assert restarted.model.interpretations == 0
            assert api.post('/api/v1/agent/chat', json={'session_id': session_id,
                'action': 'confirm', 'revision': 2}).status_code == 409
            completed = api.post('/api/v1/agent/chat', json={'session_id': session_id,
                'action': 'confirm', 'revision': 1}).json()
            assert completed['status'] == 'complete'
            assert len(completed['result']['tool_calls']) == 2
            verify_claims([Claim.model_validate(c) for c in completed['result']['claims']],
                [EvidenceRecord.model_validate(e) for e in completed['result']['evidence']])
            baseline = api.get('/api/v1/sessions/'+session_id).json()['query_context']['request']
            first = api.post('/api/v1/agent/chat', json={'session_id': session_id,
                'message': 'Only road-related complaints (Pothole)'}).json()
            assert first['status'] == 'complete' and len(first['result']['tool_calls']) == 2
            for call in first['result']['execution_plan']:
                assert call['radius_meters'] == 800 and call['time_range'] == baseline['time_range']
                assert call['service_request_aggregation']['service_name'] == 'Pothole'
            second = api.post('/api/v1/agent/chat', json={'session_id': session_id,
                'message': 'Use 1,200 meters'}).json()
            assert second['radius_meters'] == 1200
            assert all(c['service_request_aggregation']['service_name'] == 'Pothole' for c in second['result']['execution_plan'])
            isolated = api.get('/api/v1/sessions/'+other_id).json()
            assert isolated['messages'] == [] and isolated['query_context'] is None
        again = agent()
        again.tools = DatabaseTools(reader_url)
        again.model.followup = AsyncMock(return_value=patch())
        configure_api(monkeypatch, session_url, again)
        with client() as api:
            loaded = api.get('/api/v1/sessions/'+session_id).json()
            assert loaded['query_context']['request']['radius_meters'] == 1200
            assert loaded['query_context']['request']['locations'] == baseline['locations']
            assert loaded['query_context']['request']['time_range'] == baseline['time_range']
            final = api.post('/api/v1/agent/chat', json={'session_id': session_id, 'message': 'Run that comparison again'}).json()
            assert final['status'] == 'complete' and len(final['result']['tool_calls']) == 2
            assert final['result']['tool_calls'][0]['query_id'] != second['result']['tool_calls'][0]['query_id']
            listing = api.get('/api/v1/sessions').json()
            assert listing[0]['session_id'] == session_id and listing[0]['title'].startswith('Compare')
            assert api.delete('/api/v1/sessions/'+session_id).status_code == 204
            assert api.get('/api/v1/sessions/'+session_id).status_code == 404
            assert api.post('/api/v1/agent/chat', json={'session_id': session_id, 'message': 'Retry'}).status_code == 404
            with psycopg.connect(admin_url) as conn:
                for table in ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes'):
                    assert conn.execute(sql.SQL('SELECT count(*) FROM citylens_checkpoints.{} WHERE thread_id=%s').format(sql.Identifier(table)), (session_id,)).fetchone()[0] == 0
            assert api.delete('/api/v1/sessions/'+other_id).status_code == 204
    finally:
        main.app.dependency_overrides.clear()


def test_postgres_coordinate_contract_permissions_and_busy_lock(session_db, monkeypatch):
    admin_url, reader_url, session_url = session_db
    current = Agent(FakeModel(), DatabaseTools(reader_url), Settings())
    configure_api(monkeypatch, session_url, current)
    try:
        with client() as api:
            session_id = api.post('/api/v1/sessions', json={'title': 'Coordinate test'}).json()['session_id']
            body = request().model_dump(mode='json')
            body['session_id'] = session_id
            result = api.post('/api/v1/agent/query', json=body)
            assert result.status_code == 200 and result.json()['claims']
            saved = api.get('/api/v1/sessions/'+session_id).json()
            assert saved['response_kind'] == 'query'
            assert saved['latest_response'] == result.json()
            with psycopg.connect(admin_url, autocommit=True) as conn:
                conn.execute('SELECT pg_advisory_lock(hashtextextended(%s,1303))', (session_id,))
                assert api.delete('/api/v1/sessions/'+session_id).status_code == 409
                assert api.post('/api/v1/agent/query', json=body).status_code == 409
                conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,1303))', (session_id,))
            assert api.delete('/api/v1/sessions/'+session_id).status_code == 204
        with psycopg.connect(session_url) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE citylens.service_requests SET status='tampered'")
        with psycopg.connect(reader_url) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute('DELETE FROM citylens.sessions')
    finally:
        main.app.dependency_overrides.clear()


def test_actual_backend_process_restart(session_db, tmp_path):
    # Both interpreters explicitly disable dotenv and use only this fixture's
    # disposable database. Never start a server with developer credentials.
    code = '''
import asyncio, os, sys
from pathlib import Path
import uvicorn
from backend import config
config.ENV_FILE = Path(os.environ['SESSION_TEST_NO_ENV'])
from backend import main
from backend.agent.settings import Settings
from backend.agent.workflow import Agent
from backend.agent_tests.helpers import FakeModel, FakeTools
settings = Settings(session_database_url=os.environ['SESSION_TEST_URL'])
Settings.from_env = classmethod(lambda cls: settings)
main.app.dependency_overrides[main.get_agent] = lambda: Agent(FakeModel(), FakeTools(), settings)
server = uvicorn.Server(uvicorn.Config(main.app, host='127.0.0.1', port=int(os.environ['SESSION_TEST_PORT']), log_level='error'))
with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform=='win32' else None) as runner:
    runner.run(server.serve())
'''
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, SESSION_TEST_URL=session_db[2], SESSION_TEST_PORT=str(port),
               SESSION_TEST_NO_ENV=str(tmp_path/'does-not-exist.env'))
    base_url = f'http://127.0.0.1:{port}'
    process = None

    def start():
        child = subprocess.Popen([sys.executable, '-c', code], cwd=Path(__file__).parents[2],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if child.poll() is not None:
                raise RuntimeError('Isolated session test server exited during startup')
            try:
                if httpx.get(base_url+'/api/v1/status', timeout=.3).status_code == 200:
                    return child
            except httpx.TransportError:
                pass
            time.sleep(.1)
        child.terminate()
        child.wait(timeout=5)
        raise RuntimeError('Isolated session test server did not become ready')

    try:
        process = start()
        with httpx.Client(base_url=base_url, timeout=10) as api:
            session_id = api.post('/api/v1/sessions', json={}).json()['session_id']
            response = api.post('/api/v1/agent/query', json={**request().model_dump(mode='json'), 'session_id': session_id}).json()
            assert response['claims']
        process.terminate()
        process.wait(timeout=5)
        process = start()
        with httpx.Client(base_url=base_url, timeout=10) as api:
            saved = api.get('/api/v1/sessions/'+session_id).json()
            assert saved['latest_response'] == response and len(saved['messages']) == 2
            assert saved['query_context']['request']['locations'] == request().model_dump(mode='json')['locations']
            assert api.delete('/api/v1/sessions/'+session_id).status_code == 204
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)

