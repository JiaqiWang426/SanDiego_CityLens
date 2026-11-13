# District 1 Service Request Explorer

This is the course proposal's interactive visualization of 2026 Get It Done service requests in San Diego City Council District 1. It runs from the CSV already in this repository and needs neither Docker nor an LLM key.

## Run locally

From the repository root on macOS/Linux:

```bash
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python -m uvicorn backend.explorer_app:app --host 127.0.0.1 --port 8011
```

Open <http://127.0.0.1:8011/>. API documentation is at <http://127.0.0.1:8011/docs>.
On another platform, substitute the Python executable in your virtual environment. The original agent evaluation desk remains available with `backend.main:app` on a separate port when its database and session credentials are configured.

The first start builds an ignored `.local/explorer.sqlite3` cache from `backend/Raw Data/Get it done/get_it_done_requests_CD1_datasd.csv`. Later starts reuse the cache while the source file's size and modification time match. Restart the server after changing the CSV, or explicitly rebuild with:

```bash
.venv/bin/python -m backend.explorer_data
```

The source CSV is never edited. The build is atomic, so an import error does not replace a valid older cache. The cache tracks source file SHA-256, build time, row counts, date range, missing coordinates and linked requests.

## What the explorer does

- Filters the **2026 District 1** snapshot by inclusive date range, issue type, source status, community planning area (`comm_plan_name`) and ZIP code. The optional linked-report switch excludes rows with a source parent ID; it cannot identify all real-world duplicates.
- Shows all matching records in the KPI totals and charts. The map queries only its visible bounds. It returns individual report points for at most 700 matches in a viewport and otherwise returns grouped bubbles whose counts include every located match. Pan or zoom to explore; the record table remains a separate paginated view.
- Shows month and issue-type counts and a 12-row report page. Clicking a map point or a table row opens source fields useful for inspection. The public free-text description is never placed in the explorer cache or returned by the API.

## Data meaning and limits

The supplied CSV has 303,226 rows; the local projection has 31,667 distinct 2026 District 1 request IDs, dated January 1 through October 26, 2026. These are counts in this supplied snapshot, not a live feed or proof of complete city coverage. `Closed` is a source status and does not establish that an issue was repaired. Community planning areas are source `comm_plan_name` values, not verified neighborhood boundaries. Source coordinates are displayed without independent position validation. Missing coordinates would remain in chart totals but not appear on the map.

The map library is vendored locally under `frontend/vendor/`; visible OpenStreetMap tiles still require an internet connection and retain their required attribution. The charts, filters and table work with the local API even if tiles are unavailable.

## Code and checks

`backend/explorer_data.py` makes the scoped cache; `backend/explorer_api.py` serves bounded read-only queries; `backend/explorer_app.py` serves the page and API. `frontend/explorer.html`, `explorer.css` and `explorer.js` implement the dashboard. The existing agent backend, PostGIS tables, and original evaluation desk are separate.

```bash
.venv/bin/python -m pytest backend/explorer_tests/test_explorer.py -q
node --check frontend/explorer.js
```

The tests cover scope and last-row-wins behavior on a synthetic CSV, plus real snapshot summary totals, filtered list consistency, bounded map counts, record details and invalid ranges.
