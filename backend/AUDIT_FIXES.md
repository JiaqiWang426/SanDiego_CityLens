# Backend audit integration fixes — 2026-10-29

This change addresses the confirmed integration/diagnostic defects in
[BACKEND_AUDIT.md](BACKEND_AUDIT.md), without changing SQL tools, tables, ingestion,
geocoding, claim verification or the LangGraph node/edge architecture.

**No database commands, role setup, migrations, geocoding or live OpenAI calls
were executed. `.env` was not modified.** Tests requiring DB writes were skipped.

## Exact permissions in setup_agent_reader.sql

The script remains unchanged and was inspected, not executed.

| Statement | Effect |
| --- | --- |
| Conditional `CREATE ROLE citylens_agent_reader` | Creates a group role only if absent: NOLOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS. Existing roles are not hardened or reset. |
| `GRANT USAGE ON SCHEMA citylens` | Allows resolving/accessing objects for which the role separately has privileges. Does not grant schema CREATE. |
| `GRANT SELECT` on `service_requests`, `ingestions`, `traffic_collisions`, `collision_imports` | Allows reading all columns in these four tables, including raw-record fields present there. It does not grant INSERT/UPDATE/DELETE/TRUNCATE, table ownership or sequence rights. |
| `GRANT EXECUTE` on the exact signatures of `count_service_requests`, `filter_traffic_collisions`, `get_traffic_collisions`, `aggregate_traffic_collisions` | Allows executing these four existing functions. The uncapped helper is required by caller-rights composition; it remains absent from the agent tool allowlist. These functions do not elevate the caller's privileges. |

It does **not** create the `citylens_agent` login, set a password, grant group
membership to a login, grant explicit database CONNECT, grant access to
`collision_source_rows`, configure future/default privileges, revoke PUBLIC
privileges, remove old memberships, or enforce a role-wide read-only setting.
Effective access can be broader through PUBLIC, other memberships or ownership.
The existing runtime checks still reject administrative/write-capable accounts.

## Diagnostic behavior

Database errors retain status/error handling and NULL failed evidence values:

| Code | Meaning |
| --- | --- |
| `database_not_configured` | No AGENT_DATABASE_URL. |
| `database_connection_failed` | Connection establishment failed, e.g. refused port/DNS/unreachable server. |
| `database_authentication_failed` | SQLSTATE class 28 or recognizable libpq startup authentication rejection. Missing login and wrong password can be indistinguishable to the client; this code does not guess which occurred. |
| `reader_role_missing` | `to_regrole` confirmed the reader group is absent, before `pg_has_role` is called. |
| `reader_role_required` | Reader membership/effective restrictions failed (including admin/write-capable accounts). |
| `database_permission_denied` | PostgreSQL denied an operation with InsufficientPrivilege. |
| `database_function_missing` | The requested installed SQL function/signature is absent. |
| `database_execution_failed` | Other PostgreSQL statement errors. |
| `database_connection_lost` | Connection failed after establishment. |
| `tool_timeout` / `database_lock_timeout` | Statement cancellation / lock timeout. |

Logs record code, stage, query/trace identifier, exception type and SQLSTATE
where available. Database messages, URLs, SQL values and passwords are not logged.
Some libpq startup failures have no SQLSTATE; message markers are inspected only
in memory for classification, never copied into diagnostics.

Planning now separates provider calls from internal plan validation:

- `planning_provider_error:<HTTP status>` for HTTP errors (400/401/403/404/429/500, etc.).
- `planning_provider_connection_failed` for SDK connection failures.
- `planning_timeout` for both SDK APITimeoutError and the outer asyncio timeout.
- `planning_output_refused`, `planning_output_incomplete:<reason>`,
  `planning_no_structured_output`, `planning_response_not_completed`, or
  `planning_structured_output_invalid` for distinguishable response/parsing cases.
- `plan_rejected:<fixed reason>` for internal validation, e.g.
  `request_count_requires_service_counter`, `filter_quote_required`,
  `tool_budget_exceeded`, or `invalid_schema`.
- `planning_internal_error` is reserved for unexpected internal exceptions.
- `openai_not_configured` still identifies missing credentials/model.

If the SDK cannot parse truncated JSON before returning a response, only the
parsing error is observable and it is labeled structured-output-invalid; the
application does not claim to know an unseen completion status.

Composition failures use corresponding `composition_*` codes while retaining
`composition_fallback` and verified template claims. No new unchecked prose is
accepted. Provider logs include HTTP status, exception class and bounded,
credential-scrubbed message/param/code/type fields, not entire request/response
bodies, raw model output, or tracebacks. Plan-rejection reasons are allowlisted.

Fatal failures with no valid parsed plan now use **`intent="undetermined"`**.
This is an additive API response enum value. A valid inferred intent survives
subsequent plan rejection. `unsupported_dimension` is reserved for unavailable
user capabilities, rather than infrastructure failure.

When metadata shows a nonempty collision dataset with zero located reports,
responses add `collision_geocoding_incomplete` and an explicit limitation. This
does not alter counts, infer local missing-report numbers, or run geocoding.

## Schema and prompt changes

`PlanOutput` and `CollisionFiltersOutput` are conservative, required-field
provider-facing schemas, without defaults, string-length keywords or numeric
bounds. The original internal `Plan`/`CollisionFilters` retain all limits and
are validated after parsing. Tests inspect the actual schema passed to the SDK.

Planner prompt version 2 defines all six intents. Collision counts use
`location_summary`; `request_count` is explicitly only Get It Done IDs.
No extra LLM agent, SQL tool, retry loop, or model-specific reasoning setting
was introduced.

