# Agent UX & Location Redesign — Implementation Report

Date: 2026-11-15 (America/Los_Angeles) · Branch: `feature/qhy` · Nothing committed, pushed, merged or discarded.

This continues an implementation Codex started and did not finish. All existing working-tree changes were kept. The work below was built on top of them.

## 1. Status checklist (verified against code and tests)

| Requirement | Status | Where |
| --- | --- | --- |
| Explicit dates override UI and system defaults | Done | `scope_resolution.py`, `location_flow.py` |
| "in 2026" means the full 2026 calendar year | Done. **Fixed:** it no longer widens "January to March in 2026" | `scope_resolution.py` |
| Explicit radius overrides defaults | Done | `scope_resolution.py` |
| Follow-ups keep the session's scope | Done (tested with mocks and live) | `sessions.py`, `workflow.py` |
| Map Explorer filters never affect Agent queries | Done (browser-tested) | `investigation.js`, `explorer.js` |
| Address confirmation resumes the pending conversation | Done. **Fixed:** typed replies after a failed or ambiguous match now correct the location | `location_flow.py`, `investigation.js` |
| Building names are recognized | Done. **Fixed:** two live-model failure modes | `location_flow.py` |
| Spelled-number building → street reading | **New**, and always needs confirmation | `user_locations.py` |
| Google Places Text Search (identity only) | Done. Tested live on the server side | `places.py` |
| Candidate selection and confirmation | Done | `location_flow.py`, `investigation.js` |
| Independent coordinate check (Census / SanGIS / user) | Done. Google coordinates are blocked | `user_locations.analytical_candidate` |
| Manual map-pin fallback | Done (tested live) | `pick-location.*`, `location_flow.py` |
| Location provenance is recorded | Done | `GeocodedCandidate.metadata` |
| Summary, KPI and comparison cards built from evidence only | Done | `presentation.py`, `investigation.js` |
| Expandable evidence, limitations, SQL replay | Done | `investigation.js` |
| Concept B layout: left nav, center chat, right map | Done | `explorer.html`, `investigation.css` |
| The right-hand map works without a Google browser key | **Fixed:** falls back to the vendored Leaflet + OSM | `maps.js` |
| No persistent Agent filters in the main UI | Done. The old filter form remains only in `/agent` (Advanced) | `explorer.html` |
| Collision data and geocoding preserved | **Verified:** fingerprint identical | §5 |

## 2. What Codex had already implemented

- **`scope_resolution.py`** parses explicit years and radii deterministically and applies them after the model's interpretation.
- **Confirmation flow.** A new `confirmation_policy='conditional'` auto-confirms only clean numbered addresses. Everything else waits for the user. Users can also edit dates and radius at the confirmation step (`ConfirmationEdits`).
- **Entity types.** `AddressProposal.entity_type` distinguishes `street_address`, `building_or_poi`, `intersection`, `region` and `coordinates`. A guard rejects spelled-number house numbers.
- **`user_locations.py`:**
  - user-typed coordinate parsing
  - an optional self-hosted Nominatim-compatible POI source (public OSMF Nominatim is refused)
  - `analytical_candidate()`, which rejects any Google-derived coordinates
- **`places.py`.** A `/api/v1/locations/search` endpoint for Google Places Text Search. It uses a field mask that requests no coordinates, rate limiting, and expiring search tokens. Only `place_id` is stored in the session.
- **New actions:** `choose_place` and `select_manual_locations`. Manual pins pass through `ManualPoint`, which rejects Google sources, unconfirmed points and NaN values.
- **`presentation.py`.** Summary, KPI cards, timeframe label, data coverage and limitations, all built from verified evidence only. A failed evidence record shows "Unavailable", never 0.
- **Frontend.** The Agent-first page (`explorer.html`, `investigation.js/.css`, `maps.js`) has no iframe and includes a separate manual picker (`pick-location.*`). The old workspace is kept at `/agent`.
- **Baselines.** A read-only fingerprint of the collision table and hashes of the protected geocoding code (`.local/ux-*-before.json`).

## 3. What I changed or completed

Live testing with the real model and real geocoders found failures that the mocked tests did not.

1. **Year phrases no longer widen finer intervals** (`scope_resolution.py`).
   - Before: "from January to March in 2026" was replaced by the full year.
   - Now the whole-year reading applies only when the message has no month, quarter, half-year, season, explicit date, "from…to", "between", "since", "past N months" or Chinese 月/季度.
   - Otherwise the validated interpreted range is used.
   - Verified live: Jan 1 – Mar 31, 2026.
