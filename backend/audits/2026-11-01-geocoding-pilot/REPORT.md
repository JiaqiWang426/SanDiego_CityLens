# Bounded Census geocoding pilot — 2026-11-01, America/Los_Angeles

The existing read-only Agent tools worked with `AGENT_DATABASE_URL`, both
directly through `DatabaseTools` and through `POST /api/v1/tools/run`. Exactly
25 pending collision records were processed using the existing Census adapter:
19 were accepted as matched, four had no candidate, and two were classified
ambiguous by the adapter. Independent SanGIS diagnostics classified 11 as
plausible, eight as needs_review, zero as rejected, and six as unverified.
**These are exploratory diagnostic rates, not estimates of population accuracy.**

## Preflight and database safety

Before requests, the database contained 303,225 Get It Done requests and
78,494 collision reports. All collisions were pending, with zero populated
locations or geocoding attempt timestamps. `error`, `unsupported`, `no_match`,
`ambiguous` and `matched` counts were zero. There is no stored `failed` status;
the report's failed count corresponds to `error`.

The configured Agent login was `citylens_agent`, inherited the existing reader
role, and had none of superuser, create-database, create-role, replication, or
bypass-RLS flags. It had no collision write privileges or schema CREATE.
`ensure_reader` also passed. No administrator fallback was used for any Agent
adapter or endpoint check. FastAPI was exercised in-process with TestClient
against the actual application and actual database, without dependency overrides
or mocked tools. This verifies the endpoint handler; it is not a browser or
external HTTP-server test. No OpenAI model request was made.

The existing import/geocoding lifecycle regression passed in a disposable
database before Census requests. It confirms unchanged addresses preserve
geocodes, changed addresses invalidate them, and an obsolete address key cannot
save a result. Pending-status compare-and-set regressions also passed. The
worker requires BOTH the selected address key and pending status when saving.

Only these six columns were updated for the selected pending reports:
`location`, `geocode_status`, `geocode_provider`, `geocode_precision`,
`geocoded_at`, `geocode_details`. Separate maintenance credentials were used
in memory for that worker, after confirming the same host, port and database
as the Agent connection. Agent access remained read-only. Source values,
tables, roles, ownership, grants, raw CSVs and `.env` were not changed.

Before/after digests over all non-geocoding collision fields matched. The
78,469 nonselected geocodes also had identical digests. There were no completed
geocodes before the pilot, so the completed-production-result preservation
check is vacuous; non-vacuous concurrent-result/import cases passed in isolated
tests. No source CSV was reloaded and no permanent GIS table was created.
See [before.json](before.json), [after.json](after.json), [run.json](run.json).

## Tool checks and EXECUTE grants

| Call | DatabaseTools | Actual FastAPI handler, before/after pilot |
| --- | --- | --- |
| aggregate_service_requests, group_by=both | ok | HTTP 200, tool status ok |
| get_dataset_metadata, service_requests | ok | HTTP 200, tool status ok |
| get_dataset_metadata, traffic_collisions | ok | HTTP 200, tool status ok |

Aggregation scope: 32.84, -117.25; 800 metres; January 1 through March 1, 2026
at -08:00, start inclusive/end exclusive, linked duplicates included. There
were 28 groups totaling **64**, matching an independent call to the existing
`count_service_requests` function. Metadata returned the actual dataset sizes,
and collision metadata subsequently reflected 19 located reports. It continued
to leave unrecorded source publication/update timestamps unknown.

Effective EXECUTE exists on `aggregate_service_requests`, `get_dataset_metadata`
and `normalize_request_category` through PUBLIC. Explicit EXECUTE grants to
`citylens_agent_reader` are missing in the live database. This did not prevent
the calls. [minimum_grants.sql](minimum_grants.sql) prepares just the two tools
and their normalization helper; it was **not executed**. It grants no table
writes, ownership, membership, schema CREATE, or administrator capabilities.
Because execution is already effective, these grants make the intended reader
ACL explicit rather than expanding its current effective capabilities.

