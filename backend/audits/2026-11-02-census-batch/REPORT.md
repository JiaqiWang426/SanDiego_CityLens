# Census batch deployment ? 2026-11-02

**Complete for the existing eligible pending selection.** All **70,119** selected
reports were processed; **zero eligible reports remain pending**. The database
still contains **78,494** reports, including **59,627** with usable block-level
coordinates (**75.96% coordinate coverage**). This is coordinate presence, not
an independently verified geographic accuracy rate.

## Implementation

- `backend/census_addresses.py`: geocoder-only comparisons for prefix CAMINO/CAM,
  whole leading-zero ordinal tokens (04TH -> 4TH), existing street-edge direction
  and suffix abbreviations. No fuzzy spelling or dropped meaningful components.
- `backend/collision_geocoding.py`: single-address adapter includes preType,
  qualifiers and all direction/suffix fields, and uses the same comparison rules.
- `backend/census_batch.py`: official multipart upload, required five-column input
  CSV, strict headerless response parsing, output-ID plus echoed-input checks,
  finite/local coordinate checks, and bounded transient retries.
- `backend/collision_batch_geocoding.py`: frozen source-key/report-ID plan,
  cross-report request deduplication, 100-report gate, sequential batches, response
  checkpoints and atomic database commits of at most 500 reports. Reuses the
  existing pending-status/address-key guarded `save_geocode` function.
- `backend/agent_tests/test_census_batch.py`: focused parser, normalization,
  mapping, retries, guarded persistence and interruption/resumption tests.
- `backend/TRAFFIC_COLLISIONS.md`: operation, limits, validation and safe-resume
  instructions. This directory contains the new deployment artifacts.

No schema migrations, role/grant changes, source CSV reloads, new dependencies,
SanGIS downloads, Agent redesign, frontend changes or OpenAI calls were performed.
Other pre-existing workspace changes were retained.

