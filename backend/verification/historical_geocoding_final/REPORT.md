# Final historical collision geocoding — results, validation and deployment gate

> **Update, November 3, 2026:** both allowlists were deployed under explicit approval.
> 8,441 reports were updated with 0 rejected, and actual coverage is now 68,068 / 78,494 (86.72%).
> See [DEPLOYMENT_REPORT.md](DEPLOYMENT_REPORT.md). The statements below about pending
> approval describe the state before deployment.

Completed November 3, 2026, America/Los_Angeles. Every unlocated collision report
(18,867) now has a reproducible outcome. Two strict allowlists are ready for
review:

- **Stage C intersections:** 4,112 Pending reports.
- **Final allowlist:** 4,329 reports, of which 3,767 were no-match, 542 ambiguous and 20 Pending.

**Nothing has been deployed.** No approval file and no maintenance credentials
exist. Actual coverage is unchanged at **59,627 / 78,494 = 75.96%**. If both
allowlists are approved and applied, coverage becomes **68,068 = 86.72%**.
**10,426 reports stay unlocated**, each with a recorded reason.

## 1. Baseline, read from the database

The live table was read with the read-only Agent login before any analysis. It
matches the Stage B and Stage C before-states byte for byte.

| Status | Records |
| --- | ---: |
| matched | 59,627, all `block_approximate` from the Census provider |
| no_match | 7,400 |
| ambiguous | 3,117 |
| pending | 8,350 |
| **Total** | **78,494** |

## 2. Stage C intersection deployment (Phase 1)

- **Readiness:** the read-only preflight confirms that all artifact hashes are valid. All 4,112 allowlisted reports are still Pending, with no coordinate and unchanged source fingerprints. The whole table is unchanged, and the Agent is read-only.
- **Deployed:** none, because no approval and no maintenance credentials exist. No approval was created.
- **Commands:** the prepared artifacts and commands are in section 8. Stage C batches can now run before, after or between final-allowlist batches, using the shared per-row baseline described in section 7.

## 3. Method

Every step is read-only and deterministic. A rerun from cache reproduces the
validation manifest `1b500293…81f32b` exactly.

**Road blocks** apply to no-match and ambiguous reports.

- **Block semantics:** the source number is an anonymized **block** `N..N+99`, never a house number. Numbers that are not multiples of 100, offset or compound descriptions (`FT`, `OF`, `NCL`, `&`, `/`) and freeway or ramp descriptions are rejected unreinterpreted. Real names such as I ST, PACIFIC HWY and QUARTER MILE DR are kept.
- **Names:** the source canonical street must equal a SanGIS segment's own `rd30full`, with consistent components. The alternative is SanGIS's official `rd20full` abbreviation **on the same segment record**, for example BLACK MTN RD for BLACK MOUNTAIN RD. No other alias or fuzzy rule exists. Directions and suffixes always matter.
- **Candidate segments:** each must lie in the City of San Diego, have a left or right address range overlapping the block, and be Constructed or Maintained. Freeways, ramps and paper streets are excluded. The block-level number makes parity and side not applicable.
- **One location only:** compatible segments must form one cluster, joined by shared node IDs or lying within 60 m of each other, as divided carriageways do. Separated clusters are `multiple_candidates`. The cluster's extent must be at most **400 m**, otherwise the record is `range_ambiguous`. The point lies on the road geometry closest to the block centroid. Extent and uncertainty radius are stored, distant segments are never averaged, and parcels or Address Points are never used.

**Report checks:**

- **Police beat:** the report's beat must equal a SanGIS beat of the block. 99% do.
- **TIGERweb:** an exact official road name must lie within 30 m of the point. There were 1,446 identify requests, one per distinct point, within a 4,000-attempt budget. All responses are cached and checksummed.
- **Census evidence, for ambiguous reports, read but never edited:**
  - A tie gives no evidence either way.
  - A returned number in a different block is a `number_conflict`, never repaired.
  - A materially different street is a `street_conflict`, for example W versus E MISSION BAY DR, HOTEL CIR S versus N, or KEARNY versus KEARNEY.
  - A same-street or documented-equivalent match must put the Census point within 100 m of the block.
- **Source fingerprint:** the source fingerprint is preserved for every report.

**Pending triage:**

