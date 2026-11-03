# Stage C — conservative intersection validation and deployment preparation

Completed November 2, 2026, America/Los_Angeles. Every one of the 2,043 intersection
pairs that Stage B accepted now has a reproducible validation outcome.
**4,112 of the 4,319 proposed collision reports (1,925 pairs) passed every mandatory
check and form the deployment allowlist.** The allowlist is ready for separate human
review. **No collision record was updated.** The real `--apply` command was not run.

The [Stage A](../intersection_feasibility/REPORT.md) and
[Stage B](../intersection_batch/REPORT.md) reports, the
[maintenance package README](../../sangis_topology/README.md) and the existing
resolver, TIGERweb check and guarded deployment code were reviewed first. Stage C
reuses the imported SanGIS snapshot `0aa3c4d4…6fe9fc` and the Stage B artifacts,
verified against the Stage B run manifest. Nothing was downloaded or reimported,
and the Stage B candidate dataset was not regenerated.

## 1. Outcome

| Outcome | Pairs | Reports |
| --- | ---: | ---: |
| `pass`: eligible for deployment | 1,925 | **4,112** |
| `fail`: material validation conflict | 28 | 77 |
| `unknown`: evidence missing or incomplete | 90 | 130 |
| `provider_error`: external validation not completed | 0 | 0 |
| Total Stage B accepted proposals | 2,043 | 4,319 |

Only `pass` is deployable. `fail`, `unknown` and `provider_error` are excluded, each
with its exact reason preserved. An exclusion does not mean the proposed coordinate
is geographically wrong. Most exclusions are missing corroboration, not contradiction.
As instructed, no excluded case was retried through fuzzy matching, aliases,
alternative address readings or manual repair.

## 2. What was checked

Every pair and report had to satisfy all three groups. Status precedence is
`fail` > `provider_error` > `unknown` > `pass`.

**A. SanGIS topology.** The Stage B resolver was replayed locally for all 2,043
pairs, which also requires identical node, coordinates, segment IDs, levels and
reasons. PostGIS was then queried directly for each node and its complete incident
segment set:

- **Node identity:** exactly one node record for the INTERID, with a positive ID, a GLOBALID, Type I and valid geometry. No other node record lies within 1 cm.
- **Incidence and roads:** both roads are incident, and there are at least three incident segments. Every incident segment is Constructed or Maintained, in a Stage A supported class, valid, simple, single-part, and overlaps no other segment. Its far endpoint resolves to exactly one node.
- **Alignment and levels:** the junction endpoint lies within 1 cm of the node, which is far stricter than the inherited 2 m consistency ceiling. Endpoint levels are compatible.
- **Pair-level conflicts:** no topology diagnostics, no grade-separation evidence anywhere in the pair, and no second shared node, even one Stage B had definitively rejected.

**B. Independent road corroboration.** The independent source is the official US
Census TIGERweb Transportation service, layers 2, 6 and 8, queried with exactly the
Stage A/B identify request around the SanGIS node. Pass requires both requested
roads to have exact-canonical TIGER names, with each named line within 30 m of the
node. Canonicalization is the existing Census token rules, including the documented
USPS suffix and direction equivalences. No new alias was introduced.

- A named road present but more than 30 m away is a **fail**.
- An explicitly different direction at the node, such as N versus E, is a **fail**.
- A name absent from the response is **unknown**.
- A truncated response, one with 1,000 or more results, or an unconfirmed CRS is **unknown**.
- Transport or provider failures are **provider_error**.

The 30 m threshold is the inherited Stage A/B diagnostic, used here as an
acceptance condition. It corroborates that both names exist next to the junction.
It does not replace the topology checks, does not detect grade separation, and is
not a measurement of where a collision happened.

**C. Completeness and provenance.**

- **Stage B evidence:** each report has complete road and node evidence, the reviewed snapshot and method version, and `intersection_approximate` precision.
- **Source fingerprint:** the Stage B SHA-256 source fingerprint must equal a fingerprint recomputed from the current database row.
- **Current state:** the row must still be Pending with no location, and stored streets must agree with the original components.
- **Qualifiers:** the block-number qualifier must be absent or `0`. All 4,319 records had `0`.
- **Cached evidence:** each cached response is verified against its ledger SHA-256 on every read.

All 4,319 reports passed provenance.

## 3. Why candidates were excluded

