# P1.2.2 historical collision location audit

Observed November 2, 2026 using the existing restricted Agent reader. Queries
ran in a repeatable-read, read-only transaction. No reports, coordinates, statuses,
tables, roles or permissions were changed. Full-row database fingerprints before
and after were identical; see [summary.json](summary.json).

## Current coverage and status patterns

| Stored status | Records | With coordinates |
| --- | ---: | ---: |
| matched | 59,627 | 59,627 |
| no_match | 7,400 | 0 |
| ambiguous | 3,117 | 0 |
| pending | 8,350 | 0 |
| Total | 78,494 | 59,627 (75.96%) |

Matched coordinates remain provider-estimated **road-block** locations, not
verified accident positions. All stored component strings in the 160-record
sample agree with normalization of their original CSV address fields. This does
not establish that the reported source address itself is correct.

The original source fields and stored provider evidence distinguish:

- **Pending:** none is eligible for the current positive-number, primary-street,
  no-intersection Census adapter. All 8,350 lack a positive source block number;
  6,840 retain cross-street information. There are 3,988 distinct complete
  unordered primary/cross-street pairs; one record lacks a primary street.
  These are unprocessed/unsupported shapes, not confirmed Census failures.
- **No-match:** 7,396 record `provider_nomatch`; four have no stored reason.
  All 7,400 are textually eligible for the existing adapter; that does not make
  them valid property addresses. There are 2,688 distinct primary-block queries.
- **Ambiguous:** 2,433 were rejected for returned street components, 138 for
  number differences, 544 have a provider tie, and two have a legacy combined
  mismatch reason. These distinctions matter: not all ambiguous rows represent
  multiple candidate coordinates. There are 1,411 distinct primary-block queries.
- **Descriptive offsets:** text such as `FT ... OF` occurs in 96 ambiguous,
  58 no-match and 75 pending reports. A numeric prefix can describe distance,
  not a house or anonymized block number. The current normalizer preserves it;
  it must not be silently converted into a building address.

Example stored evidence includes `10 FT E OF WCL OF 4600 NIMITZ BLVD` returning
`4600 NIMITZ BLVD`, and `10 S OF THE NCL 3800 QUARTER MILE DR` returning 3800.
Those number changes were correctly rejected; do not reinterpret them as safe
house-number repairs. Other sampled mismatches include BLACK MTN / BLACK
MOUNTAIN, CALLE / CLL, quoted F / F, DOCOS / DUCOS, and an added PARKING LOT
qualifier. They require different handling; a broad fuzzy-match relaxation
would incorrectly combine real differences.

## Reproducible stratified sample and open-road evidence

[sample.csv](sample.csv) and [sample.json](sample.json) contain **160 existing
reports: 40 per status**, selected deterministically with seed
`citylens-p1.2.2-v1`. Within status, the sampler cycles through road/address type,
intersection availability, number completeness and police-beat first-digit
strata. Beat first digit is an area proxy, **not a verified neighborhood polygon**.
Rare patterns are deliberately overrepresented; for example the no-match sample
contains 14 freeway/ramp-labelled records although that text classifier finds
only 16 in the entire no-match population. Do not extrapolate unweighted rates.

