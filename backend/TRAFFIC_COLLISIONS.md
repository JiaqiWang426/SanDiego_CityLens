# Traffic collision data infrastructure

Phase 1.1 [audit and metadata](PHASE1_1.md) distinguish unlocated reports, actual
geocoding statuses and block-approximate points. The offline worker now uses a
pending-status compare-and-set to protect completed results from slower workers.

The [Stage B local SanGIS analysis](verification/intersection_batch/REPORT.md)
keeps reference ingestion separate from collision updates. Its
[setup, dry-run and deployment commands](sangis_topology/README.md) generate
`intersection_approximate` proposals with full topology/provenance evidence.
The current collision precision constraints and all historical geocodes remain
unchanged; any apply and precision migration require a separate explicit review.

The [final geocoding report](verification/historical_geocoding_final/REPORT.md)
adds SanGIS road-block candidates for no-match and ambiguous reports, with
`block_approximate` precision and provider `SanGIS road centerline`. The source
number is treated as a block, never a house number. Deployed points can therefore
come from Census, a SanGIS road block or a SanGIS intersection. Use
`geocode_provider` and `geocode_precision` to tell them apart.

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
  census_addresses.py               # Geocoder-only comparison rules; no source hash changes
  census_batch.py                   # Census batch CSV transport and strict response mapping
  collision_batch_geocoding.py       # Checkpointed sequential batch worker
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

The [25-record pilot report](audits/2026-11-01-geocoding-pilot/REPORT.md)
records live Census results, separate SanGIS plausibility diagnostics, and
read-only Agent tool checks. Provider `matched` is not an accuracy guarantee.
The pilot's validation scripts read archived responses and road geometry;
they do not snap points or change database geocoding statuses.

The later [Stage A intersection feasibility study](verification/intersection_feasibility/REPORT.md)
uses Roads_All endpoint IDs, Roads_Intersection types, levels and full incident
segments for 80 frozen pending reports. Its offline reproduction needs no
database or network access. Uniquely supported candidates remain verification
artifacts, not stored collision coordinates; ambiguous and uncertain cases remain
unresolved. No historical batch geocoder or road interpolation was added.

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
`Public_AR_Current` benchmark changes over time. Single-record responses preserve
returned benchmark metadata; batch CSV does not supply a returned benchmark,
so the worker records the requested benchmark and leaves the returned value unknown.

Geocoding is **opt-in**, separate from import:

```powershell
# Makes external requests for at most 25 pending reports; default pause is 1 s.
.\.venv\Scripts\python.exe backend/collision_geocoding.py --limit 25
```

For an explicitly reviewed selection, repeat `--report-id REPORT_ID` for each
pending record. The number of unique selected IDs must fit `--limit`; all
updates still require the original address key and pending status. Omitting
the selector preserves the existing queue ordering. The audit runner
`python -m backend.scripts.geocoding_pilot run` additionally refuses a repeat
of the completed pilot; use its `validate` mode to reproduce the report
without network requests or database writes.

No-match, ambiguous, unsupported, and failed results keep NULL coordinates.
The Census adapter explicitly does not resolve intersections or missing/zero
block numbers. It does not discard a cross street and geocode a different input.
Unsupported records need a suitable local GIS provider or review. A `Provider`
protocol allows another adapter without changing ingestion or query interfaces.
The production worker uses Census only. The pilot archives a filtered SanGIS
road subset under its audit directory for offline validation; no GIS dataset
was added to production tables.