The [official Census API](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html)
requires ID, street, city, state, ZIP (blank ZIP retained) and allows 10,000 rows
per batch. The [service form](https://geocoding.geo.census.gov/geocoder/locations/address)
also specifies a 5 MB file limit; our conservative cap is 5,000,000 bytes.
The run used `Public_AR_Current`, 180-second timeouts and at most three attempts
with backoff for transient failures. There were no retries in this run.

Unlike single-address JSON, the actual batch response contains eight matched-row
CSV fields: ID, echoed input, match indicator, match type, matched address,
longitude/latitude pair, TIGER line ID and side. It supplies neither street
components/address ranges nor a returned benchmark. Raw responses and requested
benchmark are retained; unavailable returned metadata remains unknown.

## Focused verification and live smoke test

**38 focused cases passed**: an initial 36-case run (including one real guarded-
write/idempotency integration test in an owned temporary PostGIS database), then
two additional offline checkpoint/malformed-mapping regressions. Zero failed or
skipped. The second command deselected the 36 already-run cases. No whole-repository
or unrelated long-running test suite was run. See [focused-tests.txt](focused-tests.txt).

The live gate processed **100 pending reports / 97 unique addresses** in one
request: **86 matched, 10 no-match, one native tie, three validation rejections**;
zero provider/network errors. Mapping covered every submitted ID and echoed input.
The three rejections were MT/MOUNT, a duplicated RW/ROW source suffix, and
AVENIDA/AVE. These aliases were not added to increase the match rate.
Source fields, history, all 25 completed pilot records and pilot files passed
preservation checks before bulk execution. See
[smoke-verification.json](smoke-verification.json) and `0000-smoke/`.

Every accepted result requires the same numeric source number, complete normalized
street including directions/prefix/suffix, CA and San Diego/La Jolla locality,
finite coordinates and the broad San Diego envelope (-117.6 to -116.8 longitude,
32.5 to 33.2 latitude). This envelope is not a verified city-boundary polygon.
No unique SanGIS segment or 50-metre distance rule was applied. All accepted
points retain `block_approximate` precision.

## Actual production results

| This run only, including the 100-report gate | Reports |
| --- | ---: |
| Attempted and processed | 70,119 |
| Matched | 59,608 |
| No match | 7,396 |
| Native Census tie | 544 |
| Rejected by address validation | 2,571 |
| Failed provider/network records | 0 |
| Eligible still pending | 0 |

Run match rate: **59,608 / 70,119 = 85.01% of reports**. Deduplicated addresses
are not equally weighted in this report-level rate. Validation rejections are
stored as `ambiguous` with `validation_rejected=true`; native ties are separately
reported above. Their 3,115 combined ambiguous statuses reconcile to the database.

| Whole development database, including preserved pilot | Reports |
| --- | ---: |
| Total | 78,494 |
| Matched / usable coordinates | 59,627 |
| No match | 7,400 |
| Ambiguous (includes validation rejections) | 3,117 |
| Pending, outside current eligibility | 8,350 |
| Error / unsupported stored statuses | 0 / 0 |
| Without coordinates | 18,867 |

The 8,350 pending reports comprise **6,840 with intersections** and **1,510
missing/invalid positive block numbers**. These are eligibility reasons; they
were not relabeled as stored unsupported statuses. Existing pilot results
(19 matched, four no-match, two ambiguous) were not retried or changed.

Validation rejected **2,433 street-component disagreements** and **138 number
disagreements**. Examples include `2300 W MISSION BAY DR` returned as
`2300 E MISSION BAY DR` (49 reports), and `2400 HOTEL CIRCLE PL` returned with
an additional `N` direction (21 reports). Complete component comparison prevented
these from being accepted. Other unresolved aliases include DE LA PLAZA/DE LA PLZ,
MT/MOUNT and AVENIDA/AVE. Their correctness requires review; no rules were
relaxed mid-run. Full breakdown: [final-verification.json](final-verification.json).

## Batches, requests and runtime

**16,226 unique normalized addresses**, submitted using **10 sequential HTTP
requests** (one smoke request plus nine bulk requests). No HTTP/provider errors,
retries, missing/duplicate output IDs, malformed responses, or guarded-write
skips occurred. The default chunk target was 2,000 unique addresses; cached smoke
results reduced the number of new submissions in several bulk chunks.

| Batch | Reports written | Newly submitted address rows |
| --- | ---: | ---: |
| 0000-smoke | 100 | 97 |
| 0001-bulk | 24,458 | 1,967 |
| 0002-bulk | 15,023 | 1,983 |
| 0003-bulk | 9,708 | 1,985 |
| 0004-bulk | 6,806 | 1,997 |
| 0005-bulk | 5,035 | 1,994 |
| 0006-bulk | 3,616 | 1,994 |
| 0007-bulk | 2,878 | 1,997 |
| 0008-bulk | 2,282 | 1,999 |
| 0009-bulk | 213 | 213 |

Active worker runtime across the two invocations: **216.942 seconds**.
Wall time from initialization through final verification: **279.332 seconds**,
including the smoke-review interval. Local times: **2026-11-02 00:12:54 PST**
through **00:17:34 PST**. Exact per-request timings, sizes and
retry diagnostics are in [state.json](state.json); metrics are in
[summary.json](summary.json). No unattended follow-on run was started.

## Final database verification

- Total count remained 78,494; all status counts reconcile.
- Fingerprints of every non-geocoding collision field, including original raw
  records, IDs, event timestamps, address keys and import references, are identical.
- All 25 previously completed records, including all 19 original matches, are
  byte-equivalent under the database JSON fingerprint.
- Collision import history and archived source rows are unchanged.
- Every file in the historical pilot directory, including RUN_STARTED.json,
  has the same SHA-256 digest as before this deployment.
- Invalid matched geometries/coordinates: **0**; unresolved records with
  coordinates: **0**. Matched points have POINT/4326 and block_approximate precision.
- `get_dataset_metadata('traffic_collisions')`, through the read-only Agent
  adapter, returned 78,494 records, 59,627 located and 18,867 unlocated.
- The existing `aggregate_traffic_collisions` Agent tool succeeded for
  (32.84, -117.25), radius 5,000 m, 2020-01-01 through 2026-11-03 at -08:00,
  grouped by Los Angeles calendar year: **2,586 reports across seven buckets**.
  This sanity query demonstrates functioning spatial queries, not accurate or
  complete collision coverage.
- The actual Agent login remains nonadministrator and has no collision write
  privileges. All worker writes used separate maintenance credentials. No
  credentials or full connection strings were printed or stored.

Evidence: [preservation.json](preservation.json), [before.json](before.json),
[after.json](after.json), [final-tools.json](final-tools.json),
[final-verification.json](final-verification.json).

## Commands and safe resumption

Executed from repository root:

```powershell
$env:AGENT_TEST_DOCKER='1'
.\.venv\Scripts\python.exe -m pytest backend/agent_tests/test_census_batch.py -q
.\.venv\Scripts\python.exe -m backend.collision_batch_geocoding --smoke-only
.\.venv\Scripts\python.exe -m backend.collision_batch_geocoding
# Additional offline checkpoint cases, without rerunning earlier cases:
.\.venv\Scripts\python.exe -m pytest backend/agent_tests/test_census_batch.py -q -k 'interrupted_commit or bad_realistic_mapping'
```

If interrupted, resume with the same command and original configuration:

```powershell
.\.venv\Scripts\python.exe -m backend.collision_batch_geocoding --run-dir 'backend/audits/2026-11-02-census-batch' --batch-size 2000 --benchmark Public_AR_Current --timeout 180 --attempts 3 --backoff 5
```

Keep the plan, state, response/results files and commit markers. The run reuses
saved responses, skips committed batches and uses the pending/address-key guard
when replaying an interrupted write chunk. It never resets completed/error
statuses or restarts the historical pilot. A session advisory lock prevents
concurrent instances. Persistent HTTP or mapping/schema problems stop the run
and leave the uncommitted selection pending for review. This run is complete,
so no resume is currently needed.

## Deferred work

SanGIS/review fallback remains for intersections, invalid/missing blocks,
no-match addresses, native ties and validation rejections. No intersection
coordinates were invented. Reviewed aliases and historical road/range questions
can be handled in a later bounded task; completed statuses are not automatically
reset. The current 75.96% coordinate presence does not establish spatial accuracy,
citywide source completeness, or complete coverage for any individual radius.