| Reason | Pairs | Reports |
| --- | ---: | ---: |
| TIGER does not contain one requested road name near the node (unknown) | 91 | 131 |
| A named TIGER road lies more than 30 m from the node (fail) | 22 | 59 |
| Incident segment 5645 has a dangling far endpoint (fail) | 1 | 11 |
| Another shared node exists, even though rejected (fail) | 4 | 4 |
| Grade-separation evidence in the pair (fail) | 2 | 2 |
| Different explicit direction at the node (fail) | 1 | 3 |

A pair or report can carry more than one reason, so the rows overlap. One missing-name
pair also has a topology failure, which is why 91 missing-name pairs yield 90 `unknown`
pairs. The missing names are mostly Spanish-prefix names abbreviated differently in
TIGER (AVENIDA, CAMINITO, VIA and similar, 54 road occurrences), named freeway ramps
that TIGER leaves unnamed (12), and other spelling differences. Real disagreements
include HOTEL CIR N, where TIGER names HOTEL CIR S at the node, and PARK BLVD, where
TIGER names N 12TH AVE. CARMEL VISTA RD, 46 m away, also remains excluded.
Per-report reasons are in [excluded_candidates.csv](excluded_candidates.csv).

## 4. Independent evidence and quality gate

All 1,974 distinct node positions were evaluated: one identify request per position,
shared by pairs that meet at the same node. Every scope has a complete response.

| Evidence origin | Scopes |
| --- | ---: |
| Reused from the Stage B independent review cache | 62 |
| Reused from an earlier, interrupted Stage C capture | 1,910 |
| New live requests in this run | 2 |

The documented budget was 400 live attempts, with at most two attempts per scope and
a 0.5 s request interval. The plan estimated 2 new requests before live execution.
Earlier responses were reused only when the URL was identical and the file matched
its original ledger SHA-256. That earlier capture came from a Stage C implementation
no longer present in the repository. Its parsed response was also checked against
its stored raw body. Its ledger and responses are kept in the ignored
`backend/reference_data/intersection_stage_c/prior-interrupted-run/`.

**Quality sample.** The sample has 64 passed pairs and 128 reports. It was selected
by deterministic round robin over every observed police division, every collision
year from 2016 to 2026, all four road-class groups, directional names and complex
names. Findings are recorded in [quality-review.json](quality-review.json):

- **Distance:** 59 of 64 pairs have both named roads within 1 m of the node. The largest distance is 9.1 m.
- **Geometry:** in all 64 pairs the two TIGER named lines touch or cross.
- **Population screen:** automated flags on all passed pairs were reviewed. Ramp and state-route junctions (45 pairs) are at-grade ramp terminals or surface highways, all at level 1, with no freeway mainline. Near-parallel cases (35 pairs) are Y-junctions or name changes at a junction.

**No systematic false-positive pattern was found.**

Two observations limit what this proves:

- **Shared lineage:** 1,756 of the 1,925 passed pairs have both TIGER lines within 0.1 m of the SanGIS node. That agreement indicates TIGER road geometry here shares lineage with local centerlines. It is strong name corroboration across two published products, not an independent survey.
- **Same-street pairs:** eight passed pairs, with 12 reports, name the same street with and without a directional prefix, such as BROADWAY and W BROADWAY. The point is the unique place where the two named segments meet. Whether the reporting officer meant that point is a reporting-semantics limitation.

## 5. Coverage

| Measure | Records | Coverage of 78,494 |
| --- | ---: | ---: |
| Currently located, deployed | 59,627 | 75.9638% |
| If the entire allowlist were deployed | 63,739 | 81.2024% (+5.2386 points) |
| Stage B proposal ceiling, for comparison | 63,946 | 81.4661% |

These are projections. Nothing in this table is deployed, and no sample rate was
extrapolated: every pair has its own result. Exclusions do not concentrate in time.
Every year from 2016 to 2026 passes between 84% and 99% of its proposals; 2017 has
only 19 proposals. Northeastern Division has the most unknowns (35 of 293),
reflecting Spanish-prefix street names. Full year and division tables are in
[summary.json](summary.json).

No 7,400 No-match or 3,117 Ambiguous record, no existing Census coordinate, and no
Pending record outside the Stage B accepted set was evaluated for deployment.

## 6. Deployment readiness

