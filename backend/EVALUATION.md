# Evaluate CityLens in your browser

The local evaluation desk is served by FastAPI at **http://127.0.0.1:8000/**.
It uses the real agent; there is no automatic fake response mode. No separate
frontend installation or Node.js build is needed.

Frontend source lives in the repository-root `frontend/` directory, alongside
`backend/`: `index.html` provides structure, `styles.css` provides styling, and
`app.js` provides browser interactions. FastAPI serves these files; start the
same backend command below. Asset URLs remain `/assets/...`.

## Start

Open PowerShell in the repository root. Install dependencies, then configure
your existing restricted database login and OpenAI credentials in the root
`.env` (see [configuration instructions](README.md#environment-configuration)).
The root `.env` takes precedence over existing shell variables. The shell-based
alternative below applies only to keys absent from `.env`:

```powershell
cd 'E:\Github Projects\SanDiego_CityLens'
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
$env:AGENT_DATABASE_URL = 'postgresql://citylens_agent:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:54329/citylens'
$env:OPENAI_API_KEY = 'YOUR_KEY'
$env:OPENAI_MODEL = 'YOUR_ENABLED_STRUCTURED_OUTPUT_MODEL'
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/ in your browser. Keep PowerShell running; Ctrl+C
stops the server. Restart it after changing environment variables or `.env`.
The root `.env` loads automatically and overrides conflicting process values.
Database reader setup is explained in
[AGENT.md](AGENT.md#configuration-and-running). That prepared setup script has
not been applied to your existing development database. Neither the frontend
nor verification endpoint bypasses reader permissions or creates/imports data.

The configuration indicator reports whether credentials are present, not whether
connections work. Never enter API keys or database passwords into the frontend.
Without OpenAI credentials, supported natural-language investigations return
`openai_not_configured`. The direct SQL-tool runner works without OpenAI when
the database reader is configured. Pure unsupported crime questions require
neither service and should return an explicit unsupported answer.

## What you can inspect

1. **Investigation form:** enter a question, one or two coordinate pins, radius,
   offset-aware timestamps, calendar timezone and optional requested domains.
   Question presets cover collision counts, A/B comparisons and unsupported crime.
2. **Answer:** view the verified answer, numerical claims, comparison differences,
   uncertainty and machine-readable diagnostic codes. Evidence buttons open the
   corresponding record.
3. **Tool calls:** inspect the validated plan, executed function names and arguments,
   query IDs, statuses, timings, parameterized SQL and actual result rows. Retrieval
   logs omit raw descriptions/embedded JSON; street fields remain untrusted data.
4. **Evidence:** inspect metric definitions, exact filters/scope, provenance,
   unknown metadata, calendar buckets, source report examples and limitations.
5. **SQL verification:** replay a selected executed call without the LLM, inspect
   its new SQL rows/evidence, and compare its value with the earlier evidence.
6. **Raw JSON / download:** save the full investigation for review or test records.

Logs appear **after completion**, not as live streaming events. They show actual
tool calls, not private chain-of-thought. The answer remains LLM-planned and
composed from deterministically verified templates; it is not unconstrained
LLM prose. No data or credentials are stored by the browser between reloads.

## Run SQL tools independently

Expand **Run a SQL tool without the LLM** on the left. Select a function and
location, adjust scope and filters, then execute. Supported tools:

- `count_service_requests`: one report-ID count; optional linked-duplicate exclusion.
- `aggregate_traffic_collisions`: calendar rows and an evidence total derived
  from their counts. Use this for collision counts/trends.
- `get_traffic_collisions`: a bounded page of source reports, never a total count.

Optional collision-filter JSON, for example:

```json
{"min_injured": 1, "min_killed": 0, "hit_run": true}
```

`NULL` injury/fatality counts cannot satisfy numeric filters; they are not zero.
The manual runner uses the shared form's interval and selected pin. For an exact
check of an agent result, use **Replay without LLM** on its tool-call card instead;
replay sends the original arguments, even if you have since changed the form.

For external verification, use **Copy SQL**, then paste into psql or pgAdmin
connected to the same database with your reader login. The exported script
contains the function arguments as safely escaped literals and runs inside a
read-only transaction which ends in rollback. The API always uses bound
parameters and exposes no arbitrary SQL execution endpoint.

Replays query **current data**, not an archived snapshot. A mismatch may result
from new imports or geocoding updates between executions. For collision totals,
sum `collision_count` from all returned aggregation rows; do not count the
retrieval page size. Missing calendar rows mean no returned matches, not verified
coverage. The evidence view distinguishes SQL buckets from filled buckets.

## Suggested assessment checklist

| Try | Check |
| --- | --- |
| Count collisions around A | Agent calls aggregation; the claim value equals the sum of SQL bucket counts. |
| Compare A and B across both domains | Four calls, with identical interval/radius/domain filters and duplicate policy; B-minus-A differences are correct. |
| Swap A and B | Counts follow coordinates and comparison signs reverse. |
| Request individual collision examples | Bounded retrieval is used and page size is labeled as a page, not a total. |
| Ask for crime rates or a safety ranking | Explicit unsupported result, with no collision-count substitution or invented score. |
| Run an empty-radius result | Zero is described as imported matches, with missing-geocoding/coverage caveats. |
| Enter invalid coordinates, dates or limits | Deterministic validation; no LLM needed to reject invalid requests. |
| Inspect an injury-filtered query | Unknown counts remain NULL and are excluded by numeric bounds. |
| Remove a required service configuration | Explicit diagnostic; failed/unavailable evidence is not replaced by zero. |

Do not assess the system solely by how plausible its prose sounds. Check tool
selection, scope symmetry, arithmetic, evidence references, limitations and
whether it abstains when data cannot support the request. Unlocated collisions
cannot appear in spatial searches; a database with no completed collision
geocoding can legitimately yield zero spatial matches while containing reports.

## Verification and boundaries

The interface is a local development tool: bind to loopback. Authentication and
public deployment are outside this feature. Source text is rendered as text,
not HTML. The independent endpoint accepts only typed calls to the three
allowlisted functions and enforces the configured row budget.

```powershell
$env:AGENT_TEST_DOCKER = '1'
.\.venv\Scripts\python.exe -m pytest backend/agent_tests -q
```

The evaluation update passed **47 automated tests**, including five isolated
PostGIS tests, frontend-serving tests, SQL replay/escaping tests and row-budget
validation. JavaScript syntax and actual HTTP responses were checked. Browser
interaction/visual automation was unavailable, so that QA was not run. Live
OpenAI evaluation remains dependent on your configured key/model.
