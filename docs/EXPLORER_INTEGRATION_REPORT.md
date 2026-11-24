# Service Request Explorer Integration Report

Date: 2026-11-23 · Branch: `Jiaqi-Wang` (uncommitted working tree) · Source reference: `SanDiego_CityLens_modify-detail` (`feature/modify-detail`, read-only)

## 1. Summary

The Service Request Explorer's classification system, analytical queries and dashboard visualizations are now part of CityLens. There is still one application: one PostgreSQL/PostGIS database, one FastAPI app, and the existing page at `/`. The dashboard replaces the old *Explorer Results* records section. It sits directly below the interactive map and its filter controls, in the Map Explorer panel.

| Area | Result |
| --- | --- |
| Data | Additive migration 001 adds `main_category`, `sub_category`, `classification_method` and `classification_version` to `citylens.service_requests`. It backfilled all 303,225 existing rows in about 12 s, and a second run changed 0 rows. Fingerprints of every source field, geometry, collision geocoding result, ingestion row and session count are identical before and after. |
| Ingestion | `db.py load` classifies new and changed rows in the same transaction as the upsert. A full re-ingestion of the raw CSV into a throwaway database left 0 rows unclassified. |
| Backend | `/summary` gained main and subcategory distributions. New routes: `/comparisons/monthly` and `/comparisons/community`. `/metadata` now returns the hierarchy and classification status. New shared filters: `main_category`, `sub_category` and an optional explicit analysis area. All aggregation runs in PostgreSQL. |
| Agent | All existing behavior is preserved. The explicit "current map view" branch can now filter by main or subcategory, which must be quoted literally by the user. Its consistency verification also covers the category distributions. |
| Frontend | The dashboard has KPI cards, a monthly trend, a distribution chart (main, subcategory or original type), a month × category heatmap, a community comparison and a paginated table. The main → subcategory filter cascades into the existing filter form. An explicit "Use current map view" analysis area is available. |
| Validation | 75 of 75 live API checks match independent SQL. Every one of the 303,225 stored classifications equals the Python rules. Default test run: 608 passed, 64 skipped. With isolated PostGIS: 672 passed, 0 skipped. Verification suite: 233 passed, plus 18 of 18 in the real-data run. Details are in §9. |

## 2. Starting point (Phase 1 findings)

- **CityLens (primary)** already served a PostgreSQL-backed Explorer (`explorer_store.py`, from the earlier `feature/qhy` sync) inside the unified app. It used no SQLite at runtime. It had no category hierarchy.
- **Source Explorer** added `category_rules.py`, `category_mapping.csv` (79 rows, 9 main and 66 subcategories), a pandas audit (`category_analysis.py`), comparison endpoints and a standalone dashboard. All of this was built on a scoped CSV → SQLite cache and served by its own `explorer_app`.
- **Database:** 303,225 service requests from 2016 to 2026, with 45 distinct service names (including NULL). The mapping covers every one of them. The District 1 / 2026 scope is 31,667 records, the same count as the source snapshot.
- **"Latest report" section:** CityLens has no section with that literal title. The source's table is titled *Latest matching reports*. Its CityLens counterpart was the newest-first *Explorer Results* records panel, together with the collapsed *Patterns in the source records* charts. Both were replaced.

## 3. Features migrated

| From the source | How it lives in CityLens |
| --- | --- |
| `category_rules.py` (rule compiler and classifier) | Ported with unchanged rules. It adds `classify_or_unmapped`, an exported `hierarchy(include_unmapped)` and `rule_rows()` for the DB sync. |
| `category_mapping.csv` | Copied byte for byte (SHA-256 `b148da29…94c6`). |
| `category_analysis.py` (independent pandas audit) | Copied unchanged. It is used as the test oracle. |
| Classification columns and indexes in SQLite | PostgreSQL columns, a CHECK constraint, two B-tree indexes, rule and mapping tables, and a set-based SQL backfill. |
| `/comparisons/monthly`, `/comparisons/community` | Same response contract (`{"data": [...]}`, observed groups only, community `null` preserved, sums equal `/summary.total`). Reimplemented over PostGIS. |
| Main and subcategory filters, `__missing__` | Added to the shared `ExplorerFilters`. `__missing__` on a derived field selects rows that are not yet classified. |
| Metadata `category_hierarchy` and facets | Read from the *active mapping stored in the database*, so the hierarchy always matches the classified data. |
| Map points carrying classification fields | Done. Clusters keep their aggregate shape. |
| Dashboard: hierarchy cascade, top categories, monthly heatmap, community bars, page-number pager | Rebuilt in the CityLens panel using its design tokens. There is no second map library and no chart dependency. |
| Last-row-wins duplicates and linked-report exclusion | Already equivalent in CityLens (`DISTINCT ON … ordinal DESC` upsert, `exclude_linked` → `parent_id IS NULL`). Unchanged, so counting semantics did not change. |

