# Historical collision geocoding — production deployment report

Deployed November 2–3, 2026, America/Los_Angeles, to the CityLens PostgreSQL/PostGIS
database: `citylens` in the Compose container, 127.0.0.1:54329. This was done under
the explicit user authorization of all 11 approved scopes. **All 8,441 approved
records were deployed. 0 were rejected and 0 skipped.** Actual coordinate coverage
rose from 75.96% to **86.72%**.

## Deployment summary

| Measure | Before | After (actual) |
| --- | ---: | ---: |
| Total reports | 78,494 | 78,494 |
| Matched / located | 59,627 | **68,068** |
| No-match | 7,400 | 3,633 |
| Ambiguous | 3,117 | 2,575 |
| Pending | 8,350 | 4,218 |
| Unlocated | 18,867 | **10,426** |
| Coordinate coverage | 75.9638% | **86.7175%** |

| Allowlist | Approved | Updated | Rejected |
| --- | ---: | ---: | ---: |
| Stage C intersections (original status Pending) | 4,112 | 4,112 | 0 |
| Final: no-match road blocks | 3,767 | 3,767 | 0 |
| Final: ambiguous road blocks | 542 | 542 | 0 |
| Final: Pending official-abbreviation intersections | 20 | 20 | 0 |
| **Total** | **8,441** | **8,441** | **0** |

Located reports by provider and precision, from live SQL:

| Provider | Precision | Reports |
| --- | --- | ---: |
| US Census Geocoder Batch | block_approximate | 59,608 |
| US Census Geocoder (pilot) | block_approximate | 19 |
| SanGIS road centerline | block_approximate | 4,309 |
| SanGIS local topology | intersection_approximate | 4,132 |

## Operations

