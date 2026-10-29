# Traffic collision data infrastructure

This domain uses the **existing CityLens PostgreSQL/PostGIS database**. No
Get It Done table definitions or records are changed. Existing dependencies
(`psycopg`) and Python's standard library are sufficient for these modules.

## Files and initialization

```text
backend/
  Raw Data/
    Traffic collision/
      pd_collisions_datasd.csv
  collision_addresses.py             # Reusable textual address normalization
  collision_ingest.py                # CSV parsing, validation, batch upsert
  collision_geocoding.py             # Provider interface, Census adapter, worker
  collision_queries.py               # Bounded read-only Python query tools
  sql/
    traffic_collisions.sql           # Collision tables and indexes
    tools/
      010_filter_traffic_collisions.sql
      020_get_traffic_collisions.sql
      030_aggregate_traffic_collisions.sql
```

From the repository root, with the same `DATABASE_URL` used for Get It Done:

```powershell
.\.venv\Scripts\python.exe backend/db.py init
.\.venv\Scripts\python.exe backend/collision_ingest.py
# Explicit CSV or batch size:
.\.venv\Scripts\python.exe backend/collision_ingest.py "backend/Raw Data/Traffic collision/pd_collisions_datasd.csv" --batch-size 2000
.\.venv\Scripts\python.exe -m unittest discover -s backend -p test_collisions.py -v
```

Run collision tests with `DATABASE_URL` unset for unit-only execution, or with it
pointing to an isolated test database. The integration cases write synthetic
records and must never use the development database credentials.

Initialization creates the original structure, then collision tables, then
tool definitions in filename order. It does not reload data or modify raw CSVs.
The test suite uses synthetic records in transactions which are rolled back;
it never calls the external geocoder. PostgreSQL identity sequence increments
are not transactional, so test runs may leave gaps in import IDs.

## Validation and provenance

- `REPORT_ID` remains text and is the primary key. Missing IDs are archived but
  rejected from the normalized table. Last CSV occurrence wins for repeated IDs.
- Dates are parsed as local Los Angeles wall times and stored as `timestamptz`.
  Invalid/missing dates become NULL with issues and cannot match date queries.
  The source dictionary has no explicit timezone; Los Angeles is a documented
  implementation assumption. PostgreSQL resolves ambiguous DST times.
- Missing injury/fatality counts stay **NULL**, as do invalid nonnegative integer
  values. They are never coerced to zero. Valid explicit zero remains zero.
- Empty `HIT_RUN_LVL` maps to false; FELONY/MISDEMEANOR map to true. Unknown
  nonempty labels are preserved with unknown (`NULL`) boolean status and an issue.
- Whitespace is normalized for address comparisons, without inventing numbers.
  Primary number/direction/street/type and intersecting direction/street/type
  contribute to the normalized address and its versioned key. Full street names
  include direction and suffix, e.g. `LA JOLLA SCENIC DR`.
- `collision_source_rows` archives **every parsed row for every import** as JSON,
  including extra columns, original string values, duplicates, rejected IDs,
  and validation issues. `traffic_collisions.raw_record` holds the current row.
  JSON retains source field information, not CSV byte formatting; the untouched
  source file and its SHA-256 provide file-level evidence.
- `collision_imports` records file hash/name, timestamp, and accepted/rejected
  counts. Accepted rows include repeated IDs; CLI `distinct_reports` does not.
- COPY batches avoid per-row round trips. The whole import is atomic. Missing
  required headers or malformed column counts fail the entire import.
- Reimports update IDs and retain reports absent from a later file. Each import
  adds an archive snapshot. They do not duplicate normalized collision records.
- If the normalized source-component hash is unchanged, all prior geocoding
  fields survive. Changed address components clear coordinates, provider,
  precision, timestamp, and evidence and reset status to pending.

## Approximate geocoding

