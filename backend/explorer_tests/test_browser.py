"""Optional real-browser UI/bridge checks with deterministic fixture data.

This verifies interaction, not production database parity or live LLM semantics.
Install playwright; uses installed Microsoft Edge without downloading a browser.
"""
import asyncio
import os
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import AsyncMock

import pytest
import uvicorn

playwright = pytest.importorskip('playwright.sync_api')
from backend.main import app, get_agent
from backend.explorer_api import get_store
from backend.explorer_store import ExplorerStore
from backend.agent.workflow import Agent
from backend.agent.settings import Settings
from backend.agent_tests.helpers import FakeModel, FakeTools
from backend.explorer_tests.test_unified import plan, summary


HIERARCHY=[{'main_category':'Graffiti','subcategories':['Park graffiti','Public graffiti']},
           {'main_category':'Parking & Vehicles','subcategories':['Parking obstruction']},
           {'main_category':'Roads & Sidewalks','subcategories':['Pothole']}]


class BrowserStore:
    """Deterministic stand-in for ExplorerStore; records every call's scope."""
    def __init__(self):
        self.calls=[]

    def metadata(self, dataset='service_requests'):
        facets={key:[dict(label=label,count=3)] for key,label in [
            ('category','Pothole' if dataset == 'service_requests' else 'Traffic collision'),
            ('status','New' if dataset == 'service_requests' else 'matched'),('community','La Jolla'),('zipcode','92037'),
            ('main_category','Roads & Sidewalks'),('sub_category','Pothole')]}
        return dict(records=3, first_date='2026-01-01 00:00:00',last_date='2026-10-26 00:00:00',facets=facets,
                    category_hierarchy=HIERARCHY if dataset == 'service_requests' else [],
                    classification=dict(active_mapping_sha256='fixture',detail_match=1,service_default=2,needs_review=0,
                                        unmapped=0,unclassified=0,mapping_current=True) if dataset == 'service_requests' else None,
                    historical_csv_records=31667,historical_count_difference=-31664,limitations=['Synthetic browser fixture.'])

    @staticmethod
    def total(scope, area):
        value={'Graffiti':2,'Parking & Vehicles':1}.get(scope.main_category,3)
        if scope.sub_category: value=1
        return value-1 if area is not None else value

    def summary(self, scope, bounds=None, area=None, **kwargs):
        self.calls.append(('summary',scope.model_dump(mode='json'),area))
        if scope.main_category == 'Parking & Vehicles':
            time.sleep(1.5)  # a slow, soon-stale response
        total=self.total(scope,area); main=scope.main_category or 'Roads & Sidewalks'
        result=dict(**summary(total),closed=0,communities=1,unlocated=0,linked=0)
        result['months']=[dict(month=f'2026-{i:02}',count=(total if i == 1 else 0) if i <= 10 else None) for i in range(1,13)]
        result['main_categories']=[dict(label=main,count=total)]
        result['sub_categories']=[dict(main_category=main,sub_category=scope.sub_category or 'Pothole',count=total)]
        return result

    def comparison(self, scope, dimension, area=None):
        self.calls.append((dimension,scope.model_dump(mode='json'),area))
        total=self.total(scope,area); main=scope.main_category or 'Roads & Sidewalks'; sub=scope.sub_category or 'Pothole'
        key='month' if dimension == 'monthly' else 'community'
        return [{key:'2026-01' if key == 'month' else 'La Jolla','main_category':main,'sub_category':sub,'count':total}] if total else []

    def map(self, scope, bounds, zoom, area=None):
        self.calls.append(('map',scope.model_dump(mode='json'),area))
        return dict(mode='points',total=1,items=[dict(id='fixture',lat=32.85,lng=-117.25,category='Pothole',status='New')])

    def requests(self, scope, limit, offset, area=None):
        self.calls.append(('requests',scope.model_dump(mode='json'),area))
        return dict(total=1,items=[dict(id='fixture',requested='2026-01-01',category='Pothole',community='La Jolla',status='New',
                                        main_category='Roads & Sidewalks',sub_category='Pothole',classification_method='service_default')])

    def detail(self, id, dataset):
        return dict(id=id,requested='2026-01-01',category='Pothole',status='New',community='La Jolla',zipcode='92037',address='Test',lat=32.85,lng=-117.25,closed=None,
                    main_category='Roads & Sidewalks',sub_category='Pothole',classification_method='service_default')