**Not migrated, by design:** the SQLite cache (`explorer_data.build_cache`), the CSV scoping step, the source's cache-signature invalidation and its standalone `explorer_app` server. The older CityLens `explorer_data.py` stays as a historical CSV reference used by the legacy test and the pandas audit. Nothing at runtime calls it.

## 4. Files

**Added**

| File | Purpose |
| --- | --- |
| `backend/category_rules.py`, `backend/category_mapping.csv`, `backend/category_analysis.py` | Classification rules, the approved mapping and the independent audit |
| `backend/sql/migrations/001_service_request_classification.sql` | Additive DDL, rule and mapping tables, `normalize_label`, `backfill_service_request_classification()`, reader grants |
| `backend/service_classification.py` | Apply migrations, sync the mapping and backfill. Also provides `status` and the `sql` (psql script) commands. |
| `backend/scripts/audit_classification.py` | Read-only live verification of the API against independent SQL |
| `backend/explorer_tests/test_classification.py` | Rules ↔ pandas parity; SQL ↔ Python parity; idempotency; mapping versioning; rollback; constraint; ingestion; reader privileges |
| `backend/explorer_tests/test_explorer_api.py` | Shared-filter wiring, parameter validation, migration-missing 503, Agent viewport category handling |
| `docs/category_classification_report.md` | The source's classification report, copied for provenance |
| `docs/EXPLORER_INTEGRATION_REPORT.md` | This report |
| `backend/audits/2026-11-23-explorer-integration/` | Before and after fingerprints, the applied migration script, both run logs, `live-verification.json`, real-data screenshots |

**Modified**

| File | Change |
| --- | --- |
| `backend/db.py` | `init` also applies migrations. New `migrate` subcommand. `load` uses an explicit column list and then classifies in the same transaction. |
| `backend/sql/setup_agent_reader.sql` | SELECT on the two new tables; EXECUTE on `normalize_label` |
| `backend/explorer_models.py` | `main_category` and `sub_category` filters. These are rejected for collisions. |
| `backend/explorer_store.py` | Classification projection, analysis area, distributions, comparisons, hierarchy and status metadata, canonical label resolution for the new fields |
| `backend/explorer_api.py` | New filters, `area_*` parameters (all four or none), comparison routes, a clear 503 when the migration is missing |
| `backend/agent/map_analysis.py` | Viewport plan fields for main and subcategory with exact-quote validation; distribution consistency check |
| `frontend/explorer.html`, `explorer.js`, `investigation.css`, `maps.js`, `investigation.js` | Dashboard, cascade, analysis area, tooltips and card states; `drawScope`; main categories shown in map-view answers |
| `backend/explorer_tests/test_browser.py`, `test_postgres_explorer.py`, `test_unified.py`, `backend/verification/test_db_schema.py` | Fixtures updated to the additive contract; new dashboard and PostGIS tests |

**Removed:** nothing. The untracked file `h origin Jiaqi-Wang` already existed before this work and was left untouched.

## 5. Database schema changes

Migration 001 is idempotent and additive:

```sql
ALTER TABLE citylens.service_requests
  ADD COLUMN IF NOT EXISTS main_category text, sub_category text,
                           classification_method text, classification_version text;
-- CHECK: all four NULL (unclassified) or all set, with method in
--        detail_match | service_default | needs_review | unmapped
CREATE INDEX service_requests_main_sub_requested_idx ON (main_category, sub_category, requested_at);
CREATE INDEX service_requests_sub_requested_idx      ON (sub_category, requested_at);
CREATE TABLE citylens.service_category_mappings (sha256 PK, source_file, rule_count, hierarchy jsonb, applied_at, is_active);
CREATE TABLE citylens.service_category_rules    (ordinal PK, service_name, detail NULL=default, main_category, sub_category, mapping_sha256 FK);
```