2. **Timestamps without an offset are handled** (`location_models.ExtractedTimeRange`).
   - The live model sometimes returns `2026-01-01T00:00:00` with no UTC offset. That failed the entire turn, so `1 Miramar St` was rejected on some runs.
   - These values are now read in the model's stated `calendar_timezone`, with the correct DST offset.
3. **Named places no longer fail when the model adds street fields** (`location_flow.check_addresses`).
   - The live model labeled "One Miramar Street" as `building_or_poi` but also filled `street_name`/`street_suffix`. That raised an error.
   - Those invented fields are now discarded. The original expression is still required verbatim.
4. **Spelled-number "street addresses" become named places.**
   - If the model reads "One Miramar Street" as house number 1, and the user never said "address", the location is treated as a named place instead of failing.
5. **Building names get a street-address reading built from the user's own words** (`user_locations.spelled_address_interpretation`).
   - "One Miramar Street Apartments" → "1 Miramar Street".
   - It is sent to the independent Census/SanGIS geocoder. Any match is marked `requires_confirmation=True` with `derived_from='user_text'`.
   - It never uses Google content. Names with no number, such as "Regents La Jolla", get no derived reading.
6. **Plain-language failure message.** It used to say "A: no_match (Independent POI source)… select coordinate mode". It now names the place and offers three options: type the street address, search Places, or pick on the map. Provider details moved to `diagnostic_codes`.
7. **Typed replies to a location check now correct that location** (`investigation.js`). After `geocoding_failed` or `needs_candidate_confirmation`, a typed reply is sent as `correct` instead of `clarify`. Verified live: "A should be 9253 Regents Rd" resolved and kept 2026 / 400 m.
8. **Map fallback** (`maps.js`, `investigation.css`).
   - With no `GOOGLE_MAPS_BROWSER_API_KEY` (your current `.env`), the right-hand map showed only a placeholder.
   - It now uses the vendored Leaflet + OSM map with the same interface: viewport queries, points and clusters, record details, and the radius/rectangle overlays.
   - Google Places search is offered only when a Google map is shown, so Places results are never displayed next to a non-Google map.
9. **Tests.**
   - `test_agent_ux.py`: 23 new cases covering finer intervals, whole-year phrases, spelled-number readings, building-name confirmation and provenance, no fabrication, discarded fields, and offset-less timestamps. One outdated expectation was updated.
   - `test_browser.py`: a new real-Edge test of the open-map fallback.
10. **`backend/scripts/audit_collision_fingerprint.py`**: a read-only, repeatable preservation check.

Nothing was changed in collision ingestion, geocoding, SanGIS topology, the SQL tools, or the database schema.

## 4. Test results (actual runs)

| Suite | Kind | Result |
| --- | --- | --- |
| `pytest backend/agent_tests backend/explorer_tests` | Mock model and tools; no database | **561 passed, 57 skipped** (all skips are Docker-gated) |
| Same, with `AGENT_TEST_DOCKER=1` | Adds **real PostgreSQL/PostGIS** in a throwaway tmpfs container on port 54331. The dev database is never touched. | **618 passed, 0 skipped** (70.8 s) |
| `test_browser.py` (2 tests) | **Real Microsoft Edge**, fixture data, fake model, mock Google SDK, OSM tiles blocked | 2 passed |
| `frontend/tests/*.test.cjs` | Node, DOM stubs | 3 / 3 suites passed |
| `node --check`, `git diff --check` | Static | clean |

**Live end-to-end runs.** These used the real OpenAI model, the real read-only CityLens database, the real Census and SanGIS geocoders, and the real Google Places API. Each was a single run, and model output can vary between runs.

| Input (all "… in 2026") | Result |
| --- | --- |
| `9237 Regents Rd` | Census match → **33** collision reports, Jan 1 – Dec 31, 2026, 800 m |
| `1 Miramar St` | Interpreted correctly. **No match** in Census or SanGIS Address Points (checked directly). Fallbacks offered; nothing fabricated. |
| `One Miramar Street` | Recognized as a named place. Derived "1 Miramar Street" → no independent match → fallbacks offered |
| `One Miramar Street Apartments` | Same as above. A **manual pin** then gave **11** reports; provenance `user_selected` / `manual_open_map`, original text kept |
| `Regents La Jolla` | Named place, no independent match. Typed correction "9253 Regents Rd" → Census → **12** reports at 400 m, 2026 kept |
| Compare 9237 Regents Rd vs 3969 Miramar St, within 500 m | 500 m applied. **20 vs 9**: "Location A has 11 more…". B matched by SanGIS (structure point). |
| Regents Rd, "from January to March in 2026" | Jan 1 – Mar 31 → **11** |
| Follow-up "Now only collisions with at least one injury" | Kept 2026 and 500 m: 20 → **13** |
| Google Places Text Search (server only) | "One Miramar Street Apartments" → 1 Miramar St, La Jolla. "Regents La Jolla" → 9253 Regents Rd. No coordinate fields returned. |

