# CityLens backend audit — 2026-10-29

Scope: `backend/main.py`, `backend/agent/`, `backend/db.py`, `backend/sql/`,
the existing tests, and the frontend-to-API calls. The audit compares them with
`AGENT_SPECIFICATION.md`, `backend/AGENT.md`, `backend/EVALUATION.md` and
`backend/README.md`. **No implementation file, schema, SQL function, `.env`,
development data or existing container was changed.** The only file written is
this report.

Labels used below:

- **Confirmed:** reproduced, or read directly from the dev database or its log.
- **Probable:** consistent with all the evidence, but not proven without a paid
  OpenAI call.
- **Recommendation:** a design improvement, not a defect.

---

## A. Executive summary

The deterministic core is sound:

- **SQL functions:** filtering, aggregation, pagination and time windows match
  an independent reference on the full raw data (the earlier
  `verification/` suite, re-run today).
- **Contracts:** the typed request and plan contracts work, and the tool
  allowlist and parameter binding are correct.
- **Grounding:** claim construction, verification and template rendering are
  correctly grounded in tool output.

**The system cannot currently answer a question end to end.** There are two
blocking problems and one hidden one:

1. **Failure B is a configuration problem (confirmed).** `AGENT_DATABASE_URL`
   logs in as `citylens_agent`. That role does not exist in the development
   database, and neither does the reader group `citylens_agent_reader`. Every
   connection is rejected (the PostgreSQL log shows `password authentication
   failed … Role "citylens_agent" does not exist` five times between 07:24 and
   07:32 UTC today). No SQL ever ran. `tools.py` reports this as the generic
   `database_tool_failed`.
2. **Failure A is hidden by a catch-all exception handler (confirmed). Its
   provider-side cause is unconfirmed.** `Agent.build_plan` turns *every*
   exception into `planning_failed`. That includes OpenAI 400, 401, 404 and 429
   errors, SDK timeouts, refusals, truncated output and plan-validation
   rejections. It also labels the response `intent=unsupported_dimension`. I
   reproduced the exact reported response from seven different causes, so the
   response alone cannot identify the cause. Nothing is logged. The most likely
   candidates are listed in §B.1. One approved live call will settle it.
3. **Hidden problem: no collision has a location (confirmed).** All 78,494
   collisions in the dev database have `geocode_status='pending'` and
   `location IS NULL`. Once Failures A and B are fixed, every collision radius
   query will correctly return **0**. The Failure B call returns 0 rows. The
   same window without a radius returns 604 (Jan) and 570 (Feb) collisions.
   The agent will report "0 geocoded reports" with generic caveats, and nothing
   tells the user that the cause is missing geocoding. Get It Done is fine:
   303,225 rows, all located; 64 requests near A in the same window.

Two more confirmed design flaws explain why the 47 passing tests didn't catch
this:

- **The OpenAI test uses a mocked transport that accepts any schema.**
- **The PostGIS integration fixture creates the reader role itself.** No test
  covers a real provider or a database without the role.

---

## B. Root cause analysis

### B.1 Failure A — planning fails for a supported question

**Reported:** `status=error`, `intent=unsupported_dimension`,
`limitation_codes=["planning_failed"]`, empty `unsupported_dimensions`,
`execution_plan` and `tool_calls`.

**Evidence:**

