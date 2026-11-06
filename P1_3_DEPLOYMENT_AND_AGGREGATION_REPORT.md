# P1.3 — Local Deployment and Service Aggregation Audit

Completed November 4, 2026, America/Los_Angeles. All deployment operations below
targeted the existing local development database. No public/production service,
source-data ingestion or geocoding operation was performed. The working tree's
previous P1.3 implementation was retained; nothing was committed or pushed.

## 1. Deployment status

**Deployed and running:** <http://127.0.0.1:8000/> using
`python -m backend.scripts.serve --port 8000`. The existing
`sandiego_citylens-db-1` container was healthy, with PostGIS/PostgreSQL 17.5 on
`127.0.0.1:54329`, database `citylens`. Port 8000 was free before this task; only
backend processes started and identified by this task were restarted.

The baseline contained no `citylens.sessions` table, no checkpoint schema, and
no session connection settings. The existing administrator credential worked;
no password was guessed or reset. `SESSION_DATABASE_URL` and
`SESSION_SETUP_DATABASE_URL` are now configured **only in ignored local `.env`**.
The server status endpoint reports OpenAI, SQL database and sessions configured.
Actual connectivity was independently verified by successful queries below.

## 2. Database and permission changes

| Purpose | Role | Changes |
| --- | --- | --- |
| Deterministic SQL tools | Existing `citylens_agent`, member of `citylens_agent_reader` | Existing memberships and source grants preserved |
| Session persistence | New `citylens_sessions` | Login with generated local credential; no superuser, CREATE DATABASE/ROLE, replication or BYPASSRLS privileges; no role memberships |
| Session migrations | Existing schema-owner administrator `citylens` | Separate setup connection; never used by SQL tools or the runtime session store |

Ran `python -m backend.scripts.setup_sessions` twice successfully. It created:

- `citylens.sessions`, its primary key and `sessions_recent_idx`.
- `citylens_checkpoints` with `checkpoints`, `checkpoint_blobs`,
  `checkpoint_writes`, `checkpoint_migrations`, their keys and thread indexes.
- Official checkpoint migration versions **0 through 9**.

The session role has schema USAGE and SELECT/INSERT/UPDATE/DELETE on the session
table and the three checkpoint-state tables. Checkpoint migration metadata is
**SELECT-only**; runtime DDL/migration writes are unnecessary. It has no SELECT
or mutation privileges on source/application/reference datasets. The existing
agent reader has no mutation privileges on any application/checkpoint table.
No existing role was altered or given additional memberships.

Aggregation added one 10-argument overload from
`backend/sql/tools/045_aggregate_service_request_categories.sql`. EXECUTE on that
new overload was granted to `citylens_agent_reader` and revoked from PUBLIC.
It is STABLE and SECURITY INVOKER. Existing SQL function definitions and grants,
including the 6-argument counter and 9-argument aggregate, are byte-for-byte
unchanged according to the recorded definition hashes. No source table changed.

Before/after content fingerprints, counts, ownership and source-table grants
match for **all 10 application/reference tables, totaling 763,889 rows**. This
includes 303,225 Get It Done records, collisions, imported source rows, road
reference tables and geocoding deployment history. Existing role attributes and
memberships also match. See
[preservation-and-permissions.json](backend/audits/2026-11-04-p1_3-deployment-aggregation/preservation-and-permissions.json).

## 3. Real session, restart and frontend verification

Used three explicitly identified development audit sessions; there were no
preexisting saved conversations. Real `gpt-5.4-mini` calls were used for initial
planning, radius/category follow-ups and targeted failure diagnostics. No mock
model was injected into the running local service. Catalog reads, broad-category
clarification, direct SQL comparisons and browser-only history checks need no LLM.

| Check | Observed result |
| --- | --- |
| Initial two-pin request, 800 meters, Jan 1–Mar 1, 2026 | Verified Get It Done counts **A=64, B=107** |
| Radius-only follow-up to 1,200 meters | Counts **A=157, B=174**; pins, dates, timezone, preferences and plan retained |
| History retrieval | User/assistant turns and structured query context saved |
| Actual backend termination/restart | Transcript, context and latest response matched the pre-restart snapshot exactly |
| Independent new session | Empty transcript and no borrowed query context |
| Real Edge browser | History list, selection, refresh recovery and independent selection passed |
| Real Edge after category follow-up | Restored counts, category filters and eight saved messages; no JavaScript page errors |
| Mobile viewport, 390px wide | No horizontal document overflow; screenshot inspected |
| Delete confirmation | Dismiss retained the session; accept removed only the selected audit session |
| Cleanup | Exactly this task's three sessions deleted; zero related checkpoint/blob/write rows; no saved sessions remain |