- The new columns are nullable with no default. Adding them is a catalog-only change with no table rewrite. NULL means "not yet classified" and is never a guess.
- The original `service_name` (legacy `category`), `raw_record`, primary keys, existing indexes and `location` are untouched. No geocoding was run.
- The migration grants SELECT to `citylens_agent_reader` when that role exists. It revokes PUBLIC EXECUTE on the backfill function. `ensure_reader` still sees no write privilege, so the reader account still passes.
- Exactly one mapping version is active. Earlier versions stay in the mappings table as history.

## 6. Classification and ingestion

**Rules (unchanged from the source):** service name and detail are normalized by collapsing whitespace and trimming. Matching is exact and case-sensitive. An exact detail condition beats the service default. A final *Needs Review* result always uses method `needs_review`.

**Explicit unmapped handling (CityLens addition):** the source raises an error on an unknown service name. CityLens stores every historical record, so an unknown name is labelled `Needs Review / Unmapped service name / unmapped` and counted in metadata instead of aborting an import. The hierarchy lists that subcategory only when such rows exist. Today there are 0 unmapped rows.

**One SQL classifier:** `citylens.backfill_service_request_classification()` joins each row against the active rules and updates only rows whose stored result differs. The same function serves three uses:

1. **Migration backfill:** the first run classified 303,225 rows; the second changed 0.
2. **Ingestion:** `db.py load` upserts as before and then calls `service_classification.apply`. That classifies new rows and rows whose service name or detail changed, inside the import transaction.
3. **Mapping change:** editing `category_mapping.csv` and re-running `migrate` records the new version, swaps the rules and reclassifies only the rows that changed. An invalid mapping (for example, overlapping rules) fails validation in Python before any statement runs. Nothing changes.

Results on the development DB (District 1, 2026): Sanitation & Waste 9,317 · Parking & Vehicles 7,657 · Community & Property Services 3,603 · Traffic & Mobility 2,917 · Parks & Vegetation 2,632 · Roads & Sidewalks 2,293 · Needs Review 1,473 · Graffiti 915 · Water & Drainage 860. These are exactly the source implementation's expected counts. Across all years: 236,199 `service_default`, 53,310 `detail_match` and 13,716 `needs_review`.

## 7. API

Prefix `/api/v1/explorer`. Every analytical route uses one filter dependency, so KPIs, charts, comparisons, records and markers describe the same scope.

| Parameter | Meaning |
| --- | --- |
| `start`, `end` | Inclusive local dates within 2026 (America/Los_Angeles) |
| `category` | Original source service name (legacy, unchanged) |
| `main_category`, `sub_category` | Derived classification. They can be combined; an incompatible pair returns empty results. |
| `status`, `community`, `zipcode` | Source fields. `__missing__` selects NULL. |
| `exclude_linked` | Excludes requests that have a parent ID |
| `dataset` | `service_requests` (default) or `traffic_collisions`. Category-hierarchy filters return 422 for collisions. |
| `area_west/south/east/north` | Optional explicit analysis rectangle. Supply all four or none. |

| Route | Change |
| --- | --- |
| `GET /metadata` | Adds `category_hierarchy`, `facets.main_category`, `facets.sub_category` and `classification` (active and repository mapping SHA, `mapping_current`, method counts, unclassified, unmapped and stale counts). |
| `GET /summary` | Adds `main_categories`, `sub_categories`, `unclassified` and `analysis_area`. All existing keys are unchanged. |
| `GET /comparisons/monthly` | New. `{month, main_category, sub_category, count}` |
| `GET /comparisons/community` | New. `{community, main_category, sub_category, count}`; unknown community is `null` |
| `GET /map` | Viewport bounds choose what to draw. `area_*` restricts it further. Points include the classification fields. |
| `GET /requests`, `/requests/{id}` | Records include the classification fields. `/requests` honours `area_*`. |

**Viewport vs. analysis scope.** Panning or zooming calls only `/map`. KPI and chart totals change only when the user presses *Use current map view*, which sets `area_*`, or *Clear area*. Without an area, unlocated records count in all statistics. With an area (an explicit spatial filter), unlocated records cannot match, and the UI says so.

**Errors.** A missing database configuration returns 503. If the migration has not been applied, the response is 503 `classification_migration_missing: run python -m backend.db migrate`. Other database failures return the existing sanitized codes.

