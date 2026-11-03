"""Run from the repository root: python -m uvicorn backend.main:app."""
import asyncio
from functools import lru_cache
from uuid import uuid4
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent.llm import OpenAIModel
from .agent.models import QueryRequest, QueryResponse
from .agent.settings import Settings
from .agent.tools import DatabaseTools, ToolCall, MetadataCall, tool_log
from .agent.evidence import make_evidence
from .agent.workflow import Agent
from .config import load_environment
from .agent.location_models import ChatTurn,ChatResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_environment()
    yield


app=FastAPI(title='San Diego CityLens Phase 0',version='0.1.0',lifespan=lifespan)
# Resolve the sibling frontend directory independently of the shell's cwd.
FRONTEND=Path(__file__).resolve().parent.parent/'frontend'
app.mount('/assets',StaticFiles(directory=FRONTEND),name='assets')


@lru_cache
def get_agent() -> Agent:
    settings=Settings.from_env()
    return Agent(OpenAIModel(settings),DatabaseTools(settings.database_url),settings)


@app.get('/',include_in_schema=False)
def frontend():
    return FileResponse(FRONTEND/'index.html')


@app.get('/api/v1/status')
def configuration(agent: Agent=Depends(get_agent)):
    """Configuration presence only: never expose secrets or assert connectivity."""
    return {'openai_configured':bool(agent.settings.openai_api_key and agent.settings.model),
            'database_configured':bool(agent.settings.database_url),
            'max_rows':agent.settings.max_rows,
            'answer_mode':'LLM-planned; verified template rendering',
            'logs_mode':'Tool execution logs returned after completion; no hidden reasoning'}


@app.post('/api/v1/tools/run')
async def run_tool(call: ToolCall | MetadataCall,agent: Agent=Depends(get_agent)):
    """Human verification without any model call. Fixed SELECT functions only."""
    if isinstance(call,ToolCall) and call.limit>agent.settings.max_rows:
        raise HTTPException(422,detail='Requested limit exceeds configured AGENT_MAX_ROWS')
    try:
        result=await asyncio.wait_for(asyncio.to_thread(agent.tools.execute,call),
                                     agent.settings.request_timeout_seconds)
        if isinstance(call,MetadataCall):
            return {'tool_call':tool_log(result),'metadata':result.metadata or None,'evidence':None}
        return {'tool_call':tool_log(result),'evidence':make_evidence(result)}
    except TimeoutError:
        raise HTTPException(504,detail='SQL tool execution deadline exceeded') from None
    except Exception:
        raise HTTPException(502,detail='SQL tool result could not be verified') from None


@app.post('/api/v1/agent/query',response_model=QueryResponse)
async def query(request: QueryRequest, agent: Agent=Depends(get_agent)) -> QueryResponse:
    """Validate deterministically before executing the bounded graph."""
    try:
        return await asyncio.wait_for(agent.query(request),agent.settings.request_timeout_seconds)
    except TimeoutError:
        return QueryResponse(status='error',intent='undetermined',
            answer='The request exceeded its execution budget. No unverified findings are returned.',
            limitations=['Request deadline exceeded.'],limitation_codes=['request_timeout'],
            tool_trace_id='trace_'+uuid4().hex)
    except Exception:
        # Avoid returning stack traces, provider error bodies or credentials.
        return QueryResponse(status='error',intent='undetermined',
            answer='The request could not be completed and verified.',
            limitations=['An internal execution error prevented verification.'],
            limitation_codes=['internal_execution_error'],tool_trace_id='trace_'+uuid4().hex)


@app.post('/api/v1/agent/chat',response_model=ChatResponse)
async def chat(turn: ChatTurn,agent: Agent=Depends(get_agent)) -> ChatResponse:
    """Interpret -> explicit confirmation -> Census -> existing verified graph."""
    try:
        return await asyncio.wait_for(agent.chat(turn),agent.settings.request_timeout_seconds)
    except HTTPException:
        raise
    except TimeoutError:
        return ChatResponse(status='error',message='The turn exceeded its execution budget. No unverified answer is returned.',
                            diagnostic_codes=['request_timeout'])
    except Exception:
        return ChatResponse(status='error',message='This turn could not be completed. Start again or correct the pending request.',
                            diagnostic_codes=['location_workflow_error'])
