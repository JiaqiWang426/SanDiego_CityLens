# Phase 1.1 — Data Reliability & Enrichment

## Audit first

Read [the dated pre-implementation audit](audits/2026-11-01-phase1_1/SUMMARY.md).
It includes aggregate database observations and a separate raw-file audit:

- Get It Done: 303,225 current distinct request IDs; 424 blank service names;
  44 nonblank service names and four statuses. No missing stored IDs, timestamps,
  statuses or coordinates. No whitespace/case-only collisions were found.
- Collisions: 78,494 reports, all pending, zero coordinates, zero recorded
  geocoding attempts. Geocoding is implemented in code but not completed on this
  dataset. No pending row has coordinates and no matched row lacks them.
- Current raw collision input is eligible for the existing Census adapter on
  70,144 rows; 8,350 have unsupported inputs (including 6,840 intersections and
  one missing primary street). Eligibility is **not** a successful geocode or
  proof of why pending work was not processed.
- The import tables already record file hashes and committed local ingestion
  timestamps. They do not record source publication/update times. Runtime
  metadata leaves these NULL; a newer portal page date does not date an older
  imported file. Council District 1 is established for the current raw GID file,
  not citywide completeness.

No synonym mapping was inferred. Parking/Parking Violation/Parking - 72-Hours
and the separate Graffiti categories stay separate. Read the audit for top
values, timestamp ranges, missing-value counts, schema fields and sources.

## Service-request aggregation

`sql/tools/040_aggregate_service_requests.sql` adds these caller-rights functions:

```sql
citylens.normalize_request_category(value text) -> text
citylens.aggregate_service_requests(
    latitude float8, longitude float8, radius_meters float8,
    start_time timestamptz, end_time timestamptz,
    exclude_linked_duplicates boolean DEFAULT false,
    group_by text DEFAULT 'service_name',
    service_name_filter text DEFAULT NULL, status_filter text DEFAULT NULL
) -> TABLE(service_name text, status text, request_id_count bigint)
```

- Group by `service_name`, `status`, or `both`.
- Geography uses the same inclusive meter radius (0–50,000), `[start,end)`
  request-creation interval, and parent-link policy as `count_service_requests`.
- Canonical labels use uppercase + trimming + collapsed whitespace. Original
  columns and raw records remain unchanged. No fuzzy matching or synonyms.
- SQL filter NULL disables that filter. An explicitly empty/whitespace filter
  selects unknown. Literal `Unknown` becomes `UNKNOWN`, distinct from NULL.
- An active grouping dimension with NULL is unknown. An inactive dimension is
  also NULL, so interpret it using the echoed `group_by` setting.
- Counts are distinct request IDs. All groups are returned, never a truncated
  page. Ordering uses C collation with NULL first for reproducibility.
- With no category filters, the sum of groups equals `count_service_requests`
  for equivalent scope and parent policy. The old counter does not have category
  parameters; filtered totals are verified independently against pandas.

Example:

```sql
SELECT * FROM citylens.aggregate_service_requests(
    32.84,-117.25,800,
    '2026-01-01T00:00:00-08:00','2026-03-01T00:00:00-08:00',
    group_by=>'both',service_name_filter=>' pothole '
);
```

Python: `service_request_queries.aggregate_service_requests(conn, latitude,
longitude, radius_meters, start_time, end_time, *, exclude_linked_duplicates=False,
group_by='service_name', service_name=None, status=None)`. It follows the existing
idle-connection/read-only transaction convention and requires offset-aware times.

The Agent's `ToolCall` allowlist includes `aggregate_service_requests`, with
typed `service_request_aggregation` arguments. Related planner changes only add
the new tool and arguments, allow grouped service counts, and retain the rule
that an all-category counter cannot answer a category-filtered question. Filters
need a quoted source label from the question; no synonym mapping is authorized.
Crime, safety rankings and repair completion remain unsupported. The graph,
budgets, SQL binding, restricted reader checks and claim verification are reused.

Grouped evidence retains normal evidence/query IDs, `request_id_count`, exact
scope, grouping/filter/duplicate policy and additive `groups`. Its value is the
sum of SQL group counts. Old count tool behavior is unchanged. Source group text
is data, not LLM instructions. Existing answer templates summarize totals; a
richer natural-language group-by-group presentation can follow later.

## Dataset metadata

`sql/tools/050_get_dataset_metadata.sql` adds
`citylens.get_dataset_metadata(dataset_domain text) -> jsonb`, for
`service_requests` and `traffic_collisions` only. The Python
`dataset_metadata.get_dataset_metadata(conn, domain)` validates a `DatasetMetadata`
result and uses the existing read-only transaction helper.

Metadata exposes:

- Dataset identifier, documented source URL, current record total and event span.
- Last successful local `imported_at`, read from **all committed imports**, so
  even a successful empty import is represented. This is the recorded transaction
  time, not necessarily the exact completion time or the source update time.
