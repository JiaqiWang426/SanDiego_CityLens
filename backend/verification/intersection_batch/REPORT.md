# Stage B — local SanGIS topology and historical intersection proposals

Completed November 2, 2026, America/Los_Angeles. The complete official reference
network is imported and the eligible Pending workload has been analyzed locally.
**4,319 reports have uniquely supported present-day intersection proposals.
No historical collision coordinates, statuses or evidence were changed. Keep all
candidates as proposals; deployment is not recommended from this evidence alone.**

The expanded independent review corroborates both road names for 45 of 60 sampled
accepted candidates. Fifteen remain uncorroborated, including one 46.43m named-road
disagreement. Current topology does not verify collision-date conditions or exact
collision positions. No defensible historical recovery or false-positive rate
has been established.

## Prerequisite and scope

Reviewed Stage A's [report](../intersection_feasibility/REPORT.md), classifications,
independent validation, topology limitations, archived responses, semantics and
60 focused tests before implementation. Its normal constructed, compatible-level
Type I criteria support proposal analysis, while ambiguous nodes, pseudo crossings,
conflicting types, unknown status/class and long-road gaps remain unresolved.
This Stage B request separately authorizes the full reference acquisition/import
and bulk analysis. It does not authorize historical updates.

The baseline is 78,494 reports: 59,627 matched, 7,400 no-match, 3,117 ambiguous and
8,350 Pending. All 6,840 Pending records with a cross street were inventoried:
6,839 have a complete primary/cross pair and one lacks a primary street. Another
1,510 Pending reports lack a cross street and are outside intersection matching.

The complete subset has 3,988 distinct unordered stored-field pairs, or 3,973
under the existing Census token/ordinal canonicalization. Including the missing-
primary case yields **3,974 evaluated normalized pairs**. There are 4,102 separate
qualified source keys, retaining primary/cross orientation, source block, unknown
ADDRESS_* qualifiers and police beat. Every report ID is preserved separately;
qualifiers are not assumed to select a geographic node. All stored streets and
address keys agree with their original source components.

## Reference acquisition and infrastructure

