# Local SanGIS topology and historical intersection proposals

This maintenance package is separate from the Agent, address geocoders and
collision ingestion. It imports official reference data into `citylens_ref` and
generates proposals. A default run never changes collision coordinates, statuses,
source fields, prior attempts or Agent behavior. No Google API, Census address
batch, property-address geocoder or LLM coordinates are used.

Read the [Stage A findings](../verification/intersection_feasibility/REPORT.md)
and [Stage B report](../verification/intersection_batch/REPORT.md) before using
the candidates. Precision is `intersection_approximate`: current road junctions
do not establish exact or collision-date positions.

## Setup and reproducible dry run

Use the existing PostGIS container and Python environment. From the repository
root in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m backend.sangis_topology download
.\.venv\Scripts\python.exe -m backend.sangis_topology import
.\.venv\Scripts\python.exe -m backend.sangis_topology validate
.\.venv\Scripts\python.exe -m backend.sangis_topology dry-run
.\.venv\Scripts\python.exe -m backend.sangis_topology verify --live
```

`download` uses only public SanGIS/SANDAG services. It captures both layers'
metadata, counts and complete sorted OBJECTID inventories before extracting
batches of at most 2,000 features (never above the advertised limit). Every
batch must return exactly its requested IDs and the complete declared field
schema. Truncation, missing IDs, schema drift, HTTP errors and changed provider
edit stamps stop acquisition. Every request attempt is journaled before network
access; at most 240 attempts are allowed across restarts. Failed requests do not
silently retry. A deliberate rerun may resume missing batches within that cap.
Both layers' metadata/counts/ID inventories are rechecked at the end.

The service has no atomic cross-layer `historicMoment` snapshot. Stable before/
after versions, different layer publishing timestamps and the acquisition window
are recorded explicitly. The portal item records retain full license terms and
attribution. The official web service was chosen to retain every field, native
XYZ geometry, stable road IDs and independently verifiable count/ID inventories.
Unversioned export jobs would not add an atomic cross-layer guarantee.

Native compressed responses, manifest and checksums live in ignored
`backend/reference_data/sangis/`. They are deliberately retained locally rather
than checking the full county network into Git. `source-manifest.json`, metadata,
license records, request journal and checksums in the report directory identify
the acquired snapshot. Preserve the ignored directory or archive it alongside
the report for exact offline reimports. Re-fetching later can yield a new source
version and cannot reproduce an old file checksum.

`import` verifies all checksums before doing any work. It uses the existing
maintenance `DATABASE_URL`, or the configured Compose administrator on the same
database as `AGENT_DATABASE_URL`. Credentials remain in memory. A single database
transaction imports both complete layers, adds indexes, verifies counts and
publishes an immutable snapshot; interruption rolls back the whole import.
Rerunning the same completed snapshot is a no-op. It never initializes or resets
the CityLens database. Existing snapshot rows are not overwritten on refresh.

`road_segments` preserves `roadsegid`, all 63 attributes, names/components,
endpoint IDs/levels, classes/statuses and original geometry. `road_nodes` preserves
all source records and attributes, including duplicate/zero `interid` values.
The full snapshot revealed two records for positive `interid=94245` and seven
records for `interid=0`; these are not collapsed. Nodes are unique by snapshot/
OBJECTID and snapshot/GLOBALID, with an indexed, deliberately nonunique INTERID
topology link. Roads are unique by snapshot/ROADSEGID and snapshot/OBJECTID.
Any candidate with multiple INTERID records fails Stage A's uniqueness rule.

`geom` retains original EPSG:2230 coordinates, including Z; units are US survey
feet. The source Z ordinate is not interpreted as a road level. `geom_4326` is a
documented 2D PostGIS transform for matching/export. Source ESRI geometry is also
retained as JSON. GiST indexes cover native/derived geometry; B-tree indexes
cover exact/canonical names and both endpoint IDs. No geometry is repaired.

`validate` checks counts, validity, CRS, IDs, null/invalid levels, dangling links,
duplicate references, endpoint alignment, orphan nodes, multipart/nonsimple
lines, exact/partial overlaps and coincident endpoints with distinct topology
IDs. PostGIS transforms are compared with pyproj. The inherited Stage A 2m
ceiling checks endpoint/node consistency only; it neither creates links nor
snaps/merges points. Its observed distribution and 1.99m/2.01m boundary tests are
reported. The unrelated Census 100m address-disagreement threshold is not used.

Only `USAGE`/`SELECT` are granted to `citylens_agent_reader`. Effective reference
write privileges are checked before analysis, independently of its existing
application reader guard. The raw CSV archive remains inaccessible to the Agent;
maintenance-only read transactions record archive digests without granting it
additional permissions.

## Resolver, selection and restart

The resolver calls Stage A's existing classifier directly. Local indexed name
lookups use the same existing Census canonicalization as Stage A's exact
component comparisons, preserving directional and suffix information. This
retains already-supported ordinal/direction token equivalents whose native
spelling cannot be reconstructed from a canonical label. Lookups reproduce
Stage A's spatial envelope, then
validate full structured names, shared integer nodes, unique node records,
complete incidence, geometry, node type, levels and road status/class. All
incident segments are loaded, including roads outside the two requested names.
Unexplained geometric crossings, invalid long-road geometry and conflicting
evidence remain uncertain. Multiple nodes remain multiple; source blocks and
police beats do not choose a candidate.

All 80 archived Stage A pair decisions and candidate-node decisions reproduce
locally. Coordinates use the native EPSG:2230 to WGS84 PostGIS/PROJ transform;
the archived REST `outSR=4326` values differ by at most 1.236m. That comparison
uses the existing 2m Stage A consistency ceiling with exact node/decision
identity, rather than requiring byte-identical coordinates across unspecified
provider datum-transform defaults. The PostGIS and pyproj transforms agree to
4.1e-13 degrees. Neither check establishes submeter positional accuracy.

All Pending records with a nonempty cross street are inventoried, including the
one missing a primary street. The complete-pair subset and other exclusions are
reported separately. Normalized unordered road pairs cache topology evaluation.
A separate normalized source key retains primary/cross orientation, source
block, unknown ADDRESS_* qualifiers and police beat. Every original report ID
keeps its exact source-address SHA-256 fingerprint, stored address key, source
components and qualifiers. These keys never alter the database source fields.

`input.json` freezes the selection and before fingerprints. Each pair has an
atomic checkpoint tied to snapshot ID, method version and full pair hash. An
interrupted run resumes completed pairs; changed input fingerprints require a
new output directory. JSONL/CSV rows expose all candidates, including rejected
nodes and unresolved reports. Never infer acceptance from a nonempty coordinate
column: use `classification=accepted_candidate` and `topology_outcome=supported`
together. Candidate coordinates on a rejected/ambiguous row are evidence only.

`run-manifest.json` hashes inputs, pair results, CSV/JSONL proposals, summary and
Stage A replay. Before/after full collision, original-field, existing matched
record and separate archived-source fingerprints remain reviewable. All 78,494
records are the coverage denominator; proposed gains are hypothetical, not
deployed coverage.

`verify --live` freezes a deterministic accepted sample stratified by road class,
directions and name complexity, and adds grade conflicts, uncertain nodes and
all nodes in sampled ambiguous pairs. At most 180 independent TIGER
transportation requests are permitted. Responses and the request ledger are
cached; `verify` without `--live` replays them offline. Exact-name proximity
checks are independent evidence, not grade/historical ground truth, and cannot
provide a population false-positive rate. Review the uncorroborated pairs and
limitations before considering a deployment.

## Stage C validation and allowlist

[Stage C](../verification/intersection_stage_c/REPORT.md) validates every Stage B
accepted pair, with strict local topology, TIGERweb named-road corroboration and
provenance checks. The result is a deterministic allowlist. Each outcome is `pass`,
`fail`, `unknown` or `provider_error`, and only `pass` is deployable. Stage C reads
the database with the read-only Agent login and never writes collisions.

```powershell
.\.venv\Scripts\python.exe -m backend.sangis_topology stage-c            # offline replay from cache
.\.venv\Scripts\python.exe -m backend.sangis_topology stage-c --live     # capture missing scopes, persisted 400-attempt budget
.\.venv\Scripts\python.exe -m backend.sangis_topology stage-c-preflight  # read-only allowlist and database drift check
```

TIGER responses and the request ledger are kept in ignored
`backend/reference_data/intersection_stage_c/`. Earlier responses are reused only for
an identical URL plus a matching original ledger SHA-256. Budget exhaustion leaves
scopes `unknown`. Deployment readiness also requires a `quality-review.json` bound
to the current quality sample's SHA-256.

## Separate deployment review

No deployment is authorized by the Stage B milestone. No approval file is
created by these commands. The current report recommends keeping proposals for
review. A reviewer must separately approve specific report IDs and exact artifact
hashes, record a historical review basis, and acknowledge the limitations.

The reviewer-supplied JSON document must have these fields:

```json
{
  "decision": "approve_apply",
  "reviewer": "REVIEWER NAME",
  "approved_at": "REVIEW TIMESTAMP WITH OFFSET",
  "candidate_sha256": "SHA256 OF candidates.jsonl",
  "run_manifest_sha256": "SHA256 OF run-manifest.json",
  "independent_summary_sha256": "SHA256 OF independent-summary.json",
  "snapshot_id": "REVIEWED SNAPSHOT ID",
  "method_version": "sangis-stage-a-local-v2",
  "report_ids": ["EXPLICIT ORIGINAL REPORT ID"],
  "acknowledge_temporal_uncertainty": true,
  "acknowledge_independent_validation_limitations": true,
  "historical_review_basis": "REVIEWER EVIDENCE AND ACCEPTANCE BASIS",
  "allow_precision_migration": true,
  "validation_version": "sangis-stage-c-strict-v1",
  "stage_c_validation_manifest_sha256": "SHA256 OF intersection_stage_c/validation-manifest.json",
  "stage_c_allowlist_sha256": "SHA256 OF intersection_stage_c/deployment_allowlist.csv",
  "acknowledge_intersection_approximate_precision": true
}
```

Since Stage C, every approval must also carry the four Stage C fields. Each approved
ID must be an identical `pass` row of the Stage C allowlist. `stage-c` writes a
prefilled `approval-template.json` whose decision value is deliberately invalid.
Staged rollouts are supported. A later approval is accepted only when the database
differs from the Stage C before-state by guarded updates already journaled for
allowlisted reports.

Approval is an explicit operator/reviewer record, not a cryptographic signature.
Independent artifact hashes prevent accidental stale review; operator access
controls still govern who may supply approval and maintenance credentials.
`allow_precision_migration` explicitly approves the separately provided
`deploy.sql`: the existing table only permits `block_approximate`, so the
constraint must admit `intersection_approximate` at the approved apply step.
No constraint was changed during this milestone.

Exact read-only deployment preflight after a separate approval:

```powershell
.\.venv\Scripts\python.exe -m backend.sangis_topology deployment --approval 'E:\review\stage-b-approval.json'
```

Exact deployment command, ONLY after separate explicit approval:

```powershell
$env:SANGIS_MAINTENANCE_DATABASE_URL = 'postgresql://MAINTENANCE_USER:URL_ENCODED_PASSWORD@127.0.0.1:54329/citylens'
.\.venv\Scripts\python.exe -m backend.sangis_topology deployment --approval 'E:\review\stage-b-approval.json' --apply --batch-size 100
```

The writer is a distinct maintenance identity on the same database; there is no
fallback to Agent credentials. Approval validation rechecks all artifact hashes,
the independent review, Stage A replay, exact allowlisted eligibility and a fresh
local topology replay. The initial global baseline must still match the dry run.
Rows are locked individually, must remain Pending without coordinates, and must
retain their original address fingerprint. Existing matched records cannot be
overwritten. All prior geocoding fields/details are archived in append-only
history and nested in new provenance; original collision fields remain untouched.

The database advisory lock serializes this worker. Transactions contain at most
200 IDs (100 by default). Each outcome and update commit together under a unique
run/report key. Safe reruns skip committed outcomes. Rejected stale rows remain
rejected for that run; reanalysis and new approval are needed to reconsider them.
An interruption leaves a committed prefix and rolls back the current batch.
There is no automatic reversal of prior committed batches; inspect the journal
and resume the same run. Automatic rollback of all applied history is not
attempted because it could overwrite later legitimate edits.

Before each commit, counts/statuses reconcile, original fields and archived
source digests match, nonselected and previously matched records remain equal,
and applied spatial geometry/precision/provenance are valid. A failed invariant
rolls back the current batch and stops. The final audit rechecks effective Agent
reference read-only privileges and reports cumulative and invocation outcomes.

## Refresh and verification commands

Use a new data/output directory to refresh; old snapshots/evidence remain intact:

```powershell
.\.venv\Scripts\python.exe -m backend.sangis_topology download --data 'backend/reference_data/sangis-refresh'
.\.venv\Scripts\python.exe -m backend.sangis_topology import --data 'backend/reference_data/sangis-refresh'
.\.venv\Scripts\python.exe -m backend.sangis_topology validate --data 'backend/reference_data/sangis-refresh' --output 'backend/verification/intersection_batch_refresh'
.\.venv\Scripts\python.exe -m backend.sangis_topology dry-run --data 'backend/reference_data/sangis-refresh' --output 'backend/verification/intersection_batch_refresh'
```

Replaying a different snapshot can legitimately disagree with old Stage A
responses. Such differences remain visible and block deployment until reviewed;
they are not silently treated as regression success.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/agent_tests/test_intersection_feasibility.py backend/agent_tests/test_sangis_topology.py -q
$env:AGENT_TEST_DOCKER = '1'
.\.venv\Scripts\python.exe -m pytest backend/agent_tests/test_sangis_topology.py -q
```

