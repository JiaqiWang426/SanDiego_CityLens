"""Independent CSV/pandas count, optionally compared with the SQL function."""
import argparse
from datetime import timedelta
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Geod


def count_service_requests_pandas(
    df, latitude, longitude, radius_meters, start_time, end_time,
    exclude_linked_duplicates=False,
):
    """Count unique CSV request IDs within a WGS84 radius and [start, end).

    Pass CSV columns read with dtype=str and keep_default_na=False. Source
    date_requested values are local Los Angeles wall times, just as in db.py.
    Repeated IDs keep the last CSV occurrence, before any filters are applied.
    The input DataFrame is not modified.

    Args:
        df: DataFrame containing source ID, parent ID, date_requested, lat, lng.
        latitude, longitude: Query center in WGS84 degrees, latitude first.
        radius_meters: Inclusive distance threshold, from 0 through 50,000.
        start_time, end_time: Offset-aware timestamps; start inclusive, end exclusive.
        exclude_linked_duplicates: If true, omit rows with a nonempty parent ID.

    Returns:
        Python int: number of matching distinct source request IDs.

    Raises:
        ValueError: Invalid query arguments or invalid surviving source values.
        KeyError: A required source column is absent.

    This independently computes counts from a CSV, without consulting SQL.
    It is not a complete reproduction of import validation: deduplication
    precedes validation here, whereas the importer checks all CSV occurrences.
    Compare against a DB containing the same data, since upserts retain old IDs.
    """
    if not (math.isfinite(latitude) and -90 <= latitude <= 90
            and math.isfinite(longitude) and -180 <= longitude <= 180
            and math.isfinite(radius_meters) and 0 <= radius_meters <= 50000):
        raise ValueError('Invalid coordinate or radius (0..50000 meters)')
    if not isinstance(exclude_linked_duplicates, bool):
        raise ValueError('exclude_linked_duplicates must be boolean')
    start, end = pd.Timestamp(start_time), pd.Timestamp(end_time)
    if (pd.isna(start) or pd.isna(end) or start.tzinfo is None
            or end.tzinfo is None or start >= end):
        raise ValueError('Use an increasing interval with explicit UTC offsets')

    # Deduplicate BEFORE filtering: an earlier matching version must not be
    # counted if its final CSV occurrence moved outside the query boundaries.
    records = df.drop_duplicates('service_request_id', keep='last')
    if records['service_request_id'].isna().any() or records['service_request_id'].eq('').any():
        raise ValueError('Missing service request ID')
    requested = pd.to_datetime(records['date_requested'], errors='raise')
    if requested.isna().any():
        raise ValueError('Missing request timestamp')
    # PostgreSQL prefers standard time in ambiguous fall-back timestamps and
    # the pre-transition offset for nonexistent spring-forward timestamps.
    requested = requested.dt.tz_localize(
        'America/Los_Angeles', ambiguous=False, nonexistent=timedelta(hours=1))
    # Empty coordinates become missing values, not zero. Numeric parsing is
    # strict; a row needs both components before it can participate spatially.
    lat = pd.to_numeric(records['lat'].replace('', np.nan), errors='raise')
    lng = pd.to_numeric(records['lng'].replace('', np.nan), errors='raise')
    located = lat.notna() & lng.notna()
    if ((located & (~lat.between(-90, 90) | ~lng.between(-180, 180))).any()):
        raise ValueError('Invalid source coordinates')
    # Vectorized pandas masks apply the same time and parent rules as SQL.
    # Filter before calculating distances to avoid unnecessary geodesic work.
    mask = located & (requested >= start) & (requested < end)
    if exclude_linked_duplicates:
        parent = records['service_request_parent_id']
        mask &= parent.isna() | parent.eq('')
    selected_lat = lat[mask].to_numpy(dtype=float)
    selected_lng = lng[mask].to_numpy(dtype=float)
    if len(selected_lat) == 0:
        return 0
    # Ellipsoidal distance, matching PostGIS geography's default spheroid.
    # https://pyproj4.github.io/pyproj/stable/api/geod.html
    # inv returns forward azimuth, back azimuth, and distance. Only distance
    # is needed. Repeat the center to match the candidate coordinate arrays.
    # Values extremely close to the boundary may differ by floating precision.
    _, _, distance = Geod(ellps='WGS84').inv(
        np.full(len(selected_lat), longitude),
        np.full(len(selected_lat), latitude), selected_lng, selected_lat)
    return int(np.count_nonzero(distance <= radius_meters))


def main():
    """Print pandas count; optionally compare SQL and exit 1 on disagreement."""
    parser = argparse.ArgumentParser(description=__doc__)
    # Match the SQL loader's default dataset while allowing any explicit CSV.
    parser.add_argument(
        'csv', type=Path, nargs='?',
        default=Path(__file__).resolve().parent / 'Raw Data' / 'Get it done'
        / 'get_it_done_requests_CD1_datasd.csv',
        help='Get It Done CSV (default: backend/Raw Data/Get it done/get_it_done_requests_CD1_datasd.csv)',
    )
    parser.add_argument('--lat', type=float, required=True)
    parser.add_argument('--lng', type=float, required=True)
    parser.add_argument('--radius', type=float, default=800)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--exclude-linked-duplicates', action='store_true')
    parser.add_argument('--compare-sql', action='store_true')
    args = parser.parse_args()
    # Preserve IDs as strings and empty source fields as empty strings. Default
    # CSV inference could change ID formatting or interpret literal text as NA.
    df = pd.read_csv(args.csv, dtype=str, keep_default_na=False, encoding='utf-8-sig')
    params = (args.lat, args.lng, args.radius, args.start, args.end,
              args.exclude_linked_duplicates)
    count = count_service_requests_pandas(df, *params)
    result = {'pandas_count': count}
    # SQL is optional: pure pandas operation needs no database connection.
    if args.compare_sql:
        import psycopg
        with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout = '10s'")
            sql_count = conn.execute(
                'SELECT citylens.count_service_requests(%s,%s,%s,%s,%s,%s)',
                params).fetchone()[0]
        result.update(sql_count=sql_count, match=count == sql_count)
    print(json.dumps(result))
    if args.compare_sql and not result['match']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