def test_agent_first_browser_map_scope_candidates_and_responsive_layout(monkeypatch):
    edge=Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
    if not edge.exists():
        pytest.skip('Optional browser test requires installed Microsoft Edge.')
    from backend.agent_tests.test_agent_ux import make_agent,conventional
    from backend.agent_tests.test_location_flow import Geocoder
    current=make_agent([conventional(),conventional('3969 Miramar St','B')])
    current.model.plan_viewport=AsyncMock(return_value=plan())
    settings=Settings(google_maps_browser_api_key='mock-browser-key',openai_api_key='test-only',model='mock')
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:settings))
    fixture=BrowserStore()
    app.dependency_overrides[get_agent]=lambda:current
    app.dependency_overrides[get_store]=lambda:fixture
    monkeypatch.setattr(ExplorerStore,'summary',fixture.summary)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    def serve():
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == 'win32' else None) as runner:
            runner.run(server.serve())
    thread=threading.Thread(target=serve,daemon=True); thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started:
            if time.monotonic()>deadline:raise RuntimeError('Browser fixture server failed to start')
            time.sleep(.05)
        with playwright.sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.set_default_timeout(10000)
            errors=[]; requests=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda request:requests.append(request.post_data_json) if request.url.endswith('/api/v1/agent/chat') else None)
            sdk=Path('frontend/tests/google-maps.mock.js').read_text(encoding='utf-8')
            page.route('https://maps.googleapis.com/maps/api/js*',lambda route:route.fulfill(content_type='application/javascript',body=sdk))
            page.route('https://tile.openstreetmap.org/**',lambda route:route.abort())
            page.goto(f'http://127.0.0.1:{port}/')
            page.locator('#metric-total').filter(has_text='3').wait_for()
            page.locator('#map[data-map-provider="google-maps-mock"]').wait_for()
            assert page.locator('iframe').count()==0
            assert page.locator('.investigation input').count()==0
            page.locator('#category').select_option('Pothole')
            page.locator('#chat-input').fill('Summarize Get It Done in my current map view.')
            page.locator('#chat-send').click()
            page.locator('.assistant-message h2').filter(has_text='Your map-view investigation').wait_for()
            assert len(page.evaluate('window.CityLensExplorer.map.analysis'))==1
            assert current.model.plan_viewport.call_args.args[1]['filters']['category'] is None
            page.locator('#chat-input').fill('Compare traffic collisions near 9237 Regents Rd and 3969 Miramar St in 2026 within 800 meters.')
            page.locator('#chat-send').click()
            page.locator('.assistant-message h2').filter(has_text='Location comparison').wait_for()
            assert page.locator('.result-kpi strong').all_text_contents()[-2:]==['7','11']
            assert page.evaluate('window.CityLensExplorer.map.analysis.length')==4
            assert current.tools.calls[0].time_range.end.year==2027
            assert current.tools.calls[0].radius_meters==800
            assert 'viewport_context' not in requests[-1]
            assert not any(key in requests[-1] for key in ['time_range','radius_meters','preferences'])
            assert page.locator('#metric-total').inner_text()=='3'
            Path('.local').mkdir(exist_ok=True)
            page.screenshot(path='.local/ux-investigation-desktop.png',full_page=True)
            page.locator('#dataset').select_option('traffic_collisions')
            page.locator('#metric-closed-label').filter(has_text='LOCATED REPORTS').wait_for()
            assert page.locator('#community').is_disabled()
            page.locator('#clear-analysis').click()
            assert page.evaluate('window.CityLensExplorer.map.analysis.length')==0
            current.conversations.geocoder=Geocoder('needs_confirmation')
            page.locator('#new-investigation').click()
            page.locator('#chat-input').fill('Compare traffic collisions near 9237 Regents Rd and 3969 Miramar St in 2026.')
            page.locator('#chat-send').click()
            page.locator('input[name="candidate-A"]').wait_for()
            page.locator('input[name="candidate-A"]').check();page.locator('input[name="candidate-B"]').check()
            page.get_by_role('button',name='Confirm selected candidates',exact=True).click()
            page.locator('.assistant-message h2').filter(has_text='Location comparison').wait_for()
            assert len(current.tools.calls)==4
            page.set_viewport_size({'width':390,'height':844})
            page.locator('#nav-explorer').click()
            assert page.locator('#records-heading').inner_text()=='Matching reports'
            page.screenshot(path='.local/ux-investigation-mobile.png',full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert errors==[]
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);app.dependency_overrides.clear()


