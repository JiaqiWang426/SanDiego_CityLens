"""Read-only map API over the same PostgreSQL reader as the Agent."""
from datetime import date
from typing import Literal
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from .agent.settings import Settings
from .agent.tools import database_failure_code
from .explorer_models import Bounds, ExplorerFilters
from .explorer_store import ExplorerStore

router = APIRouter(prefix='/api/v1/explorer', tags=['Service request explorer'])


def get_store():
    return ExplorerStore(Settings.from_env().database_url)


def filters(start: date = date(2026, 1, 1), end: date = date(2026, 12, 31),
            category: str | None = Query(None, max_length=200), status: str | None = Query(None, max_length=100),
            community: str | None = Query(None, max_length=200), zipcode: str | None = Query(None, max_length=20),
            exclude_linked: bool = False,
            dataset: Literal['service_requests', 'traffic_collisions'] = 'service_requests'):
    try:
        return ExplorerFilters(**locals())
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from None


def execute(operation):
    try:
        return operation()
    except RuntimeError:
        raise HTTPException(503, 'database_not_configured: configure AGENT_DATABASE_URL; SQLite fallback is disabled.') from None
    except (psycopg.Error, PermissionError) as exc:
        code = database_failure_code(exc, 'connect' if isinstance(exc, psycopg.OperationalError) else 'query')
        raise HTTPException(503, 'Explorer database unavailable: ' + code) from None


@router.get('/metadata')
def metadata(dataset: Literal['service_requests', 'traffic_collisions'] = 'service_requests', store=Depends(get_store)):
    return execute(lambda: store.metadata(dataset))


@router.get('/summary')
def summary(scope=Depends(filters), store=Depends(get_store)):
    return execute(lambda: store.summary(scope))


@router.get('/map')
def map_points(west: float = Query(-180, ge=-180, le=180), south: float = Query(-90, ge=-90, le=90),
               east: float = Query(180, ge=-180, le=180), north: float = Query(90, ge=-90, le=90),
               zoom: int = Query(11, ge=0, le=19), scope=Depends(filters), store=Depends(get_store)):
    try:
        bounds = Bounds(west=west, south=south, east=east, north=north)
    except ValidationError:
        raise HTTPException(422, 'Invalid map bounds.') from None
    return execute(lambda: store.map(scope, bounds, zoom))


@router.get('/requests')
def requests(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
             scope=Depends(filters), store=Depends(get_store)):
    return execute(lambda: store.requests(scope, limit, offset))


@router.get('/requests/{request_id}')
def detail(request_id: str, dataset: Literal['service_requests', 'traffic_collisions'] = 'service_requests', store=Depends(get_store)):
    row = execute(lambda: store.detail(request_id, dataset))
    if row is None:
        raise HTTPException(404, 'Record not found in the selected 2026 dataset scope.')
    return row
