# Local data infrastructure

## Environment configuration

The backend automatically loads `.env` from the repository root using
`python-dotenv`. The path is resolved from `backend/config.py`, not the shell's
working directory. Values defined in `.env` take precedence over existing process
environment variables (`override=True`), including explicitly empty file values.
Process values remain available for keys absent from `.env`. A missing `.env` is OK.

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
# Edit .env locally; preserve any values already present.
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Configure `OPENAI_API_KEY`, `OPENAI_MODEL`, and the restricted-reader
`AGENT_DATABASE_URL` in `.env`. `CITYLENS_DB_PASSWORD` is the separate Docker
database administrator password; it is loaded into the environment but never
used as an agent credential fallback. For maintenance scripts, optionally set
`DATABASE_URL` separately; do not give the agent administrator credentials.
URL-encode passwords embedded in database URLs.

FastAPI loads the file during startup, before application settings are created.
`Settings.from_env()` also loads it for standalone agent commands (including the
explicitly gated smoke test). The `db.py`, collision ingestion and collision
geocoding CLI entry points use the same loader before reading their environment.
Importing the loader or a maintenance module alone does not load secrets or run
any operation. Loading configuration does not create roles, import data, run
geocoding or call OpenAI.

Restart FastAPI after editing `.env`, because application settings are cached.
The next startup reloads file values even if PowerShell still contains conflicting
variables; there is no need to clear those variables first. `.env`, `.env.*`, `.envrc`,
`.secrets/` and `secrets.local.*` are ignored by Git; `.env.example` remains
trackable. Never print or commit real credentials. Configuration loading is
silent and does not modify `.env`.

The runnable Phase 0 Agent API, setup instructions, examples, and test commands
are documented in [AGENT.md](AGENT.md).
To open the frontend, inspect tool logs/evidence, and independently execute SQL
tools, follow [EVALUATION.md](EVALUATION.md).

Traffic collision ingestion, geocoding, and query tools are documented in
[TRAFFIC_COLLISIONS.md](TRAFFIC_COLLISIONS.md).

## Database structure and query tools

```text
backend/
  sql/
    get_it_done.sql
    traffic_collisions.sql
    tools/
      count_service_requests.sql
      010_filter_traffic_collisions.sql
      020_get_traffic_collisions.sql
      030_aggregate_traffic_collisions.sql
```

The two files directly under `sql/` create storage for each subject:

| File | Subject | What it sets up |
| --- | --- | --- |
| [get_it_done.sql](sql/get_it_done.sql) | Get It Done service requests | Stores current request records and import history, and adds indexes to speed up location and date searches. Also enables PostGIS and creates the shared `citylens` namespace. |
| [traffic_collisions.sql](sql/traffic_collisions.sql) | Police-recorded traffic collisions | Stores current collision reports, an archive of every imported source row, import history, and approximate geocoding results. Adds indexes for location, date, and street searches. |

The files under `sql/tools/` define functions for answering questions:

| Tool file | Subject | When to use it | What it returns |
| --- | --- | --- | --- |
| [count_service_requests.sql](sql/tools/count_service_requests.sql) | Get It Done service requests | Ask “How many requests were submitted within 800 meters of this coordinate during January?” Optionally exclude submissions explicitly linked to a parent request. | One number: the count of matching request IDs. This is a count of reports, not necessarily distinct physical problems. |
| [010_filter_traffic_collisions.sql](sql/tools/010_filter_traffic_collisions.sql) | Traffic collisions: shared selection rules | Internal helper that selects reports by date, optional radius, injury/fatality counts, hit-and-run status or level, and primary or intersecting street. Both collision tools below reuse these rules. | All matching collision records, without a result limit. Intended as a building block rather than the agent-facing retrieval tool. |
| [020_get_traffic_collisions.sql](sql/tools/020_get_traffic_collisions.sql) | Traffic collisions: individual reports | Ask “Show me the collision reports matching these conditions.” Use it to inspect records, source fields, and geocoding information. | A page of reports, ordered by occurrence time and report ID. Returns up to 100 records by default, at most 1,000 per call; an offset lets callers request later pages. |
| [030_aggregate_traffic_collisions.sql](sql/tools/030_aggregate_traffic_collisions.sql) | Traffic collisions: trends over time | Ask “How many matching reports occurred each month?” Group by day, week, month, quarter, or year using a specified calendar timezone. | One summary row per period with matching reports: total reports, reports with coordinates, and reports with unknown injury/fatality counts. Counts all matches, not just one retrieval page. Empty periods are omitted. |