See [session-verification.json](backend/audits/2026-11-04-p1_3-deployment-aggregation/session-verification.json),
[frontend verification](backend/audits/2026-11-04-p1_3-deployment-aggregation/frontend-and-cleanup-verification.json),
and [actual browser screenshot](backend/audits/2026-11-04-p1_3-deployment-aggregation/frontend-multi-category.png).
Failed audit attempts remain visible in saved audit snapshots; they were not
silently presented as successful turns. Their live sessions were cleaned up.

## 4. Existing aggregation capabilities and exact gaps

The installed definitions were inspected directly using `pg_get_functiondef`,
then compared with source, Python wrappers, schemas, planning, evidence and tests.

| Audit question | Before this follow-up | Now |
| --- | --- | --- |
| Group by service name? | Yes: `service_name`, `status`, or `both`; all groups returned | Preserved |
| Single category filter? | Yes, exact label after uppercase/trim/whitespace normalization | Preserved; agent also checks stored vocabulary |
| Several categories in one request? | No; scalar `service_name_filter` only, no array overload | Optional `service_names` union via additive overload |
| Broad road-category group? | No validated mapping in repository, deployed functions or schema | Still no silent broad mapping; available interpretations are presented |
| Grouping/filter schema exposed? | Existing scalar/grouping arguments correctly exposed | Optional list exposed through plan, tool, wrapper, evidence and follow-up state |
| Discover valid categories? | A scoped unfiltered group query could reveal observed labels; no catalog API or automatic follow-up vocabulary | Read-only catalog API plus catalog supplied to service follow-up planning |
| Validate against actual DB categories? | Formatting and quote checks only; an invented exact label could yield zero | Agent tools reject unknown labels as `unsupported_service_category`, without numerical evidence |

`count_service_requests` counts stored IDs for the spatial/time/parent-link
scope; it has no category/status filtering. The original aggregate additionally
normalizes labels, filters one category/status and groups distinct IDs. Both use
inclusive radius boundaries, half-open time `[start,end)` and optional exclusion
of parent-linked submissions. The source primary key permits one row/category
per request ID. NULL/blank normalize to the unknown group; literal `UNKNOWN`
remains a separate named category. No synonym/legacy-name merging exists.

The local vocabulary contains **44 nonblank canonical labels plus an unknown
category**, with 424 blank source labels. There are **303,225 records and
303,225 distinct request IDs**. No NULL location, timestamp or status was present
in this local snapshot; NULL behavior was exercised in isolated synthetic tests.
These observations do not establish full geographic coverage or freshness.

### Road interpretations and verified definitions

