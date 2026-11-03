# Census and SanGIS evaluation

Measured November 2, 2026. Recommendation: **Option A — Census primary, SanGIS
fallback after a legitimate Census no-match.** The implementation keeps Census
primary. SanGIS adds useful coverage, but this sample does not support replacing
Census or claiming better positional accuracy.

## Feasibility before integration

The [initial eight-address check](FEASIBILITY.md) preceded application changes.
Census uniquely resolved 6/8 addresses; SanGIS uniquely resolved 5/8, with one
ambiguous case and two no-matches. SanGIS supplied a Structure-labelled point
for Casa Mira View, 9800 Mira Lee Way, where Census returned no-match. This
incremental coverage justified the small fallback integration.

The live [official layer](https://geo.sandag.org/server/rest/services/Hosted/Address_Points/FeatureServer/0)
was inspected before queries; its response is archived in
[sangis-layer-metadata.json](sangis-layer-metadata.json). `addrnmbr` is Double;
street name, suffix, pre/post-direction, ZIP, community and jurisdiction are
separate fields. `addrjur=SD` identifies San Diego. The native coordinate system
is State Plane 102646/latest 2230; every query requests `outSR=4326` and validates
that response reference before interpreting x as longitude and y as latitude.

Documented placement codes are S Structure, E Entrance, C Centroid, U Unknown
and R Review. Source codes are S SanGIS and M Assessor Master Property Record.
The layer describes **SITUS property addresses**, which need not equal mailing
addresses, and assessor points placed at parcel centroids. Apartment-unit
completeness and entrance/rooftop accuracy are not established.

## Sample and method

[addresses.csv](addresses.csv) contains 36 independently verified addresses,
categories, locality/neighborhood labels, exact source URLs and considerations.
Sources include official UCSD/SDSU/USD pages, City library pages, apartment
operators, the Port, Zoo and museum, plus public residential property listings.
The 28 additional cases were frozen before their provider requests; see
[selection.json](selection.json). No addresses were selected from SanGIS records
or removed because a provider failed.

The sample includes 3 ordinary residential properties, 7 apartment addresses,
4 large apartment communities, 5 campus addresses, 13 civic locations, 2
commercial and 2 commercial/mixed-use sites. Its 27 neighborhood/locality labels
include overlapping campus and neighborhood descriptions, not 27 independently
verified neighborhood polygons. It spans northern, central, eastern and southern
San Diego. This is a convenience sample with civic/large-property bias; three
single-family properties cannot establish residential population performance.

Both providers were queried independently for every case. There were **72
address requests: 36 Census and 36 SanGIS**, each attempted once, with zero
transport/provider errors. The initial eight pairs were reused from the
feasibility captures; they were not queried again. Layer metadata and independent
source-document inspections are separate from that address-request count.
Exact URLs, timestamps, latencies and raw JSON are retained under `raw/`.

Matching validates number, complete street and directional components, supplied
ZIP, California/San Diego locality, geometry and the existing supported envelope.
Missing ZIP is not silently accepted when the input supplies one. Community is
retained as metadata, not treated as a postal-city synonym. SanGIS searches exact
number plus structured street name and checks all remaining components locally.
There is no fuzzy matching, whole-layer download or road interpolation. Results
therefore measure this conservative adapter protocol, not the maximum coverage
achievable with every possible SanGIS address alias or field segmentation.

During response inspection, two documented exact USPS equivalences were needed:
CALLE/CLL and PKY/PKWY. PLAZA/PLZA/PLZ is also normalized, without equating PLZ
with BLVD. See [USPS Appendix H](https://pe.usps.com/text/pub28/28aph.htm) and
[Appendix C1](https://pe.usps.com/text/pub28/28apc_002.htm). This refinement applies
only to interactive matching; historical collision normalization was untouched.
Initial outputs are retained as `initial-results.csv` and `initial-summary.json`
(Census 27, SanGIS 21 unique matches). Re-parsing the same raw responses with the
documented aliases produced the final numbers below. There were no extra
requests, selected-address changes, number changes or directional omissions.

## Coverage and ambiguity

Unique-match rate uses all 36 addresses as denominator, including errors if any.
Ambiguous, invalid and no-match responses do not count as unique matches.

| Outcome | Census | SanGIS |
| --- | ---: | ---: |
| Validated unique match | 28 (77.8%) | 22 (61.1%) |
| No match | 8 | 9 |
| Multiple valid locations / ambiguous | 0 | 3 |
| Invalid result after component checks | 0 | 2 |
| Provider errors | 0 | 0 |

| Unique coverage | Addresses |
| --- | ---: |
| Both providers | 19 |
| Census only | 9 |
| SanGIS only | 3 |
| Neither uniquely resolves | 5 |

Census-primary fallback uniquely resolves **31/36 (86.1%)**, an increase of three
addresses / 8.3 percentage points. The three additions are **9800 Mira Lee Way**,
**3195 Voigt Dr** and **9625 Scholars Dr N**. All three had Census no-match. The
post-direction N in Scholars Dr N remains part of the match.

All eleven apartment/community cases together give Census 8/11 and SanGIS 7/11
unique matches. The complete category breakdown is in [summary.json](summary.json).
This small category sample cannot establish general apartment coverage.

SanGIS ambiguous cases are 9500 Gilman Dr, 1100 Market St and 1450 El Prado.
Census already resolves the latter two, so production fallback does not query
SanGIS for them. The two SanGIS invalid cases are:

- **100 Park Plz:** returned features describe 100 Park **Blvd**, not the requested
  Plaza. They are rejected; the house number alone does not establish a match.
- **5500 Campanile Dr:** ten returned features lack ZIP; input explicitly supplies
  92182. They fail this conservative policy rather than being reported as an
  empty provider result.

Other cases without a unique result from either provider are 7918 Collective
Way and 1 Park Blvd, in addition to those two and ambiguous 9500 Gilman Dr.
Official campus/mailing or shared-site addresses need not denote one entrance.

Across raw SanGIS results, case-level diagnostics include 0 wrong-number cases,
2 street-component disagreement cases, 0 directional conflicts, 4 missing-ZIP
cases, 1 conflicting-ZIP case, 5 missing-community cases and 1 conflicting
jurisdiction/state case. Some are extraneous rejected features alongside an
accepted match: for example, a Vista 330 Park Ave feature is rejected for the
330 Park Blvd query. These counts **do not mean accepted candidates had those
conflicts**. Parser rejection counts record the first failed validation per
feature; raw case diagnostics can expose multiple conflicts in the same feature.

## Special case: 9500 Gilman Drive

Input: `9500 Gilman Dr, La Jolla, CA 92093`, independently documented on
[UCSD's contact page](https://www.ucsd.edu/about/contact.html). UCSD also
[documents building-specific addressing](https://facilityservices.ucsd.edu/information/addressing.html);
the campus mailing address is not assumed to identify a unique building.

- Census: **no_match**, no coordinates.
- SanGIS: seven raw features; four lack the requested ZIP and are rejected.
  Three full-address/ZIP/SD features form **two distinct coordinates**:

| Placement/source | Latitude | Longitude | Preserved identifiers |
| --- | ---: | ---: | --- |
| Structure / SanGIS | 32.87728328608443 | -117.23618279140439 | objectid 771466; APN 3440801600 |
| Centroid / Assessor | 32.88458878729452 | -117.23961381126536 | objectids 1071683, 1126415; APNs 7601039705, 7601035100 |

The two centroid features coincide exactly and retain both source records.
Distinct positions are not collapsed or ranked by placement. The surviving
components match the requested address; a missing-ZIP feature is not used as a
substitute. SanGIS supplies usable **candidates**, not a uniquely resolved pin.
The interface requires an explicit choice and displays provider, precision and
coordinates. No SQL analysis runs until that choice resolves the location.

## Coordinates, placement and latency

Coordinate distances use WGS84 geodesic distance (`pyproj.Geod`, already a
dependency) for the 19 cases with validated unique matches from both providers.
Percentiles use linear interpolation between ordered samples.

| Distance statistic | Meters |
| --- | ---: |
| Median | 74.875 |
| Mean | 157.932 |
| 90th percentile | 334.082 |
| Maximum | 1,039.786 |
| Cases above 50 m | 13 of 19 |
| Cases above 100 m | 7 of 19 |

Above-100 m cases are 17110 Bernardo Center Dr (109.564 m), 4235 Beyer Blvd
(108.830 m), 10301 Scripps Lake Dr (318.421 m), 10615 Calle Mar De Mariposa
(396.726 m), 11100 Vista Sorrento Pkwy (287.645 m), 2920 Zoo Dr (1,039.786 m)
and 4739 Redland Dr (176.775 m). Every above-50 m case is identified in
`summary.json` and [results.csv](results.csv).

These are disagreement diagnostics, **not ground-truth accuracy measurements**.
They justify retaining spatial limitations in answers. Agreement can also occur
when both coordinates are wrong; the 50/100 m thresholds do not automatically
accept or reject a candidate.

SanGIS unique matches include **18 Structure, 2 Centroid and 2 Review** labels;
there are no Entrance, Unknown or undocumented placement codes among these
22 matches. A Review label remains `review_required`; a Centroid remains
`parcel_centroid`. None proves an apartment unit or building entrance.

| Measured request latency | Census | SanGIS |
| --- | ---: | ---: |
| Median | 411.134 ms | 271.418 ms |
| 90th percentile | 438.707 ms | 294.595 ms |

Requests were sequential, in fixed Census-then-SanGIS order, from one workstation
with 36 observations per provider. This does not establish general performance,
capacity, cache behavior or production tail latency. Fallback incurs both
requests when Census has no match.

## Integration and verification

The existing LLM interpretation, explicit address review, correction, candidate
selection, LangGraph nodes, SQL tools and evidence verification remain in place.
Only confirmed addresses are sent to providers. Census success, ambiguity,
invalid output or provider errors never invoke SanGIS. SanGIS HTTP uses bounded
timeouts/retries; ArcGIS JSON errors remain provider errors, not no-match.
Truncated results, wrong CRS, malformed geometry and component conflicts fail
closed. Only exact coincident duplicate full-address points are grouped, with
all source identifiers preserved.

No database schema, role, raw CSV, historical collision coordinate or status was
changed. No historical geocoding pipeline or paid/live OpenAI call was run. The
existing coordinate-entry mode is available; a manual map-pin widget is not.

Verification:

- Focused backend suite: **89 passed, 0 failed, 0 skipped** in 2.70 seconds.
  Covers fallback routing, timeouts/errors, malformed fields, address conflicts,
  WGS84, metadata, duplicates, confirmation/correction, mixed-provider locations,
  identical analytical filters and no SQL before all pins resolve.
- Broader existing `backend/agent_tests` regression suite: **243 passed,
  36 skipped, 0 failed** in 13.46 seconds. Disposable-database integration
  cases were not enabled; their guarded fixture requires an explicitly selected
  empty test database or opt-in temporary Docker database. No full historical
  real-data verification suite was run. These 243 passes include the focused
  suite; the counts must not be added.
- Frontend: **six scenarios passed** (three startup and three location-flow),
  using the existing Node DOM harness. This is not a full browser visual test.
- [Integration smoke](integration-smoke.json): recorded **live** provider
  responses replayed through the default fallback adapter and FastAPI, mocked
  LLM, **two real read-only Agent SQL calls**, zero new provider requests.
  Mixed Census/SanGIS comparison generated evidence. UCSD remained awaiting a
  candidate choice with no additional SQL. All-column collision fingerprints
  before/after were equal: 78,494 records, 59,627 with locations.

## Reproduction

From the repository root, re-evaluate saved responses **without network calls**:

```powershell
.\.venv\Scripts\python.exe -m backend.verification.geocoding_benchmark.benchmark --offline --limit 36
.\.venv\Scripts\python.exe -m pytest backend/agent_tests/test_sangis_geocoding.py backend/agent_tests/test_location_flow.py -q
```

`benchmark.py` without `--offline` captures only missing cache entries, once per
provider/address; existing entries are reused. Do not remove the raw cache or
expand the sample without budgeting additional requests. Cached results are a
dated snapshot, not a promise of current provider availability. `results.csv`
stores coordinates as **[longitude, latitude]**; ambiguous candidate arrays are
separate from unique coordinates. `address_mismatch` flags rejected extraneous
features as well as invalid whole results; `details.json` explains each case.

The optional controlled SQL smoke requires the existing restricted reader
configuration and performs no new external calls:

```powershell
.\.venv\Scripts\python.exe -m backend.verification.geocoding_benchmark.integration_smoke
```

## Changed files and deferred work

| Files | Purpose |
| --- | --- |
| `agent/sangis_geocoding.py` | New strict Address Points parser, provenance and Census-primary fallback |
| `agent/geocoding_http.py` | New bounded public JSON transport with sanitized diagnostics |
| `agent/user_geocoding.py` | Shared interactive Census parsing, preserved supplied locality/ZIP, exact token aliases |
| `agent/location_models.py`, `agent/location_flow.py` | Additive ZIP/post-direction/diagnostics; reuse confirmation states |
| `agent/workflow.py`, `agent/llm.py` | Default fallback adapter, precision caveat, preserve supplied address fields |
| `frontend/app.js`, `frontend/index.html` | Provider/precision/candidate coordinates, generic confirmation wording, script version |
| `agent_tests/test_sangis_geocoding.py`, `frontend/tests/location-flow.test.cjs` | Focused provider regressions and adjusted UI expectation |
| `README.md`, `AGENT.md`, `P1_2.md` | Updated workflow and provider limitations |
| This directory | Address provenance, archived metadata/raw responses, scripts, metrics and report |

Keep Census primary pending review. A larger independently stratified sample
and surveyed/building/entrance references are needed before changing primary
provider or asserting positional superiority. Exact-name field-segmentation
aliases, missing ZIP policy and difficult campus/mailing addresses merit a later
review. Any additional provider, broader live lookup, map interface or historical
enrichment is deferred; none is necessary to deploy this bounded fallback.