- **Reused outcomes:** Stage B and Stage C outcomes are reused, and nothing is re-resolved except Stage B `street_not_found` pairs.
- **Abbreviation retry:** only those `street_not_found` pairs are retried through the official `rd20full` abbreviation. They then pass Stage C's strict topology and TIGER checks.
- **Not reprocessed:** reports without a positive block number and without a cross street are classified, not processed.

**Calibration against existing Census matches.** The unchanged method was run on
3,000 matched reports chosen deterministically, and the Census point was compared
with the SanGIS block accepted for the same report.

| Accepted blocks | Median distance | 95th percentile | Share over 100 m |
| ---: | ---: | ---: | ---: |
| 2,531 | 6.4 m | 52.6 m | 2.65% |

The method reproduces the provider's block in about 97% of cases. Long blocks of
200–400 m and divided roads do not perform worse. Disagreement does not show which
source is wrong. Details are in [method-validation.json](method-validation.json).

## 4. Outcomes

**No-match records:** 7,400 evaluated, **3,767 accepted**.

| Outcome | Reports |
| --- | ---: |
| accepted_candidate | 3,767 |
| range_ambiguous (block over 400 m) | 1,614 |
| insufficient_evidence | 1,171 |
| street_not_found | 640 |
| invalid_source | 152 |
| source_conflict, mostly beat disagreement | 42 |
| multiple_candidates | 14 |

`insufficient_evidence` is mostly a block outside the city's address ranges or a
TIGER name that could not be confirmed. `invalid_source` covers offsets, freeways
and unabstracted numbers.

**Ambiguous records:** 3,117 evaluated, **542 accepted**. The original stored
reasons map as follows:

| Outcome | Reports |
| --- | ---: |
| accepted_candidate: Census ties, resolved by one unique city block, beat and TIGER | 335 |
| documented_alias_resolved: official abbreviation; Census point, when present (196), within 93 m of the block | 207 |
| street_conflict | 1,029 |
| insufficient_evidence | 1,307 |
| unsupported_description (offsets) | 228 |
| multiple_candidates | 11 |

Every row of [geocoding_results.csv](geocoding_results.csv) keeps its original
status and stored Census reason. Number conflicts are counted inside the
insufficient and conflict rows above.

**Pending records:** 8,350 evaluated, 4,112 routed to Stage C and **20 new**
accepted.

| Outcome | Reports |
| --- | ---: |
| Stage C intersection allowlist | 4,112 |
| New official-abbreviation intersections accepted | 20 of 32 candidates |
| Stage C excluded | 207 |
| Stage B unresolved: street not found, uncertain, multiple, no intersection, invalid | 2,418 |
| Official-abbreviation retry rejected by topology | 71 |
| No positive block and no cross street | 1,510 |

**Primary exclusion reasons**, from [summary.json](summary.json):

| Reason | Reports |
| --- | ---: |
| Block extent over 400 m | 2,136 |
| No exact or official name | 1,174 |
| No positive block number | 1,089 |
| Census matched a different street | 1,029 |
| Stage B street not found | 1,005 |
| Block outside city ranges | 785 |
| Offset or compound description | 671 |

Spanish street-type abbreviations returned by Census, such as PASEO/PSO,
AVENIDA/AVE and CALLE/CLL, were **not** accepted as aliases. No record-level
official pairing was available, so those reports remain `street_conflict`.

## 5. Quality review

A bounded sample of 110 decisions covers every outcome, name source, extent band,
police division and year. Its findings are in
[quality-review.json](quality-review.json). **No systematic false-acceptance
pattern was found.**

- **Passing blocks:** every one had an agreeing beat, a TIGER name within 30 m and a single compact city cluster.
- **Rejected samples:** every rejection was correct.
- **Ambiguous acceptances:** 196 have a documented-equivalent Census point, all within 93 m of the block. The other 346 were Census ties without candidate coordinates, accepted only because exactly one compact city block exists, and the beat and TIGER name agree. Census never chose among them.

Limits:

- **Long blocks:** 795 allowlisted blocks exceed 300 m, so a point can be about 200 m from the block ends.
- **Abbreviation-based names:** 249 allowlisted reports rely on official abbreviations, which the matched-Census calibration cannot test. They rely on TIGER name corroboration and beat agreement instead.

The review is an AI-assisted inspection of digital evidence, not a deployment
approval.

## 6. Coverage: actual versus projected

| | Located | Coverage |
| --- | ---: | ---: |
| **Actual, now** | 59,627 | **75.96%** |
| Projected with Stage C applied | 63,739 | 81.20% |
| Projected with both allowlists applied | 68,068 | 86.72% |

