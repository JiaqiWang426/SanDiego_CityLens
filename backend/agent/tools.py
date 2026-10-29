"""Typed allowlist over existing SQL functions; never accept SQL from the LLM."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from pydantic import Field

from .models import CollisionFilters, Location, Model, TimeRange, ToolName, ToolLog
from .diagnostics import log_failure


class ToolCall(Model):
    name: ToolName
    location: Location
    radius_meters: float = Field(ge=0, le=50000)
    time_range: TimeRange
    collision_filters: CollisionFilters = Field(default_factory=CollisionFilters)
    period: str = Field(pattern=r'^(day|week|month|quarter|year)$')
    exclude_linked_duplicates: bool = False
    limit: int = Field(ge=1, le=1000)


@dataclass
class ToolResult:
    call: ToolCall
    query_id: str = field(default_factory=lambda: 'q_'+uuid4().hex)
    executed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    elapsed_ms: float = 0
    status: str = 'ok'
    rows: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


REGISTRY = {
    'count_service_requests': ('service_requests','citylens.service_requests',
        'get_it_done_requests', 'https://data.sandiego.gov/datasets/get-it-done-reports/'),
    'get_traffic_collisions': ('traffic_collisions','citylens.traffic_collisions',
        'police_recorded_traffic_collisions','https://data.sandiego.gov/datasets/traffic-collisions/'),
    'aggregate_traffic_collisions': ('traffic_collisions','citylens.traffic_collisions',
        'police_recorded_traffic_collisions','https://data.sandiego.gov/datasets/traffic-collisions/'),
}


class ReaderRoleMissing(PermissionError):
    """The required reader group is absent, rather than merely not granted."""


def database_failure_code(exc: Exception,stage: str) -> str:
    """libpq often omits SQLSTATE on startup failures; never log its raw text."""
    state=getattr(exc,'sqlstate',None) or ''
    if isinstance(exc,ReaderRoleMissing):
        return 'reader_role_missing'
    if isinstance(exc,PermissionError):
        return 'reader_role_required'
    if state.startswith('28'):
        return 'database_authentication_failed'
    if isinstance(exc,psycopg.OperationalError) and stage=='connect':
        message=str(exc).lower()  # Classify in memory, never expose credentials.
        if any(marker in message for marker in ('authentication failed','no password supplied',
                'password is required','no pg_hba.conf entry')) or ('role ' in message and 'does not exist' in message):
            return 'database_authentication_failed'
        return 'database_connection_failed'
    if isinstance(exc,psycopg.errors.InsufficientPrivilege):
        return 'database_permission_denied'
    if isinstance(exc,psycopg.errors.UndefinedFunction):
        return 'database_function_missing'
    if isinstance(exc,psycopg.errors.QueryCanceled):
        return 'tool_timeout'
    if isinstance(exc,psycopg.errors.LockNotAvailable):
        return 'database_lock_timeout'
    if state.startswith('08') or isinstance(exc,psycopg.OperationalError):
        return 'database_connection_lost'
    return 'database_execution_failed'


def ensure_reader(conn: psycopg.Connection) -> None:
    """Fail closed for owner/admin/write-capable credentials, even in read-only TX.

    The named reader role must be an active role or inherited membership. We
    inspect effective privileges, including grants inherited through PUBLIC.
    No permissions are changed here. Setup SQL is a separately reviewed step.
    """
    # to_regrole returns NULL for a missing group instead of raising from
    # pg_has_role. No role or permission changes occur in this check.
    if conn.execute("SELECT to_regrole('citylens_agent_reader')").fetchone()[0] is None:
        raise ReaderRoleMissing('Reader group is not installed')
    safe = conn.execute('''SELECT NOT (rolsuper OR rolcreaterole OR rolcreatedb
        OR rolreplication OR rolbypassrls),
        pg_has_role(current_user, 'citylens_agent_reader', 'USAGE'),
        NOT has_schema_privilege(current_user,'citylens','CREATE'),
        NOT has_database_privilege(current_user,current_database(),'CREATE')
        FROM pg_roles WHERE rolname=current_user''').fetchone()
    if not safe or not all(safe):
        raise PermissionError('Dedicated restricted reader credentials required')
    writes = conn.execute('''SELECT count(*) FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='citylens' AND c.relkind IN ('r','p')
        AND has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')''').fetchone()[0]
    if writes:
        raise PermissionError('Agent account has application write privileges')


def _filters(call: ToolCall) -> tuple:
    """Mirror actual SQL argument order; the internal filter is never registered."""
    f=call.collision_filters
    # Use the existing normalization convention without changing its importer.
    from ..collision_addresses import normalize_street_name
    return (call.time_range.start,call.time_range.end,call.location.latitude,
            call.location.longitude,call.radius_meters,f.min_injured,f.max_injured,
            f.min_killed,f.max_killed,f.hit_run,
            normalize_street_name(f.street_name) if f.street_name is not None else None,
            f.hit_run_level)


def query_spec(call: ToolCall) -> tuple[str, tuple]:
    """Single source for executed SQL and reproducible human inspection."""
    if call.name == 'count_service_requests':
        return ('SELECT citylens.count_service_requests(%s,%s,%s,%s,%s,%s) AS request_id_count',
            (call.location.latitude,call.location.longitude,call.radius_meters,
             call.time_range.start,call.time_range.end,call.exclude_linked_duplicates))
    if call.name == 'aggregate_traffic_collisions':
        return ('SELECT * FROM citylens.aggregate_traffic_collisions('+','.join(['%s']*14)+')',
                (*_filters(call),call.period,call.time_range.calendar_timezone))
    if call.name == 'get_traffic_collisions':
        return ('SELECT * FROM citylens.get_traffic_collisions('+','.join(['%s']*14)+')',
                (*_filters(call),call.limit,0))
    raise ValueError('Tool is not allowlisted')


def tool_log(result: ToolResult) -> ToolLog:
    """Export only fixed allowlisted SQL; no credentials or hidden LLM reasoning."""
    from psycopg import sql
    statement,params=query_spec(result.call)
    # SQL.Literal performs PostgreSQL-aware escaping, including quotes and
    # backslashes. This export is for psql/pgAdmin; execution still binds params.
    pieces=statement.split('%s')
    rendered=pieces[0]+''.join(sql.Literal(value).as_string()+tail for value,tail in zip(params,pieces[1:]))
    rows=result.rows
    if result.call.name=='get_traffic_collisions':
        # Raw descriptions and embedded JSON may contain personal details or
        # prompt injection. Show report fields needed for verification instead.
        excluded={'raw_record','charge_desc','geocode_details'}
        rows=[{key:value for key,value in row.items() if key not in excluded} for row in rows]
    return ToolLog(query_id=result.query_id,tool=result.call.name,
        executed_at=result.executed_at,elapsed_ms=result.elapsed_ms,status=result.status,
        error_code=result.error_code,arguments=result.call.model_dump(mode='json'),
        parameterized_sql=statement,parameters=list(params),
        read_only_sql='BEGIN TRANSACTION READ ONLY;\n'+rendered+';\nROLLBACK;',
        result_rows=rows)


def execute_query(conn: psycopg.Connection, call: ToolCall) -> list[dict]:
    """Invoke existing SQL without duplicating SQL filtering/aggregation logic."""
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(*query_spec(call))
        return cursor.fetchall()


def dataset_metadata(conn: psycopg.Connection, name: ToolName) -> dict:
    """Small dataset-wide diagnostics in the same snapshot as the measurement.

    Import IDs/hashes below describe retained dataset rows, not exact query
    contributors. Query-specific provenance, coverage and freshness stay unknown.
    SQL identifiers come exclusively from these two hard-coded branches.
    """
    if name == 'count_service_requests':
        stats_sql='''SELECT count(*) AS dataset_records,count(location) AS dataset_located_records,
            count(requested_at) AS dataset_dated_records,min(requested_at) AS dataset_observed_start,
            max(requested_at) AS dataset_observed_end FROM citylens.service_requests'''
        imports_sql='''SELECT DISTINCT i.id,i.source_file,i.sha256,i.imported_at
            FROM citylens.ingestions i JOIN citylens.service_requests r ON r.ingestion_id=i.id
            ORDER BY i.id DESC LIMIT 101'''
    else:
        stats_sql='''SELECT count(*) AS dataset_records,count(location) AS dataset_located_records,
            count(occurred_at) AS dataset_dated_records,min(occurred_at) AS dataset_observed_start,
            max(occurred_at) AS dataset_observed_end FROM citylens.traffic_collisions'''
        imports_sql='''SELECT DISTINCT i.id,i.source_file,i.sha256,i.imported_at
            FROM citylens.collision_imports i JOIN citylens.traffic_collisions c ON c.import_id=i.id
            ORDER BY i.id DESC LIMIT 101'''
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(stats_sql)
        stats=cursor.fetchone()
        cursor.execute(imports_sql)
        imports=cursor.fetchall()
    return {**stats,'dataset_imports':imports[:100],'dataset_imports_truncated':len(imports)>100}


class DatabaseTools:
    def __init__(self, database_url: str | None):
        self.database_url=database_url

    def execute(self, call: ToolCall) -> ToolResult:
        """One read-only repeatable-read snapshot with bounded statements."""
        # Revalidate at the execution boundary, including injected/test callers.
        call=ToolCall.model_validate(call.model_dump())
        result=ToolResult(call)
        started=time.perf_counter()
        if not self.database_url:
            result.status='unavailable'
            result.error_code='database_not_configured'
            return result
        stage='connect'
        try:
            with psycopg.connect(self.database_url,connect_timeout=3) as conn:
                stage='transaction_setup'
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                conn.execute("SET LOCAL statement_timeout='10s'")
                conn.execute("SET LOCAL lock_timeout='2s'")
                stage='reader_check'
                ensure_reader(conn)
                stage='query'
                result.rows=execute_query(conn,call)
                stage='metadata'
                result.metadata=dataset_metadata(conn,call.name)
                if result.metadata['dataset_records']==0:
                    result.status='unavailable'
                    result.rows=[]
                    result.error_code='dataset_empty'
        except (PermissionError,psycopg.Error) as exc:
            result.status='error'
            result.error_code=database_failure_code(exc,stage)
            log_failure(result.error_code,exc,stage=stage,query_id=result.query_id)
        finally:
            result.elapsed_ms=(time.perf_counter()-started)*1000
        return result
