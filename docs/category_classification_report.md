# District 1 Get It Done: Classification Report

## Dataset overview

The supplied snapshot contains **303,226 rows and 23 fields**, with request dates from 2016-05-20 through 2026-10-26. It contains 44 nonempty service names. The approved mapping has 79 rules, nine main categories and 66 designed subcategories. Counts below are report counts, not unique physical problems.

Source: `backend/Raw Data/Get it done/get_it_done_requests_CD1_datasd.csv`. Source SHA-256: `876a8a8b505d2339a812ee34e7cffb487eb21d309d97d61f1059a8fbd9dcecab`. Mapping SHA-256: `9fe7f58f2f40b52bd501113d4f0e86feb3bde3d85305bd166cf934f05f9bd9d3`. Runtime: pandas 3.1.1.

## Data scope and filtering

The Explorer scope contains **31,667 distinct request IDs** dated **2026-01-01 through 2026-10-26**, inclusive, in Council District 1. Normalize IDs and keep the last CSV row for each ID before filtering, matching `explorer_data.normalize` and cache replacement/deletion semantics. Require a nonempty ID and a valid 2026 request date. Dates retain source-local wall time; no UTC conversion is applied. The actual first and last timestamps are 2026-01-01 03:48:00 and 2026-10-26 22:47:39.

Do not filter by detail availability, status, coordinates or map viewport. All 3,490 parent-linked requests remain included, matching the default Explorer. The script separately reports counts excluding linked requests. It stops if the fixed snapshot no longer has 31,667 scoped records or contains dates outside the agreed window.

## Data quality and missing values

| Field | Raw missing | Raw % | Explorer missing | Explorer % |
| --- | --- | --- | --- | --- |
| service_name | 424 | 0.1398 | 319 | 1.0074 |
| service_name_detail | 116574 | 38.4446 | 14277 | 45.0848 |
| case_record_type | 0 | 0.0 | 0 | 0.0 |
| service_request_id | 0 | 0.0 | 0 | 0.0 |
| date_requested | 0 | 0.0 | 0 | 0.0 |
| lat | 0 | 0.0 | 0 | 0.0 |
| lng | 0 | 0.0 | 0 | 0.0 |

The raw source has 1 excess ID row and 0 exact duplicate rows. The Explorer has 0 duplicate IDs. The repeated raw ID is outside the 2026 scope. Invalid nonempty request dates: 0 raw / 0 Explorer. Missing or invalid coordinate pairs: 0 raw / 0 Explorer.

Repeated valid timestamps contribute 19,467 excess raw rows and 1,941 Explorer rows after QHY normalization to seconds. Equal timestamps do not imply duplicate requests and do not justify deletion. Coordinate checks cover numeric parsing, finiteness and global latitude/longitude ranges; they do not establish District 1 polygon membership.

Missing `service_name` maps to `Needs Review / Missing service name`; source `Other` maps to `Needs Review / Source Other - unresolved`. Missing detail uses the service default, without inventing a specific cause. All 14,277 Explorer rows missing detail remain: 12,804 use `service_default` and 1,473 use `needs_review`. No source value is filled, overwritten or deleted.

## Classification methodology

Use issue type as the primary organizing principle, with the explicitly retained review boundaries below. Whitespace normalization uses `explorer_data.text` in derived columns only. For an exact service name, an `in` rule matches a pipe-separated set of exact normalized details and overrides its service default. A `fallback` handles all remaining details; `any` is unconditional and cannot coexist with detail conditions. Matching is case-sensitive, with case/spacing/punctuation diagnostics rather than automatic fuzzy reassignment. Unknown service names fail validation; unmatched details keep the specified default.

`classification_method` priority is: final `Needs Review` -> `needs_review`; otherwise an explicit detail condition -> `detail_match`; otherwise -> `service_default`. The original columns, IDs, row order and source values remain unchanged. `case_record_type` is a receiving workflow, not sufficient evidence to impute issue type.

## Final nine-category hierarchy