The second command launches and removes an owned disposable PostGIS container on
port 54331. It tests synthetic import, original Z preservation, duplicate node
quarantine, permissions, dry run, guarded apply, preserved attempts, interruption
after one committed batch and rerun idempotence. It never applies real candidates.

## Final historical geocoding (road blocks, ambiguous revalidation, triage)

See the [final report](../verification/historical_geocoding_final/REPORT.md).
`final-geocoding` is read-only. It evaluates every no-match, ambiguous and Pending
report and writes a consolidated results file plus a strict allowlist:

- **Road blocks** (`roadblock.py`): the source number is an anonymized block
  `N..N+99`, never a house number. A block is accepted only with exact names, or
  SanGIS's own `rd20full` abbreviation of the same segment. It must also have a
  compatible left or right address range, City of San Diego jurisdiction and
  Constructed/Maintained non-freeway segments. The candidates must form one
  connected or carriageway cluster, with segments within 60 m of each other, and
  an extent of at most 400 m. The point lies on the road geometry nearest the
  block centroid. Distant candidates are never averaged, and parcels or Address
  Points are never used.
- **Ambiguous reports:** stored Census evidence is classified, never edited. A
  number in a different block is a `number_conflict`, and a materially different
  street is a `street_conflict`. A documented equivalent, or the same street,
  must place the Census point within 100 m of the block.