| Check | Result |
| --- | --- |
| Request validity | The frontend (`frontend/app.js` `request()`) sends a request that passes `QueryRequest`. The domain checkboxes are unchecked by default, so `preferences=[]`. The scope guard finds nothing unsupported, so the graph reaches `build_plan`. |
| Code path | `backend/agent/workflow.py` `Agent.build_plan`, lines 167–179. `self.model.plan()` → `Plan.model_validate` → `validated_calls`. Any exception other than `ModelNotConfigured` or the built-in `TimeoutError` → `{'codes': [..., 'planning_failed'], 'fatal': True}`. |
| Intent label | `Agent.abstain` (line 288) uses `'unsupported_dimension'` whenever `state['plan']` is missing. That's why a planning crash *looks* like an unsupported question. **Confirmed defect** (misleading label). |
| Logging | No `logging` call anywhere in `main.py` or `agent/`. The exception is discarded. **Confirmed.** |
| Offline replay (real OpenAI SDK 2.54.0 plus the real LangGraph graph, mocked HTTP, no network) | Each of these produced the **exact** reported response: HTTP 400 (invalid schema), 404 (model not found or no access), 401 (bad key), 429 (quota), `status=incomplete` with `max_output_tokens`, a refusal, and an SDK read timeout. A valid plan produced `status=partial`. Script: `scratchpad/diag_planner.py` (see §D). |
| SDK timeout classification | The SDK raises `openai.APITimeoutError`, which is not a `TimeoutError`. It is reported as `planning_failed`, not `planning_timeout`. The SDK timeout and the `asyncio.wait_for` timeout are both `llm_timeout_seconds` (25 s), so which one fires first is a race. **Confirmed.** |
| Schema actually sent (`type_to_text_format_param(Plan)`) | `strict: true`. Every property is in `required`. It still contains `"default": "month"` (`period`), `"default": false` (`exclude_linked_duplicates`), `maxLength: 500` (`filter_quote`), and `minLength`/`maxLength` (`street_name`). The SDK only strips `default: null`. |
| Deterministic plan validation | Plausible correct plans pass `validated_calls`: `location_summary` or `collision_trend` with `aggregate_traffic_collisions`, with or without retrieval or a harmless `filter_quote`. But some plausible model choices are **rejected**: `intent=request_count` ("How many…") with the collision tool, `collision_drilldown` without retrieval, or a non-null filter like `min_injured=0` without a quote. |
| Prompt | `PLANNER_PROMPT` (`agent/llm.py`) names the tools but **never defines the six intents**. No intent means "count collisions". The model sees only enum names, and the natural match for "How many traffic collisions…" is `request_count`, which validation rejects ("Request counts require the service counter"). **Confirmed** that the prompt omits the definitions. Whether the live model actually chose `request_count` is **unknown**. |

**Root cause:**

- **Confirmed:** the failure can't be diagnosed. `build_plan` has a catch-all
  handler, nothing is logged, and the fallback intent is misleading.