The [City dataset dictionary](https://data.sandiego.gov/datasets/traffic-collisions/)
states that primary street numbers are abstracted to block level. **No coordinate
is an exact collision location.** Neither police beat nor street text is used to
guess a point.

The [Census Geocoder API](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html)
requires a structure address and interpolates along address ranges. The provided
adapter uses the original block number unchanged. It accepts only one matching
street/number in CA with a supported locality (San Diego or La Jolla), validates
coordinate bounds, and labels results `block_approximate`. Provider response,
requested benchmark, input, timestamp, and limitations are preserved. The default
`Public_AR_Current` benchmark changes over time; the returned response preserves
the benchmark metadata available at request time.

Geocoding is **opt-in**, separate from import:

```powershell
# Makes external requests for at most 25 pending reports; default pause is 1 s.
.\.venv\Scripts\python.exe backend/collision_geocoding.py --limit 25
```

No-match, ambiguous, unsupported, and failed results keep NULL coordinates.
The Census adapter explicitly does not resolve intersections or missing/zero
block numbers. It does not discard a cross street and geocode a different input.
Unsupported records need a suitable local GIS provider or review. A `Provider`
protocol allows another adapter without changing ingestion or query interfaces.
No SanGIS layer is downloaded or guessed at in this implementation.

A future adapter can ingest the official
[SanGIS road centerlines](https://data.sandiego.gov/datasets/gis-roads-all/),
verify CRS and address-range fields, and locate a unique segment/block or
intersection. Multiple plausible segments must remain ambiguous. Geometry from
this source would still be approximate, not evidence of an exact crash point.

The worker caches repeated address results within one run, processes a bounded
queue, and uses optimistic address-key checks to reject stale HTTP responses.
It uses an autocommit connection so requests do not hold a database transaction.
Failures are not automatically retried; explicit status reset/review is needed
before retrying. No bulk network geocoding occurs during setup or import.

Inspect coverage before interpreting radius results:

```sql
SELECT geocode_status, count(*) FROM citylens.traffic_collisions GROUP BY 1;
SELECT count(*) FILTER (WHERE location IS NULL) AS unlocated,
       count(*) FILTER (WHERE occurred_at IS NULL) AS undated,
       count(*) AS total FROM citylens.traffic_collisions;
```

## Python tools

Use a dedicated idle connection. The wrappers start read-only transactions,
set a 10-second SQL timeout, and bind all values as parameters. Query timestamps
must include offsets. A restricted DB account is still needed for a deployed
agent; transaction-level read-only mode is not account access control.

```python
import os
import psycopg
from collision_queries import get_collisions, aggregate_collisions

with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
    reports = get_collisions(
        conn, '2026-01-01T00:00:00-08:00', '2026-02-01T00:00:00-08:00',
        latitude=32.84, longitude=-117.25, radius_meters=800,
        min_injured=1, min_killed=0, hit_run=True, limit=100,
    )
    street_reports = get_collisions(
        conn, '2026-01-01T00:00:00-08:00', '2026-11-01T00:00:00-07:00',
        street_name='La Jolla Scenic Drive',
    )
    monthly = aggregate_collisions(
        conn, '2026-01-01T00:00:00-08:00', '2026-11-01T00:00:00-07:00',
        period='month', hit_run_level='FELONY',
    )
```

Run this example from `backend`, or add `backend` to your Python module path.

Filters can be combined or omitted (except the required date interval):

- `latitude`, `longitude`, `radius_meters`: all supplied together, 0–50,000 m.
- `min_injured`, `max_injured`, `min_killed`, `max_killed`: inclusive bounds;
  equal bounds select an exact count. NULL source values fail numeric filters.
- `hit_run`: true, false, or None for no filter; `hit_run_level`: exact level.
- `street_name`: exact normalized full primary or intersecting street name.
- Retrieval: limit 1–1,000, offset 0–100,000; ordered by timestamp and report ID.
- Aggregation: day/week/month/quarter/year in an explicit calendar timezone
  (default Los Angeles; weeks begin Monday). Empty periods are omitted. Counts
  cover all filtered reports, independent of retrieval pagination. Returned
  coverage fields include located reports and unknown injury/fatality counts.

SQL equivalents (street text is already normalized when calling SQL directly):

```sql
SELECT * FROM citylens.get_traffic_collisions(
    '2026-01-01T00:00:00-08:00', '2026-02-01T00:00:00-08:00',
    latitude => 32.84, longitude => -117.25, radius_meters => 800,
    min_injured => 1, hit_run => true, result_limit => 100
);
SELECT * FROM citylens.get_traffic_collisions(
    '2026-01-01T00:00:00-08:00', '2026-11-01T00:00:00-07:00',
    street_name => 'LA JOLLA SCENIC DR'
);
SELECT * FROM citylens.aggregate_traffic_collisions(
    '2026-01-01T00:00:00-08:00', '2026-11-01T00:00:00-07:00',
    period => 'month'
);
```

`filter_traffic_collisions` is the shared internal uncapped SQL primitive; only
the bounded retrieval and aggregate wrappers should be exposed as agent tools.
Approximate radius membership is uncertain near search boundaries. An empty
spatial result can mean missing geocoding. The source is police-recorded
collisions, not a complete census of collisions or a neighborhood safety score.