Parentheses contain actual Explorer counts, including zero-count designed subcategories.

| Main category | Subcategories (Explorer count) |
| --- | --- |
| Sanitation & Waste | Biohazard report (0); Container request (0); Dead animal removal (291); Delayed collection (0); ESD collections - unspecified (388); Environmental services compliance (767); Illegal dumping (3,551); Litter (42); Missed collection (3,506); Park litter and bins (53); Potential missed collection (221); Shopping cart (0); Street sweeping (252); Trash and recycling collection (148); Waste on private property (98) |
| Traffic & Mobility | Bicycle facility maintenance (16); Bicycle-related issue (1); Road markings (257); Shared mobility device (0); Street lighting (662); Traffic devices and delineators (121); Traffic engineering and evaluation (293); Traffic maintenance - unspecified (1); Traffic sign maintenance (428); Traffic signal fault (657); Traffic signal timing (481) |
| Parking & Vehicles | Oversized vehicle (710); Parking - 72 hours (4,032); Parking - unspecified (11); Parking obstruction (1,511); Parking zone or time restriction (1,333); Residential parking permit (60) |
| Community & Property Services | Building and land-use enforcement (354); COVID-19 service report (0); Development services enforcement (0); Encampment report (2,748); Homeless outreach (0); STRO violation report (501) |
| Parks & Vegetation | Park activity and enforcement (429); Park facilities (256); Park feedback and improvements (25); Park irrigation and drainage (131); Park paths and pavement (18); Park vegetation and grounds (158); Parks - other or unspecified (300); Tree maintenance (837); Vegetation encroachment (449); Weed cleanup (29) |
| Roads & Sidewalks | Pavement maintenance (125); Pothole (1,561); Right-of-way enforcement - unspecified (0); Right-of-way maintenance - unspecified (34); Roadside barriers and structures (43); Sidewalk repair (514); Trenches and right-of-way enforcement (16) |
| Water & Drainage | Stormwater compliance (279); Stormwater infrastructure (364); Street flooding and ponding (184); Surface water flow (33) |
| Graffiti | Graffiti enforcement (145); Graffiti removal - unspecified (0); Park graffiti (152); Private graffiti (21); Public graffiti (597) |
| Needs Review | Missing service name (319); Source Other - unresolved (1,154) |

## Main-category counts and percentages

Raw percentages use all source rows; Explorer percentages use distinct scoped requests. Rounding can cause small differences when displayed percentages are added.

| main_category | Raw rows | Raw % | Explorer rows | Explorer % |
| --- | --- | --- | --- | --- |
| Sanitation & Waste | 88887 | 29.3138 | 9317 | 29.4218 |
| Traffic & Mobility | 51018 | 16.8251 | 2917 | 9.2115 |
| Parking & Vehicles | 50363 | 16.6091 | 7657 | 24.1797 |
| Community & Property Services | 20913 | 6.8968 | 3603 | 11.3778 |
| Parks & Vegetation | 19980 | 6.5891 | 2632 | 8.3115 |
| Roads & Sidewalks | 32534 | 10.7293 | 2293 | 7.241 |
| Water & Drainage | 10659 | 3.5152 | 860 | 2.7158 |
| Graffiti | 15156 | 4.9983 | 915 | 2.8894 |
| Needs Review | 13716 | 4.5234 | 1473 | 4.6515 |

## Classification provenance

| classification_method | Raw rows | Raw % | Explorer rows | Explorer % |
| --- | --- | --- | --- | --- |
| detail_match | 53310 | 17.5809 | 5319 | 16.7967 |
| service_default | 236200 | 77.8957 | 24875 | 78.5518 |
| needs_review | 13716 | 4.5234 | 1473 | 4.6515 |

## Approved parking and park changes

