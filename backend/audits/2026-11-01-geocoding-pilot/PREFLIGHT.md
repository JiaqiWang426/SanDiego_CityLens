# Pilot preflight

- Both Phase 1.1 tools passed through DatabaseTools using AGENT_DATABASE_URL and through the actual FastAPI endpoint (TestClient), including metadata for both domains.
- The configured login is not an administrator and has no collision write/schema-create privileges. No administrator fallback was used for these checks.
- Effective EXECUTE is present through PUBLIC; explicit reader-group EXECUTE grants are absent. minimum_grants.sql is prepared, not executed. Current calls need no privilege expansion.
- Before pilot: 78,494 pending, zero matched/no_match/ambiguous/error/unsupported, zero located or attempted. These are actual stored statuses; failed is not a schema status (error is).
- Existing repeated-import/address-key/geocode lifecycle regression passed in an isolated DB. Eight selected-worker/race/unit checks passed before Census requests.
- Worker saves require both the original address_key and pending status; source-field upserts preserve geocoding on unchanged address keys.
- A minimal optional report_ids/--report-id selector was added to the existing worker. It only narrows the pending query, remains bound by limit, and is parameterized.
- sample.json fixes at most 25 supported block-address IDs across distinct streets and beat/suffix strata. This purposive sample is not representative; no rate will be extrapolated.
- The pilot runner will call geocode_pending(..., limit=25, report_ids=manifest IDs) with the existing Census adapter, default Public_AR_Current benchmark and one-second pause, with no retries or follow-on batches.
- Authorized writes: location, geocode_status, geocode_provider, geocode_precision, geocoded_at, geocode_details for selected still-pending records only. Original source columns/raw_record and completed geocodes must remain unchanged.
- Worker writes use separate maintenance credentials in memory (DATABASE_URL, or the configured Docker admin password for the same verified database). Agent credentials remain read-only. No roles/grants/ownership/schema changes will be made.
- Exact reproducible worker CLI (maintenance DATABASE_URL configured):

```powershell
.\.venv\Scripts\python.exe backend/collision_geocoding.py --limit 25 --benchmark Public_AR_Current --report-id 22201592 --report-id 22203409 --report-id 25201589 --report-id 21204884 --report-id 187919 --report-id 19205964 --report-id 192915 --report-id 1607476 --report-id 183276 --report-id 22025558 --report-id 185499 --report-id 25206103 --report-id 26205983 --report-id 24201057 --report-id 20204952 --report-id 25015379 --report-id 21207785 --report-id 23204448 --report-id 19203017 --report-id 190006 --report-id 19206280 --report-id 19205779 --report-id 25201149 --report-id 23201072 --report-id 193527
```

For this audit use the guarded pilot runner instead, so request counts, snapshots and artifacts are captured. Do not rerun the CLI after the pilot; it is recorded for reproducibility, not an instruction to repeat requests.
