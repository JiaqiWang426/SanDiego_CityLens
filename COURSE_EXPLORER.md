# CityLens Explorer

Explorer now shares one FastAPI application and the existing PostgreSQL/PostGIS
source of truth with the CityLens Agent. The original map, filters, charts,
paginated records and details remain available. The Agent workspace is embedded
in the page and is also available at `/agent`.

See [the implementation and verification report](EXPLORER_AGENT_INTEGRATION.md)
for architecture, field mapping, radius/viewport semantics, API contracts,
verification limits and full Windows setup instructions.

## Start on Windows

Start the existing CityLens PostgreSQL container and configure the repository-root
`.env` using `.env.example`. `AGENT_DATABASE_URL` must be the existing restricted
reader; both the Explorer and Agent use it. OpenAI configuration is required for
natural-language analysis, and the separate session writer is required for saved
conversations. The map itself does not require an LLM call.

```powershell
cd 'E:\Github Projects\SanDiego_CityLens_QHY'
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m backend.scripts.serve --port 8000
```

Open <http://127.0.0.1:8000/>. The API documentation is at `/docs`.
`backend.explorer_app:app` remains an alias of this same application, but the
Windows launcher above supplies psycopg's required selector loop.

## Data and map scope

Service requests retain the source District 1 / LA-local 2026 scope. Collision
maps use imported 2026 coverage and do not infer District 1 membership. Global
filtered totals, viewport totals and rendered marker/bubble counts are separate.
The 700-point threshold and server grid aggregation are preserved.

SQLite is no longer read or built by the running application. The historical CSV
projection contained 31,667 distinct request IDs from 303,226 source rows, dated
January 1 through October 26, 2026. This is a reference snapshot, not a claim about
the current PostgreSQL database. Run the read-only comparison after connecting:

```powershell
.\.venv\Scripts\python.exe -m backend.scripts.audit_explorer
```

`Closed` does not establish completed repair. Parent links do not identify all
real-world duplicates. Coordinates and planning-area labels are not independently
verified. Missing coordinates remain in global totals and cannot enter map views.
Leaflet is vendored locally; OpenStreetMap tiles still require internet access.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-agent-test.txt
.\.venv\Scripts\python.exe -m pytest backend\explorer_tests backend\agent_tests -q
```

PostGIS integration tests are explicitly opt-in; see the report. Tests that
cannot run without Docker or external services are distinguished from passes.
