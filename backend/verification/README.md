# CityLens verification suite

This suite checks CityLens's SQL tools and ingestion against an independent
Pandas reference. The reference was written only from `backend/README.md`,
`backend/TRAFFIC_COLLISIONS.md`, the table definitions in `backend/sql/*.sql`,
and the raw CSVs. No production query or ingestion code was read.

| Path | Purpose |
| --- | --- |
| `refimpl/` | The reference: CSV cleaning, last-row-wins deduplication, WGS84 geodesic distance (pyproj), and tracking of daylight-saving ambiguity. No database access. |
| `fixtures/*.csv`, `fixtures/expected.py` | Small synthetic CSVs and their expected outputs, worked out by hand. |
| `test_reference_unit.py` | Checks the reference against the hand-specified outputs. No database. |
| `test_db_schema.py` | Types, nullability, primary keys, foreign keys, indexes, CHECK and NOT NULL constraints, transaction rollback, repeatable `init`. |
| `test_db_ingest.py` | `db.py load` and `collision_ingest.py`: upsert, repeated imports, rollback on failure, missing and invalid data, the source-row archive. |
| `test_differential.py`, `test_db_limits.py` | SQL functions vs the reference: ID sets, row order, aggregates, time windows, spatial filters, `NULL` and duplicate handling. |
| `test_realdata.py` | Opt-in. Compares both full raw CSVs against the reference. |
| `COVERAGE_MATRIX.md`, `REPORT.md`, `AMBIGUITIES.md` | Deliverables. |

## Test categories

- **Strict tests:** assert behavior the documentation defines.
- **`probe` tests:** record behavior the docs leave undefined in
  `results/observations.json`. A probe passes once its observation is recorded.
  It never counts as evidence of correctness.
- **Uncertain records:** some records' expected result depends on an
  undocumented rule. Examples are a repeated daylight-saving hour, a point
  within ±0.5 m of the radius edge, and an undocumented street spelling. These
  records are left out of strict comparison only where that rule could change
  the answer. They are listed in `observations.json` and never dropped
  silently.

## Running

The database tests use a throwaway PostGIS container. It keeps its data in a
tmpfs, so it creates no Docker volume and never touches the development
database on port 54329.

```bash
PW=$(python -c "import secrets;print(secrets.token_hex(16))")
docker run -d --name citylens_verify_db -p 127.0.0.1:54330:5432 \
  --tmpfs /var/lib/postgresql/data:rw \
  -e POSTGRES_DB=citylens_test -e POSTGRES_USER=verify -e POSTGRES_PASSWORD="$PW" \
  postgis/postgis:17-3.5
export TEST_DATABASE_URL="postgresql://verify:$PW@127.0.0.1:54330/citylens_test"

cd backend/verification
../../.venv/Scripts/python.exe -m pip install -r requirements.txt
../../.venv/Scripts/python.exe -m pytest                  # unit + DB tests (~40 s)
../../.venv/Scripts/python.exe -m pytest --run-realdata   # + full raw CSVs (~6 min)
docker rm -f citylens_verify_db                           # discard everything
```

**Without `TEST_DATABASE_URL`, only the 74 reference unit tests run; every
database test is skipped.**

The suite refuses to connect when any of these is true:

- the database name does not end in `_test`
- the port is 54329
- the URL matches `DATABASE_URL`

Subprocess output is masked so the password is never printed.