Projected coverage by collision year:

| Year | Actual | With both allowlists |
| --- | ---: | ---: |
| 2016 | 80.4% | 90.4% |
| 2018 | 80.8% | 90.6% |
| 2020 | 76.3% | 85.4% |
| 2022 | 74.5% | 85.8% |
| 2024 | 73.8% | 85.5% |
| 2026 | 72.2% | 85.8% |

Recent years remain about 4–5 points behind 2016 and 2018. Projected coverage by
police division, using the report's own beat rather than a guessed coordinate:

| Division | Actual | With both allowlists |
| --- | ---: | ---: |
| Northwestern | 46.8% | 66.2% |
| Northern | 64.9% | 83.8% |
| Southern | 66.1% | 79.8% |
| Northeastern | 69.9% | 79.0% |
| Central | 77.4% | 88.9% |
| Eastern | 80.4% | 88.5% |
| Western | 81.5% | 89.7% |
| Southeastern | 84.6% | 90.9% |
| Mid-City | 88.7% | 94.2% |

**The data are not geographically unbiased.**

- **Divisions:** Northwestern, Southern and Northeastern stay far behind Mid-City. Long suburban and rural blocks and long Spanish-named streets are excluded more often.
- **Description types:** intersection-only reports without a block, offsets and freeway descriptions remain unlocated everywhere.

By source, the deployed points are currently 59,627 Census `block_approximate`.
After approval they would add:

- **SanGIS intersections:** 4,132 `intersection_approximate`, all from SanGIS local topology.
- **SanGIS road blocks:** 4,309 `block_approximate`, from the SanGIS road centerline.

## 7. Deployment safety and Agent behavior

**Deployment.** The existing guarded mechanism was extended, not replaced. In
`apply.py`, `deploy_final` is a status-specific path:

- **Approval binding:** the approval names `allowed_original_statuses`, exact IDs, the final manifest and allowlist hashes, and three acknowledgements. Each row must still hold that exact original status, a NULL location and its source fingerprint, so matched rows can never qualify.
- **Replay:** every approved coordinate is recomputed from the local reference before any write.
- **Shared safeguards:** it reuses the journal tables, advisory lock, bounded transactions, append-only history and per-batch invariants.
- **Preserved evidence:** the original Census evidence is preserved, nested under `prior_geocoding` and in the history table.
- **Stage C:** its behavior is unchanged.
- **Per-row baseline:** `before-row-digests.json.gz` lets both rollouts interleave. Any row change not journaled by a guarded run stops every later batch.

**Agent behavior:**

- **SQL tools unchanged:** they remain deterministic PostGIS and already count every located row, so new points appear in radius counts automatically once deployed.
- **Precision labels:** the evidence label, caveat and workflow text now say "approximate (road block or intersection)" instead of block-approximate only.
- **Metadata function:** the repository's `050_get_dataset_metadata.sql` text was updated. It takes effect on the production database only after the tool SQL is re-applied, which was not done here.
- **Provenance:** `geocode_provider` and `geocode_precision` distinguish Census blocks, SanGIS blocks and SanGIS intersections.

## 8. Pending approval: exact artifacts and commands

There are two independent approvals. Copy each template outside the repository,
fill it in and keep it there. Templates have deliberately invalid decision values.

- **Stage C:** [../intersection_stage_c/approval-template.json](../intersection_stage_c/approval-template.json). Fields are listed in the Stage C report. The first batch is the 128 reports in its `first-batch-proposal.json`. The first apply needs `allow_precision_migration: true`.
- **Final:** [approval-template.json](approval-template.json). The reviewer must set:
  - `decision`: `approve_apply`
  - `reviewer` and `approved_at`
  - `allowed_original_statuses`, for example `["no_match","ambiguous","pending"]`
  - `report_ids`
  - the three acknowledgement flags: `true`
  - `historical_review_basis`
  - `allow_precision_migration`: `true`, if any intersection entry is approved before Stage C has migrated the constraint

  The template already binds the method `citylens-final-historical-v1`, the snapshot, the manifest `1b500293…81f32b` and the allowlist hash. The first batch is [first-batch-proposal.json](first-batch-proposal.json), 100 reports including all inspected sample entries.
- **Order:** [rollout-plan.json](rollout-plan.json) gives 11 batches of at most 1,000 reports. Apply Stage C batch 1 first, verify, then continue one approved batch at a time.

