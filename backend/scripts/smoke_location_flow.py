"""Small ASGI conversation smoke: mock model/Census, real read-only Agent SQL.

No paid/external API calls. The fixture pins below are TEST coordinates and do
not claim to geocode these street addresses. Never run ingestion or batch workers.
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import psycopg

from backend.agent.location_models import (AddressProposal,GeocodedCandidate,InterpretationOutput)
from backend.agent.user_geocoding import AddressResolution
from backend.agent.models import Composition,Location,Plan
from backend.agent.settings import Settings
from backend.agent.tools import DatabaseTools,ensure_reader
from backend.agent.workflow import Agent
from backend.main import app,get_agent

QUESTION='Compare Pothole complaints near 9000 Regents Driev and 9500 Gilman Drive within 800 meters over the past six months.'


class MockModel:
    async def interpret_locations(self,question,defaults,reference_time):
        addresses=[AddressProposal(id=id,house_number=number,street_name=street,street_suffix='Dr',
            direction=None,city='San Diego',state='CA',source_quote=quote)
            for id,number,street,quote in [('A','9000','Regents','9000 Regents Driev'),('B','9500','Gilman','9500 Gilman Drive')]]
        plan=Plan(intent='compare_locations',tools=['aggregate_service_requests'],
            service_request_aggregation=dict(service_name='Pothole',group_by='status'),filter_quote='Pothole')
        return InterpretationOutput.model_validate(dict(locations=[a.model_dump() for a in addresses],
            time_range=dict(start='2026-05-02T00:00:00-07:00',end='2026-11-02T00:00:00-08:00',calendar_timezone='America/Los_Angeles'),
            radius_meters=800,plan=plan.model_dump(),clarification=None,unsupported_filters=[]))
    async def plan(self,request): raise AssertionError('Confirmed scope must not be re-planned')
    async def compose(self,claims):
        return Composition(ordered_claim_ids=[c.claim_id for c in claims],introduction='comparison_only')


class MockGeocoder:
    calls=0
    def resolve(self,address):
        self.calls+=1
        candidate=GeocodedCandidate(candidate_id='fixture_'+address.id,matched_address='[MOCK] '+address.display,
            location=Location(id=address.id,label='[MOCK] '+address.display,
                latitude=32.84 if address.id=='A' else 32.85,longitude=-117.25 if address.id=='A' else -117.24),
            provider='MOCK Census fixture',precision='interpolated_address')
        return AddressResolution('resolved',[candidate])


def main():
    settings=Settings.from_env()
    with psycopg.connect(settings.database_url) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        ensure_reader(conn)
        before=conn.execute('''SELECT count(*),count(location),count(*) FILTER(WHERE geocode_status='matched')
            FROM citylens.traffic_collisions''').fetchone()
    geocoder=MockGeocoder()
    agent=Agent(MockModel(),DatabaseTools(settings.database_url),settings,geocoder=geocoder)
    app.dependency_overrides[get_agent]=lambda:agent
    try:
        with TestClient(app) as client:
            proposed=client.post('/api/v1/agent/chat',json=dict(message=QUESTION)).json()
            assert proposed['status']=='needs_confirmation' and geocoder.calls==0
            final=client.post('/api/v1/agent/chat',json=dict(action='confirm',conversation_id=proposed['conversation_id'],revision=proposed['revision'])).json()
            assert final['status']=='complete' and geocoder.calls==2
            result=final['result']
            assert result['status']=='partial' and all(c['status']=='ok' for c in result['tool_calls'])
            assert len(result['evidence'])==len(result['tool_calls'])==2 and result['comparison']['metrics']
            first,second=result['execution_plan']
            assert {k:v for k,v in first.items() if k!='location'}=={k:v for k,v in second.items() if k!='location'}
    finally: app.dependency_overrides.clear()
    with psycopg.connect(settings.database_url) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        after=conn.execute('''SELECT count(*),count(location),count(*) FILTER(WHERE geocode_status='matched')
            FROM citylens.traffic_collisions''').fetchone()
    assert before==after
    artifact=dict(external_apis='MOCKED; fixture pins are not real address geocodes',
        database='actual configured restricted Agent reader',before=before,after=after,
        proposal=proposed,final=final,passed=True)
    target=Path(__file__).resolve().parents[1]/'audits/2026-11-02-p1_2-smoke.json'
    target.write_text(json.dumps(artifact,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps(dict(passed=True,external_api_calls=0,geocodes='mocked',
        sql_calls=2,values=[e['value'] for e in result['evidence']],collision_counts_unchanged=True)))


if __name__=='__main__':
    try: main()
    except Exception as exc:
        raise SystemExit('Location smoke failed: '+type(exc).__name__+'; connection details withheld') from None
