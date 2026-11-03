# Stage A — SanGIS intersection feasibility

Observed November 2, 2026 (Los Angeles). **A conservative, reproducible candidate
method is feasible; automatic historical updates are not justified by this
study.** The frozen sample contains 80 pending reports and 80 distinct normalized
road pairs. There are 27 uniquely supported present-day intersection candidates,
with remaining cases explicitly unresolved. These are not verified historical
accident coordinates and no production geocoder was deployed.

## Database and existing implementation

The existing restricted Agent reader confirmed 78,494 collision reports:
59,627 matched, 7,400 no-match, 3,117 ambiguous and 8,350 pending. All matched
locations remain unchanged; no pending report acquired a location. Full-row
fingerprints before/after are identical in
[database-final-check.json](database-final-check.json). No migration, temporary
production table, role change, source CSV change or historical geocoding run
occurred. Google and Census address-geocoding requests: **zero**.

Reviewed `TRAFFIC_COLLISIONS.md`, `P1_2_2.md`, schema, ingestion address
normalization, Census validation, the previous historical audit and relevant
tests. Reused `census_street`, raw-component normalization, the existing reader
guard, full-row fingerprint, UTM projection and distance helper. Previous cached
SanGIS geometry informed class strata; it lacked the complete node/level fields
required here and was not treated as sufficient topology evidence.

## Verified provider semantics

Live metadata was inspected **before query design** and archived as
[Roads_All-metadata.json](Roads_All-metadata.json) and
[Roads_Intersection-metadata.json](Roads_Intersection-metadata.json).
Current IDs are integer fields; levels are small integers; classifications and
statuses have coded domains. The native CRS is State Plane 102646/latest 2230.
All study geometry requests specify WGS84 `outSR=4326`; coordinate comparisons
use UTM zone 11N meters.

