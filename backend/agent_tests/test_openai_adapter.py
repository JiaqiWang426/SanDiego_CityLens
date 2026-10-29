"""Exercise the real OpenAI SDK wire format over an in-memory HTTP transport."""
import asyncio
import json
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from backend.agent.llm import OpenAIModel
from backend.agent.models import Composition,Plan
from backend.agent.settings import Settings
from backend.agent.workflow import Agent
from backend.agent_tests.helpers import FakeTools,request


def test_responses_parse_and_graph_without_network():
    bodies=[]
    def handler(req):
        body=json.loads(req.content)
        bodies.append(body)
        schema=body['text']['format']['name']
        if schema=='Plan':
            parsed=Plan(intent='location_summary',tools=['aggregate_traffic_collisions'])
        else:
            facts=json.loads(body['input'][1]['content'])
            parsed=Composition(ordered_claim_ids=[f['claim_id'] for f in facts],introduction='records_only')
        return httpx.Response(200,json={
            'id':'resp_fixture','object':'response','created_at':0,'status':'completed',
            'model':'fixture-model','output':[{'id':'msg_fixture','type':'message','role':'assistant',
                'status':'completed','content':[{'type':'output_text',
                    'text':parsed.model_dump_json(),'annotations':[]}]}],
        })
    def factory(**kwargs):
        return AsyncOpenAI(**kwargs,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    settings=Settings(openai_api_key='synthetic-not-a-real-key',model='fixture-model')
    with patch('backend.agent.llm.AsyncOpenAI',side_effect=factory):
        response=asyncio.run(Agent(OpenAIModel(settings),FakeTools(),settings).query(request()))
    assert response.status=='partial' and response.evidence[0].value==7
    assert len(bodies)==2
    assert bodies[0]['store'] is False
    assert bodies[0]['text']['format']['strict'] is True
    assert 'raw_record' not in bodies[1]['input'][1]['content']