**The allowlist is ready for separate review and explicit approval. It is not
approved.** The read-only preflight confirms that all 4,112 allowlisted reports
are still Pending without coordinates, with unchanged source fingerprints. The whole
collision table matches the Stage C before-state, and the Agent's reference access
is still read-only. No production write was performed: the precision constraint
still allows only `block_approximate`, and no deployment journal tables exist.

The existing Stage B deployment mechanism was reused, not replaced. Its approval now
also binds Stage C:

- **Allowlist binding:** each approved ID must be an identical `pass` allowlist entry, with the same node, coordinates, segments and fingerprint.
- **Integrity:** the Stage C manifest and allowlist hashes must match.
- **Quality gate:** the quality review must be current.
- **Provenance:** every update stores its Stage C evidence.
- **Staged rollout:** the first-batch global baseline check now accepts later batches. The database may differ from the Stage C before-state only by guarded updates already journaled for allowlisted reports. Any other change blocks deployment.

**Approval document.** Copy [approval-template.json](approval-template.json) to a
location outside the repository, for example
`E:\review\stage-c-approval-batch-1.json`. The template's decision value is
deliberately invalid. The reviewer must set:

- `decision`: `approve_apply`
- `reviewer` and `approved_at`, with a timezone offset
- `report_ids`: an explicit list of IDs. The suggested first batch is the 128 reports at the 64 inspected pairs, listed in [first-batch-proposal.json](first-batch-proposal.json).
- `acknowledge_temporal_uncertainty`, `acknowledge_independent_validation_limitations` and `acknowledge_intersection_approximate_precision`: all `true`
- `historical_review_basis`: the reviewer's acceptance rationale
- `allow_precision_migration`: `true`. This is required for the first apply, which runs `deploy.sql` so the column accepts `intersection_approximate`.

The template already holds the Stage B candidate, run-manifest and independent-summary
hashes, the snapshot, method and validation versions, and the Stage C manifest hash
`5692f7a6…e8632c` and allowlist hash `5cc1ae3c…e00f8`. No approval was generated.

Commands, from the repository root in PowerShell:

```powershell
# Read-only, no approval needed: allowlist eligibility and database drift
.\.venv\Scripts\python.exe -m backend.sangis_topology stage-c-preflight

# Read-only, approval-bound preflight (writes intersection_stage_c\deployment-preflight.json)
.\.venv\Scripts\python.exe -m backend.sangis_topology deployment --approval 'E:\review\stage-c-approval-batch-1.json'

# FUTURE apply, ONLY after explicit approval; separate maintenance identity
$env:SANGIS_MAINTENANCE_DATABASE_URL = 'postgresql://MAINTENANCE_USER:URL_ENCODED_PASSWORD@127.0.0.1:54329/citylens'
.\.venv\Scripts\python.exe -m backend.sangis_topology deployment --approval 'E:\review\stage-c-approval-batch-1.json' --apply --batch-size 50
```

Suggested first batch: the 128 inspected-pair reports, committed in transactions of 50.
After verification, later approvals can cover larger allowlist slices, such as 500 to
1,000 reports each.

Post-deployment verification:

```powershell
.\.venv\Scripts\python.exe -m backend.sangis_topology stage-c-preflight
docker exec sandiego_citylens-db-1 psql -U citylens -d citylens -c "SELECT geocode_status, geocode_precision, count(*) FROM citylens.traffic_collisions GROUP BY 1,2 ORDER BY 1,2;"
docker exec sandiego_citylens-db-1 psql -U citylens -d citylens -c "SELECT run_id, outcome, count(*) FROM citylens_ref.geocode_apply_history GROUP BY 1,2;"
docker exec sandiego_citylens-db-1 psql -U citylens -d citylens -c "SELECT count(*) FROM citylens.traffic_collisions WHERE geocode_precision='intersection_approximate' AND geocode_details->'stage_c'->>'validation_status' IS DISTINCT FROM 'pass';"
```

After a correct batch:

- **Preflight:** it reports the applied IDs as `not_pending_or_already_located` and `non_allowlist_rows_unchanged: true`.
- **Counts:** Pending falls and matched rises by exactly the updated count.
- **Provenance query:** the last query returns 0.
- **Result file:** the apply also writes `apply-result.json` to the Stage B directory.

## 7. Limitations