**Approvals.** There were 11 approval files, kept outside the repository in
`E:\review\citylens-geocoding-2026-11\`. Their SHA-256 hashes are listed in
[approval-index.json](approval-index.json). Each binds the exact allowlist,
validation manifest, snapshot, method version and report IDs, and was filled in
from the user's written authorization.

The authorization gave the reviewer name as the unfilled placeholder
`[YOUR NAME]`. The repository account that issued it, `JiaqiWang426`, is recorded
instead, with a `reviewer_note` in each file. Correct it if a different reviewer
name should appear.

**Credentials.** Writes used the separate Compose maintenance login `citylens`.
It was read from `.env` into the child process environment only and was never
printed, logged or committed. The Agent login `citylens_agent` performed only reads.

**Runs.** Every run used the existing CLI (`deployment` and `final-deployment`)
with transactions of 50 rows. A runner, [deploy_runner.py](deploy_runner.py),
performed a read-only approval-bound preflight before each batch and a full
invariant check after each batch, and would stop on any failure. Its full log is
in [deployment-log.json](deployment-log.json).

| Order | Approval file | Reports | Run ID (prefix) | Result |
| ---: | --- | ---: | --- | --- |
| 1 | stage-c-approval-01 | 128 | b19cd0c0b7cf | passed; applied the approved precision migration |
| 2–5 | stage-c-approval-02 … 05 | 996 each | eaa357813…, e8de75566…, 2112de6c1…, 366bd19e8… | passed |
| 6 | final-approval-01 | 100 | 1252d77c9ac3 | passed |
| 7–11 | final-approval-02 … 06 | 846, 846, 846, 846, 845 | 8f46dc264…, 935f32216…, b98b581cb…, f93b87455…, b6da4eb48… | passed |

**Interruption and fix.** The first approval-bound preflight for Stage C batch 1
stopped before any write with "Candidate differs from local topology replay".

- **Cause:** the Stage B approval-time replay in `apply.py` resolved each pair in the report's primary/cross order. Stage B had evaluated and stored pairs in canonical sorted order, so 10 of the 128 reports had swapped primary and cross segment lists.
- **Fix:** the replay now resolves in that same canonical order. The comparison stays exact, comparing node, coordinates and segment lists to 1e-10.
- **Regression test:** a reversed-orientation report was added to the Stage C rollout test. That test fails on the old code and passes on the fix.
- **Resumption:** the rollout resumed from batch 1. No batch was re-applied, and no rows were skipped or rejected.

**Metadata function.** After deployment, only `sql/tools/050_get_dataset_metadata.sql`
was re-applied with `CREATE OR REPLACE`, in one transaction, as the maintenance
login. Before the change, the live definition differed from the file only in two
caveat strings. Function identity and grants are unchanged, and the stored
definition equals the repository file.

## Integrity results

All checks below come from live SQL using the read-only Agent login. They are in
[deployment-verification.json](deployment-verification.json), reproducible with
`deployment_checks.py`.

| Check | Result |
| --- | --- |
| Records deleted or duplicated | none; 78,494 unique report IDs |
| Original source fields | unchanged; digest equals the pre-deployment baseline |
| Original 59,627 Census matches | all still located and byte-identical. Every row not journaled as updated equals its pre-deployment md5. |
| Status and located counts | reconcile exactly with journaled original statuses: 4,132 Pending, 3,767 no-match, 542 ambiguous |
| Journal | 11 runs, 8,441 `updated` rows, no duplicate or rejected rows. The located increase equals the journal count. |
| New rows outside approved allowlists | 0 |
| Excluded or unknown candidates located | 0, for both Stage C and final exclusions |
| WGS84 geometry | 0 invalid, wrong-SRID or out-of-area points among all 68,068 located |
| Precision and provenance | 0 located rows without precision, provider or details. All 8,441 new rows carry Stage C or final `validation_status=pass` with a matching provider and precision. |
| Prior evidence | preserved under `prior_geocoding` for every updated row. The original Census reason is kept for all 542 ambiguous rows. |
| Artifact hashes and snapshot | Stage C and final artifact verifiers report no problems, and the SanGIS snapshot is unchanged |
| Agent permissions | no update, insert or delete on collisions; reference schema and tables read-only |
| Precision constraint | allows `block_approximate` and `intersection_approximate` only |

## Agent verification

**Tests.** The full backend suite, including all opt-in throwaway-PostGIS
integration tests, gave **482 passed, 0 failed, 0 skipped**. The existing 400 m
tests are unchanged and pass.

**Real-data queries.** These went through the actual Agent tool interface,
`validated_calls` plus `DatabaseTools` as the restricted reader. Each pin covered
400, 800 and 1,000 m and two time windows. Every case shows:

- **Exact counts:** the tool count equals an independent `ST_DWithin` PostGIS count.
- **Determinism:** repeated runs return the same count and the same page.
- **Clean pages:** retrieved report IDs are unique, all inside the radius and window, and all located. Unlocated reports never appear.

The three pins are deterministic: one SanGIS road-block point, one SanGIS
intersection point and one Census point.

| Pin | Radius | Window | Count | Census | SanGIS block | SanGIS intersection |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| SanGIS road block (report 26204236) | 800 m | 2017–2026 | 634 | 532 | 57 | 45 |
| same | 1,000 m | 2017–2026 | 927 | 797 | 57 | 73 |
| same | 800 m | 2023 | 77 | 59 | 3 | 15 |
| SanGIS intersection (report 25207193) | 800 m | 2017–2026 | 331 | 307 | 0 | 24 |
| same | 1,000 m | 2017–2026 | 384 | 356 | 0 | 28 |
| Census block (report 25204137) | 400 m | 2017–2026 | 710 | 616 | 18 | 76 |
| same | 800 m | 2017–2026 | 2,478 | 2,114 | 113 | 251 |
| same | 1,000 m | 2017–2026 | 3,416 | 2,937 | 128 | 351 |

A two-location comparison at 800 m returned 634 and 331, the same values as the
single-pin queries.

**Caveats and metadata.** Collision evidence is labelled
`approximate_block_or_intersection`, and its caveats state the following:

- **Unlocated reports:** they are excluded, and their number inside any radius cannot be determined.
- **Positions:** coordinates are road-block or intersection approximations, never exact crash positions, and radius membership is uncertain near boundaries.
- **Meaning of counts:** counts are police-recorded reports, not risk or safety measures.

The metadata tool now reports:

- **Coverage:** 78,494 records, 68,068 located, coverage 0.8672.
- **Statuses:** the full status breakdown.
- **Precision breakdown:** 63,936 `block_approximate`, 4,132 `intersection_approximate`, 10,426 not recorded.
- **Spatial accuracy:** the new text naming Census or SanGIS road blocks and SanGIS junctions.

**Radius.** The product default was already 800 m, in the request model, the
location flow and the frontend form. It was not changed, and explicit user radii
are preserved. The Agent's existing 3,660-day maximum time range bounds the long
test window to 2017–2026.

## Remaining limitations

- **Unlocated reports:** 10,426 reports stay unlocated by design. They are 4,218 Pending (no block number or cross street, unresolved intersections, or Stage C exclusions), 3,633 no-match and 2,575 ambiguous. Reasons are in `geocoding_results.csv`. They are excluded from every radius count.
- **Approximate positions:** all new points are approximations on the 2026 SanGIS network. A road-block point represents up to 400 m of road, with a stored uncertainty radius of up to about 200 m. Collision-date road geometry is not verified, and TIGERweb corroboration is not independent ground truth.
- **Uneven coverage:** coverage remains uneven, for example Northwestern Division far below Mid-City. Counts describe imported, located police reports, not complete accident incidence or neighborhood safety.
- **No historical geocoding work remains.** Further recovery would need new, independently documented evidence, such as an official Spanish street-type abbreviation table, and a separate review.