A future geocoding adapter could use the official
[SanGIS road centerlines](https://data.sandiego.gov/datasets/gis-roads-all/),
whose CRS and address-range fields were inspected in the pilot, to locate a unique segment/block or
intersection. Multiple materially different locations need review; adjoining
segments on one street alone do not make a Census result ambiguous. Geometry from
this source would still be approximate, not evidence of an exact crash point.

The worker caches repeated address results within one run, processes a bounded
queue, and uses optimistic address-key checks to reject stale HTTP responses.
It uses an autocommit connection so requests do not hold a database transaction.
Failures are not automatically retried; explicit status reset/review is needed
before retrying. No bulk network geocoding occurs during setup or import.

## Census batch worker

The [batch deployment report](audits/2026-11-02-census-batch/REPORT.md) records
the 100-report live test, bulk progress, actual coverage and remaining fallback
work. The historical 25-record pilot and its statuses are preserved.

From the repository root, start or resume the same checkpointed selection:

```powershell
.\.venv\Scripts\python.exe -m backend.collision_batch_geocoding
# Optional: stop after the initial 100-report verification.
.\.venv\Scripts\python.exe -m backend.collision_batch_geocoding --smoke-only
```

The root `.env` loader supplies maintenance credentials without printing them.
The worker uses `DATABASE_URL`, or the existing local Compose maintenance
identity with `CITYLENS_DB_PASSWORD` on the same database as `AGENT_DATABASE_URL`.
It never uses the Agent account for writes and never changes database grants.

The default is 2,000 **unique normalized addresses** per batch, deduplicated
across reports. `--batch-size` accepts 100 through 10,000; input CSV is capped
at 5,000,000 bytes. Census receives the required ID, street, city, state and
blank ZIP columns, without a header. The API has a 10,000-record maximum and
its web form documents a 5 MB file limit. All requests are sequential, with
180-second timeouts and at most three attempts for transient network/HTTP
failures, exponential backoff and bounded Retry-After support. Permanent HTTP
failures stop immediately; exhausted retries leave the current batch pending.

Geocoder-only normalization handles `preType` and all returned direction/type/
qualifier components, prefix CAMINO/CAM equivalence, whole leading-zero ordinal
tokens (04TH -> 4TH), and existing street-edge direction/suffix abbreviations.
No spelling similarity or missing-component matching is applied. Original
collision fields, displayed normalized addresses and source address keys are
unchanged. Names such as MT/MOUNT or AVENIDA/AVE are not silently added as aliases.

The batch response is a headerless CSV, not the single-address JSON schema.
Matched rows have ID, echoed input, match indicator, match type, matched address,
longitude/latitude pair, TIGER line ID and side. The parser verifies every ID
and echoed input and accepts reordered rows. Duplicate, unknown or missing IDs,
unexpected schema, or suspicious coordinate output stop the batch before writes.
Ordinary no-match and tie results remain unlocated. Basic consistency requires
the same number, complete normalized street, supported CA locality, valid
coordinates and the broad San Diego admission envelope. This does not prove
municipal-boundary membership or exact accident location. Batch CSV provides no
address-range components; the worker does not invent them.

Validation rejections use existing `ambiguous` status with
`geocode_details.validation_rejected=true`; native ties are separately counted
in the summary. Provider invalid/error rows use `error`. No new schema statuses
are introduced. All accepted points remain `block_approximate`. SanGIS segment
uniqueness and distance thresholds are **not** acceptance criteria for this
worker. The earlier pilot's stricter independent assessments remain historical.

Checkpoints under `backend/audits/2026-11-02-census-batch/` contain the frozen
report-ID/address-key mapping, input and raw response CSVs, parsed results,
request diagnostics and per-batch commit markers. Commits contain at most 500
report updates and use the existing pending-status/address-key saver. A resumed
run reuses archived results and safely skips committed reports. An advisory
lock prevents concurrent instances of this batch worker. Completed/no-match/
ambiguous/error records are never reset automatically. To resume, use the same
command, run directory and configuration; keep all checkpoint files. Existing
pilot `RUN_STARTED.json` is unrelated and must not be removed.

The summary separates this run's accepted/no-match/tie/validation-rejected/error
results from dataset-wide coverage. A matched point is a reasonable block
estimate subject to Census interpolation and source limitations. Unresolved
addresses and intersections remain for later SanGIS/review work.

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
