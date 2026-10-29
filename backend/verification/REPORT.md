# Verification report — 2026-10-28

**Bottom line:** every query result, record count and aggregate matched the
independent reference, on the synthetic fixtures and on the full raw CSVs.
The problems found are in what ingestion accepts without complaint, and in
the documentation. See [AMBIGUITIES.md](AMBIGUITIES.md), section A.

**Setup:** PostgreSQL 17.5 with PostGIS 3.5.2, in a throwaway container on
port 54330 (data in tmpfs, database collation `en_US.utf8`). The reference
used pandas 3.1.0 and pyproj (WGS84 geodesics), with pytest 9.2.0. The
command was `pytest --run-realdata` (6 min 13 s).

## Totals

| Suite | Tests | Passed | Failed | Skipped |
| --- | --- | --- | --- | --- |
| Reference unit (`test_reference_unit.py`) | 74 | 74 | 0 | 0 |
| Schema and constraints (`test_db_schema.py`) | 33 | 33 | 0 | 0 |
| Ingestion (`test_db_ingest.py`) | 29 | 29 | 0 | 0 |
| Differential (`test_differential.py`) | 92 | 92 | 0 | 0 |
| Retrieval cap (`test_db_limits.py`) | 1 | 1 | 0 | 0 |
| Full raw data (`test_realdata.py`) | 18 | 18 | 0 | 0 |
| **Total** | **247** | **247** | **0** | **0** |

24 of the 247 are **probes**. A probe passes once it has recorded what
production does; it does not show the behavior is correct. Their findings are
in AMBIGUITIES.md. Without `TEST_DATABASE_URL`, the 173 database tests are
skipped and the 74 unit tests still run. Without `--run-realdata`, the 18
full-data tests are skipped.

### Failures during development (all fixed in the tests, not in production)

1. A pandas index/column name clash in the reference's ordering code.
2. A keyword clash in a schema test helper.
3. SQL NULL arriving as NaN, which made `None != NaN`.
4. A test that assumed the CLI prints a bare number (it prints `{"count": N}`).
5. One test truncating shared data partway through a module.
6. Two tests assuming an empty table.
7. **A reference assumption:** mixed-case street text was treated as
   unambiguous. Production uppercases it and the docs don't say whether it
   should, so those rows were reclassified as uncertain under rule A6.
8. **An expectation dropped:** I expected an empty window `[t, t)` to return
   0. It raises instead, and the docs don't settle it, so it became a probe
   and finding D5.

No production file was modified.

## Differential results

| Comparison | Scope | Result |
| --- | --- | --- |
| Record counts | Get It Done 22 rows → 20 IDs; collisions 17 rows → 16 accepted / 1 rejected / 15 IDs; full data 303,226 → 303,225 and 78,494 → 78,494 | Match |
| Exact ID sets | 18 fixture filters × 2 tools; 52 full-data filter queries; full stored-ID sets for both domains | Match |
| Aggregates | 5 fixture period/timezone combinations; 9 full-data period/timezone combinations; total = filter count for 16 filters | Match |
| Time filtering | Start included, end excluded, last second, LA and UTC calendars, month and quarter boundaries near midnight, DST-overlap windows | Match (records whose result depends on the DST choice are reported, not asserted) |
| Spatial | 0 / 0.3 / 1 / 10 / 799 / 800 / 801 / 49,999 / 50,000 / 50,001 m points; 160 full-data count queries | Match. PostGIS and pyproj distances agree to within 0.01 m. |
| NULL handling | NULL counts fail numeric filters; NULL times never match; NULL locations are never inside a radius; unknown hit-and-run label → NULL | Match |
| Duplicates | Last row wins (including the real ID 5449467); archive keeps every row; repeated import is idempotent | Match |

### Records left out of strict comparison (57 observations)

- **Radius edge band (±0.5 m).**
  - *Fixtures:* the points at exactly 800 m, 801 m (with an 801 m radius),
    50,000 m and 0.3 m (with radius 0). PostGIS counted the 800 m and 801 m
    points as inside. It counted the 50,000 m and 0.3 m points as outside.
    All are within tolerance of the edge, so none counts as a pass or a
    failure.
  - *Full data:* 32 of the 160 count queries had 1–98 records in the band
    (dense real points on the 5 km circle), so the test only checked a
    range.
  - *Extra check (not part of the suite):* I re-scored those 32 queries at
    **zero tolerance**, counting a point as inside when its pyproj distance is
    at most the radius. **All 32 matched the PostGIS count exactly.** This,
    plus the 0.01 m agreement measured on the fixtures, suggests
    `DISTANCE_TOLERANCE_M` (now 0.5 m) could be tightened to about 0.05 m to
    make more of the full-data comparisons exact.
- **DST overlap.** 1 Get It Done row and 18 collision rows. All stored as the
  later (PST) instant. In the 2016-11-06 08:30–09:30Z window, 3 of 4
  uncertain rows were returned.
- **Undocumented street spelling.** Real-data street queries such as
  `GARNET AVE` had all 882 candidates returned. Since that agrees with the USPS
  assumption, the remaining uncertainty is only about the docs, not the
  results.

## Not tested

| Item | Reason |
| --- | --- |
| `collision_geocoding.py` (Census adapter, worker, cache, rejecting stale responses) | It needs the external geocoder, and network geocoding was out of scope. Only how reimport preserves or resets a geocoded row is covered. |
| Comparing Get It Done category and status filters with production | No production tool exists. Only the reference is unit-tested. |
| Read-only mode, the 10-second timeout, and connection handling in the CLI and wrappers | Not observable without timing or permission tests. |
| `imported_at` values; `collision_source_rows.row_number` base | The contract doesn't define them. |
| Ordering of report IDs with different lengths or mixed case | Depends on collation (A13). |
| Concurrent imports and lock behavior | Out of scope. |
| `db.py load` with no path argument | It would load the full development CSV. That is covered more directly by `--run-realdata`. |
