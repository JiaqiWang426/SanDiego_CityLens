# CityLens Phase 0 Agent

[P1.3 session persistence](P1_3.md) wraps the analysis graph in a PostgreSQL-backed
LangGraph conversation graph. Saved messages, pending confirmations and validated
scope/plan survive restarts. The request-local analysis graph and its deterministic
tools/verification retain their responsibilities. Persistence is enabled only by
the separately configured session writer; see P1.3 for migration and Windows startup.

P1.2 adds [address interpretation and confirmation](P1_2.md) through
`POST /api/v1/agent/chat` and the frontend's default street-address mode.
The same configured model produces a structured proposed analysis/address
interpretation. A new `resolve_locations` entry node on the existing LangGraph
pauses for explicit confirmation, then uses Census with SanGIS Address Points
fallback only on legitimate no-match, and the existing analysis nodes. Census
ambiguity/errors retain their original handling; SanGIS candidates use the same
confirmation interface. The saved plan is validated against resolved pins without another
planning call. Coordinate requests still enter at `resolve_scope`.
See the [provider benchmark](verification/geocoding_benchmark/REPORT.md) for
coverage, placement limitations and evidence supporting Census as primary.

[P1.2.2](P1_2_2.md) prepares a capped Google experiment and a disabled selective-
review prototype, without changing this production chain or adding a LangGraph
node. Live Google use is gated separately from key configuration. Historical
audit/enrichment proposals remain independent of interactive address resolution.

Phase 1.1 extends these adapters with service-request grouping and dataset
metadata; see [PHASE1_1.md](PHASE1_1.md) for the current additive contracts.

Implemented against [AGENT_SPECIFICATION.md](../AGENT_SPECIFICATION.md).
For audited integration fixes, diagnostic codes, exact test results, manual
reader-role setup and the gated live smoke command, see [AUDIT_FIXES.md](AUDIT_FIXES.md).
This is a runnable FastAPI + LangGraph + OpenAI backend, with deterministic
database tools and numerical verification. Live OpenAI calls require your key
and enabled structured-output model. No mock planner is enabled in the app.

The local [evaluation desk](EVALUATION.md) at `http://127.0.0.1:8000/` now
provides question entry, executed tool logs, evidence inspection, JSON downloads,
and a direct SQL-tool runner. Its HTML, CSS, and JavaScript source lives in the
top-level `frontend/` directory. `POST /api/v1/tools/run` bypasses the LLM and accepts
only typed allowlisted calls. Query responses also include `execution_plan` and
`tool_calls` with reproducible read-only SQL and bounded result rows. Logs are
returned after completion; no private model reasoning is exposed.

## Architecture

```text
POST /api/v1/agent/query
  Pydantic request validation (no LLM)
  → resolve supported scope / explicit unsupported result
  → OpenAI structured plan → deterministic plan validation
  → bounded allowlisted read-only SQL tools
  → evidence normalization / calendar buckets
  → Python comparisons + claim construction
  → claim verification
  → same OpenAI model orders verified claims (restricted composition)
  → deterministic rendering + final response verification
```

There is one configured LLM for interpretation/planning/composition. Analysis
graph state is explicit and request-local. With P1.3 persistence configured,
the outer conversation graph saves structured context and pending address state.
Without it, legacy address requests use bounded process-local pending state
with revision checks and 30-minute expiry.
Conditional edges route unsupported/failed requests to
abstention; independent tool failures preserve successful evidence. Claims are
constructed **before** verification; final validation runs **after** composition.

To prevent unsupported semantic assertions, Phase 0 does not accept free-form
LLM answer prose. The composer orders verified claim IDs and selects a fixed
introduction style. Python renders the checked facts, scope and limitations.
Invalid composition falls back to the verified template. This trades expressive
prose for testable grounding; it is a deliberate conservative implementation of
the specification's semantic-claim restriction.

## Inspecting and reusing the existing implementation

No ingestion, geocoding, table definitions, or existing query functions were
rewritten. Existing storage is `ingestions`, `service_requests`,
`collision_imports`, `collision_source_rows`, and `traffic_collisions`, all in
the `citylens` schema of the existing database.

Actual reusable signatures:

| Existing interface | Inputs / behavior |
| --- | --- |
| SQL `count_service_requests` | `latitude, longitude, radius_meters, start_time, end_time, exclude_linked_duplicates=false`; returns bigint |
| SQL `get_traffic_collisions` | `start_time, end_time, latitude=NULL, longitude=NULL, radius_meters=NULL, min_injured=NULL, max_injured=NULL, min_killed=NULL, max_killed=NULL, hit_run=NULL, street_name=NULL, hit_run_level_filter=NULL, result_limit=100, result_offset=0`; returns collision rows |
| SQL `aggregate_traffic_collisions` | Same filters as retrieval, ending with `period='month', calendar_timezone='America/Los_Angeles'` instead of limit/offset; returns calendar aggregate rows |
| Python `get_collisions` | `(conn, start_time, end_time, *, limit=100, offset=0, **filters)` |
| Python `aggregate_collisions` | `(conn, start_time, end_time, *, period='month', calendar_timezone='America/Los_Angeles', **filters)` |

The agent calls these **existing SQL functions**, rather than duplicating their
filtering or aggregation in Python. Its connection wrapper adds role checks and
dataset diagnostics in the same repeatable-read snapshot. Existing Python
collision wrappers start their own transactions and require an idle connection,
so they cannot directly surround measurement + diagnostics in one snapshot.
The internal uncapped SQL filter is not in the agent registry.

Discrepancies / decisions relative to the proposed specification:

- The spec's existing-code section was unverified; the actual signatures above
  are authoritative. Python uses `hit_run_level`, SQL uses `hit_run_level_filter`.
- SQL aggregates return `collision_count`, `unknown_injured_count`, and
  `unknown_killed_count`; normalization maps these to `report_count`,
  `unknown_injury_count`, and `unknown_fatality_count` in calendar buckets.
- SQL omits empty buckets. Python fills only intersecting requested calendar
  periods, marking absent source buckets. First/last periods can be partial;
  the exact query scope is retained. Missing buckets do not prove data coverage.
- Source completeness, freshness, local unlocated-report counts, and exact
  query-contributing import IDs are not supplied by the existing interfaces.
  They remain unknown. Dataset-wide diagnostics/import lists are explicitly
  separate from query provenance; they are never a local coverage denominator.
- A dataset with no stored records yields unavailable evidence, not zero.
  A successful query with zero matches yields a partial observed count with
  coverage warnings. All current successful data answers are `partial` because
  completeness and freshness remain unverified.
- Phase 0 requires one or two valid coordinate pins and an explicit offset-aware
  date interval; missing/invalid pins produce HTTP 422, not an LLM guess.
  `needs_location` is reserved in the response model but is not currently emitted.
- Geographic admission uses a conservative envelope (32.5–33.2 latitude,
  −117.6–−116.8 longitude), not a verified city polygon. Circles can cross the
  imported coverage boundary. Default radius is 800 m; max is 50,000 m.
- The API date interval is capped at 3,660 days; no rolling default is invented.
- Requested collision filters are extracted by the model, typed/bounded, and
  require an exact supporting question quote for nondefaults. That quote is
  traceability, not a deterministic proof of semantic interpretation. Clients
  should inspect executed filters for nuanced questions. Source field contents
  never override policy; the model cannot change structured dates or pins.
- The coordinate endpoint accepts legacy `conversation_id` without changing its
  stateless behavior. P1.3 adds optional `session_id` for durable storage. With
  session persistence enabled, the chat endpoint retains pending requests and
  confirmations in PostgreSQL and supports follow-ups across server restarts.
- No cross-request cache is used without reliable dataset versioning. Plans are
  deduplicated within each request. SQL calls are not retried; the interactive
  SanGIS HTTP adapter permits at most two attempts for transient transport errors.

## Files

| File | Responsibility |
| --- | --- |
| `main.py` | FastAPI endpoint, deterministic HTTP validation, request deadline |
| `agent/models.py` | Request, plan, tool-related, evidence, claim, comparison and response contracts |
| `agent/settings.py` | Environment credentials and bounded execution settings |
| `agent/llm.py` | One OpenAI model, typed planning and restricted composition |
| `agent/tools.py` | Measurement-tool allowlist, dataset metadata, SQL binding and read-only/role enforcement |
| `agent/evidence.py` | Evidence records, calendar normalization, comparison arithmetic, independent claim checker |
| `agent/workflow.py` | LangGraph state, nodes, bounded routing, partial/error handling and final verification |
| `agent/location_models.py` | Additive chat, address-interpretation and candidate contracts; no LLM coordinate fields |
| `agent/location_flow.py` | Pending requests, explicit confirmations, corrections, expiry and revision checks |
| `agent/user_geocoding.py` | Interactive Census candidates and interpolation metadata, separate from collision persistence |
| `agent/sangis_geocoding.py` | Strict WGS84 Address Points candidates; fallback only after Census no-match |
| `agent/geocoding_http.py` | Bounded public-provider JSON requests and sanitized transport diagnostics |
| `sql/setup_agent_reader.sql` | Optional, manually reviewed least-privilege role setup; not loaded by `db.py init` |
| `agent_tests/` | Mocked/unit, SDK-wire, and isolated PostGIS integration tests |
| `examples/agent/` | Three request examples and explicitly synthetic response fixtures |
| `requirements-agent-test.txt` | Test dependencies layered on the runtime requirements |