```powershell
# Read-only checks (no approval needed / approval-bound preflight)
.\.venv\Scripts\python.exe -m backend.sangis_topology stage-c-preflight
.\.venv\Scripts\python.exe -m backend.sangis_topology deployment --approval 'E:\review\stage-c-approval-1.json'
.\.venv\Scripts\python.exe -m backend.sangis_topology final-deployment --approval 'E:\review\final-approval-1.json'

# FUTURE applies, ONLY with explicit approval files and a separate maintenance identity
$env:SANGIS_MAINTENANCE_DATABASE_URL = 'postgresql://MAINTENANCE_USER:URL_ENCODED_PASSWORD@127.0.0.1:54329/citylens'
.\.venv\Scripts\python.exe -m backend.sangis_topology deployment --approval 'E:\review\stage-c-approval-1.json' --apply --batch-size 50
.\.venv\Scripts\python.exe -m backend.sangis_topology final-deployment --approval 'E:\review\final-approval-1.json' --apply --batch-size 50
```

After each batch, run the read-only preflights again and these checks:

```powershell
docker exec sandiego_citylens-db-1 psql -U citylens -d citylens -c "SELECT geocode_status, geocode_provider, geocode_precision, count(*) FROM citylens.traffic_collisions GROUP BY 1,2,3 ORDER BY 1,2,3;"
docker exec sandiego_citylens-db-1 psql -U citylens -d citylens -c "SELECT run_id, outcome, count(*) FROM citylens_ref.geocode_apply_history GROUP BY 1,2;"
```

The checks should show:

- **Counts:** matched rises, and each original status falls, by exactly the updated count.
- **Rejections:** the journal shows no unexpected rejections.
- **Drift:** the preflights report no drift.

The appliers stop on any drift. The journal is append-only, and reruns skip
committed reports.

## 9. Reproduction, tests and limitations

```powershell
.\.venv\Scripts\python.exe -m backend.sangis_topology final-geocoding           # offline replay from cache
.\.venv\Scripts\python.exe -m backend.sangis_topology final-geocoding --live    # only missing TIGER scopes, persisted budget
.\.venv\Scripts\python.exe -m backend.verification.historical_geocoding_final.verify_artifacts
$env:AGENT_TEST_DOCKER = '1'
.\.venv\Scripts\python.exe -m pytest backend/agent_tests/test_intersection_feasibility.py backend/agent_tests/test_sangis_topology.py backend/agent_tests/test_stage_c.py backend/agent_tests/test_final_geocoding.py -q
```

**Tests:**

- **Focused suites:** 141 passed, including a disposable-PostGIS end-to-end test.
- **Broader Agent regression:** 443 passed, 39 opt-in skips.

The end-to-end test covers:

- **Classification:** road-block outcomes and Census conflicts, with deterministic replay.
- **Approval gate:** refusals for templates, wrong status scope, excluded reports and tampered hashes.
- **Rollout:** status-specific no-match and ambiguous batches, recovery after interruption, idempotent reruns, and interleaving with a Stage C batch. An outside edit blocks further writes.
- **Agent:** 400 m and 800 m counts that include new block and intersection points, through a read-only Agent login.

**Artifacts:**

- **Results:** [geocoding_results.csv](geocoding_results.csv), [accepted_candidates.csv](accepted_candidates.csv), [excluded_candidates.csv](excluded_candidates.csv), and [deployment_allowlist.csv](deployment_allowlist.csv) with its `.sha256`.
- **Summary and manifests:** [summary.json](summary.json), [validation-manifest.json](validation-manifest.json) and [source-manifest.json](source-manifest.json).
- **Baseline:** [before-state.json](before-state.json) and the per-row baseline.
- **Calibration and plan:** method validation, quality sample and review, and the plan with its budget.
- **Cache:** TIGER responses are kept in ignored `backend/reference_data/historical_geocoding_final/`.

**Limitations:**

- **Present-day reference:** coordinates are approximate positions from October 2026 SanGIS geometry. They are not exact or collision-date positions.
- **Representative points:** a block point represents up to 400 m of road.
- **Shared lineage:** TIGER corroborates names, not independent surveys.
- **No incidence claim:** police reports are not complete accident incidence.
- **Unlocated reports:** the 10,426 unlocated reports stay excluded from every radius count, and their number within any radius is unknown.