- **Report checks:** the police beat must agree with the block's SanGIS beats. A
  TIGERweb exact road name must lie within 30 m, using a bounded and cached
  budget of 4,000 live attempts.
- **Pending reports:** Stage B/C outcomes are reused. Only Stage B
  `street_not_found` pairs are retried through the official abbreviation, with
  Stage C's strict checks.

```powershell
.\.venv\Scripts\python.exe -m backend.sangis_topology final-geocoding           # offline replay from cache
.\.venv\Scripts\python.exe -m backend.sangis_topology final-geocoding --live    # capture missing TIGERweb scopes
.\.venv\Scripts\python.exe -m backend.sangis_topology final-deployment --approval 'E:\review\final-approval-1.json'
```

`final-deployment` is a separate, status-specific extension of the guarded
deployment.

- **Status binding:** the approval lists `allowed_original_statuses` and exact
  IDs, and binds the final manifest and allowlist hashes. Each row must still hold
  its recorded original status, a NULL location and its source fingerprint.
  Matched rows can never qualify.
- **Shared safeguards:** it uses the same journal, advisory lock and per-batch
  invariants as Stage C, and replays every approved coordinate from the local
  reference.
- **Per-row baseline:** `before-row-digests.json.gz` allows Stage C and final
  batches to interleave. Any row change not journaled by a guarded run stops
  every later batch.
