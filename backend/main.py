"""Run from the repository root: python -m uvicorn backend.main:app."""
import asyncio
from functools import lru_cache
from uuid import uuid4
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response
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
from .agent.session_models import SessionCreate, SessionMetadata
from .agent.sessions import SessionStore
from .agent.diagnostics import log_failure
from .agent.service_categories import road_scope_suggestions


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_environment()
    settings=Settings.from_env()
    store=SessionStore(settings.session_database_url) if settings.session_database_url else None
    app.state.sessions=store
    try:
        if store:
            await store.open()
        yield
    finally:
        if store:
            await store.close()
        app.state.sessions=None


app=FastAPI(title='San Diego CityLens Phase 0',version='0.1.0',lifespan=lifespan)
# Resolve the sibling frontend directory independently of the shell's cwd.
FRONTEND=Path(__file__).resolve().parent.parent/'frontend'
app.mount('/assets',StaticFiles(directory=FRONTEND),name='assets')


@lru_cache
def get_agent() -> Agent:
    settings=Settings.from_env()
    return Agent(OpenAIModel(settings),DatabaseTools(settings.database_url),settings)


def get_session_store() -> SessionStore:
    store=getattr(app.state,'sessions',None)
    if store is None:
        raise HTTPException(503,'Session persistence is unavailable. Configure SESSION_DATABASE_URL and run the session setup command.')
    return store


@app.post('/api/v1/sessions',response_model=SessionMetadata,status_code=201)
async def create_session(body: SessionCreate=SessionCreate(),store: SessionStore=Depends(get_session_store)):
    return await store.create(body.title)


@app.get('/api/v1/sessions',response_model=list[SessionMetadata])
async def list_sessions(limit: int=Query(default=50,ge=1,le=100),offset: int=Query(default=0,ge=0),
                        store: SessionStore=Depends(get_session_store)):
    return await store.list(limit,offset)


@app.get('/api/v1/sessions/{session_id}')
async def retrieve_session(session_id: str,store: SessionStore=Depends(get_session_store),agent: Agent=Depends(get_agent)):
    return await store.retrieve(session_id,agent)


@app.delete('/api/v1/sessions/{session_id}',status_code=204)
async def delete_session(session_id: str,store: SessionStore=Depends(get_session_store)):
    await store.delete(session_id)
    return Response(status_code=204)


@app.get('/',include_in_schema=False)
def frontend():
    return FileResponse(FRONTEND/'index.html')


@app.get('/api/v1/status')
def configuration(agent: Agent=Depends(get_agent)):
    """Configuration presence only: never expose secrets or assert connectivity."""
    return {'openai_configured':bool(agent.settings.openai_api_key and agent.settings.model),
            'sessions_configured':getattr(app.state,'sessions',None) is not None,
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


@app.get('/api/v1/tools/service-categories')
async def service_category_catalog(agent: Agent=Depends(get_agent)):
    try:
        categories=await asyncio.to_thread(agent.tools.service_categories)
        return {'service_names':[name for name in categories if name is not None],
                'unknown_category_available':None in categories,
                'road_scope_suggestions':road_scope_suggestions(categories),
                'mapping_policy':'Suggestions require explicit category selection; road-related has no automatic mapping'}
    except Exception:
        raise HTTPException(503,'Service category catalog is unavailable') from None


@app.post('/api/v1/agent/query',response_model=QueryResponse)
async def query(request: QueryRequest, agent: Agent=Depends(get_agent)) -> QueryResponse:
    """Validate deterministically before executing the bounded graph."""
    try:
        if request.session_id:
            return QueryResponse.model_validate(await asyncio.wait_for(
                get_session_store().turn(request.session_id,'query',request,agent),agent.settings.request_timeout_seconds))
        return await asyncio.wait_for(agent.query(request),agent.settings.request_timeout_seconds)
    except HTTPException:
        raise
    except TimeoutError:
        return QueryResponse(status='error',intent='undetermined',
            answer='The request exceeded its execution budget. No unverified findings are returned.',
            limitations=['Request deadline exceeded.'],limitation_codes=['request_timeout'],
            tool_trace_id='trace_'+uuid4().hex)
    except Exception as exc:
        # Avoid returning stack traces, provider error bodies or credentials.
        log_failure('internal_execution_error',exc,stage='query',
                    secrets=(agent.settings.openai_api_key,agent.settings.session_database_url))
        return QueryResponse(status='error',intent='undetermined',
            answer='The request could not be completed and verified.',
            limitations=['An internal execution error prevented verification.'],
            limitation_codes=['internal_execution_error'],tool_trace_id='trace_'+uuid4().hex)


@app.post('/api/v1/agent/chat',response_model=ChatResponse)
async def chat(turn: ChatTurn,agent: Agent=Depends(get_agent)) -> ChatResponse:
    """Interpret -> explicit confirmation -> Census -> existing verified graph."""
    try:
        store=getattr(app.state,'sessions',None)
        session_id=turn.session_id
        if session_id is None and turn.conversation_id and len(turn.conversation_id)==32:
            session_id=turn.conversation_id
        if session_id or (store and turn.action=='ask' and not turn.conversation_id):
            store=get_session_store()
            if session_id is None:
                session_id=(await store.create())['session_id']
            return ChatResponse.model_validate(await asyncio.wait_for(
                store.turn(session_id,'chat',turn,agent),agent.settings.request_timeout_seconds))
        return await asyncio.wait_for(agent.chat(turn),agent.settings.request_timeout_seconds)
    except HTTPException:
        raise
    except TimeoutError:
        return ChatResponse(status='error',message='The turn exceeded its execution budget. No unverified answer is returned.',
                            diagnostic_codes=['request_timeout'])
    except Exception as exc:
        log_failure('location_workflow_error',exc,stage='chat',
                    secrets=(agent.settings.openai_api_key,agent.settings.session_database_url))
        return ChatResponse(status='error',message='This turn could not be completed. Start again or correct the pending request.',
                            diagnostic_codes=['location_workflow_error'])