- **Present-day topology:** SanGIS is an October 2026 snapshot, while collisions date from June 2016 to October 2026. Present-day topology does not prove that a junction existed or looked the same on the collision date.
- **Approximate positions:** coordinates are `intersection_approximate`. They mark a reference road junction, not where the collision happened.
- **Limited independence:** TIGER corroborates names and position but is not fully independent of SanGIS, and the 30 m diagnostic cannot detect a grade separation that both sources share. The strict level and node checks are the safeguard against that.
- **Not a field survey:** the quality sample is a bounded, stratified inspection of digital evidence, not a field, aerial or historical survey. It supplies no population error rate.
- **Unrecovered candidates:** the 207 excluded reports stay Pending. Many are probably correct, but recovering them was out of scope.

## Artifacts and reproduction

- **Outcomes:** [validation_results.csv](validation_results.csv) holds all 4,319 report outcomes, with pair, topology, independent and provenance status, reasons and evidence hashes.
- **Allowlist:** [deployment_allowlist.csv](deployment_allowlist.csv) has its checksum in [deployment_allowlist.sha256](deployment_allowlist.sha256). [excluded_candidates.csv](excluded_candidates.csv) lists the exclusions.
- **Per-pair evidence:** [pair-validation.json](pair-validation.json) has full per-pair evidence. [evidence-index.json](evidence-index.json) maps each scope to its cached response and SHA-256.
- **Summary and plan:** [summary.json](summary.json) and [verification-plan.json](verification-plan.json), which records the frozen budget and estimate.
- **Manifests and state:** [validation-manifest.json](validation-manifest.json) hashes the deployable artifacts. [provenance-manifest.json](provenance-manifest.json) covers Stage B inputs, snapshot, source and implementation hashes. [before-state.json](before-state.json) holds the database fingerprints.
- **Quality:** [quality-sample.json](quality-sample.json) and [quality-review.json](quality-review.json).
- **Deployment preparation:** [approval-template.json](approval-template.json), [first-batch-proposal.json](first-batch-proposal.json) and [allowlist-preflight.json](allowlist-preflight.json).
- **Cache:** the ignored folder `backend/reference_data/intersection_stage_c/` holds every TIGER response and the request ledger.

```powershell
# Offline replay from cache (no network): regenerates byte-identical hashed artifacts
.\.venv\Scripts\python.exe -m backend.sangis_topology stage-c
# Live capture of any missing scopes within the persisted 400-attempt budget
.\.venv\Scripts\python.exe -m backend.sangis_topology stage-c --live
# Offline artifact consistency check (no database, no network)
.\.venv\Scripts\python.exe -m backend.verification.intersection_stage_c.verify_artifacts
# Tests; the second run adds the disposable PostGIS rollout on port 54331
.\.venv\Scripts\python.exe -m pytest backend/agent_tests/test_intersection_feasibility.py backend/agent_tests/test_sangis_topology.py backend/agent_tests/test_stage_c.py -q
$env:AGENT_TEST_DOCKER = '1'
.\.venv\Scripts\python.exe -m pytest backend/agent_tests/test_stage_c.py backend/agent_tests/test_sangis_topology.py -q
```

Two consecutive offline reruns reproduced the validation manifest SHA-256
`5692f7a6…e8632c` exactly, and the offline artifact check reports no problems.

Test results:

- **Focused suites:** Stage A, B and C with the disposable database gave **114 passed**, zero failed.
- **Broader Agent regression:** **417 passed, 38 skipped**, zero failed. The skips are opt-in database fixtures.

The disposable test applies a synthetic approved batch and covers the following:

- **Correct writes:** only approved Pending rows receive coordinates. Allowlisted but unapproved rows, a qualifier-excluded row, a No-match row and previously matched rows stay byte-identical, and original fields are preserved.
- **Migration and provenance:** the precision migration admits `intersection_approximate`, and Stage C provenance and prior evidence persist.
- **Recovery:** an interrupted batch resumes, and reruns are idempotent. A second staged batch is accepted.
- **Guards:** an outside edit blocks later batches. Template and mismatched approvals are refused. The Agent login cannot update collisions.

Code changes:

- **New modules:** `stage_c.py` and `stage_c_evidence.py` in the maintenance package.
- **Deployment and CLI:** the Stage C binding in `apply.py`, and the `stage-c` and `stage-c-preflight` commands.
- **Tests:** `test_stage_c.py`. The Stage B integration test was refactored into reusable helpers, since deployment now requires Stage C.
- **Unchanged:** Agent behavior, user-facing location resolution, Census and Google adapters, and the collision schema files.