The official [City road dataset](https://data.sandiego.gov/datasets/gis-roads-all/)
and live [SanGIS Roads_All layer](https://geo.sandag.org/server/rest/services/Hosted/Roads_All/FeatureServer/0)
were inspected. Metadata, including documented `segclass` and left/right address
ranges, is archived in [roads-layer-metadata.json](roads-layer-metadata.json).
Eight bounded exact-name REST queries obtained 3,521 distinct relevant road
features, requested in WGS84, without downloading the full layer. The compressed
cache `roads-sample.json.gz` retains query URLs, timestamps and source attributes;
subsequent audit runs reuse it without network access.

The script checks exact normalized road names and whether the reported number
falls within a positive left or right segment address range. Distances are from
existing Census points to projected road polylines in local UTM 11N meters.
This is a **plausibility check**, with no interpolation or newly inferred pin.
Current road geometry cannot establish road configuration at every historical
event date; road providers may also share underlying sources.

| Status sample | Named road support | Number-range support |
| --- | ---: | ---: |
| matched (40) | 40 | 40 |
| no_match (40) | 24 | 21 |
| ambiguous (40) | 24 | 23 |
| pending (40) | 19 | 0 |

No road match from this conservative query is not proof that the road is absent:
aliases, field segmentation and description text can prevent exact-name lookup.
Cross-street geometry was not exhaustively queried or intersected in this audit.
Counts represent supporting segments, not unique verified accident locations.

For the 40 matched samples, distance to the closest same-name road has median
**5.538 m**, mean **5.518 m**, maximum **8.246 m**. Restricting to segments whose
address range supports the reported block gives median **5.766 m**, mean
**17.297 m**, maximum **109.589 m**; five exceed 50 m and one exceeds 100 m.
These warrant individual review. They are not direct error measurements or
evidence that a property address point would be a better accident location.

## Opportunities and uncertainty

1. **Intersection records:** prioritize the 6,840 pending reports retaining
   cross-street information, representing 3,988 complete unique road pairs plus
   one incomplete-primary report. Candidate work is exact aliases, connectivity,
   grade separation and jurisdiction checks against reusable road/intersection
   data. These are workload ceilings, not a forecast of 6,840 recovered pins.
2. **Stored component mismatches:** review the 2,433 street mismatches separately
   from 138 number mismatches and 544 ties. Existing stored Census evidence may
   permit offline revalidation after independently documented exact aliases,
   without new provider requests. Material qualifier/direction/number changes
   must remain rejected. No alias map was applied to historical data here.
3. **Numbered no-matches:** 21 of the 40 sampled no-match reports have road-name
   and number-range support. This makes a later open-data road-block feasibility
   study worthwhile. Segment uniqueness and source block semantics remain
   unresolved, so it does not establish 21 recoverable coordinates.
4. **Offsets, ramps, missing blocks:** require specialized source interpretation
   and route/topology evidence. Neither a parcel centroid nor a campus entrance
   repairs missing collision-location information.

The recovery estimate remains **null/unknown**. The stratified audit is too small
and has no validated repair outcomes from which to derive defensible population
recovery rates or confidence intervals. Additional geometry/topology validation
should be separately approved before any enrichment migration or data update.

## Query counts and hypothetical costs — not an execution plan

There are **18,867 unresolved reports** and **9,271 unique full source-address
hashes** across them. Primary-block-only deduplication gives 6,627 strings but
drops meaningful intersection distinctions; it is not a safe bulk-query unit.
Exact block/intersection semantics and unique road-segment candidates should
determine any future open-data workload, using the cached road evidence first.
The separately proposed historical provider is open SanGIS geometry, with no
paid geocoder requests authorized or required by this audit. Hosting/engineering
cost is not established here.

For scale only, at the [Google base list price](https://developers.google.com/maps/billing-and-pricing/pricing)
of USD 5/1,000, submitting all unresolved rows would be **USD 94.335** before
free allowance; one query per unique full source hash would be **USD 46.355**.
Those figures do not account for retries, taxes, shared monthly usage or the
fact that many source records are unsuitable geocoding requests. **Neither
operation is approved or recommended, and zero historical records were sent to
Google.** No property geocodes should become permanent historical accident pins.

## Reproduction and recommendation

Existing restricted-reader configuration is required. The default command uses
the frozen open-road cache and performs read-only database inspection:

```powershell
.\.venv\Scripts\python.exe -m backend.scripts.audit_historical_locations --sample-size 160
```

`--fetch-roads` was used once to populate missing cache data, capped at eight
requests. It does not refresh existing cache automatically. A changed sample
outside the cache's queried names is rejected rather than misreported as absent
road coverage. No migration, ingestion or geocoding command is involved.

Keep Census as the existing historical block baseline; the sample provides no
independent evidence for replacing its matched pins. Prioritize intersection
feasibility and documented alias review, then exact road-block/range validation
for no-matches. Any actual repair, interpolation or enrichment remains a separate
milestone with independent checks and explicit approval.
