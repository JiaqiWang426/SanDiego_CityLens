"""Read-only map and dashboard API over the same PostgreSQL reader as the Agent.

Every analytical route (/summary, /comparisons/*, /requests, /map) shares one
filter dependency, so KPIs, charts, records and markers describe the same scope.
The map's viewport (west/south/east/north) only selects what to draw; the
optional ``area_*`` rectangle is an explicit analytical scope applied everywhere.
"""
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
Dataset = Literal['service_requests', 'traffic_collisions']


def get_store():
    return ExplorerStore(Settings.from_env().database_url)


def filters(start: date = date(2026, 1, 1), end: date = date(2026, 12, 31),
            category: str | None = Query(None, max_length=200, description='Original source service name'),
            status: str | None = Query(None, max_length=100),
            community: str | None = Query(None, max_length=200), zipcode: str | None = Query(None, max_length=20),
            exclude_linked: bool = False,
            dataset: Dataset = 'service_requests',
            main_category: str | None = Query(None, max_length=200),
            sub_category: str | None = Query(None, max_length=200)):
    try:
        return ExplorerFilters(**locals())
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from None


def analysis_area(area_west: float | None = Query(None, ge=-180, le=180),
                  area_south: float | None = Query(None, ge=-90, le=90),
                  area_east: float | None = Query(None, ge=-180, le=180),
                  area_north: float | None = Query(None, ge=-90, le=90)):
    values = [area_west, area_south, area_east, area_north]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise HTTPException(422, 'Provide all four area_* bounds, or none.')
    try:
        return Bounds(west=area_west, south=area_south, east=area_east, north=area_north)
    except ValidationError:
        raise HTTPException(422, 'Invalid analysis area bounds.') from None


def execute(operation):
    try:
        return operation()
    except RuntimeError:
        raise HTTPException(503, 'database_not_configured: configure AGENT_DATABASE_URL; SQLite fallback is disabled.') from None
    except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable):
        raise HTTPException(503, 'classification_migration_missing: run python -m backend.db migrate.') from None
    except (psycopg.Error, PermissionError) as exc:
        code = database_failure_code(exc, 'connect' if isinstance(exc, psycopg.OperationalError) else 'query')
        raise HTTPException(503, 'Explorer database unavailable: ' + code) from None


@router.get('/metadata')
def metadata(dataset: Dataset = 'service_requests', store=Depends(get_store)):
    """Coverage, facets, the active category hierarchy and classification status."""
    return execute(lambda: store.metadata(dataset))


@router.get('/summary')
def summary(scope=Depends(filters), area=Depends(analysis_area), store=Depends(get_store)):
    """KPIs plus month, original category, status, main and subcategory distributions."""
    return execute(lambda: store.summary(scope, area=area))


@router.get('/comparisons/monthly')
def monthly_comparison(scope=Depends(filters), area=Depends(analysis_area), store=Depends(get_store)):
    """Observed month/main/sub groups for all matching requests."""
    return {'data': execute(lambda: store.comparison(scope, 'monthly', area=area))}


@router.get('/comparisons/community')
def community_comparison(scope=Depends(filters), area=Depends(analysis_area), store=Depends(get_store)):
    """Community/main/sub groups; unknown communities stay explicit JSON null."""
    return {'data': execute(lambda: store.comparison(scope, 'community', area=area))}


@router.get('/map')
def map_points(west: float = Query(-180, ge=-180, le=180), south: float = Query(-90, ge=-90, le=90),
               east: float = Query(180, ge=-180, le=180), north: float = Query(90, ge=-90, le=90),
               zoom: int = Query(11, ge=0, le=19), scope=Depends(filters), area=Depends(analysis_area),
               store=Depends(get_store)):
    try:
        bounds = Bounds(west=west, south=south, east=east, north=north)
    except ValidationError:
        raise HTTPException(422, 'Invalid map bounds.') from None
    return execute(lambda: store.map(scope, bounds, zoom, area=area))


@router.get('/requests')
def requests(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
             scope=Depends(filters), area=Depends(analysis_area), store=Depends(get_store)):
    return execute(lambda: store.requests(scope, limit, offset, area=area))


@router.get('/requests/{request_id}')
def detail(request_id: str, dataset: Dataset = 'service_requests', store=Depends(get_store)):
    row = execute(lambda: store.detail(request_id, dataset))
    if row is None:
        raise HTTPException(404, 'Record not found in the selected 2026 dataset scope.')
    return row
