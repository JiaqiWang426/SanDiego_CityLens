# Phase 1.1 implementation and verification — 2026-11-01

## Audit summary (before implementation results)

The [pre-implementation summary](SUMMARY.md) and aggregate JSON observations
establish 303,225 stored Get It Done requests (424 blank service labels; four
statuses) and 78,494 collisions (all pending, zero coordinates/recorded attempts).
No format-only label collisions or inconsistent pending/matched coordinate
states were found. Source publication times are untracked; local imports and
event spans are known and distinct. See SUMMARY.md for full counts and sources.

## Implemented

- Read-only `aggregate_service_requests` SQL/Python/Agent tool, with service,
  status or combined grouping, exact canonical filters, distinct IDs, unknown
  groups, deterministic ordering, matching radius/time/duplicate semantics,
  and query-time normalization preserving original fields.
- Read-only `get_dataset_metadata` SQL/Python/typed Agent tool for both domains.
  Existing automatic evidence metadata now uses it in the measurement snapshot.
  Source timestamps remain NULL; no inferred freshness or local coverage.
- Additive evidence groups, geocoding metadata, and local ingestion provenance.
  Related planner wiring only enables the new grouping capability. Existing
  graph, SQL tools and verified answer/claim logic are preserved.
- Worker compare-and-set protects against late concurrent results. Unmatched
  precision and nonfinite worker pauses are rejected. No external requests.
- No ingestion changes or table migrations were necessary: committed import
  timestamps/hashes already exist. Future verified download metadata remains
  deferred rather than fabricated for local files.

## Actual tests and results

| Suite | Passed | Failed | Skipped / not run |
| --- | ---: | ---: | --- |
| `AGENT_TEST_DOCKER=1 python -m pytest backend/agent_tests -q` | 127 | 0 | 0 |
| Existing independent suite, `pytest --run-realdata -q -o addopts=` from `backend/verification` in a dedicated `_test` DB | 247 | 0 | 0 |
| Existing collision tests, `pytest test_collisions.py -q` from `backend` in a separate empty `_test` DB | 7 | 0 | 0 |
| **Distinct backend test total** | **381** | **0** | **0** |

The full-data suite completed in 465.23 seconds. It reads the real source CSVs
into its own disposable DB and compares authoritative SQL with the independent
reference. No real development record was used as a writable test fixture.
All owned test containers were removed. Python compilation and `pip check`
passed. No new dependency was added.

The planner prompt version marker was subsequently bumped to 3 to identify its
Phase 1.1 tool contract; the SDK/routing unit checks were rechecked separately.
Live LLM quality/intent evaluation, external Census requests, bulk geocoding,
and frontend visual QA were **not run** (not authorized / no UI changes).

## Existing database deployment / live checks

Only `040_aggregate_service_requests.sql` and `050_get_dataset_metadata.sql`
were installed in one transaction. These create/replace three caller-rights
read-only functions (including the normalization helper). No schema/table/data
migration, role/grant script, source rewrite or geocoding update was executed.
Existing table definitions, original SQL tool bodies and `.env` are unchanged.

[live-verification.json](live-verification.json) records executed aggregate
results and metadata:

- A=(32.84,-117.25), 800 m, January–February 2026: **28 groups, total 64**;
  existing `count_service_requests` also **64**.
- `service_name_filter=' pothole '`: **11**, independently counted from source
  `service_name='Pothole'` under the same spatial/time conditions, using the
  existing reader role in a read-only transaction.
- Get It Done / collision totals remain **303,225 / 78,494**; all collision
  statuses remain pending. Both raw CSV hashes match their original imports.
- Existing reader has EXECUTE access to both new tools through its current
  effective privileges. The optional reader setup SQL now includes explicit
  additive grants for future/manual deployments but was not executed here.
- Metadata reports local import dates from stored history and source publication
  and update timestamps as NULL. No portal date was assigned to old local files.

Restart the FastAPI process to load the updated Python code and tool contracts.
No additional DB or ingestion action is required on this development instance.
Future installations can use the normal `db.py init` and reviewed reader setup.

## Files created or changed

| Path | Purpose |
| --- | --- |
| `sql/tools/040_aggregate_service_requests.sql` | New category normalizer and grouped query |
| `sql/tools/050_get_dataset_metadata.sql` | Dataset metadata for both existing domains |
| `service_request_queries.py` | Offset-aware read-only Python aggregation wrapper |
| `dataset_metadata.py` | Typed metadata contract and read-only Python wrapper |
| `agent/tools.py` | New allowlisted measurement and location-free metadata call; authoritative metadata reuse |
| `agent/models.py` | Typed service grouping arguments/wire schema and additive evidence fields |
| `agent/evidence.py` | Grouped SQL result normalization and metadata provenance |
| `agent/llm.py` | Related tool description and prompt version 3 |
| `agent/workflow.py` | Related grouped-service routing/validation; graph unchanged |
| `agent/diagnostics.py` | Stable rejection codes for the new service arguments |
| `main.py` | Additive metadata argument type on existing direct-tool endpoint |
| `collision_queries.py` | Package/direct-script compatible import for shared read-only helper reuse |
| `collision_geocoding.py` | Pending-status race guard, input checks, package-compatible import |
| `sql/setup_agent_reader.sql` | Prepared additive EXECUTE grants; no live execution |
| `agent_tests/test_phase1_data.py` | Independent normalization reference, unit and isolated integration tests |
| `agent_tests/test_phase0.py` | Existing budget test kept on its intended three tools; obsolete unsupported-category example removed |
| `PHASE1_1.md`, `README.md`, `AGENT.md`, `TRAFFIC_COLLISIONS.md` | Contracts, usage, limitations and audit links |
| `audits/README.md`, this dated audit folder | Reproducible read-only audit, raw-file aggregates, results and live verification |
| `verification/results/observations.json` | Generated probe observations from the independent suite; reference calculations/assertions and its REPORT.md unchanged |

## Remaining issues / Phase 1.2 or later

- All collisions are still unlocated. Any geocoding run/provider extension needs
  separate approval. 8,350 current inputs are unsupported by the existing adapter.
- Pending records have no recorded attempt; exact operational reasons for not
  processing/resetting them are not established by the stored data alone.
- No verified provider publication/update tracking exists for the imported files.
  Capture reliable source evidence tied to download hashes in a future downloader.
- District/city boundaries, source timezone/DST ambiguity, positional accuracy,
  exposure denominators and source completeness remain limitations.
- No synonym/taxonomy mapping; potentially related Parking/Graffiti/collection
  labels remain distinct pending source-owner documentation.
- No queue-performance index, distributed job claim/retry system, complete
  historical replay, or query-specific import contributor IDs.
- Group-level narrative/frontend controls and live-model evaluation remain later
  work. Current evidence/SQL/API expose the groups without safety/quality scoring.