The [official City-hosted SanGIS field documentation](https://www.sandiego.gov/sites/default/files/appendices_-_6-10.pdf),
PDF pages 131–139 and 146–147 (zero-based), explains the endpoint link and
relative levels. `FNODE`/`TNODE` reference `INTERID`, **not OBJECTID**. Level 0
is below ground, 1 generally ground, and 2–9 represent increasing relative
separation; a segment can change endpoint level. These are not surveyed heights.
Type I denotes a normal junction of at least three segments; P denotes a pseudo
crossing. Endpoints, name changes, boundary splits, review and multiple-use types
must be distinguished. Current service domains were checked separately from
the older explanatory document; see [semantics.json](semantics.json).

This relationship was checked against live returned data: the 39 evaluated
shared node candidates have matching `INTERID`/endpoint references, with zero
observed endpoint-to-node geometry discrepancy at returned precision. Identity
alone does not establish physical connection: pseudo nodes can join lines in
the data model while levels describe separation.

## Frozen sample and selection limitations

[sample.csv](sample.csv) preserves report IDs and every original address
component, including whitespace and source qualifiers. [sample.json](sample.json)
also preserves the original source records and selection context. All 80 stored
street pairs agree with normalization of their raw components.

Seed: `citylens-intersections-stage-a-v1`. Selection was frozen before live
road-name/node queries and did not use match success. It cycles through:

- Police-beat first-digit area proxies: all nine represented, with counts in
  `sample.json`. These are not independently verified neighborhood polygons.
- Road class from the previous partial cache, with explicit unknown and source
  freeway/ramp-hint strata. True class for uncached roads was unavailable before
  sampling; these proxies are not invented provider classes.
- Naming complexity: 41 ordinary names, 17 complex names, 22 descriptions.
- Directions: 28 records with directional components and 52 without.
- Ambiguity proxy: 32 repeated population pairs and 48 single-report pairs.
  Report frequency is not a claim that an intersection is spatially ambiguous.
- Additional cross-suffix information: 59 present and 21 absent. Dates, beats,
  source numbers and qualifiers remain available; they are not guessed positions.

This deliberately diverse quota sample overrepresents difficult descriptions and
rare combinations. It is not random or population-proportional. The 80 selected
pairs happen to be distinct, so report-pair deduplication removes none in this
sample; reversed/repeated pairs are nevertheless normalized and tested before
requests. Unresolved placeholders and descriptions were retained in the sample.

## Method and safeguards

1. Normalize each road separately with existing historical rules, preserving
   suffix, direction and qualifiers. Reject same-road placeholders and compound/
   offset descriptions that cannot safely describe two named roads. No fuzzy
   spelling, dropped direction, new road alias or changed source number is used.
2. Query exact full-name variants for valid source pairs. Validate both
   `rd30full` and the reconstructed pre-direction/name/suffix/post-direction.
   Retain ROADSEGID, endpoints, levels, class, status, one-way and geometry.
3. Generate shared endpoint IDs, query the corresponding intersection records,
   and retrieve **all incident road segments** for those IDs, not just the two
   named-road subsets. Validate that both requested roads are incident.
4. Require valid geometry, distinct segment identities, at least three incident
   segments, compatible known levels, Type I, constructed/maintained status and
   supported road classes. The 2-meter endpoint/node agreement tolerance is a
   consistency check; it never creates a node join, snaps a point or selects the
   nearest candidate.
5. Compute 2D crossings/overlaps as secondary evidence. A geometric crossing
   without verified connectivity remains uncertain. Documented constant unequal
   levels or a pseudo node with conflicting known levels reject an ordinary
   connection. Missing/invalid levels remain uncertain.
6. Treat a split-type label conflicting with otherwise four-way incident
   topology as **uncertain**, not automatically absent. Actual endpoint/name-change
   splits without physical-junction support remain rejected.
7. Preserve every supported distinct node. Multiple nodes remain ambiguous;
   neither ordering, proximity nor police-district centers select a winner.
   Any additional invalid geometry or incomplete candidate evidence prevents a
   unique acceptance. Complete cached query scopes can satisfy smaller subsets.

The supported envelope is the existing broad San Diego admission envelope, not
a city-boundary polygon. Some long-road geometries leave that envelope even
when a local candidate is plausible; those cases conservatively retain a global
uncertainty flag rather than claim a complete search. No road-block interpolation
was implemented.

## Measured outcomes

| Road-pair classification | Pairs |
| --- | ---: |
| accepted_candidate | 27 |
| multiple_candidates | 2 |
| topology_uncertain | 7 |
| street_not_found | 20 |
| no_intersection | 2 |
| invalid_source | 22 |
| provider_error | 0 |
| Total | 80 |

There are **39 shared candidate nodes**, of which 33 pass node-level support
checks. The 33 include nodes in ambiguous pairs and two otherwise plausible
nodes whose pairs fail broader completeness checks; they do not imply 33 uniquely
resolved pairs. The candidate types are 36 I, two P and one Z. Geometry supplies
crossing evidence for 37 pairs. Full details and source segment IDs are in
[candidates.csv](candidates.csv) and [pair-results.json](pair-results.json).

Examples requiring review:

| Pair | Observed evidence | Decision |
| --- | --- | --- |
| El Camino Real / Via de la Valle | Two supported nodes, 355.847 m apart | Multiple candidates; long-road completeness also flagged |
| Martin Ave / S 32nd St | Two supported nodes, 35.723 m apart | Multiple candidates; do not collapse nearby distinct junctions |
| Friars Rd / Mission Village Dr | Pseudo node 77391; levels 1 and 2 | No compatible ordinary intersection |
| Interstate 15 SB / Mira Mesa Blvd | Pseudo node 56231; levels 1 and 2, plus incomplete long-road geometry | Topology uncertain; no pin accepted |
| 13th St / Imperial Ave | Type I but levels 1/9, unconfirmed road status and paper-road class | Topology uncertain |
| 11th Ave / E St | Type Z despite four incident segments at level 1 | Conflicting type/topology; uncertain, not proof of absence |
| Calle Tres Lomas / Roanoke St | Shared physical-node evidence but unconfirmed construction status | Uncertain |

The **three level-conflict pairs** are not three independently verified
overpasses: two include pseudo-node evidence and one has mixed road-status/class
evidence. All remain unassigned. Some `street_not_found` cases may reflect source
spelling/abbreviations, provider naming or strict structured-field consistency;
this study does not claim those roads do not exist.

## Independent geographic review and false-positive risk

The frozen 20-pair review includes ten accepted pairs, both multi-node pairs,
all seven topology-uncertain pairs and the Friars/Mission Village pseudo crossing.
Each candidate in a multi-node pair was checked, giving **22 candidate checks**.
The initial selection was preserved from recorded request centers after a
conservative parser refinement, rather than expanding live requests for a
different validation sample; see [independent-plan.json](independent-plan.json).

Separate official [Census TIGERweb transportation](https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer)
geometry was inspected around each candidate. It is not a SanGIS node record.
The review checks exact named-road features and their projected distance from
the candidate. The 30-meter criterion is a spatial diagnostic, not a connectivity
or accuracy acceptance rule. Results are in
[independent-validation.csv](independent-validation.csv) and JSON/cache evidence.

- Both roads are spatially corroborated in **20/22** checks, including all ten
  reviewed accepted candidates and all four candidates of the ambiguous pairs.
- Two checks are uncorroborated under exact-name matching: Interstate 15 SB and
  Calle Tres Lomas. Naming conventions differ in the independent response; no
  extra fuzzy alias was added to force agreement. This is not a confirmed
  positional disagreement or proof of road absence.
- Crucially, the Friars/Mission Village **grade-separated pseudo crossing also
  passes the named-road spatial check**. Nearby named lines alone would produce
  a false physical-intersection claim. The mixed-level 13th/Imperial case is also
  spatially corroborated but remains uncertain.

This is an automated evidence review, not field inspection or independent
surveyed topology. TIGER and SanGIS may share underlying source information.
Neither this service comparison nor a unique SanGIS node certifies the historical
collision position. Current roads can differ from the collision-date network.
No defensible numerical false-positive rate or recovered-record count is claimed.

## Request accounting and cache

Exactly **40 direct REST attempts**, zero transport/provider failures:

| Kind | Requests |
| --- | ---: |
| SanGIS and TIGER metadata | 4 |
| Exact road-name batches | 10 |
| Intersection-node batches | 2 |
| Incident-segment batches | 2 |
| Independent TIGER checks | 22 |

[requests.json](requests.json) records every URL and attempt. The hard persisted
cap is 64 attempts; no automatic retry or pagination is allowed. Errors,
truncation, malformed features or unconfirmed CRS fail closed. Redirects are
disabled. Cached errors cannot trigger uncontrolled repeat calls. Final offline
replay made **zero new requests**.

The initial bounded captures contain 3,877 named-road features and 236 node
records; only 39 shared nodes are evaluated for valid sample pairs. Initial
broader node batches included endpoints from repeated-name placeholder pairs;
the final runner screens those before constructing node scopes and reuses the
already complete broader cache without new calls. This was a filtered subset,
not a full road-network download. Gzip caches total about 2 MB.

Separately, documentation research used seven bounded web search queries, one
official PDF open and three in-document find operations. These tool operations
are outside the direct REST ledger; their internal HTTP/cache behavior is not
observable as a request count. No Google API was used.

## Larger-scale potential and decision

The read-only [population workload](population-workload.json) covers all 6,839
pending reports with both streets; another pending record retains a cross street
but lacks a primary street. There are 3,988 distinct unordered stored field pairs.
Existing Census canonicalization gives 3,973 pairs by already-supported token/
ordinal equivalences; original values/hashes were not changed.

The textual screen passes **6,325 reports / 3,515 distinct pairs**, involving
2,679 canonical road names. It rejects 200 same-road placeholders, 96 compound/
incomplete descriptions and 218 descriptive offsets. These are direct whole-
population counts, **not extrapolations from the sample** and not recovered pins.
Known query variants total 2,899, which would require 242 groups at the current
12-name batch size before any pagination, node/incidence or validation requests.
That future workload exceeds this study's budget and was **not executed**.

Recommendation: proceed only to a separately approved **read-only local
candidate-processing pilot**, using an explicitly scoped and versioned open-road
graph and a larger independent validation set. This method has useful support
for ordinary constructed, compatible-level Type I junctions. Keep multiple-node,
type-conflict, long-road completeness, ramp/offset, alias and historical-network
cases unresolved. Do not deploy a batch geocoder or update collision rows yet.
Any future network acquisition must have its own request budget/approval; this
report does not authorize a full SanGIS download. A historical recovery rate
remains **unknown**.

## Reproduction and tests

From the repository root, replay everything **without database or network access**:

```powershell
.\.venv\Scripts\python.exe -m backend.verification.intersection_feasibility.study
.\.venv\Scripts\python.exe -m pytest backend/agent_tests/test_intersection_feasibility.py -q
```

`--live` permits only missing free-provider cache captures within the persisted
64-request cap, plus read-only sample/fingerprint checks. It reuses the frozen
sample and does not silently expand it. Missing offline evidence is an artifact
error, not a provider no-match. Do not remove the cache/ledger to bypass the cap.

Recheck textual workload counts with the restricted reader, without REST calls:

```powershell
.\.venv\Scripts\python.exe -m backend.verification.intersection_feasibility.study --audit-population
```

Focused tests: **60 passed, zero failed/skipped** in 1.83 seconds. They cover
ordinary/T junctions, multiple nodes, 2D crossings and overlaps, grade separation,
dead ends/name changes/splits, IDs/levels, directions, missing roads, malformed
coordinates, deduplication, truncated/failed REST, scoped cache reuse, caps,
deterministic classification and selection. All external responses are mocked.

Broader Agent suite: **365 passed, 36 skipped, zero failed** in 17.02 seconds.
The skips are existing guarded opt-in disposable-database integration cases;
none was initialized. Totals overlap the focused suite and are not added.
No full historical batch, paid API, new frontend or visual browser test was run.

Changed files: new `study.py`, `topology.py`, verification artifacts and
`agent_tests/test_intersection_feasibility.py`; small documentation links in
`backend/README.md` and `TRAFFIC_COLLISIONS.md`. Existing Agent, SQL, Census,
ingestion, geocoding persistence and environment configuration remain unchanged.