The [official OpenAI structured-output guidance](https://developers.openai.com/api/docs/guides/structured-outputs)
describes schema restrictions and refusal/incomplete paths. Local schema checks
and mocked HTTP do **not** prove acceptance by the configured live model or
identify the original provider-side failure. **That root cause remains unconfirmed.**

## Tests actually executed

DB-triggering variables (`DATABASE_URL`, `TEST_DATABASE_URL`,
`TEST_AGENT_DATABASE_URL`, `AGENT_TEST_DOCKER`) and any live-test flag were removed
from the test subprocess environment only. No persistent environment file was
changed. Results:

| Suite | Exact command | Result |
| --- | --- | --- |
| Agent | `python -m pytest backend/agent_tests -q` | **76 passed, 5 skipped** |
| Existing collision tests (from backend/) | `python -m pytest test_collisions.py -q` | **4 passed, 3 skipped** |
| Existing verification suite (from backend/verification/) | `python -m pytest -q` | **74 passed, 173 skipped** |
| Python syntax | `python -m compileall -q backend/agent backend/scripts backend/main.py` | Passed |
| Installed dependencies | `python -m pip check` | No broken requirements |

Total: **154 passed, 181 skipped**. DB fixtures were not run because they create
roles/schema or write records; real-data tests are also opt-in. The audit's prior
DB results were not counted as results of this run. No live provider request was
made. Real SDK parsing/error behavior was exercised using an in-memory HTTP
transport, with one mocked request and zero retries per failure scenario.

Regression coverage includes HTTP provider statuses, SDK/outer timeouts,
connection errors, refusal, incomplete/missing/malformed output, wire-schema
constraints, rejected intents/plans, retained valid intent, neutral failed intent,
DB connection/auth/role/permission/function/statement errors, log redaction,
composition fallback, geocoding readiness, and smoke-test approval gating.

## Manual role setup — only after your approval

Review `backend/sql/setup_agent_reader.sql` and the permission table above.
These commands change DB roles/grants and are **instructions, not executed work**.
They assume the existing initialized database and Docker Compose service `db`.

From the repository root, after approval:

```powershell
Get-Content -Raw backend/sql/setup_agent_reader.sql | docker compose exec -T db psql -X -U citylens -d citylens -v ON_ERROR_STOP=1
docker compose exec db psql -X -U citylens -d citylens
```

In the interactive administrator psql session, if the login does not yet exist:

```sql
CREATE ROLE citylens_agent LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\password citylens_agent
GRANT citylens_agent_reader TO citylens_agent;
\q
```

If it already exists, inspect it and its memberships first rather than blindly
resetting privileges. `\password` prompts without putting a password in SQL
history. Then verify using the login (TCP/password auth inside the container):

```powershell
docker compose exec db psql -X -h 127.0.0.1 -U citylens_agent -d citylens -W
```

Use catalog checks only, without test writes:

```sql
BEGIN READ ONLY;
SELECT current_user, pg_has_role(current_user,'citylens_agent_reader','USAGE');
SELECT name,
       has_table_privilege(current_user,name,'SELECT') AS can_select,
       has_table_privilege(current_user,name,'INSERT,UPDATE,DELETE,TRUNCATE') AS can_write
FROM (VALUES ('citylens.service_requests'),('citylens.ingestions'),
             ('citylens.traffic_collisions'),('citylens.collision_imports')) AS t(name);
SELECT has_schema_privilege(current_user,'citylens','CREATE') AS can_create_in_schema,
       has_database_privilege(current_user,current_database(),'CREATE') AS can_create_schema;
ROLLBACK;
```

Expect membership/SELECT true, writes and CREATE false. This reader script grants
no new database CONNECT privilege; if the cluster's defaults have been restricted,
the administrator must explicitly review that separately.

Set the login password in your current PowerShell session without printing it:

```powershell
$secureDb = Read-Host 'citylens_agent password' -AsSecureString
$dbPassword = [System.Net.NetworkCredential]::new('', $secureDb).Password
$env:AGENT_DATABASE_URL = "postgresql://citylens_agent:$([uri]::EscapeDataString($dbPassword))@127.0.0.1:54329/citylens"
Remove-Variable secureDb, dbPassword
```

Restart uvicorn from that same shell to discard its cached settings. No `.env`
edit is needed. Role setup does not populate collision coordinates; the audited
all-pending collision dataset will still return zero geocoded spatial matches.

## One controlled live-model smoke test — separate paid-call approval

Review `backend/scripts/smoke_plan.py`. It uses the fixed collision-count example,
your existing `OPENAI_API_KEY` / `OPENAI_MODEL`, and configured token/time limits.
Only **one planner request** is attempted (`max_retries=0`), without composition,
database connections, tools, migrations, or geocoding. SDK HTTP errors still count
as the one attempted call. The command without its flag refuses to call the API.

After approving that one billable request and configuring your key/model:

```powershell
.\.venv\Scripts\python.exe -m backend.scripts.smoke_plan --approve-paid-call
```

Success prints `plan_accepted`, the validated plan and tool names, with zero DB
tools and composition calls. Failure prints a classified code/class/HTTP status;
sanitized provider diagnostics go to the terminal log. Exit codes: 0 accepted,
1 failed, 2 no approval. No credentials or request bodies are printed.

Do not loop/retry automatically. Review this single result before authorizing
another paid call or changing model/token settings. It can diagnose current
provider behavior, but cannot retrospectively prove the cause of a past failure.
