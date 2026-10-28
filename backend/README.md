# Local data infrastructure

PostgreSQL 17 + PostGIS runs in Docker with persistent storage, exposed only on
localhost port 54329. Python imports the CSV; SQL performs spatial counting.
The existing `Database.py` contains design notes and is left intact.

From the repository root in PowerShell:

```powershell
# A local .env has been generated on this workstation. On a fresh checkout:
# Copy-Item .env.example .env
# Edit .env to choose a local password.
docker compose up -d --wait
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
$dbPassword = (Get-Content .env | Where-Object { $_ -like 'CITYLENS_DB_PASSWORD=*' }) -replace '^CITYLENS_DB_PASSWORD=', ''
$env:DATABASE_URL = "postgresql://citylens:$([uri]::EscapeDataString($dbPassword))@localhost:54329/citylens"
.\.venv\Scripts\python.exe backend/db.py init
.\.venv\Scripts\python.exe backend/db.py load backend/get_it_done_requests_CD1_datasd.csv
.\.venv\Scripts\python.exe backend/test_database.py
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