If a database owner reviews and approves those exact grants, apply only that
file in one transaction, with stop-on-error. From the repository root:

```powershell
Get-Content -Raw 'backend/audits/2026-11-01-geocoding-pilot/minimum_grants.sql' |
    docker compose exec -T db psql -U citylens -d citylens --single-transaction -v ON_ERROR_STOP=1
```

This command is a manual instruction, not a command executed during the audit.
It targets the configured local Compose database. An owner using another
connection should verify the target database first. Do not rerun the full role
setup script for this narrow task. PUBLIC revocation would be a separate access
review and was neither performed nor bundled into these grants. Afterwards,
rerun the read-only checks in [verification.sql](verification.sql) or the
`verify-tools` mode below. Full outputs are in [preflight.json](preflight.json),
[live-preflight.json](live-preflight.json),
[live-postflight.json](live-postflight.json) and
[independent-sql-checks.json](independent-sql-checks.json).

## Selection, provider and request accounting

[sample.json](sample.json) fixes the exact 25 report IDs and original address
components. Selection used positive integer block numbers, a primary street,
no intersecting street, distinct primary street names, beat-prefix/suffix
diversity, and deterministic MD5 report-ID ordering. The first pass preferred
new beat-prefix/suffix strata; a second pass filled remaining distinct streets.
There were 70,144 eligible source addresses. This is a purposive convenience
sample, with no claim of random sampling or geographic representativeness.
All 25 still-pending addresses and keys were rechecked immediately before use.

`geocode_pending(..., limit=25, pause=1.0, report_ids=manifest_ids)` ran once
with the existing `CensusGeocoder`, default `Public_AR_Current` benchmark,
and no retry or follow-on batch. Actual responses reported benchmark ID 4.
The exact native worker CLI with all selected IDs is recorded in
[PREFLIGHT.md](PREFLIGHT.md). The executed guarded equivalent was:

```powershell
.\.venv\Scripts\python.exe -m backend.scripts.geocoding_pilot run
```

The runner writes an exclusive `RUN_STARTED.json` marker before external calls
and refuses to rerun this pilot. Do not delete the marker or reset statuses.
It records each request and response incrementally, enforces a Census hostname
guard and 25-request budget, and captures postflight state even on interruption.

Exactly **25 Census HTTP requests** are recorded in
[census-requests.json](census-requests.json), with **zero provider errors**.
Worker runtime was **37.792 seconds**, including the one-second pacing pauses.
The reproducible SanGIS fetch made **two requests**: layer schema and one
filtered geometry query, recorded in [sangis-requests.json](sangis-requests.json).
Earlier portal/service discovery requests were not fully metered, so the exact
entire-session HTTP total is unknown; 27 is the instrumented pilot/validation
total, not the browsing-inclusive total. No paid geocoder or LLM was called.

## Independent road diagnostics