- **Unconfirmed (it can't be determined from the saved response):** which
  exception actually occurred.

Ranked by likelihood given the evidence:

1. **The OpenAI request was rejected (400 or 404).** Either the configured
   `OPENAI_MODEL` doesn't support Responses structured outputs or isn't enabled
   for the key, or strict mode rejected `default`, `minLength` or `maxLength` in
   the schema. OpenAI's strict mode supports only a subset of JSON Schema
   keywords. I did **not** verify against the live API whether this
   account/model accepts these keywords.
2. **The model produced a schema-valid plan that `validated_calls` rejects**,
   most likely `intent=request_count`, because intents are undefined in the
   prompt.
3. **A reasoning model used up `max_output_tokens=2500`** (`status=incomplete`,
   `output_parsed=None`).
4. **Key, quota or network failure.** The status endpoint shows only that
   credentials are *present* (`main.py` `configuration()`).

Independently of A, the same request would then fail at the database because of
Failure B. Fixing A alone would give `status=error` with `database_tool_failed`.

**Recommended fix (smallest first):**

1. **Log and classify.** In `build_plan`, log the exception class, the HTTP
   status, OpenAI's error `message`/`param`, `incomplete_details.reason`, and
   the `validated_calls` reason. Never log the key. Return specific codes:
   `planning_provider_error:<status>`, `planning_timeout` (also for
   `openai.APITimeoutError`), `planning_no_structured_output`,
   `plan_rejected:<reason>`.
2. **Fix the failure label.** Don't use `intent='unsupported_dimension'` for
   fatal failures. Use the plan's intent when there is one. Otherwise add a
   neutral value (for example `undetermined`) to `Intent`, which is an API
   contract change, or keep `location_summary` as `main.py` already does for
   timeouts.
3. **Separate the LLM-facing schema from the internal one.** Use an LLM-facing
   `PlanOutput` model with no non-null defaults and no string-length
   constraints. Enforce lengths and defaults when converting it to `Plan`.
4. **Define each intent in `PLANNER_PROMPT`**, in one line each. State that
   counting collisions is `location_summary`, or add a `collision_count` intent.
5. **Reasoning models:** if `OPENAI_MODEL` is a reasoning model, set a low
   reasoning effort and/or raise `max_output_tokens` (the setting already allows
   up to 8,000).

### B.2 Failure B — `/api/v1/tools/run` returns `database_tool_failed`

**Evidence (all read-only on the dev database, or on the isolated test DB):**

| Check | Result |
| --- | --- |
| Configured target | The PostgreSQL log on `sandiego_citylens-db-1` shows `FATAL: password authentication failed for user "citylens_agent"` / `DETAIL: Role "citylens_agent" does not exist` at 07:24:28, 07:25:09, 07:25:49, 07:31:20 and 07:32:44 UTC on 2026-10-29. No other errors. **Confirmed:** the server targets the dev DB (port 54329) as `citylens_agent`. |
| Roles in the dev DB | Only `citylens` exists (superuser). There's no `citylens_agent` login and no `citylens_agent_reader` group. `sql/setup_agent_reader.sql` was never applied, as `AGENT.md` says. **Confirmed.** |
| Function existence and signatures | `aggregate_traffic_collisions`, `get_traffic_collisions`, `filter_traffic_collisions` and `count_service_requests` all exist with the signatures `tools.py` binds, in the same 14-argument order. **Confirmed.** |
| Function execution | The exact Failure B call, run as `citylens` in a `REPEATABLE READ READ ONLY` transaction, **succeeds and returns 0 rows**. **Confirmed:** the binding and the SQL are correct. |
| Data | 78,494 collisions with `occurred_at` set and **0 located** (all geocoding pending). Without the radius the same window has 604 + 570 collisions. Get It Done has 303,225 rows, all located; `count_service_requests` near A = 64. **Confirmed.** |
| Error mapping | `DatabaseTools.execute` (`agent/tools.py` lines 172–193) catches every `psycopg.Error`, including connection failures, as `database_tool_failed`. |
| Reproduction through the real endpoint (isolated DB on port 54330, `FastAPI TestClient`, real `DatabaseTools`) | **Case 1, login role missing:** HTTP 200, `status=error`, `database_tool_failed`, `result_rows=[]`, `evidence.value=null`. The underlying error is `psycopg.OperationalError` (password authentication failed). **Case 2, superuser login with no reader group:** the same response. The underlying error is `psycopg.errors.UndefinedObject: role "citylens_agent_reader" does not exist`, raised by `pg_has_role` in `ensure_reader`. **Confirmed:** both match the report exactly. |

**Root cause (confirmed):**

- **The login role is missing.** The database login named in
  `AGENT_DATABASE_URL` does not exist, so the connection fails before any
  query.
- **The fallback won't work either.** Using the existing superuser instead
  would fail too, because the reader group doesn't exist (case 2). Once the
  group exists, the superuser would get `reader_role_required` by design.
- **Secondary defect (confirmed): wrong error codes.** Neither case reports
  `reader_role_required` or a connection-specific code. `ensure_reader` assumes
  the group role exists, and the `except psycopg.Error` branch merges
  connection, authentication, missing-role, permission and SQL errors into one
  code.
- **Remaining uncertainty:** none for the connection failure. Whether the
  configured password would work after the role is created depends on the
  value you set.

**Recommended fix:**

1. **Configuration (no code change; a DBA action I did not perform).** As the
   `citylens` admin, review and run `backend/sql/setup_agent_reader.sql`. Then
   `CREATE ROLE citylens_agent LOGIN …; \password citylens_agent; GRANT
   citylens_agent_reader TO citylens_agent;`, exactly as in `AGENT.md`. Make
   sure `AGENT_DATABASE_URL` uses that password, URL-encoded. Restart uvicorn:
   `get_agent` is `lru_cache`d and reads the environment only once.
2. **Code.** Split the exception mapping in `DatabaseTools.execute`:
   - `OperationalError` during connect → `database_connection_failed`
   - `InsufficientPrivilege` → `database_permission_denied`
   - `UndefinedFunction` → `database_function_missing`

   Also make `ensure_reader` check `to_regrole('citylens_agent_reader')` first
   and raise `PermissionError` (`reader_role_required`) if it's NULL. Log the
   SQLSTATE and the exception class, never the URL.
3. **Data readiness (separate from B).** Run collision geocoding
   (`collision_geocoding.py`, which uses an external service) before expecting
   non-zero collision radius counts. Until then, add an explicit limitation,
   for example `collision_geocoding_incomplete`, when
   `dataset_located_records == 0` or is very low. The `Quality` diagnostics
   already carry this number, but nothing acts on it.

---

## C. Architecture assessment

The architecture described in `AGENT.md` matches the code: request validation →
scope guard → LLM plan → deterministic plan validation → allowlisted
parameterized SQL → evidence → Python claims → verification → constrained
composition → template rendering → final validation. One planner, no free
prose, no LLM-generated SQL.

| Component | Assessment |
| --- | --- |
| SQL tools (`sql/tools/*.sql`) | **Correct.** Results match the independent reference on the full data (verification suite, 247/247 on 2026-10-28; 229 passed and 18 opt-in skipped today). Known ingestion and docs findings D1–D7 in `verification/AMBIGUITIES.md` still apply. |
| `QueryRequest` / `ToolCall` validation (`models.py`, `tools.py`) | **Correct.** Offset-aware times, increasing interval, San Diego envelope, radius 0–50,000 m, row budget. 11 validation cases are tested. |
| Tool allowlist and binding (`tools.py` `query_spec`) | **Correct.** Fixed SQL text, bound parameters, argument order matches the installed signatures, and the uncapped `filter_traffic_collisions` isn't exposed. |
| Read-only transaction and timeouts | **Correct.** `REPEATABLE READ READ ONLY`, `SET LOCAL statement_timeout=10s`, `lock_timeout=2s`, `connect_timeout=3`. |
| Reader-role enforcement (`ensure_reader`) | **Intent is correct; it fails in the wrong way.** It fails closed, but with a generic code when the group is missing (§B.2). The reader grant on `filter_traffic_collisions` is needed for invoker-rights composition. PostgreSQL's default `PUBLIC EXECUTE` already applies (`proacl` is NULL on all four functions). |
| Evidence and claims (`evidence.py`) | **Correct by design.** Values come only from executed results, bucket filling marks missing SQL rows, comparisons require identical scope, and claims are recomputed during verification. Unit-tested with fakes and on PostGIS. |
| Planner (`llm.py`) | **Incomplete.** No intent definitions. The LLM-facing schema keeps `default` and length keywords under `strict`. No reasoning-effort control. No diagnostics. |
| Graph error handling (`workflow.py`) | **Incorrect classification.** Catch-all handlers in `build_plan` and `execute_tools`, a misleading `unsupported_dimension` intent on fatal errors, `APITimeoutError` reported as `planning_failed`. The structure (abstain path, keeping partial evidence) is sound. |
| Observability | **Missing.** No logging in `main.py` or `agent/`. The status endpoint reports only that settings are present. There's no connectivity or role self-check, so both failures look identical from outside. |
| Data readiness | **Not ready for collision spatial queries.** 0 of 78,494 collisions have a location. |
| Frontend integration | **Consistent with the API.** Field names and the request shape match `QueryRequest` and `ToolCall`. `frontend/app.js` and `index.html` have uncommitted changes (not reviewed as part of this audit). The Node test couldn't run (§D). |
| Security | API keys and URLs come only from the environment, are never echoed, and the provider is called with `store=False`. Untrusted record fields aren't sent to the LLM. No auth on the endpoints, which is documented as local-only. `/api/v1/tools/run` has no rate limit: acceptable on loopback, not for deployment. |
| Docs | `README.md` line 97 still runs the deleted `backend/test_database.py`. `backend/Database.py` is a notes module with no code. `test_collisions.py` uses bare imports, so it only runs from inside `backend/`. |

---

## D. Test results

All of these ran today. None used the network or a paid API.

| Command | Result | Notes |
| --- | --- | --- |
| `pytest backend/agent_tests -q` (no Docker) | **42 passed, 5 skipped** | The 5 PostGIS integration tests skip without a database. |
| `AGENT_TEST_DOCKER=1 pytest backend/agent_tests -q` | **47 passed** | The fixture starts **and removes** its own in-memory container on port 54331. I confirmed no container was left over. |
| `cd backend; pytest test_collisions.py -q` (`DATABASE_URL` unset) | **4 passed, 3 skipped** | |
| `cd backend; DATABASE_URL=<isolated 54330 citylens_test> pytest test_collisions.py -q` | **7 passed** | Its DB tests **write**, so they ran only against the throwaway verify container. **Never** run this with the dev `DATABASE_URL`. |
| `cd backend/verification; pytest -q` (isolated DB) | **229 passed, 18 skipped** | The 18 skipped are the opt-in `--run-realdata` tests. They passed (247/247) on 2026-10-28. |
| `scratchpad/diag_planner.py` (offline SDK and graph replay) | 7 of 7 failure modes reproduce the Failure A response; a valid plan → `partial` | Diagnostic script, not a test. |
| `scratchpad/diag_tools.py` (isolated DB, real endpoint) | Both role scenarios reproduce the Failure B response | Diagnostic script, not a test. |
| Read-only `psql` in the dev container (`default_transaction_read_only=on`, transactions rolled back) | Catalog, data counts and the Failure B SQL (0 rows) | No writes. Socket auth inside the container, so no credentials were read. |

Scratchpad path:
`C:\Users\JIAQIW~1\AppData\Local\Temp\claude\e--Github-Projects-SanDiego-CityLens\42b7ddd0-fef7-403c-a7da-d37fd8e0391b\scratchpad\`.

**Not run, and why:**

| Test | Why |
| --- | --- |
| `node frontend/tests/startup.test.cjs` | Node.js is not installed in this environment. |
| Live OpenAI planning | Paid call; needs your approval. This is the only way to confirm the cause of Failure A. |
| Agent against the dev DB | Needs the reader role, which requires a DBA write I was told not to make. |
| `collision_geocoding.py` | External service. Its output is also a write to dev data. |

**Coverage gaps that let both failures through:**

- **Fake provider.** `test_openai_adapter.py` returns a canned success for
  any schema, so it can't detect a provider rejecting the schema, the model
  or the key.
- **Role always present.** `test_integration.py` always creates
  `citylens_agent_reader`, so it never sees a database without it (the dev
  state).
- **No error-code tests.** Nothing checks error codes for connection or
  authentication failures, or for `openai.APIStatusError` and
  `APITimeoutError`.
- **No live smoke test.** There's no opt-in live-model test.

---

## E. Prioritized fix plan

Smallest and highest-impact first.

| # | Severity | Fix | Type | Resolves |
| --- | --- | --- | --- | --- |
| 1 | Blocker | Create `citylens_agent_reader` and the `citylens_agent` login on the dev DB (reviewed `setup_agent_reader.sql` plus `CREATE ROLE`, `\password`, `GRANT`), then restart uvicorn | DBA config; you run it | B |
| 2 | Blocker (diagnosis) | Approve **one** live planner call through a small script that prints only the exception class, HTTP status and OpenAI's error message, or the parsed plan | Diagnostic | Identifies A |
| 3 | High | Log and classify planner errors in `build_plan`, including `APITimeoutError` → `planning_timeout`. Return the `validated_calls` rejection reason as a code | ~20 lines, `workflow.py` | A observability |
| 4 | High | Split the `psycopg` error mapping in `DatabaseTools.execute`. Pre-check `to_regrole` in `ensure_reader` | ~15 lines, `tools.py` | B observability |
| 5 | High | Fix the specific cause found in step 2. If it's the schema: an LLM-facing `PlanOutput` without defaults or length keywords. If it's intent choice: define intents in the prompt. If it's the model or token budget: config | Small, `llm.py`/`models.py` | A |
| 6 | Medium | Stop labeling fatal failures `unsupported_dimension` | Small, `workflow.py` (± `Intent` enum) | A misreporting |
| 7 | Medium | Add a geocoding-coverage limitation code when `dataset_located_records` is 0 or low. Run collision geocoding | Code plus a data operation | Silent zeros |
| 8 | Medium | Optional `GET /api/v1/status?probe=db` that runs `SELECT 1` plus `ensure_reader` and reports a code, never the URL | Small, `main.py` | Faster setup diagnosis |
| 9 | Medium | Tests: provider 400/401/404/429/incomplete/timeout → specific codes; no reader group / no login → specific codes; opt-in live-model smoke test (`OPENAI_LIVE_TEST=1`) | Tests | Regression |
| 10 | Low | Docs: remove the `test_database.py` reference; note that `test_collisions.py` must run from `backend/` and must not use the dev `DATABASE_URL`; mark or remove `Database.py` | Docs | Drift |

---

## F. Verification plan

### Failure B is fixed when:

1. **Login works.** Running psql as `citylens_agent` shows
   `pg_has_role(current_user,'citylens_agent_reader','USAGE') = true`, and
   `INSERT INTO citylens.ingestions DEFAULT VALUES` is rejected with
   `permission denied`. Run this inside `BEGIN … ROLLBACK`.
2. **No more authentication failures.** The PostgreSQL log shows no new
   `FATAL … citylens_agent` lines after the uvicorn restart.
3. **The original call succeeds.** Re-POSTing the original
   `/api/v1/tools/run` body returns HTTP 200 with
   `tool_call.status="ok"` and `error_code=null`. While geocoding is pending,
   `result_rows=[]` and `evidence.value=0`, which matches the read-only psql
   result above. After geocoding, `evidence.value` must equal
   `SUM(collision_count)` from the same SQL in psql.
4. **The control query agrees.** The `count_service_requests` tool for the same
   pin and window returns **64**, as measured read-only today, unless data
   changes.
5. **Regression tests (after fix #4).** A missing login gives
   `database_connection_failed`. A superuser login with no group gives
   `reader_role_required`.

### Failure A is fixed when:

1. **The live diagnostic shows a valid plan.** One approved call (fix #2)
   prints a parsed `Plan` with an intent in {`location_summary`,
   `collision_trend`} and `tools` containing `aggregate_traffic_collisions`,
   and `validated_calls` accepts it.
2. **The original query succeeds.** The original question returns
   `status="partial"`, an intent other than `unsupported_dimension`,
   `execution_plan` with one `aggregate_traffic_collisions` call (A, 800 m,
   the given interval, `period=month`), one `tool_calls` entry with
   `status="ok"`, and one `record_count` claim. The claim's value equals the
   sum of `collision_count` over `result_rows` (currently 0, with a
   geocoding-coverage caveat after fix #7).
3. **Consistent across runs.** Repeat 5 times. Plan acceptance should be 5/5;
   record any rejection reason, which is now logged.
4. **Offline regression tests.** Mocked-transport tests (fix #9) assert that
   each provider failure gives its specific code, so `planning_failed` with
   no detail can't come back unnoticed.
5. **Re-run** `AGENT_TEST_DOCKER=1 pytest backend/agent_tests` and the
   verification suite. Both should stay green.