For collisions, `010` chooses **which reports match**, `020` returns **individual
reports to inspect**, and `030` returns **counts over time**. The numeric prefixes
ensure the shared filter is created before the functions that depend on it.
The date interval includes its start and excludes its end. Geographic queries
exclude records without coordinates; collision coordinates are approximate
because source addresses are anonymized to block level. A zero spatial count
does not establish that no collisions occurred.

`python backend/db.py init` executes `sql/get_it_done.sql`, then
`sql/traffic_collisions.sql`, then all `.sql`
files directly inside `sql/tools/` in alphabetical filename order, in one
transaction. Add each new query function in a separate tool file using
`CREATE OR REPLACE FUNCTION`. Initialization can be repeated without reloading
CSV data. Existing table changes still require explicit migrations; deleting
a tool file does not remove its installed function from the database.

## Raw data layout

Store source CSVs by data domain:

```text
backend/
  Raw Data/
    Get it done/
      get_it_done_requests_CD1_datasd.csv
    Traffic collision/
      pd_collisions_datasd.csv
```

Add separate folders under `Raw Data` for other domains as they are introduced.
`db.py load` interprets Get It Done columns only;
`collision_ingest.py` separately loads traffic collision records.
Both commands default to the CSV shown above when the positional CSV path is
omitted. Defaults resolve relative to the scripts, not the working directory.
To select another Get It Done CSV, pass its path in quotes because these
directory names contain spaces. Each loader invocation imports one file;
it does not automatically scan folders or merge every CSV.

## Local database setup

PostgreSQL 17 + PostGIS runs in Docker with persistent storage, exposed only on
localhost port 54329. Python imports the CSV; SQL performs spatial counting.

From the repository root in PowerShell:

```powershell
# A local .env has been generated on this workstation. On a fresh checkout:
# Copy-Item .env.example .env
# Edit .env to choose a local password.
docker compose up -d --wait
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
$dbPassword = (Get-Content .env | Where-Object { $_ -like 'CITYLENS_DB_PASSWORD=*' }) -replace '^CITYLENS_DB_PASSWORD=', ''
$env:DATABASE_URL = "postgresql://citylens:$([uri]::EscapeDataString($dbPassword))@127.0.0.1:54329/citylens"
.\.venv\Scripts\python.exe backend/db.py init
.\.venv\Scripts\python.exe backend/db.py load "backend/Raw Data/Get it done/get_it_done_requests_CD1_datasd.csv"
# Optional tests: see AGENT.md and verification/README.md (use isolated test databases).
.\.venv\Scripts\python.exe backend/db.py count --lat 32.8 --lng -117.2 --radius 800 --start '2026-01-01T00:00:00-08:00' --end '2026-02-01T00:00:00-08:00'
```

URL-encode special characters in the password when constructing `DATABASE_URL`.
Docker Desktop requires a functioning Linux/WSL2 backend.
`docker compose stop` stops the database while preserving its volume.

The same count is callable directly in SQL:

```sql
SELECT citylens.count_service_requests(
  32.8, -117.2, 800,
  '2026-01-01T00:00:00-08:00'::timestamptz,
  '2026-02-01T00:00:00-08:00'::timestamptz
);
```

Semantics and limitations:

- Latitude then longitude at the function boundary; meters for radius (0–50,000).
  PostGIS geography and a GiST index support distance queries.
  See the [official ST_DWithin documentation](https://postgis.net/docs/ST_DWithin.html).
- Time filters use request creation time, inclusive start and exclusive end.
  CLI timestamps require explicit UTC offsets. Source timestamps have no offset;
  ingestion currently **assumes America/Los_Angeles**, pending source metadata audit.
- Counts are distinct request IDs, including linked duplicate submissions by
  default. Add `--exclude-linked-duplicates` (SQL sixth argument `true`) to omit
  records with a parent ID. This does not establish the number of physical issues.
- Missing coordinates are retained but excluded from spatial counts. Invalid
  coordinates or dates fail the import and roll back the entire transaction.
- Reimports upsert by request ID. Last CSV occurrence wins for repeated IDs;
  absent IDs from older imports remain. This is not snapshot replacement.
- Raw records, source filename, SHA-256, ingestion time, and row counts are retained.
  Import time is not proof of source freshness.
- The provided file is named CD1; counts describe this imported subset, not
  verified citywide coverage. An empty result does not prove no issues exist.
- The local account is an administrative setup account. The CLI count runs in a
  read-only transaction with a 10-second timeout. A separate restricted account
  is required before connecting an agent or deploying a service.

The integration script tests spatial/time boundaries, linked duplicates,
missing locations, and invalid inputs inside a transaction that is rolled back.
It temporarily locks the request table; run it only against this local database.