## 8. Frontend

The page structure is unchanged: navigation, the Agent conversation, and the Map Explorer panel with the map at the top. Below the map and its filter controls, the old records list and collapsed charts are replaced by **Service request patterns**:

1. KPI cards: total, closed (or located, for collisions), planning areas, unlocated.
2. **Reports by month**: a column chart with a y-axis; selecting a month focuses the date range. Months beyond the source data are greyed out.
3. **Distribution**: horizontal bars, switchable between *Main*, *Subcategory* and *Original type*. Selecting a bar sets that filter. The top 12 are shown and the remainder is summarised in a note.
4. **Monthly comparison**: a month × category heatmap on a single-hue sequential ramp with a legend. It switches to subcategories when a main category is selected.
5. **Community comparison**: the top 10 planning areas. It ignores only the planning-area filter so areas stay comparable; selecting a bar filters by that area.
6. **Matching reports**: a paginated table (12 per page, with page numbers), category and subcategory per row, and the detail dialog with the classification fields.

**Filters.** *Main category → Subcategory* were added to the existing filter form; there is no second filter set. The subcategory select is disabled until a main category is chosen. Its options are limited to that main category, and an incompatible selection is cleared when the main category changes. The original issue type stays as its own control.

**Behavior.** Each card shows its own loading, empty and error states. Every request is version-checked, so a slow stale response cannot overwrite newer results; a browser test checks this. Bars, columns and heatmap cells have hover and focus tooltips with exact counts. The layout is responsive, with no horizontal scroll at 390 px. The styling uses the existing CityLens tokens and typography.

**Agent independence.** The Agent never reads Explorer filters during ordinary questions. As before, an explicit "current map view" question sends the visible map bounds and the *applied* filters, now including main and subcategory. It never sends the dashboard analysis area. Unapplied form edits are never sent.

## 9. Testing and actual results

| Suite / check | Command | Result |
| --- | --- | --- |
| Baseline before changes | `pytest backend/explorer_tests backend/agent_tests -q` | 561 passed, 57 skipped |
| Same suite after integration | same | **608 passed, 64 skipped** |
| With isolated PostGIS (throwaway container) | `AGENT_TEST_DOCKER=1 pytest backend/explorer_tests backend/agent_tests -q` | **672 passed, 0 skipped** |
| Verification suite (throwaway `*_test` DB) | `TEST_DATABASE_URL=… pytest backend/verification -q` | **233 passed**, 18 skipped (real-data tests are opt-in) |
| Real-data differential (full CSV ingestion) | `… pytest backend/verification/test_realdata.py --run-realdata` | **18 passed** (7 min). After ingestion: 303,225 rows, 0 unclassified, identical category counts. |
| Browser (Microsoft Edge, fixture data) | `pytest backend/explorer_tests/test_browser.py` | **3 passed**, including the new dashboard test (cascade, shared filters across all five panels, stale-response protection, analysis area, Agent context without the area, reset) |
| Frontend Node tests | `node frontend/tests/*.test.cjs` | 3/3 passed. `node --check` passes for all scripts. |
| Live API vs. independent SQL (development DB) | `python -m backend.scripts.audit_classification` | **75/75 checks**: 10 filter combinations, including area, area + category, an incompatible pair, DST-correct March and linked exclusion. Totals, main distributions, sub sums, months, both comparisons and `/requests.total` all match. All **303,225** stored classifications equal the Python rules. |
| Migration safety (development DB) | before/after fingerprints | Identical for service-request source fields and geometry, collision geocoding (68,068 located), ingestions and sessions. The second migration run changed 0 rows. |
| Real-app smoke test | `backend.scripts.serve` + Edge | Page and API serve real data. `/summary` takes about 0.45 s. Screenshots are in the audit folder. |

Rules ↔ pandas audit parity is also checked for every mapping rule (plus whitespace noise and unseen details) and for all 31,667 records in the 2026 scope.

The Node test runner was the Node bundled with Playwright (`.venv/Lib/site-packages/playwright/driver/node.exe`), because `node` is not on PATH. The only browser console error is the pre-existing missing `/favicon.ico` (404).

**Not executed:** live OpenAI calls (Agent tests use fake models, as before) and online geocoding. Neither was needed, and neither was touched.