There was no preexisting validated road group to reuse. The City lists separate
street, sidewalk, lighting, traffic and vehicle issue types; it does not establish
one aggregate matching every meaning of “road-related.” The terminology and
service descriptions were checked against the City's
[Get It Done page](https://www.sandiego.gov/get-it-done),
[service FAQ](https://getitdone.sandiego.gov/TSWCommunityFAQ), and
[street resurfacing/pothole guidance](https://www.sandiego.gov/street-div/services/street-resurfacing-pothole-repair).

The following are **CityLens scope suggestions**, derived from those descriptions
and exact values observed locally; they are not a claimed official hierarchy or
an automatically applied classification:

| Suggested interpretation | Exact locally stored categories |
| --- | --- |
| Road surface | `Pothole`, `Pavement Maintenance` |
| Traffic infrastructure | `Traffic Sign Maintenance`, `Traffic Signal Issue`, `Traffic Signal Timing`, `Traffic Engineering`, `Street Light Maintenance` |
| Additional street/pedestrian maintenance, if requested | `Sidewalk Repair Issue`, `ROW Maintenance`, `Right-of-Way Code Enforcement`, `Street Sweeping`, `Street Flooded` |

Parking/parking violations, shared mobility, trees/vegetation, stormwater, waste,
encampments and broad “Other” records may concern a street, but their service
labels alone do not prove a road issue. They are not silently added. Suggested
lists are intersected with the current database vocabulary so absent/historical
categories cannot be invented. A user can choose any explicit supported labels.

For “What if we only consider road-related issues?”, the live session now
returns a catalog-backed clarification with available interpretations, retains
both pins/800 meters/original dates, and executes **no analytical SQL or model
call** for that ambiguity. This is justified by the absence of an existing
validated broad mapping. The explicit answer “Only include Pothole and Pavement
Maintenance” then executes fresh tools and returns **A=14, B=12**, with three
verified claims and matching query/evidence references. It does not approximate
the broad group with potholes alone.

## 5. Root causes and minimal corrections

1. **Deployment configuration:** session persistence was implemented but schemas,
   role grants and local connection settings had not been installed. Applied
   the existing migration; source preparation was not rerun.
2. **Follow-up wire schema:** live model output put `radius_meters` in
   `changed_plan_fields`, although only plan leaves belong there. Replaced the
   open-ended string list with an enum shared by schema and patch validation;
   clarified that radius/date fields are separate. Preserved the diagnostic
   output proving the original rejection and verified a successful live rerun.
3. **Category representation:** the old SQL and tool accepted only one label.
   Added a validated 1–20-label list and an overload that calls the original
   function with `group_by='both'`, filters exact normalized labels and combines
   disjoint groups. Repeated labels cannot multiply contributions. Empty lists,
   NULL elements, unknown categories and simultaneous scalar/list selection fail
   validation. The original signatures and scalar semantics are preserved.
4. **Discovery/validation:** added current-label discovery and validation using
   the same restricted reader. Unknown labels produce an explicit tool error,
   not a fabricated zero. The legacy raw SQL scalar function still permits a
   no-match scalar and returns zero; agent/tool behavior is intentionally stricter.
5. **Planner-to-tool diff:** a live filter update could omit the required
   `count_service_requests` → `aggregate_service_requests` switch from its diff.
   The deterministic patch compiler now applies that necessary dependency. It
   preserves unrelated scope, filters and tool budgets; existing verification
   still checks each resulting query and numerical claim.

No embeddings, new classifier, vector store, new data pipeline or replacement
planner was introduced. The original working SQL implementations were reused.

## 6. Files changed in this follow-up

| Files | Reason |
| --- | --- |
| `backend/agent/service_categories.py` (new) | Verified catalog, category validation, explicitly nonautomatic road-scope suggestions |
| `backend/sql/tools/045_aggregate_service_request_categories.sql` (new) | Additive category-union overload |
| `backend/agent/models.py`, `session_models.py` | Backward-compatible list selection and constrained patch leaf enum |
| `backend/agent/sessions.py`, `workflow.py`, `llm.py` | Catalog-backed clarification, required tool dependency, typed list handling and instructions |
| `backend/agent/tools.py`, `backend/service_request_queries.py`, `backend/main.py` | Array binding, DB vocabulary validation, catalog endpoint and wrapper support |
| `backend/agent_tests/test_service_categories.py` (new) | 19 category schema, union, NULL/blank, reference-count, API and evidence cases |
| `backend/agent_tests/test_sessions.py`, `test_phase1_data.py` | Strict provider-schema regression, realistic isolated category fixture and overload privilege assertions |
| `backend/scripts/audit_p1_3_database.py`, `audit_service_aggregation.py` (new) | Repeatable read-only preservation and SQL-comparison audits |
| `backend/P1_3.md`, `backend/README.md`, this report | Deployment, API, permission, compatibility and verification documentation |
| `backend/audits/2026-11-04-p1_3-deployment-aggregation/` (new) | Sanitized evidence, diagnostic failures, successful checks and browser screenshots |
| `.gitignore` | Keep runtime backend logs/PID local |
| Local `.env` (ignored) | Separate runtime/migration credentials; existing secrets preserved |

The production frontend needed no additional source change in this follow-up;
its existing generic history, grouping/evidence and raw-JSON views handle the
additive list field. Original ingestion, collision and geocoding files were not
changed. Preexisting uncommitted P1.3 work remains uncommitted.

## 7. Test results

| Verification | Final outcome |
| --- | --- |
| Complete backend suite, `AGENT_TEST_DOCKER=1` | **516 passed**, 0 failed, 0 skipped; 60.98 seconds |
| Focused session/aggregation/SDK suites before the final dependency regression | **70 passed** |
| Actual local tool versus independent direct SELECT | **22 passed** |
| Frontend startup/location-flow/session suites in Node | All passed |
| Actual Edge desktop/mobile history and deletion checks | Passed; no JavaScript page errors |
| Real process restart and state equality | Passed |
| Source/reference content, existing roles, function definitions and grants | Unchanged |
| Test-session and checkpoint cleanup | Passed; only the three recorded task IDs deleted |

The 22 local SQL cases include both pins, service/status/both grouping, scalar
and union selection, repeated normalized labels, linked-parent exclusion,
status filters, blank/unknown categories, and exact timestamp boundaries. For
800 meters and Jan 1–Mar 1, 2026, representative results were:

| Measurement | A | B |
| --- | ---: | ---: |
| All service IDs | 64 | 107 |
| Pothole | 11 | 9 |
| Pothole + Pavement Maintenance | 14 | 12 |
| Same union excluding parent-linked submissions | 12 | 8 |
| Same union, status Closed | 12 | 10 |

Each tool result matched the independent raw-table SELECT and passed numerical
claim verification. A real boundary timestamp produced 1 match at an inclusive
start and 0 at an exclusive end. Isolated tests cover NULLs and deliberately
invalid list inputs without changing development records.

Failures found during verification are retained, not concealed: the first live
radius update requested unnecessary clarification; the first live union update
hit the missing tool-switch dependency. Both were fixed and live-rerun. The
first focused test run reported 68 passed / 2 failed because the old session
fixture had no Pothole category and a schema test assumed one aggregate
signature. The isolated synthetic fixture and overload assertion were corrected;
no development source record was edited to make a test pass. Final results above
are after these corrections.

Read-only audit commands, from the repository root:

```powershell
.\.venv\Scripts\python.exe -m backend.scripts.audit_p1_3_database --output local-baseline.json
.\.venv\Scripts\python.exe -m backend.scripts.audit_service_aggregation --output local-aggregation.json
$env:AGENT_TEST_DOCKER = '1'
.\.venv\Scripts\python.exe -m pytest backend/agent_tests -q
```

The audit scripts inspect only this project's existing local DB or configured
restricted reader; they do not initialize, ingest, geocode or delete records.
The SQL overload can be installed independently by the schema owner from its
045 file; rerunning source `db.py init` is unnecessary. A fresh isolated test DB
uses the existing initialization convention to load the added tool file.

## 8. Remaining limitations and unverified cases

- “Road-related” remains genuinely scope-dependent. The suggested lists are
  transparent project interpretations, not an official exhaustive taxonomy.
  Additional label groups should be reviewed and versioned before automatic use.
- Live-model semantic annotations remain imperfect: the Part A query attached a
  spurious `development` unsupported-dimension tag to development-labelled pins,
  and a supported category response retained `unavailable_dimension`. These
  annotations are recorded in the live artifacts. SQL counts, selected filters
  and evidence references still matched independently verified results. This
  audit does not claim broad live-model semantic accuracy or silently rewrite
  uncertain annotation intent.
- Known historical names are kept distinct, including parking variants. A
  catalog of stored labels does not prove exhaustive real-world issue coverage,
  source freshness, physical defect counts, completed repairs or safety.
- Category presence is validated globally against the local dataset. A valid
  category can correctly have zero matches for a specific pin/date interval;
  new official labels become selectable after local data contains them.
- Session endpoints remain unauthenticated and local-only. Session IDs are not
  authorization. Completed turns persist; interrupted SQL/LLM work is not
  guaranteed exactly once or resumed in the middle of execution.
- Browser checks covered history, restore, refresh, responsive width and
  deletion. They were not a full accessibility/security audit or a test of every
  address/geocoding workflow. No live geocoding provider was invoked by this task.

The service is left running locally with no audit conversations remaining.
