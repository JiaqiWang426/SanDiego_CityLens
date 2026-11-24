"""API contract tests with a recording store: no database or model needed."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import explorer_api
from backend.agent.location_models import ChatTurn
from backend.agent.map_analysis import ViewportAnalysis
from backend.explorer_models import ExplorerFilters
from backend.explorer_store import ExplorerStore
from backend.explorer_tests.test_unified import context, plan, summary

PREFIX = '/api/v1/explorer'
AREA = dict(area_west=-117.3, area_south=32.8, area_east=-117.2, area_north=32.9)


class Recorder:
    def __init__(self):
        self.calls = []

    def summary(self, scope, bounds=None, *, area=None, **kwargs):
        self.calls.append(('summary', scope, area))
        return {'total': 0}

    def comparison(self, scope, dimension, area=None):
        self.calls.append((dimension, scope, area))
        return []

    def map(self, scope, bounds, zoom, area=None):
        self.calls.append(('map', scope, area))
        return {'mode': 'points', 'total': 0, 'items': []}

    def requests(self, scope, limit, offset, area=None):
        self.calls.append(('requests', scope, area))
        return {'total': 0, 'items': []}


@pytest.fixture
def client():
    recorder = Recorder()
    app = FastAPI()
    app.include_router(explorer_api.router)
    app.dependency_overrides[explorer_api.get_store] = lambda: recorder
    with TestClient(app) as instance:
        instance.recorder = recorder
        yield instance


ROUTES = ['/summary', '/comparisons/monthly', '/comparisons/community', '/map', '/requests']


def test_every_analytical_route_receives_the_same_filters_and_area(client):
    params = dict(start='2026-02-01', end='2026-03-31', main_category='Graffiti', sub_category='Park graffiti',
                  category='Parks Issue', status='Closed', community='LA JOLLA', zipcode='92037', exclude_linked='true', **AREA)
    for route in ROUTES:
        response = client.get(PREFIX + route, params=params)
        assert response.status_code == 200, route
    first = client.recorder.calls[0][1]
    assert all(scope == first for _, scope, _ in client.recorder.calls)
    assert first.main_category == 'Graffiti' and first.sub_category == 'Park graffiti' and first.exclude_linked
    assert all(area is not None and area.west == -117.3 for _, _, area in client.recorder.calls)
    assert client.get(PREFIX + '/comparisons/monthly').json() == {'data': []}


@pytest.mark.parametrize('params', [
    {'area_west': -117.3},                                         # partial area
    {**AREA, 'area_west': -117.1},                                 # west >= east
    {'main_category': 'x' * 201},
    {'start': '2025-12-31'}, {'start': '2026-03-02', 'end': '2026-03-01'},
    {'dataset': 'traffic_collisions', 'main_category': 'Graffiti'},
    {'dataset': 'traffic_collisions', 'sub_category': 'Pothole'},
])
@pytest.mark.parametrize('route', ROUTES)
def test_invalid_parameters_are_rejected(client, route, params):
    assert client.get(PREFIX + route, params=params).status_code == 422
    assert client.recorder.calls == []


def test_missing_migration_is_a_clear_503():
    app = FastAPI()
    app.include_router(explorer_api.router)
    store = MagicMock()
    store.summary.side_effect = psycopg.errors.UndefinedColumn('column "main_category" does not exist')
    app.dependency_overrides[explorer_api.get_store] = lambda: store
    response = TestClient(app).get(PREFIX + '/summary')
    assert response.status_code == 503 and 'migrate' in response.json()['detail']


def test_viewport_context_accepts_category_filters_but_not_area():
    filters = ExplorerFilters(main_category='Graffiti', sub_category='Park graffiti')
    assert ExplorerFilters.model_validate(filters.model_dump()).main_category == 'Graffiti'
    with pytest.raises(ValueError):
        ExplorerFilters.model_validate({**filters.model_dump(), 'area_west': -117.3})


def agent_with(output, monkeypatch, result=None):
    agent = MagicMock()
    agent.settings.llm_timeout_seconds = 5
    agent.settings.database_url = 'postgresql://unused'
    agent.model.plan_viewport = AsyncMock(return_value=output)
    execute = MagicMock(return_value=result or summary())
    monkeypatch.setattr(ExplorerStore, 'summary', lambda self, *a, **k: execute(*a, **k))
    return agent, execute


def test_viewport_plan_can_filter_by_main_category_from_an_exact_quote(monkeypatch):
    output = plan(main_category='Roads & Sidewalks', change_quote='only Roads & Sidewalks')
    agent, execute = agent_with(output, monkeypatch)
    turn = ChatTurn(action='ask', message='Summarize only Roads & Sidewalks in my current map view.', viewport_context=context())
    reply, saved = asyncio.run(ViewportAnalysis(agent).run(turn))
    assert reply.status == 'complete'
    assert execute.call_args.args[0].main_category == 'Roads & Sidewalks'
    assert saved['viewport']['filters']['main_category'] == 'Roads & Sidewalks'


def test_viewport_plan_rejects_an_unquoted_category(monkeypatch):
    output = plan(main_category='Graffiti', change_quote='current map view')
    agent, execute = agent_with(output, monkeypatch)
    turn = ChatTurn(action='ask', message='Summarize my current map view.', viewport_context=context())
    reply, _ = asyncio.run(ViewportAnalysis(agent).run(turn))
    assert reply.status == 'needs_clarification' and not execute.called


def test_viewport_rejects_inconsistent_main_category_evidence(monkeypatch):
    broken = summary()
    broken['main_categories'] = [dict(label='Graffiti', count=1)]
    agent, _ = agent_with(plan(), monkeypatch, broken)
    turn = ChatTurn(action='ask', message='Summarize my current map view.', viewport_context=context())
    reply, _ = asyncio.run(ViewportAnalysis(agent).run(turn))
    assert reply.status == 'error' and reply.viewport_analysis is None