## 10. Commands

From the repository root (Windows PowerShell):

```powershell
# 1. Start the existing database container (do not recreate it or its volume)
docker start sandiego_citylens-db-1

# 2a. Apply migration + mapping sync + backfill with an owner connection
$env:DATABASE_URL = '<table-owner connection string>'   # not stored in .env
.\.venv\Scripts\python.exe -m backend.db migrate        # prints JSON status; safe to repeat

# 2b. Same transaction without host credentials (container-local owner socket)
.\.venv\Scripts\python.exe -m backend.service_classification sql | docker exec -i sandiego_citylens-db-1 psql -U citylens -d citylens -v ON_ERROR_STOP=1

# Status / verification (read-only)
.\.venv\Scripts\python.exe -m backend.service_classification status       # needs DATABASE_URL
.\.venv\Scripts\python.exe -m backend.scripts.audit_classification        # uses AGENT_DATABASE_URL from .env

# 3. Import new data (classification runs automatically in the same transaction)
.\.venv\Scripts\python.exe -m backend.db load "backend\Raw Data\Get it done\<file>.csv"

# 4. Start the unified app (unchanged workflow)
.\.venv\Scripts\python.exe -m backend.scripts.serve --host 127.0.0.1 --port 8000
# http://127.0.0.1:8000/   (API docs at /docs)
```

On the development database the migration has **already been applied** with option 2b. New databases get it automatically from `python -m backend.db init`. If a database was set up before this change and its reader role was created later, re-run `backend/sql/setup_agent_reader.sql`.

## 11. Architectural decisions

- **Classify in PostgreSQL, from the validated Python mapping.** Python (`CategoryRules`) stays the single place that validates the approved CSV. Its compiled rules go into a table, and one set-based SQL function applies them. Backfill and ingestion then run where the data lives, with no export or re-import of rows. Tests show SQL and Python agree on every rule and on all 303,225 real rows.
- **Hierarchy comes from the database, not the file.** The API reports the hierarchy of the mapping that actually classified the data. `mapping_current` flags drift when the repository CSV changes before `migrate` is run.
- **Nullable columns rather than NOT NULL with a default.** This keeps the migration non-destructive and instant, and other write paths (test fixtures, future tools) keep working. Rows those paths write stay explicitly unclassified until the next backfill.
- **The analysis area is a separate parameter from the map viewport.** This follows the source design: viewport-only rendering, totals over the full scope. The only spatial constraint on statistics is the one the user chooses.
- **Agent tools.** The radius tools (`count/aggregate_service_requests`) are SQL functions with fixed signatures, evidence records and claim templates, and they work on the original service names. Adding hierarchy grouping to them would change their SQL contract and the evidence and rendering pipeline. The map-view branch already reuses `ExplorerStore`, so category questions are supported there with the same quote-grounding and evidence verification. The radius tools are unchanged.
- **No new dependencies.** The charts are plain DOM and CSS in the existing style, and the map stays the existing Google/Leaflet abstraction.

## 12. Limitations and potential improvements

- **Radius questions about categories** ("graffiti within 800 m of …") are not yet supported by the Agent's radius tools; see §11. The natural next step is an additive overload of `aggregate_service_requests` with a `main_category`/`sub_category` grouping.
- **Hierarchy filters cover one main and one subcategory.** There is no multi-select.
- **District 1 scope.** The `raw_record->>'council_district'` filter has no index. Summaries take about 0.45 s on 303k rows. An expression index would help if the table grows substantially.
- **Mapping drift.** The repository's `docs/category_classification_report.md` (copied from the source) quotes mapping SHA `9fe7f58f…`. The mapping file itself, in both repositories, hashes to `b148da29…`, so the report predates the final mapping edits. The classification follows the file.
- **Unchanged source caveats.** The source's caveats on park irrigation, park pavement, mixed park enforcement and street-light placement are unchanged pending review. Categories group reports; they do not verify an issue, and *Closed* is not proof of repair.
- **Duplicate detection** is still limited to parent-linked reports. Equal timestamps are not treated as duplicates.
- **Legacy tooling.** `explorer_data.py` and the SQLite-based `test_explorer.py` remain as legacy CSV references and could be retired once the pandas audit no longer needs them.
- **Favicon.** A favicon would remove the console 404.

Nothing was committed, pushed or merged. The source repository was not modified.