def test_open_map_fallback_without_google_browser_key(monkeypatch):
    """No GOOGLE_MAPS_BROWSER_API_KEY: vendored Leaflet keeps the map, overlays
    and viewport queries working; Google Places search stays disabled."""
    edge=Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
    if not edge.exists():
        pytest.skip('Optional browser test requires installed Microsoft Edge.')
    from backend.agent_tests.test_agent_ux import make_agent,conventional,entity
    from backend.agent.user_geocoding import AddressResolution
    current=make_agent([conventional(),conventional('3969 Miramar St','B')])
    current.model.plan_viewport=AsyncMock(return_value=plan())
    settings=Settings(google_places_api_key='server-only',openai_api_key='test-only',model='mock')
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:settings))
    fixture=BrowserStore()
    app.dependency_overrides[get_agent]=lambda:current
    app.dependency_overrides[get_store]=lambda:fixture
    monkeypatch.setattr(ExplorerStore,'summary',fixture.summary)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    def serve():
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == 'win32' else None) as runner:
            runner.run(server.serve())
    thread=threading.Thread(target=serve,daemon=True); thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started:
            if time.monotonic()>deadline:raise RuntimeError('Browser fixture server failed to start')
            time.sleep(.05)
        with playwright.sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.set_default_timeout(10000)
            errors=[]; google=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda request:google.append(request.url) if 'googleapis' in request.url else None)
            page.route('https://tile.openstreetmap.org/**',lambda route:route.abort())
            page.goto(f'http://127.0.0.1:{port}/')
            page.locator('#map.leaflet-container').wait_for()
            page.locator('#metric-total').filter(has_text='3').wait_for()
            assert page.evaluate('window.CityLensExplorer.map.provider')=='open_map'
            page.wait_for_function('window.CityLensExplorer.map.reports.getLayers().length===1')
            page.locator('#chat-input').fill('Summarize Get It Done in my current map view.')
            page.locator('#chat-send').click()
            page.locator('.assistant-message h2').filter(has_text='Your map-view investigation').wait_for()
            assert page.evaluate('window.CityLensExplorer.map.analysis.getLayers().length')==1
            page.locator('#chat-input').fill('Compare traffic collisions near 9237 Regents Rd and 3969 Miramar St in 2026 within 500 meters.')
            page.locator('#chat-send').click()
            page.locator('.assistant-message h2').filter(has_text='Location comparison').wait_for()
            assert page.evaluate('window.CityLensExplorer.map.analysis.getLayers().map(l=>l.getRadius())')==[500,500]
            assert current.tools.calls[0].radius_meters==500
            # Unresolved building name: manual pin offered, Google search disabled without a Google map.
            class Missing:
                def resolve(self,address):return AddressResolution('no_match',[])
            named=make_agent([entity('One Miramar Street Apartments')],Missing())
            app.dependency_overrides[get_agent]=lambda:named
            page.locator('#new-investigation').click()
            page.locator('#chat-input').fill('Find traffic collisions near One Miramar Street Apartments in 2026.')
            page.locator('#chat-send').click()
            page.get_by_role('button',name='Pick on independent map').wait_for()
            assert page.get_by_role('button',name='Search Google Places').is_disabled()
            assert named.tools.calls==[]
            assert google==[] and errors==[]
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);app.dependency_overrides.clear()