Not tested live:
- The Google Places **browser** flow, because no browser key is set. The `choose_place` step is covered by mocks only.
- Online OSM tiles.
- Follow-ups through the PostgreSQL session store. That path is covered by the Docker tests; the live runs used in-process conversations.

## 5. Collision data preservation

`.\.venv\Scripts\python.exe -m backend.scripts.audit_collision_fingerprint` was run read-only before and after my changes:

- Records: **78,494**, matching Codex's baseline.
- Fingerprint `md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id))`: **`72e5205f558b1a83c39a0d0ac146410b`**, identical to the baseline.
- Geocode status counts: matched 68,068 · pending 4,218 · no_match 3,633 · ambiguous 2,575. Located rows: 68,068.
- Protected geocoding and ingestion source files: **0 changed** (SHA-256 against `.local/ux-protected-code-before.json`).

No collisions were re-geocoded, re-imported or reset, and no migrations were run.

## 6. Remaining limitations

- **`1 Miramar St` / One Miramar Street.** Neither independent source has this address, so it can be analyzed only from a manual pin. A self-hosted POI source would also help. This is a data gap, not a code bug.
- **Google addresses are not re-geocoded.** Places identifies the building, but its address is not sent to Census automatically. The user has to type it, which keeps Google content out of the analysis.
- **Places search in the UI needs `GOOGLE_MAPS_BROWSER_API_KEY`.** Without it, the Places button is disabled and the open map is used.
- **Mixed pending state.** If location A has candidates and location B has no match, the conversation enters the failed state. The user must then correct the text or pin both locations. This behavior predates this work.
- **Year detection is conservative.** When a message contains any finer time word, including the verb "may", whole-year expansion is skipped and the model's validated range is used.
- **The `/agent` Advanced workspace still has its old fallback date and radius fields.** That is intentional: it is the technical tool. The main page has none.
- **Live checks are single runs.** Natural-language accuracy has not been measured statistically.

## 7. Credentials and configuration

Set in your current `.env`: `OPENAI_API_KEY`, `OPENAI_MODEL`, `AGENT_DATABASE_URL`, `SESSION_DATABASE_URL`, `SESSION_SETUP_DATABASE_URL`, `GOOGLE_PLACES_API_KEY`, `GOOGLE_MAPS_API_KEY`.

Optional and not set:
- `GOOGLE_MAPS_BROWSER_API_KEY` (+ `GOOGLE_MAPS_MAP_ID`) enables the Google map and Places search in the UI. Restrict the key to HTTP referrer `http://127.0.0.1:8000/*` and the Maps JavaScript API.
- `CITYLENS_INDEPENDENT_SEARCH_URL`, `_ATTRIBUTION` and `_USER_AGENT` enable a self-hosted, Nominatim-compatible POI source.

## 8. Launching the app (Windows PowerShell)

```powershell
cd 'E:\Github Projects\SanDiego_CityLens_QHY'

# 1. Start the existing CityLens PostgreSQL container in Docker Desktop (do not create a new volume), then check:
docker ps --format '{{.Names}} {{.Image}} {{.Ports}}'

# 2. Dependencies (first time only)
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

# 3. Optional read-only preservation check
.\.venv\Scripts\python.exe -m backend.scripts.audit_collision_fingerprint

# 4. Run the server (uses SelectorEventLoop on Windows)
.\.venv\Scripts\python.exe -m backend.scripts.serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`. The Advanced workspace is at `/agent` and the API docs are at `/docs`. Press Ctrl+C to stop, and restart after editing `.env`.

Re-run the tests:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\agent_tests backend\explorer_tests -q
$env:AGENT_TEST_DOCKER = '1'; .\.venv\Scripts\python.exe -m pytest backend\agent_tests backend\explorer_tests -q; Remove-Item Env:AGENT_TEST_DOCKER
```
