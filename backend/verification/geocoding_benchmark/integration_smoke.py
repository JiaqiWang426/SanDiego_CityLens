"""Exercise the default provider chain with recorded LIVE responses and real
read-only SQL. No new provider requests, model calls, migrations or data writes.
"""
import json
from pathlib import Path
from urllib.parse import parse_qs,urlparse
from unittest.mock import patch

from fastapi.testclient import TestClient
import psycopg

from backend.agent.location_models import AddressProposal,InterpretationOutput
from backend.agent.models import Plan,Composition
from backend.agent.settings import Settings
from backend.agent.tools import DatabaseTools,ensure_reader
from backend.agent.workflow import Agent
from backend.main import app,get_agent

HERE=Path(__file__).resolve().parent
QUESTION='Compare traffic collisions near 3350 Front St, San Diego, CA 92103 and 9800 Mira Lee Way, San Diego, CA 92126 within 800 meters from May 2 to November 2, 2026.'
HARD_CASE='Traffic collisions near 9500 Gilman Dr, La Jolla, CA 92093 from May 2 to November 2, 2026.'


class ModelFixture:
    async def interpret_locations(self,question,defaults,reference_time):
        if question==QUESTION:
            inputs=[('A','3350','Front','St','San Diego','92103'),('B','9800','Mira Lee','Way','San Diego','92126')]
            intent='compare_locations'
        else:
            inputs=[('A','9500','Gilman','Dr','La Jolla','92093')];intent='location_summary'
        proposals=[AddressProposal(id=id,source_quote=f'{number} {name} {suffix}',house_number=number,
            street_name=name,street_suffix=suffix,direction=None,city=city,state='CA',postal_code=zip)
            for id,number,name,suffix,city,zip in inputs]
        return InterpretationOutput.model_validate(dict(locations=[a.model_dump() for a in proposals],
            plan=Plan(intent=intent,tools=['aggregate_traffic_collisions']).model_dump(),
            time_range=dict(start='2026-05-02T00:00:00-07:00',end='2026-11-02T00:00:00-08:00',calendar_timezone='America/Los_Angeles'),
            radius_meters=800,clarification=None,unsupported_filters=[]))
    async def plan(self,request):raise AssertionError('Saved plan must be reused')
    async def compose(self,claims):return Composition(ordered_claim_ids=[c.claim_id for c in claims],introduction='comparison_only')


class CountingTools(DatabaseTools):
    def __init__(self,url):super().__init__(url);self.calls=[]
    def execute(self,call):self.calls.append(call);return super().execute(call)


def snapshot(url):
    with psycopg.connect(url) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='30s'")
        ensure_reader(conn)
        records,located,digest=conn.execute('''SELECT count(*),count(location),
            md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id)) FROM citylens.traffic_collisions c''').fetchone()
        return dict(records=records,located=located,all_collision_fields_digest=digest)


def main():
    settings=Settings.from_env()
    before=snapshot(settings.database_url)
    replayed=[]
    def replay(url,**kwargs):
        query=parse_qs(urlparse(url).query)
        provider='census' if 'census.gov' in url else 'sangis'
        number=query.get('street',[''])[0].split(' ')[0] if provider=='census' else query['where'][0].split('addrnmbr=')[1].split(' ')[0]
        id={'3350':'02','9800':'04','9500':'01'}[number]
        raw=json.loads((HERE/'raw'/f'{id}-{provider}.json').read_text())
        replayed.append(dict(id=id,provider=provider))
        return raw['payload'],dict(replayed=True,http_attempts=0,source_capture=raw['fetched_at'])
    tools=CountingTools(settings.database_url)
    agent=Agent(ModelFixture(),tools,settings)
    app.dependency_overrides[get_agent]=lambda:agent
    try:
        with patch('backend.agent.user_geocoding.get_json',side_effect=replay),patch('backend.agent.sangis_geocoding.get_json',side_effect=replay),TestClient(app) as client:
            initial=client.post('/api/v1/agent/chat',json=dict(message=QUESTION)).json()
            assert initial['status']=='needs_confirmation' and not replayed and not tools.calls
            result=client.post('/api/v1/agent/chat',json=dict(action='confirm',conversation_id=initial['conversation_id'],revision=initial['revision'])).json()
            assert result['status']=='complete' and len(tools.calls)==2
            assert [r['provider'] for r in result['resolved_locations']]==['US Census Geocoder','SanGIS Address Points']
            assert all(log['status']=='ok' for log in result['result']['tool_calls'])
            a,b=tools.calls
            assert a.model_dump(exclude={'location'})==b.model_dump(exclude={'location'})
            hard=client.post('/api/v1/agent/chat',json=dict(message=HARD_CASE)).json()
            candidates=client.post('/api/v1/agent/chat',json=dict(action='confirm',conversation_id=hard['conversation_id'],revision=hard['revision'])).json()
            assert candidates['status']=='needs_candidate_confirmation' and len(candidates['candidates']['A'])==2 and len(tools.calls)==2
    finally:app.dependency_overrides.clear()
    after=snapshot(settings.database_url)
    assert before==after
    artifact=dict(passed=True,live_api_requests=0,replayed_live_responses=replayed,model='fixture',
        sql_calls=2,sql_account='actual restricted Agent reader',before=before,after=after,
        mixed_provider_comparison=result,ucsd_9500_requires_confirmation=candidates)
    (HERE/'integration-smoke.json').write_text(json.dumps(artifact,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(passed=True,new_provider_requests=0,read_only_sql_calls=2,collision_fingerprint_unchanged=True,ucsd_candidates=2)))


if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit('Integration smoke failed: '+type(exc).__name__+'; credentials withheld') from None
