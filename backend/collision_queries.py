"""Bounded, parameterized Python tools for read-only collision investigations.

Rows include geocoding status/precision and import ID. Radius results represent
approximate block locations, not exact collision sites or comprehensive risk.
"""
from datetime import datetime

from psycopg.rows import dict_row

from collision_addresses import clean_text, normalize_street_name


def _parameters(start_time, end_time, *, latitude=None, longitude=None,
                radius_meters=None,min_injured=None,max_injured=None,
                min_killed=None,max_killed=None,hit_run=None,street_name=None,
                hit_run_level=None):
    """Keep filter order aligned with SQL and reject offset-free caller times."""
    times = [datetime.fromisoformat(x) if isinstance(x,str) else x for x in (start_time,end_time)]
    if any(not isinstance(t,datetime) or t.utcoffset() is None for t in times):
        raise ValueError('Query timestamps must include UTC offsets')
    return (*times,latitude,longitude,radius_meters,min_injured,max_injured,
            min_killed,max_killed,hit_run,
            normalize_street_name(street_name) if street_name is not None else None,
            clean_text(hit_run_level) if hit_run_level is not None else None)


def _run(conn, sql, params):
    """Use a dedicated idle connection; avoid altering an existing transaction."""
    if conn.info.transaction_status.name != 'IDLE':
        raise ValueError('Query tools require an idle dedicated connection')
    with conn.transaction():
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='10s'")
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql,params)
            return cursor.fetchall()


def get_collisions(conn,start_time,end_time,*,limit=100,offset=0,**filters):
    """Retrieve collisions using optional spatial, severity, hit/run, street filters.

    Injury/fatality minima and maxima are inclusive; pass equal bounds for an
    exact count. hit_run=None means no status filter; False selects known non-
    hit/run records. Unknown counts/status are never treated as zero/false.
    street_name matches a complete normalized primary or intersecting street.
    """
    params = (*_parameters(start_time,end_time,**filters),limit,offset)
    return _run(conn,'SELECT * FROM citylens.get_traffic_collisions('+
                ','.join(['%s']*len(params))+')',params)


def aggregate_collisions(conn,start_time,end_time,*,period='month',
                         calendar_timezone='America/Los_Angeles',**filters):
    """Count all filtered reports per calendar period (empty periods omitted).

    Includes counts of located reports and unknown injury/fatality values.
    Week starts Monday. Radius filtering excludes unlocated reports, so inspect
    nonspatial coverage before interpreting spatial counts.
    """
    params=(*_parameters(start_time,end_time,**filters),period,calendar_timezone)
    return _run(conn,'SELECT * FROM citylens.aggregate_traffic_collisions('+
                ','.join(['%s']*len(params))+')',params)