`backend/__init__.py`, `backend/README.md`, and runtime requirements are also
added/updated for package execution and discovery. Existing schemas, source CSVs,
`.env`, and database initialization behavior are unchanged.

## Configuration and running

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
```

Use your existing database; initialize it with the existing `db.py init` only if
needed. **Before using the agent**, a database administrator should review and
manually run `backend/sql/setup_agent_reader.sql`. This script was **not applied
to your development database** during implementation. It only creates/grants the
reader group and does not revoke existing PUBLIC permissions.

Connect using psql as the existing administrator and run:

```sql
\i 'E:/Github Projects/SanDiego_CityLens/backend/sql/setup_agent_reader.sql'
CREATE ROLE citylens_agent LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\password citylens_agent
GRANT citylens_agent_reader TO citylens_agent;
```

`\password` prompts securely; do not put a password in committed SQL. Ensure
there are no inherited write/admin memberships or schema ownership. Existing
PUBLIC grants are inherited: review them separately before deployment. The
uncapped helper requires EXECUTE for invoker-rights SQL composition, but is never
an LLM-facing tool. These application functions do not elevate role privileges.

Configure the repository-root `.env` as described in
[README.md](README.md#environment-configuration). It loads automatically before
settings are initialized. File values override existing shell variables. The
following shell configuration is an alternative only for keys absent from `.env`:

```powershell
# Supply your actual values; URL-encode special characters in the DB password.
$env:AGENT_DATABASE_URL = 'postgresql://citylens_agent:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:54329/citylens'
$env:OPENAI_API_KEY = 'YOUR_OPENAI_API_KEY'
$env:OPENAI_MODEL = 'YOUR_ENABLED_STRUCTURED_OUTPUT_MODEL'
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Use a model enabled for your account that supports Responses structured outputs.
No implicit model is chosen. API usage can incur charges. OpenAPI is available
at `http://127.0.0.1:8000/docs`. The SQL tools use only `AGENT_DATABASE_URL`, with no
fallback to administrative `DATABASE_URL`. It rejects superuser/owner or
effectively write-capable credentials even though queries also run read-only.
Keep this Phase 0 server local: authentication and public deployment hardening
are not included. Provider calls use `store=False`; LangSmith tracing is not
enabled by this implementation.

| Optional variable | Default | Allowed |
| --- | --- | --- |
| `AGENT_MAX_TOOL_CALLS` | 6 | 1–6 measurement calls, each with fixed metadata diagnostics |
| `AGENT_MAX_ROWS` | 100 | 1–1,000 retrieved reports per call |
| `AGENT_MAX_OUTPUT_TOKENS` | 2,500 | 500–8,000 per model call; at most two calls |
| `AGENT_LLM_TIMEOUT_SECONDS` | 25 | 1–60 per model call |
| `AGENT_REQUEST_TIMEOUT_SECONDS` | 120 | 5–180 total request deadline |

Statements have 10-second timeouts, lock waits 2 seconds, connections 3 seconds.
An overall deadline cancels the response; an already-running threaded DB call
can continue until its own bounded statement timeout. Query and diagnostic SQL
share one read-only repeatable-read snapshot per measurement, not a global
snapshot across both pins. Concurrent imports can therefore change data between
measurements; no persistent replayable snapshot is claimed.

## API examples

Send any supplied example:

```powershell
$body = Get-Content -Raw backend/examples/agent/collision_count.request.json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/agent/query' -ContentType 'application/json' -Body $body
```

### Single-location collision count

```json
{
  "question": "How many traffic collisions were recorded within 800 meters of location A during the specified period?",
  "locations": [{"id": "A", "latitude": 32.84, "longitude": -117.25}],
  "radius_meters": 800,
  "time_range": {
    "start": "2026-01-01T00:00:00-08:00",
    "end": "2026-03-01T00:00:00-08:00",
    "calendar_timezone": "America/Los_Angeles"
  },
  "preferences": ["traffic_collisions"]
}
```