Parking & Vehicles contains six approved issue subcategories: Oversized vehicle; Parking - 72 hours; Parking - unspecified; Parking obstruction; Parking zone or time restriction; Residential parking permit. All 14 corresponding mapping rows were moved, including the detailed conditions under historical `Parking` and `Parking Violation`. This assigns 50,363 raw rows and **7,657 Explorer requests (24.1797%)** to Parking & Vehicles. Shared mobility devices and bicycle issues remain in Traffic & Mobility. The split follows the final rule-selected issue, rather than a blanket service-name reassignment.

The previous approval remains: `Parks Issue + Graffiti` -> `Graffiti / Park graffiti` (716 raw / 152 Explorer). The new approval maps `Parks Issue + Trash Can/Overflowing` and `Parks Issue + Trash/Litter` to `Sanitation & Waste / Park litter and bins` (126 raw / 53 Explorer). Both park conditions override the Parks Issue default. Other graffiti subcategories are unchanged.

The following comparison uses the preceding Phase 2 mapping: park graffiti was already in Graffiti, while parking and park litter had not yet moved.

| main_category | before | after | delta |
| --- | --- | --- | --- |
| Sanitation & Waste | 9264 | 9317 | 53 |
| Traffic & Mobility | 10574 | 2917 | -7657 |
| Parking & Vehicles | 0 | 7657 | 7657 |
| Community & Property Services | 3603 | 3603 | 0 |
| Parks & Vegetation | 2685 | 2632 | -53 |
| Roads & Sidewalks | 2293 | 2293 | 0 |
| Water & Drainage | 860 | 860 | 0 |
| Graffiti | 915 | 915 | 0 |
| Needs Review | 1473 | 1473 | 0 |

## Remaining ambiguities and limitations

- Park irrigation/drainage (131), paths/pavement (18) and mixed activity/enforcement (429) remain in Parks & Vegetation, as instructed. Broken drains, sprinkler maintenance, trail defects and park parking need individual review before further changes.

- Street lighting (662) remains in Traffic & Mobility. Its relationship to public-facility maintenance, along with park lighting and restroom cleanliness, remains open.

- Labels explicitly containing unspecified/unresolved/missing account for 2,207 Explorer records (6.9694%). This lexical measure is not an accuracy score and excludes broad labels without those words. ESD collections, AVA, DSD BLUE and Quality of Life Issues do not justify more specific inferred causes.

- Historical naming changes, inconsistent capitalization, Community Clean Up/Community Cleanup, the source spelling Polution and workflow-maintenance text complicate comparisons. Source labels remain intact. Closed does not establish repair; reports are affected by reporting behavior, linked submissions and incomplete-year coverage.

## Validation and reproducibility

All 36 scope-specific classification checks passed. All nine main-category counts sum to 31,667; every scoped record has exactly one main category and subcategory. No records or original fields were changed. The only category changes from Phase 2 are the approved parking split and park litter move; subcategory names and classification provenance are unchanged.

All source service labels are covered. No overlapping condition values, multiple conditional matches or conflicting defaults were found. There are 0 near-label condition candidates after case/spacing/punctuation normalization. Semantic equivalence still needs human review. Source CSV and existing Explorer cache/sidecar fingerprints (SHA-256, size and mtime) were unchanged. The script never calls cache build, refresh or SQLite connection functions.

Run from the repository root using the existing environment:

```bash
.venv/bin/python backend/category_analysis.py --write-report
.venv/bin/python -m backend.category_analysis --compact
git diff --check
```

The first command regenerates this concise report and prints the complete English audit. Omit `--write-report` for stdout-only analysis. The full audit includes every source/detail pair, all rule hit counts, all designed subcategory counts and validation results. Only pandas and the standard library are required.

## Backend integration status

The approved mapping is integrated into the Explorer SQLite projection and API, with main/subcategory filters, the complete metadata hierarchy and monthly/community comparisons. See [the API integration contract](explorer_classification_api.md). The frontend and source CSV remain unchanged. Versioned local caches rebuild atomically when the source signature, schema version or mapping content changes. This analysis script itself remains read-only except for explicitly requested report generation. Frontend integration is the next step.
