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


class BrowserStore:
    def metadata(self, dataset='service_requests'):
        facets={key:[dict(label=label,count=3)] for key,label in [
            ('category','Pothole' if dataset == 'service_requests' else 'Traffic collision'),
            ('status','New' if dataset == 'service_requests' else 'matched'),('community','La Jolla'),('zipcode','92037')]}
        return dict(records=3, first_date='2026-01-01 00:00:00',last_date='2026-10-26 00:00:00',facets=facets,
                    historical_csv_records=31667,historical_count_difference=-31664,limitations=['Synthetic browser fixture.'])

    def summary(self, scope, bounds=None, **kwargs):
        return dict(**summary(),closed=0,communities=1,unlocated=0,linked=0)

    def map(self, scope, bounds, zoom):
        return dict(mode='points',total=1,items=[dict(id='fixture',lat=32.85,lng=-117.25,category='Pothole',status='New')])

    def requests(self, scope, limit, offset):
        return dict(total=1,items=[dict(id='fixture',requested='2026-01-01',category='Pothole',community='La Jolla',status='New')])

    def detail(self, id, dataset):
        return dict(id=id,requested='2026-01-01',category='Pothole',status='New',community='La Jolla',zipcode='92037',address='Test',lat=32.85,lng=-117.25,closed=None)


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
            assert page.locator('#records-heading').inner_text()=='Explorer Results'
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
