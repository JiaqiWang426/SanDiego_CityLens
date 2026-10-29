# Ambiguous requirements and suspected defects

The contract used is `backend/README.md`, `backend/TRAFFIC_COLLISIONS.md`, and
the table definitions in `backend/sql/`. **`backend/TEST_CONTRACT.md`, which the
task named, does not exist.** "Observed" values come from the probe tests in
the throwaway database (`results/observations.json`). They describe production
behavior, not the intended contract.

## A. Suspected implementation defects

Each item is a place where the observed behavior conflicts with the docs or
loses information without notice. Severity is my judgment.

| # | Severity | Finding | Evidence |
| --- | --- | --- | --- |
| D1 | Medium | **A Get It Done row with only one coordinate is accepted silently.** `lat=32.8` with a blank `lng` imports with `location = NULL` and no error. The README says "Invalid coordinates … fail the import and roll back". A half-present pair looks invalid rather than missing. | `gid_probe_one_sided.csv`: exit code 0, row 9500002 stored without a location |
| D2 | Medium | **(0, 0) is accepted as a real Get It Done location.** That point is about 13,000 km from San Diego (a classic placeholder value). It gets a geography point and could match any 50 km query centered near it. | `gid_probe_zero_coord.csv`: row 9500005 stored at (0, 0) |
| D3 | Medium | **Times that don't exist (the spring-forward hour) are shifted forward without any record.** `2026-03-08 02:30` is stored as 10:30Z, the same instant as the real `03:30` row. Get It Done has no issues column, so nothing records the change. | `gid_dst_gap.csv`: 9300004 and 9300005 both stored at 10:30Z |
| D4 | Low–Medium | **Times in the repeated autumn hour always resolve to the second occurrence (PST), with no validation issue.** This affects 18 real collision rows and 1 real Get It Done row. TRAFFIC_COLLISIONS.md says "PostgreSQL resolves ambiguous DST times" but never says which way. The choice moves those rows by one hour inside hourly windows and is invisible to anyone reading the stored data. | `R::test_collision_stored_rows_match_reference` observation (18 of 18 resolved to the later instant); a check of `validation_issues` found `[]` on all 18 |
| D5 | Low | **A zero-length window `[t, t)` raises `InvalidParameterValue` instead of returning 0.** The docs define the interval as start-inclusive and end-exclusive, under which an empty interval is valid and matches nothing. | `D::test_probe_gid_degenerate_window[empty]` |
| D6 | Low (docs) | **The SQL parameter is named `hit_run_level_filter`, but TRAFFIC_COLLISIONS.md never names it** and only shows the Python keyword `hit_run_level`. A direct SQL caller copying the docs would use the wrong name. | `pg_get_function_arguments` |
| D7 | Low (docs) | **The README's setup steps run `backend/test_database.py`, which has been deleted.** TRAFFIC_COLLISIONS.md says psycopg plus the standard library are enough, but `requirements.txt` also needs pandas and pyproj. A comment in `get_it_done.sql` says "including repeated timestamp", which probably means repeated IDs. | Files on disk |

**No test found a wrong count, a wrong ID set, a wrong order or a wrong
aggregate.** That holds for the fixtures and for the full raw data: 303,225
Get It Done records, 160 count queries, 78,494 collision records, 52 filter
queries and 9 aggregate grids.

## B. Ambiguous or missing requirements

For each, the table shows the reference's choice (`refimpl/`) and what
production did. Records affected by an open question were left out of strict
comparison and are listed in `results/observations.json`.

| # | Question | Reference | Production (observed) |
| --- | --- | --- | --- |
| A1 | How do repeated-hour and skipped-hour local times resolve? | Both candidates kept; marked uncertain | Later (PST) for repeated times; shifted forward for skipped times (D3, D4) |
| A2 | What counts as an invalid Get It Done coordinate: one-sided, (0,0), outside San Diego? | One-sided = invalid (fail); (0,0) = valid range | One-sided → NULL; (0,0) stored (D1, D2) |
| A3 | Is a blank Get It Done ID a failed import or a skipped row? | Failed import | Failed import ("Missing ID or date") |
| A4 | Is a blank Get It Done `service_name` stored as `''` or NULL? | Undecided | `''` |
| A5 | Which street-suffix abbreviations are used? Only DRIVE→DR is documented. | Assumed the standard US Postal Service abbreviations (Publication 28); everything except DRIVE treated as uncertain | Matches USPS for 63k+ rows; 394 rows differ, all PLAZA/COVE/SQUARE/POINT/TRAIL/ALLEY (kept unabbreviated) plus mixed case |
| A6 | Are street names case-folded? The docs mention only whitespace. | Uncertain | Uppercased (`La Jolla Scenic` becomes `LA JOLLA SCENIC DR`) |
| A7 | Is `HIT_RUN_LVL` case-folded or trimmed? | Trimmed and case-sensitive | `felony` becomes FELONY / true; `" "` becomes false |
| A8 | What counts as a valid count: `" 2 "`, `"2.0"`, `"+1"`, values above the 32-bit integer maximum? | Digits only after trimming; anything else NULL | `" 2 "`=2; the others NULL (agrees) |
| A9 | Are report IDs trimmed? Is a whitespace-only ID "missing"? | Trimmed; whitespace-only = missing | Same |
| A10 | Which date formats are accepted? | `YYYY-MM-DD HH:MM:SS[.fff]` | Also accepts `YYYY-MM-DD HH:MM` |
| A11 | Out-of-range limit, offset, radius or coordinates; inverted min > max; partly supplied spatial arguments: error or clamp? | Error | Error (`InvalidParameterValue`) in every case |
| A12 | On reimport, does an updated row's `ingestion_id` change? Does an absent row keep its old one? | Undecided | Updated rows get the new ID; absent rows keep the old one |
| A13 | Tie-break order by `report_id` is text order, which depends on collation (`en_US.utf8`; `"10"` sorts before `"9"`) | Code-point order; fixtures use IDs of equal length | Not compared for IDs of different lengths |
| A14 | The `address_key` hash and the `normalized_address` format | Not modelled | Only stable-vs-changed behavior is tested |
| A15 | Which collision headers are "required"? | All 16 source columns | Leaving out `KILLED` fails the import |
| A16 | Does `collision_source_rows.row_number` start at 1, and does it count data rows or file lines? | Not asserted | Not asserted |
| A17 | Category and status filters for Get It Done are requested, but no documented tool has them | Implemented in the reference only | — |
| A18 | `TEST_CONTRACT.md` does not exist | — | — |