It selects aggregation, creates evidence from actual executed SQL, sums its
authoritative bucket counts, constructs a count claim, verifies it and responds.
The full [synthetic response](examples/agent/collision_count.synthetic-response.json)
shows the exact JSON shape. Its value of **7 is a fixture**, not an observed
San Diego finding. Actual values depend on the database and geocoding coverage.

### Two-pin comparison

Use [compare_locations.request.json](examples/agent/compare_locations.request.json).
The same filters, dates, radius and duplicate policy are applied to both pins.
The [synthetic response](examples/agent/compare_locations.synthetic-response.json)
has fixture counts A=7 and B=11 and a deterministic B-minus-A difference of +4
for each domain. It labels circle overlap and does not rank safety or quality.
Every difference references the two source evidence records. An unavailable
domain has NULL evidence values and no fabricated comparison.

### Unsupported crime question

Use [unsupported_crime.request.json](examples/agent/unsupported_crime.request.json).
The [response fixture](examples/agent/unsupported_crime.synthetic-response.json)
has `status="unsupported"`, `unsupported_dimensions=["crime"]`, and no claims,
evidence or tool calls. The scope guard can handle this without a provider key.
Mixed supported/unsupported questions return supported findings as partial.

The response contract includes `status`, `intent`, `answer`, `comparison`,
`claims`, `evidence`, `limitations`, `limitation_codes`, `unsupported_dimensions`,
and `tool_trace_id`. Successful evidence retains exact scope, filters, tool
version, query/evidence IDs, execution time, source references and available
provenance. Missing freshness and exact contributing hashes remain NULL/unknown.

## Automated verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-agent-test.txt
# No external services: unit tests + real SDK over mocked HTTP; DB tests skip.
.\.venv\Scripts\python.exe -m pytest backend/agent_tests -q

# Include isolated PostGIS integration. Launches and removes its OWN tmpfs
# container on port 54331; never uses .env or the development DB on port 54329.
$env:AGENT_TEST_DOCKER = '1'
.\.venv\Scripts\python.exe -m pytest backend/agent_tests -q
```

Alternatively set `TEST_AGENT_DATABASE_URL` to an **empty** isolated `*_test`
database on a non-development port. The fixture refuses populated CityLens
tables; it does not truncate existing imports. It creates synthetic records and
reader roles only in the test database. This external test database is retained;
prefer the owned disposable container for repeatable runs.

Verified areas include deterministic FastAPI validation, typed allowlisting,
planning/routing, symmetric comparisons and swapped inputs, arithmetic,
evidence creation, claim references/prose validation, unknown coverage and NULL
counts, unsupported requests, partial failures, timeout handling, real SQL
limits, DB read-only permissions, DST calendar normalization, prompt injection,
and actual OpenAI SDK serialization/parsing using an in-memory HTTP transport.
Live OpenAI calls were not executed because the API key/model were unavailable.
The Phase 0 suite passed **43 tests**, including **five real PostGIS integration
tests**, in the owned disposable container. The OpenAI SDK wire test used mocked
HTTP, not a live provider. Production database permissions were not changed.
Mocked planner tests do not establish live-model intent-selection accuracy.
The preexisting real-data verification report is not a result of this Agent run.

## Remaining limits and Phase 1

- Coverage/freshness and CD1 geography still need source verification. No crime,
  exposure denominators, comprehensive safety assessment, or composite score.
- Local unlocated collision counts cannot be inferred from global diagnostics.
  Collision positions remain block-approximate and spatial membership uncertain.
- Exact query-contributor provenance and historical replay are not available
  from current aggregate interfaces; dataset import lists do not replace them.
- Intent/filter interpretation still depends on the model. A quote is not a
  semantic proof; unsupported language or invalid plans abstain rather than
  executing arbitrary operations. Live-model evaluation remains outstanding.
- A trend returns calendar buckets and descriptive counts, not a causal or
  risk trend assertion. Empty/partial periods must not be treated as verified
  complete periods.
- Phase 1.1 now supplies service categories/statuses and dataset metadata.
  Full apartment-address resolution, session memory, expanded source catalogs
  and local coverage tools remain deferred. Commute, amenities, development data, ranking/scoring and public
  deployment are outside this Phase 0 implementation.

Implementation references: [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api),
and [FastAPI request bodies](https://fastapi.tiangolo.com/tutorial/body/).