def test_dashboard_hierarchy_shared_filters_stale_responses_and_analysis_area(monkeypatch):
    """Dashboard below the map: hierarchical filters, one shared filter set for
    every panel, stale-response protection and an explicit analysis area that
    never reaches the Agent."""
    edge=Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
    if not edge.exists():
        pytest.skip('Optional browser test requires installed Microsoft Edge.')
    from backend.agent_tests.test_agent_ux import make_agent,conventional
    current=make_agent([conventional()])
    current.model.plan_viewport=AsyncMock(return_value=plan())
    settings=Settings(openai_api_key='test-only',model='mock')
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:settings))
    fixture=BrowserStore()
    app.dependency_overrides[get_agent]=lambda:current
    app.dependency_overrides[get_store]=lambda:fixture
    monkeypatch.setattr(ExplorerStore,'summary',fixture.summary)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    def serve():
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == 'win32' else None) as runner:
            runner.run(server.serve())
    thread=threading.Thread(target=serve,daemon=True); thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started:
            if time.monotonic()>deadline:raise RuntimeError('Browser fixture server failed to start')
            time.sleep(.05)
        with playwright.sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.set_default_timeout(10000)
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('https://tile.openstreetmap.org/**',lambda route:route.abort())
            page.goto(f'http://127.0.0.1:{port}/')
            for card in ['trend','distribution','heatmap','community','records']:
                page.locator(f'#card-{card}[data-state=ready]').wait_for()
            assert page.locator('#metric-total').inner_text()=='3'
            # The dashboard replaced the old records section and follows the map.
            assert page.evaluate("document.getElementById('map').compareDocumentPosition(document.getElementById('dashboard')) & Node.DOCUMENT_POSITION_FOLLOWING")
            assert page.locator('#category-chart .category-row').count()==1

            # Hierarchy: the main category restricts subcategories; an incompatible
            # subcategory is reset when the main category changes.
            assert page.locator('#sub-category').is_disabled()
            page.select_option('#main-category','Graffiti')
            assert page.eval_on_selector_all('#sub-category option','o=>o.map(x=>x.value)')==['','Park graffiti','Public graffiti']
            page.select_option('#sub-category','Park graffiti')
            page.select_option('#main-category','Parking & Vehicles')
            assert page.input_value('#sub-category')==''
            assert page.eval_on_selector_all('#sub-category option','o=>o.map(x=>x.value)')==['','Parking obstruction']

            # Stale protection: a slow Parking summary must not overwrite Graffiti.
            page.evaluate("document.getElementById('filters').requestSubmit()")
            page.select_option('#main-category','Graffiti')
            page.evaluate("document.getElementById('filters').requestSubmit()")
            page.locator('#metric-total').filter(has_text='2').wait_for()
            page.wait_for_timeout(2000)
            assert page.locator('#metric-total').inner_text()=='2'
            assert page.locator('#heatmap-heading').inner_text()=='Monthly comparison by subcategory'
            # Concurrent requests may arrive in any order; every panel must have
            # been queried with the identical Graffiti scope.
            graffiti={name:scope for name,scope,_ in fixture.calls if scope['main_category']=='Graffiti'}
            assert set(graffiti)=={'summary','monthly','community','map','requests'}
            assert all(scope==graffiti['summary'] for scope in graffiti.values())

            # Explicit analysis area: applied to every panel and drawn on the map.
            page.locator('#area-use-view').click()
            page.locator('#metric-total').filter(has_text='1').wait_for()
            assert page.locator('#area-clear').is_visible()
            assert page.evaluate('window.CityLensExplorer.map.scope.getLayers().length')==1
            page.wait_for_timeout(300)
            latest={name:area for name,_,area in fixture.calls}
            assert all(area is not None for area in latest.values()), latest
            # Panning alone never changes dashboard totals.
            before=sum(1 for call in fixture.calls if call[0]=='summary')
            page.evaluate('window.CityLensExplorer.map.map.panBy([120,40],{animate:false})')
            page.wait_for_timeout(600)
            assert sum(1 for call in fixture.calls if call[0]=='summary')==before
            assert fixture.calls[-1][0]=='map'
            assert page.locator('#metric-total').inner_text()=='1'

            # An explicit map-view question sends map bounds and the applied
            # filters, never the dashboard analysis area.
            page.locator('#chat-input').fill('Summarize Get It Done in my current map view.')
            page.locator('#chat-send').click()
            page.locator('.assistant-message h2').filter(has_text='Your map-view investigation').wait_for()
            context=current.model.plan_viewport.call_args.args[1]
            assert not any(key.startswith('area') for key in context['filters'])
            assert context['filters']['main_category']=='Graffiti'

            page.locator('#area-clear').click()
            page.locator('#metric-total').filter(has_text='2').wait_for()
            assert page.evaluate('window.CityLensExplorer.map.scope.getLayers().length')==0
            page.locator('#reset').click()
            page.locator('#metric-total').filter(has_text='3').wait_for()
            assert page.locator('#sub-category').is_disabled()
            Path('.local').mkdir(exist_ok=True)
            page.locator('#explorer-panel').screenshot(path='.local/dashboard-fixture.png')
            assert errors==[]
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);app.dependency_overrides.clear()