| Official layer | Imported features | Source modification, Los Angeles |
| --- | ---: | --- |
| [Roads_All](https://geo.sandag.org/server/rest/services/Hosted/Roads_All/FeatureServer/0) | 164,584 | October 6, 2026, 08:33:50.236 PDT |
| [Roads_Intersection](https://geo.sandag.org/server/rest/services/Hosted/Roads_Intersection/FeatureServer/0) | 130,637 | October 6, 2026, 08:39:30.371 PDT |

Snapshot ID:
`0aa3c4d45aabeb493257e7b5c01e5b22e6ab9797f236af729f018f74d76fe9fc`.
Acquisition ran November 2, approximately 19:38:59–19:43:59 PST. Source item
modification times, metadata, complete license text, attribution, file hashes and
batch inventories are retained in [source-manifest.json](source-manifest.json),
[source-checksums.json](source-checksums.json), layer metadata/item JSON and
[requests.json](requests.json).

The public REST service was used to retain all attributes and original XYZ
geometry while verifying complete OBJECTID inventories. Both layers' schemas,
counts, ID sets and edit timestamps were checked before and after extraction.
Batches contain at most the advertised 2,000 records and must return exactly the
requested IDs. Missing/truncated results or schema/version drift stop processing.
The persisted acquisition ledger records 166 attempts within its 240-attempt cap,
including a failed item lookup on the public ArcGIS portal; the official owning
SANDAG portal supplied the item records. Failed attempts are retained, not erased.
An additional diagnostic GeometryServer transformation lookup returned token-
required and was not used; its response is archived separately.

No atomic cross-layer historical snapshot is offered. The publishing timestamps
differ by about six minutes, although both remained stable through acquisition.
This is a documented stable download window, not proof of simultaneous source
surveying. The [SanGIS use agreement](https://gis.sangis.org/sanportal/apps/storymaps/stories/d26146d84e834ff6bcd58e4e620a983a)
and layer attribution are preserved verbatim in the item metadata. These generalized
public GIS data have accuracy/use limitations; no survey-quality or public-domain
license assumption was made.

The full compressed native snapshot remains locally in ignored
`backend/reference_data/sangis/`; preserve/archive it for exact offline reimports.
The committed manifest/checksums identify all 170 snapshot files. A later live
download can have different content/checksums and must use a new snapshot directory.

The separate `citylens_ref` namespace contains immutable-versioned `snapshots`,
`road_segments` and `road_nodes`, with spatial GiST, full/canonical road-name,
endpoint and node lookup indexes. Every original attribute and ESRI geometry is
preserved. Roads are unique by snapshot/ROADSEGID and snapshot/OBJECTID. Node source
identities are unique by snapshot/OBJECTID and snapshot/GLOBALID; topology INTERID
is indexed but cannot safely be declared unique in this actual source snapshot.
The Agent receives only USAGE/SELECT and remains effectively read-only.

Native geometry is EPSG:2230, NAD83 California zone 6, US survey feet, including
the source Z ordinate. Derived 2D WGS84 geometry is generated explicitly with
PostGIS. Z is not interpreted as road level. No geometry is repaired or snapped.
The import publishes both complete layers in one transaction; interruption rolls
back the import. Reimporting the same immutable snapshot is a no-op.

## Full-network topology findings

[topology-integrity.json](topology-integrity.json) and the two investigation JSON
files contain counts, distributions and source IDs.

| Check | Finding |
| --- | ---: |
| Missing/empty or invalid geometries | 0 in either layer |
| Wrong SRIDs | 0 |
| Duplicate road/source OBJECTIDs | 0; unique constraints enforced |
| Duplicate topology INTERIDs | 2 groups: seven ID 0 records; two ID 94245 records |
| Endpoint observations | 329,168 |
| Missing/nonpositive endpoint identities | 9 |
| Dangling endpoint references | 62 |
| Endpoint references to duplicate node records | 12 |
| Invalid endpoint level codes outside 0–9 | 0 |
| Orphan source node records | 292 |
| Multipart road geometries | 0 |
| Valid but nonsimple road geometries | 17 |
| Exact duplicate geometry groups | 2 |
| Partially/exactly overlapping segment pairs | 44 |
| Coincident endpoints with distinct topology IDs | 28 groups |

The initial import deliberately failed its INTERID uniqueness constraint and
rolled back. Investigation found a positive coincident duplicate at ID 94245
with distinct source GLOBALIDs, plus seven ID 0 records. The corrected source
model preserves every record. Duplicate topology identities remain visible to
Stage A's `len(node_records)==1` guard; coincident copies are not collapsed.

Endpoint/native-node distance median, 95th and 99th percentiles are all zero.
There are 21 endpoint observations beyond 2m: nine have invalid ID 0 and twelve
have positive node IDs. The 45.23km overall maximum is dominated by treating
unrelated ID 0 records as links and is not a valid connectivity observation.
The largest positive-ID disagreement is 809.62m at BULLARD LN. The affected IDs
and road statuses are retained in
[endpoint-alignment-investigation.json](endpoint-alignment-investigation.json).
These defects are documented, not repaired. They preclude treating the whole
network as uniformly clean or inferred crossings as physical connections.

The inherited Stage A 2m alignment ceiling only validates an existing integer
node link. It never joins/snaps roads, merges nodes or selects a nearest node.
Boundary tests check 1.99m acceptance versus 2.01m uncertainty. For the actual
accepted proposal nodes the maximum endpoint discrepancy is **0.0011m**, and
none would change classification at a 0.01m consistency ceiling. The network's
approximately 1mm native XY tolerance and measured distribution support this
consistency interpretation. No Census 100m address-disagreement tolerance is used.

PostGIS and pyproj native-to-WGS84 transforms agree within 4.1e-13 degrees over
100 ordered nodes. Archived Stage A REST WGS84 coordinates differ from the local
native transform by at most **1.235219m**. REST datum-transform defaults were not
specified in Stage A; exact coordinate-byte equality is therefore not asserted.
Node identity, classification, incident segment IDs, levels, type and reasons
match exactly. Coordinate comparison stays within the existing Stage A 2m
consistency ceiling and does not establish submeter positional accuracy.

## Resolver and measured batch outcomes

The local resolver directly calls the verified Stage A classifier. Indexed
canonical-name retrieval uses the existing token rules, followed by full
structured-component matching. Directions, suffixes, unknown tokens and meaningful
qualifiers are not dropped. This avoids losing already-supported native ordinal/
direction spellings when looking up an already canonical label. No new aliases,
fuzzy matches, address interpolation or approximate name acceptance were added.

The full official network is stored; matching retains Stage A's documented
admission envelope and completeness guards. The local workload loads 38,943
named-road segments and 2,519 shared node identities, with all incident segments.
Current candidates validate unique source node records, geometry, Type I,
compatible endpoint levels and supported constructed/maintained roads. Geometry
crossings alone never establish connection. Definitively rejected alternate nodes
can coexist with one supported node; all such evidence is retained distinctly.

| Pair classification | Normalized pairs | Reports |
| --- | ---: | ---: |
| accepted_candidate | 2,043 | 4,319 |
| multiple_candidates | 119 | 260 |
| topology_uncertain | 270 | 494 |
| street_not_found | 999 | 1,108 |
| no_intersection | 84 | 144 |
| invalid_source | 459 | 515 |
| Total | **3,974** | **6,840** |

The invalid-source total includes the missing-primary record. Other causes are
same-road placeholders, offsets and compound/incomplete descriptions. Exact-name
`street_not_found` is not a claim that the physical road does not exist.

Across all pairs there are 2,317 supported candidate-node occurrences, 62 rejected
occurrences and 240 uncertain occurrences. Supported occurrences include ambiguous
or globally incomplete pairs and are not all eligible pins. There are 69 pairs
with grade-conflict evidence, and ambiguous pairs have up to three supported
nodes. Four accepted pairs also contain a definitively rejected alternate node;
the apply parser explicitly selects only the sole supported node.

[candidate-diagnostics.json](candidate-diagnostics.json) records source/status/type
reasons and alignment sensitivity. Long-road geometry outside the supported
envelope affects 49 pairs; missing endpoint identity affects two. These guards
remain conservative. Counts are whole-selection observations, not extrapolations.

The [JSONL proposals](candidates.jsonl) and [CSV proposals](candidates.csv) have
7,183 evidence rows. Each original report ID retains its source SHA-256 fingerprint,
normalized streets, qualified source key, matched segment IDs and actual source
class/status/level attributes, node/type, coordinates, distinct valid-node count,
validation/rejection reasons, precision, snapshot/version and temporal limitation.
Coordinates on rejected/ambiguous rows are evidence, not accepted pins. All rows
have `deployment_approved=false`.

## Regression and independent verification

[stage-a-replay.json](stage-a-replay.json) reproduces all **80/80** classifications,
node decisions and node evidence. The [REST archive comparison](rest-archive-comparison.json)
finds all **4,148 archived road features and 236 node records**, with no differing
archived attributes and identical provider edit metadata. No Stage A requests were
reissued. Local checkpoints were replayed after the completed batch with identical
pair/report classification totals and unchanged collision fingerprints.

The frozen independent plan selects 60 accepted pairs across road-class,
direction and naming-complexity strata, plus hard cases. The final 102 pairs
comprise 60 accepted, 19 uncertain, 12 no-intersection and 11 multiple-candidate
pairs. Every candidate node in selected pairs is retained, including rejected
alternatives, for **119 separate geographic checks**.

Separate official [TIGERweb transportation geometry](https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer)
supplies independent spatial evidence. This is not the Census address geocoder
or a rerun of the Census batch. The 30m exact-name proximity diagnostic is not
a topology tolerance or collision-accuracy guarantee. WGS84 response CRS is
checked; all responses and their checksums are archived. The request budget is
180 across restarts; 119 requests were used. Offline replay used no new requests
and reproduced the original independent-validation file checksum.

| Independent result | All candidate checks | Supported nodes of accepted pairs |
| --- | ---: | ---: |
| Both named roads spatially corroborated | 88 | 45 |
| Not corroborated | 31 | 15 |
| Provider errors | 0 | 0 |
| Total | 119 | 60 |

The 15 accepted-node gaps comprise 14 with missing exact names and one named-road
spatial disagreement. Frozen-response inspection found, for example, SAINT ANDREWS
versus `St Andrews`, AVENIDA/VISTA versus `Ave`/`Vis`, named SanGIS ramps versus
unnamed TIGER features, HISTORIC DECATUR versus `Decatur`, and an unavailable
DISCOVERY PL name. These observations explain validation limitations but **do not
introduce aliases or promote the failures to corroborated results**.

CARMEL VISTA RD / RUETTE DE VILLE has the expected CARMEL VISTA geometry 46.43m
away, while TIGER names CARMEL VIEW RD and HIGH BLUFF DR near the candidate. This
is an unresolved source name/position conflict; the study cannot decide which
source reflects the collision-date network. Five grade-conflict candidate checks
also pass the named-road spatial test, demonstrating that proximity can support
a false physical-connectivity claim if node/level rejection is omitted.

See [independent-summary.json](independent-summary.json), the
[119-check inspection](independent-inspection.json), raw response cache, and
[report/node crosswalk](independent-candidate-crosswalk.csv). This is an automated
source comparison plus inspection of its frozen road evidence, not a field survey,
aerial/historical validation or a statistically representative sample. TIGER and
SanGIS can share upstream information. The 15/60 uncorroborated fraction is **not
a measured false-positive rate**. No systematic classifier false-positive pattern
was confirmed, but reliable historical deployment has not been established.

## Preservation, coverage and deployment decision

[database-final-check.json](database-final-check.json) verifies:

- All 78,494 full collision rows, original/source fields and all 59,627 existing
  matched rows have identical before/after fingerprints; no record was deleted.
- Archived parsed CSV rows have identical count/digest. The on-disk source CSV
  SHA-256 still matches its registered import.
- Status counts remain 59,627 matched / 7,400 no-match / 3,117 ambiguous /
  8,350 Pending. No Pending report acquired stored coordinates.
- Agent application checks and effective reference permissions remain read-only.
- The precision migration was not applied, and deployment journal/run tables do
  not exist in the working database. Historical collision updates: **zero**.

Actual deployed coverage remains **59,627 / 78,494 = 75.9638%**. If all 4,319
supported proposals were separately validated and approved, the hypothetical
increase would be **5.5023 percentage points**, to **63,946 / 78,494 = 81.4661%**.
This is a proposal ceiling, not an estimated realized recovery rate. Current
coordinates are not counted as deployed records. Proposed report dates range from
June 2016 to October 2026, while the reference is current October 2026 topology.

**Do not approve bulk deployment on the present evidence.** Resolve the independent
name/position conflicts, obtain appropriate historical/physical connectivity
evidence for an explicitly reviewed subset, and retain ambiguity and uncertainty.
Even corroborated ordinary junctions establish approximate intersections rather
than exact collision positions. No automatically deployable subset is claimed.

## Implementation, commands and testing

The [maintenance package](../../sangis_topology/README.md) provides acquisition,
immutable PostGIS import, topology checks, local resolver, checkpointed batch,
independent evidence review and a guarded apply implementation. It documents exact
setup/refresh/restart commands and reviewer approval fields.

Exact dry-run command from the repository root:

```powershell
.\.venv\Scripts\python.exe -m backend.sangis_topology dry-run
```

Only after a separate explicit review/approval, the deployment interface is:

```powershell
# Read-only deployment preflight; approval must bind exact artifact hashes/IDs.
.\.venv\Scripts\python.exe -m backend.sangis_topology deployment --approval 'E:\review\stage-b-approval.json'

# Future authorized apply only; no real approval file was created here.
$env:SANGIS_MAINTENANCE_DATABASE_URL = 'postgresql://MAINTENANCE_USER:URL_ENCODED_PASSWORD@127.0.0.1:54329/citylens'
.\.venv\Scripts\python.exe -m backend.sangis_topology deployment --approval 'E:\review\stage-b-approval.json' --apply --batch-size 100
```

Apply requires separate maintenance credentials, exact hash/ID approval,
independent evidence integrity, Stage A replay and a fresh local topology replay.
It checks current Pending status, empty location and original source fingerprint;
stale or already located records cannot be overwritten. Prior attempts/evidence
are retained in history and new provenance. Bounded transactions atomically commit
updates and durable run/report checkpoints; reruns skip committed outcomes.
Counts, original fields, archived sources, existing matched/nonselected records,
spatial validity, precision and provenance are checked before each commit. The
separate precision migration requires explicit reviewer approval too.

Verification results, with overlapping suites reported separately:

- Stage A + Stage B focused tests, including an owned disposable PostGIS database:
  **81 passed, zero skipped/failed** in 6.16s. This includes 60 Stage A tests and
  21 Stage B tests. Real historical apply was not executed.
- Broader Agent regression: **385 passed, 37 skipped, zero failed** in 13.39s.
  Skips are guarded opt-in database fixtures, including the separately tested
  Stage B integration case.
- The disposable integration verifies original Z storage, duplicate INTERID
  quarantine, unique supported versus rejected alternate nodes, read-only grants,
  default dry-run behavior, explicit synthetic apply, preservation of prior
  evidence, interruption after one committed batch and idempotent recovery.
- Source file/checksum corruption, incomplete/truncated batches, schema drift,
  request budgets, qualifiers/fingerprints, stale candidates and coordinate/status
  overwrite guards are covered. Cached independent replay made zero new requests.

New work is confined to the maintenance package, focused tests, Stage B artifacts,
two documentation links and ignore rules for large reference/checkpoint files.
Existing Agent reasoning, chat memory, interactive location flow, Census workers,
Google adapters, collision ingestion/schema files and unrelated working-tree
changes were preserved. No new dependency was required.