- Source publication/update timestamps NULL and freshness verification false
  until import-specific source evidence is actually recorded.
- Non-NULL, nonempty WGS84 point count and its complement. This does not verify
  positional accuracy, city boundaries, or whether a point is a placeholder.
- Dataset-wide coordinate coverage; collision geocoding coverage, actual status
  counts and precision counts. Empty datasets have NULL ratios, not fabricated
  0% coverage. Get It Done has no invented geocoding status field.
- Spatial-accuracy, upsert/history, source-coverage and freshness limitations.
- At most 100 current-row import references, with a truncation flag. These are
  dataset context, not exact contributors to each measurement query.

The named metadata tool has a separate typed `MetadataCall`, because dataset
metadata must not pretend to have a point/radius. Call it through the same
restricted `DatabaseTools` adapter:

```python
import os
from backend.config import load_environment
from backend.agent.tools import DatabaseTools, MetadataCall
load_environment()
tools = DatabaseTools(os.environ['AGENT_DATABASE_URL'])
result = tools.execute(MetadataCall(domain='traffic_collisions'))
# result.status, result.query_id, result.executed_at, result.metadata
```

Every measurement now uses this authoritative metadata function in the same
read-only repeatable-read snapshot. Existing evidence quality keys remain and
gain geocoding status/coverage and unlocated counts; provenance gains the known
local ingestion timestamp. Source freshness stays explicitly unknown.

Direct API execution without an LLM (unchanged endpoint, additive argument type):

```json
{"name":"get_dataset_metadata","domain":"traffic_collisions"}
```

POST this to `/api/v1/tools/run`; the response has `tool_call`, `metadata`, and
`evidence: null`. Metadata carries dataset-wide attribution and execution IDs,
not a fictitious location-specific count evidence record. Arbitrary domains or
SQL are rejected. The LLM planner still gets metadata automatically with its
measurements rather than a new location-free graph branch.

For grouping, POST a normal `ToolCall` with `name="aggregate_service_requests"`
and `service_request_aggregation={"group_by":"both","service_name":null,"status":null}`.
The other scope fields are unchanged. See `/docs` for the complete typed contract.
No frontend component was changed; the new calls can be inspected in tool logs,
JSON, the API docs, or Python.

## Geocoding correctness fixes

The bounded worker now saves with `only_if_pending=True` plus the existing
address-key check. If another worker completes a pending report first, a slower
result cannot overwrite it. Rejected worker saves use the clearer
`source_or_status_changed` counter. Explicit callers of `save_geocode` keep the
old refresh behavior by default (`only_if_pending=False`). Nonmatched results
cannot retain a coordinate precision label, and nonfinite pause values fail
before worker execution. No provider was changed and no external geocoding ran.

The existing schema already prevents pending rows with non-NULL locations.
Metadata additionally treats an empty point as unusable, even if its status says
matched. We did not reinterpret block positions as exact collision sites. The
current indexes cover spatial, temporal and street lookups; a queue index and
multi-worker job claiming are deferred pending performance evidence.

## Initialization and privileges

The two new function files were installed in the existing development database
without table/data changes. `db.py init` also discovers them on future checkouts.
No table migration, historical backfill or ingestion rewrite is required. Both
importers already persist committed local ingestion timestamps and hashes; local
CSV inputs provide no defensible source-publication evidence to populate.
Future automated downloads should retain source headers/verified publication
timestamps tied to the actual file hash before setting freshness claims.

The existing reader setup file has additive EXECUTE grants for the new functions
and normalization helper. It adds no table write rights or ownership; the
functions use caller privileges. Reader-role/grant changes in the development
database were not executed. Review any deployment grant step separately under
the existing approval policy. Never configure the Agent with administrator
credentials. Existing effective privilege checks remain in place.

## Verification and deferred work

New tests live in `agent_tests/test_phase1_data.py`. They independently normalize
and group synthetic records with pandas/pyproj, reconcile unfiltered group totals
against the old counter, test null/blank/literal-Unknown labels and separate
semantic names, radius and DST boundaries, metadata missing history/empty imports,
geocoding status/coordinate distinctions, reader privileges, invalid parameters,
original value preservation, claim attribution and worker race behavior.
Database tests use only the existing guarded disposable container pattern.
Actual run results and deployed-function verification are recorded in the dated
audit folder; earlier independent test reports are not claimed as this run.

Deferred to Phase 1.2 or later:

- Approved geocoding runs and an approved intersection-capable/local GIS source.
- Provider match-quality evaluation, retry/job claiming and queue indexing.
- Verified source update/publication tracking when a real download pipeline exists.
- Positional accuracy, city/subset boundary verification, and source DST ambiguity.
- Any documented synonym/taxonomy mappings; none were inferred here.
- Full historical snapshots and query-specific contributor provenance.
- Richer grouped answers and dedicated frontend controls; no safety score.