The [official City Roads Lines page](https://data.sandiego.gov/datasets/gis-roads-all/)
links to SANDAG-hosted SanGIS data. The page's REST copy link rendered as
`#ZgotmplZ`; the official server catalog led to
[Hosted/Roads_All/FeatureServer/0](https://geo.sandag.org/server/rest/services/Hosted/Roads_All/FeatureServer/0).
The actual [service](sangis-service-schema.json) and
[layer schemas](sangis-layer-schema.json) were inspected and archived first.

Observed geometry is polyline, native WKID 102646/latestWKID 2230 (State Plane,
US survey feet). Actual fields include `roadsegid`, `rd30full`, `rd20full`,
`rd30pred`, `rd30name`, `rd30sfx`, `rd30postd`, `llowaddr`, `lhighaddr`,
`rlowaddr`, `rhighaddr`, jurisdiction/status fields, and `fnode`/`tnode`.
Status domains identify constructed/maintained versus recorded/tentative roads.
The service description identifies jurisdiction-assigned street names and
address ranges and notes possible unconstructed or outdated geometry. The
portal provides no field dictionary. Node IDs alone do not verify an
intersection geocoder, which was not implemented or exercised here.

The schema and description support an independent *diagnostic* comparison.
A query on the 25 supplied street names returned **1,307 segments**, in
WGS84 (`outSR=4326`), with no transfer-limit truncation. The full filtered
response is [sangis-roads.json](sangis-roads.json). It remains an audit file,
not a production table. “Complete” means complete for that exact name query;
unknown aliases or differently formatted names can still be absent.

For each adapter-accepted point, the validator checks:

1. Finite global coordinate bounds and a broad San Diego screen
   (-117.65 to -116.80 longitude, 32.50 to 33.20 latitude). This is **not**
   a city-boundary polygon check. All 19 points passed.
2. Normalized returned street, direction, suffix, CA state and San Diego/La
   Jolla locality. Census numeric ranges must contain the submitted number.
3. Exact normalized SanGIS street-name compatibility, followed by positive
   numeric left/right address-range overlap. A source number ending in 00 is
   compared as [N,N+99], an explicitly stated hundred-block diagnostic; other
   numbers are literal. Zero/missing ranges supply no confirmation. No parity
   or exact house position is inferred from the anonymized number.
4. Point-to-line distance after transforming WGS84 to **EPSG:32611, UTM zone
   11N in metres**, using existing pyproj and point/segment projection.
   The nearest name-and-range-compatible segment and all compatible segment
   distances are retained. The nearest road of any name is never confirmation.
5. A **50-metre review threshold** accommodates roadway offsets and coarse
   interpolation as a conservative triage rule. It is not calibrated against
   known crash coordinates and supplies no accuracy guarantee. More than one
   compatible segment within 50 m is flagged, including adjoining segments at
   a shared endpoint. The nearest road must be constructed/maintained,
   nonpending, nonabandoned, and carry the observed SD jurisdiction code.
   SD is treated as a consistency signal, not an independent boundary proof.

Assessments stay in audit artifacts, separate from production `geocode_status`.
Missing independent geometry is unverified, incomplete or ambiguous evidence
needs_review, and a clear geographical/address mismatch is rejected. None
was rejected in this sample. No point was snapped, replaced or promoted to
exact precision; all 19 stored coordinates remain `block_approximate`.

## Results and suspicious examples

| Existing adapter status | Records |
| --- | ---: |
| matched | 19 |
| no_match | 4 |
| ambiguous | 2 |
| error (failed) | 0 |
| unsupported | 0 |

| Independent assessment | Records |
| --- | ---: |
| plausible | 11 |
| needs_review | 8 |
| rejected | 0 |
| unverified | 6 |

Accepted provider match rate: **19/25 = 76%**. Independent plausibility under
the stated rules: **11/25 = 44%**, or **11/19 = 57.9%** of accepted points.
Census actually returned one candidate for 21 records and no candidate for
four; it returned multiple candidates for zero records. Both ambiguous statuses
were assigned by the existing adapter's conservative address comparison, not
by multiple native Census candidates.

For all 19 accepted points there was name-and-block-range-compatible SanGIS
geometry. Nearest distances: minimum **5.253 m**, median **5.766 m**, maximum
**799.694 m**; 17 were within 10 m, none between 10 and 50 m, two above 50 m.
Six of the 17 nearby points had multiple compatible nearby segments and remain
needs_review. Shared street geometry and ranges are corroboration, not ground
truth about an accident site or historical street configuration.

| Report | Observation | Assessment / interpretation |
| --- | --- | --- |
| 25201589, 7900 PARK VILLAGE RD | Nearest range-compatible segment 74795 is 799.694 m away. A same-name segment 74790 is only about 5.4 m away but has 7300–7499 ranges. | needs_review: Census/SanGIS block-range disagreement; a nearby street alone would conceal it. |
| 23201072, 1700 DEL SUR BLVD | Compatible segment 24184 is 77.923 m away; the nearby same-name segment covers 1600–1699. | needs_review: adjacent-block disagreement; source/provider/GIS cause is not established. |
| 187919, 4600 NIMITZ BLVD | Two compatible segments within 50 m; nearest 7.180 m. | needs_review: adjoining/parallel segments require topology/manual review; not necessarily a wrong point. |
| 1607476, 2200 CAM DE LA REINA | One native Census candidate; `preType=CAM`, `streetName=DE LA REINA`. | adapter ambiguous, audit unverified: existing parser excludes `preType`, causing textual mismatch. |
| 25015379, 9400 CAMINO RUIZ | One native candidate; `preType=CAM`, `streetName=RUIZ`. | adapter ambiguous, audit unverified: preType handling plus CAMINO/CAM formatting needs a separately tested policy. |
| 183276 / 19206280 | No candidates for 800 04TH AVE / 3800 08TH AVE. | unverified: zero-padded ordinal formatting is a possible issue, not confirmed; no alternate requests were made. |
| 22203409 / 23204448 | No candidates for 8300 NAVAJO RD / 100 MARKETPLACE AVE. | unverified: provider nonmatch cause remains unknown. |

After the pilot: 78,469 pending, 19 matched, four no_match, two ambiguous;
error/unsupported remain zero. All six unsuccessful results have NULL
coordinates. Dataset-wide coordinate presence is **19/78,494 = 0.0242%**;
the 76% pilot rate must not be used as citywide coverage or accuracy.
See [coverage-postflight.json](coverage-postflight.json).

## Verification and reproducibility

Final automated results:

| Suite | Passed | Failed | Skipped | Scope |
| --- | ---: | ---: | ---: | --- |
| backend/agent_tests | 152 | 0 | 0 | Full suite, including 25 new pilot cases, real SQL tests in an owned tmpfs database |
| Existing test_collisions.py | 7 | 0 | 0 | Full legacy suite in another owned tmpfs database, port 54334 |
| Before-request guard/lifecycle checks | 8 + 1 | 0 | 0 | Earlier subsets; overlap final suites and must not be added to their totals |

The 25 new cases cover bounded parameterized selection, invalid selections,
one-shot refusal before credentials/network access, source-key checks,
projected distances and degenerate segments, null/zero/descending ranges,
block overlap, direction/suffix/locality mismatches, invalid/out-of-area
coordinates, absent/incomplete/ambiguous/distant road evidence, provider-status
preservation, and offline artifact totals/NULL coordinates/GeoJSON output.
The final command and full output are in [agent-tests-final.txt](agent-tests-final.txt).
[agent-tests.txt](agent-tests.txt) records the earlier 151-pass run before the
last artifact regression was added. [legacy-tests.txt](legacy-tests.txt) and
[preflight-tests.txt](preflight-tests.txt) preserve other test outputs.

An initial audit-runner ToolCall fixture failed validation before database or
network use (missing required fields and wrong timezone field name). The
fixture was corrected, then actual preflight and postflight passed. This
resolved development failure is retained in [failure.json](failure.json).
No live database-tool, endpoint, Census HTTP, or final regression failure occurred.

Executed checks and reproduction commands, from repository root:

```powershell
# Offline tests; integration fixture creates/removes its own DB on port 54331.
$env:AGENT_TEST_DOCKER='1'
.\.venv\Scripts\python.exe -m pytest backend/agent_tests -q

# Executed once: schema + filtered road query, before Census run.
.\.venv\Scripts\python.exe -m backend.scripts.geocoding_pilot fetch-roads

# Executed once after preflight; refuses repetition now.
.\.venv\Scripts\python.exe -m backend.scripts.geocoding_pilot run

# Safe offline reproduction of CSV, JSON and GeoJSON from saved data.
.\.venv\Scripts\python.exe -m backend.scripts.geocoding_pilot validate

# Optional fresh read-only adapter/endpoint checks, no LLM or geocoding call.
.\.venv\Scripts\python.exe -m backend.scripts.geocoding_pilot verify-tools
```

Legacy tests were run as `python -m pytest test_collisions.py -q` from backend
with only the temporary database URL in the child environment; dotenv was
disabled for that child. The owned container was initialized and removed
inside a `try/finally`; production was not used for those tests.
The standalone [verification.sql](verification.sql) was also executed using
the Agent connection in a READ ONLY transaction that was rolled back.

**Not executed in this pilot:** the separate 247-case verification suite/full
raw-data reload tests; bulk geocoding; geocoding intersections; a live OpenAI
call; browser/Uvicorn transport tests; manual GIS ground-truth validation;
role/grant changes. Earlier audit test totals are not claimed as current runs.
No dependencies were added. An in-memory comparison found no configured secret
values or full connection strings in the audit artifacts.

## Changed files and artifacts

| File | Purpose |
| --- | --- |
| backend/collision_geocoding.py | Optional parameterized report_ids / repeated --report-id selector; existing worker/provider and save guards retained |
| backend/scripts/geocoding_pilot.py | One-shot bounded runner, actual read-only tool/endpoint checks, snapshots, request journals, schema-checked filtered GIS fetch, offline report artifacts |
| backend/scripts/collision_validation.py | Independent name/range/geography/metric-distance diagnostics; never writes database fields |
| backend/agent_tests/test_geocoding_pilot.py | 25 offline regression cases |
| backend/TRAFFIC_COLLISIONS.md | Selection and pilot-validation documentation |
| backend/audits/README.md | Link to this pilot |
| This audit directory | Report, per-record CSV, summary, GeoJSON, manifests, schemas, filtered road geometry, provider responses, SQL, grants prepared only, and exact verification outputs |

The workspace already contained Phase 1.1 changes when this pilot began. This
list describes pilot work; other pre-existing modifications were not reverted
or attributed to this task.

## Decision and remaining work

**Choose B (SanGIS validator) and D (improve validation before expanding).**
The real schema, 19 compatible geometry comparisons, and two block-range
outliers justify retaining the independent validator. Improve topology handling
for the six multiple-segment cases, manually inspect both outliers, calibrate
thresholds against reviewed examples, and check historical geometry/range
changes before relying on these labels operationally. No production GIS
geocoder, automatic snapping or validation-status table is introduced.

**Keep A (Census) as a restricted option, conditional on those improvements.**
Nineteen accepted block estimates and 11 independently consistent results show
potential for supported primary-block addresses, not readiness for unattended
bulk runs. Add separately reviewed tests/policy for `preType`, CAMINO/CAM,
post-directions/qualifiers and ordinal formats. Preserve original components
and address keys; any retry of completed statuses requires separate review.
The preType omission is observed in responses and code; geographic correctness
of those rejected candidates has not been established.

**Plan C (intersection-specific geocoding) for a later bounded phase.**
Source-address counts show 6,840 reports with cross streets and 1,510 with
missing/invalid blocks, beyond the current adapter's supported primary-block
path. Those are eligibility categories, not stored unsupported statuses.
No intersections were discarded or assigned guessed coordinates. SanGIS node
fields suggest a path worth investigating; reliable intersection matching,
levels/overpasses, ambiguity and source completeness still need separate work.

The principal observed problems are adapter address-component handling (two
conservative rejections), Census/SanGIS block-range disagreements (two points),
multiple compatible nearby road segments (six), and four unexplained provider
nonmatches. Intersections are a known unprocessed limitation. This sample found
usable local GIS coverage for all 19 accepted points; it does not establish
complete GIS coverage elsewhere. The remaining 78,469 reports stay pending;
no larger batch will follow automatically.
