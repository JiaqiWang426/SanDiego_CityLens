# San Diego CityLens — Evidence-Based Agent Specification

**Version:** 0.1 (proposed design)  
**Status:** Specification, **not** a claim that the Agent or proposed tools are implemented  
**Primary use case:** Compare candidate rental locations using traceable, bounded queries against the project's preloaded civic datasets.  
**Current data domains:** Get It Done service requests and police-recorded traffic collisions.  
**Proposed application stack:** FastAPI, LangGraph, an LLM with structured tool calling, Python, PostgreSQL/PostGIS.

> **Design principle:** The LLM decides *which questions to ask* and *how to explain findings*. Read-only tools and deterministic code decide *what the data says*. Every quantitative user-facing claim must be traceable to a reproducible query and its limitations.

## 1. Goals and boundaries

### 1.1 MVP goals (MUST)

1. Answer factual questions about service-request counts and collision reports for a specified point, radius, and time window.
2. Compare **two** candidate locations using the **same** time window, radius, filters, and measurement definitions within each data domain.
3. Explain trade-offs using observed evidence rather than an LLM-invented score.
4. Attach evidence references, query settings, coverage caveats, and data provenance to major claims.
5. Avoid unsupported conclusions when queries return zero, geocoding is incomplete, or the imported data is only a subset.
6. Provide a structured response that FastAPI can return to a frontend and that can be evaluated automatically.

### 1.2 Non-goals for MVP (MUST NOT claim)

- Predict future collision risk, property crime, rent, or neighborhood safety.
- Declare a property or neighborhood “safe” or “unsafe” based on these two datasets.
- Calculate an objective, validated overall **safety score**, or claim causal impact.
- Infer incident rates per resident, trip, vehicle, pedestrian, or exposure: those denominators are not available.
- Provide commute times, amenity coverage, planning/development risk, or apartment listings until corresponding datasets and tools exist.
- Search the internet or execute unrestricted text-to-SQL as part of the initial Agent.
- Treat a request status of `Closed` as proof that a physical issue was fixed.

### 1.3 Supported MVP user intents

| Intent | Example | Required behavior |
|---|---|---|
| `location_summary` | “What do public records show near this pin?” | Query supported domains; name missing dimensions. |
| `compare_locations` | “Compare apartment A and B for road incidents and service complaints.” | Execute matching queries for both locations; explain trade-offs. |
| `collision_trend` | “Have recorded collisions increased near A?” | Use time-bucketed aggregation; handle empty periods deterministically. |
| `collision_drilldown` | “What reports support that number?” | Retrieve a bounded page of matching collision reports. |
| `request_count` | “How many Get It Done reports were filed near this place?” | Return report-ID count and duplicate/subset caveats. |
| `unsupported_dimension` | “Which building has less crime / shorter commute?” | State unavailable data; do not substitute collision counts for crime or commute. |

## 2. Existing implementation and factual data contract

This section describes **documented current capabilities**, based on the supplied README, TRAFFIC_COLLISIONS.md, and two SQL schemas. The four `sql/tools/` function bodies and Python wrappers were not supplied for code review, so exact runtime behavior beyond the documentation is **not independently verified**.

### 2.1 Storage

- `citylens.service_requests`: one *current* record per `service_request_id`; stores `requested_at`, `parent_id`, `service_name`, `status`, nullable `location`, `raw_record`, and `ingestion_id`.
- `citylens.ingestions`: import filename, SHA-256, import timestamp, and counts.
- `citylens.traffic_collisions`: one *current* record per `report_id`; stores nullable `occurred_at`, source descriptors, injury/fatality counts, raw record and issues, and nullable approximate geocode.
- `citylens.collision_imports`: collision import metadata.
- `citylens.collision_source_rows`: per-import raw parsed rows, including invalid and repeated rows.
- Collision positions, when available, are labeled `block_approximate` and **are not exact crash locations**.

### 2.2 Existing query tools to expose

| Agent-facing adapter | Underlying documented function | What it can support | Important restriction |
|---|---|---|---|
| `count_service_requests` | `citylens.count_service_requests(...)` | Count matching request IDs in a radius/time window; optional exclusion of explicitly parent-linked requests | Does not break down `service_name`, `status`, or infer distinct physical issues. |
| `get_traffic_collisions` | Python `get_collisions` / `citylens.get_traffic_collisions(...)` | Retrieve bounded collision records with filters | Use bounded results for evidence inspection, **not** to compute an uncapped total from one page. |
| `aggregate_traffic_collisions` | Python `aggregate_collisions` / `citylens.aggregate_traffic_collisions(...)` | Counts grouped by day/week/month/quarter/year; includes documented coverage information | Time-bucket rows omit empty periods; spatial queries exclude records without coordinates. |

**Internal only:** `citylens.filter_traffic_collisions(...)` is an uncapped shared SQL helper; do **not** expose directly to the LLM.

Existing query semantics to preserve:

- Date intervals: `[start, end)`; use offset-aware timestamps and `America/Los_Angeles` for calendar interpretation.
- Location filter: coordinate point + radius in meters; supported radius is documented as 0–50,000 m.
- Get It Done timestamps denote **request creation**, not occurrence or repair.
- Collision timestamps denote **recorded occurrence**; missing/invalid dates cannot satisfy a date filter.
- `injured` and `killed` can be `NULL` (unknown), distinct from zero; numeric filters exclude unknowns.
- Collision radius membership can be uncertain near the radius boundary because the position is approximate.
- The imported Get It Done CSV is designated **CD1**; citywide completeness is **not established**.
- Imports are **upserts**, not full snapshot replacements: records missing from a later CSV remain in the normalized table.
- Import timestamps / hashes prove what was processed and when; they do **not** prove the civic source is current.

## 3. Ambiguities and missing interfaces

| Priority | Question / gap | Why it matters | Proposed resolution |
|---|---|---|---|
| P0 | **What does “near” mean?** | Radius strongly changes counts. | Default to **800 m** for location comparisons only when not specified; display radius prominently; use the same value for A and B. Treat this as a product default, not a scientific standard. |
| P0 | **What time interval applies?** | Counts cannot be compared across different observation windows. | User-specified interval wins. Otherwise request a common window justified by known data coverage; never assume the latest imported file covers the past year. A configurable “past 12 months” default is permitted only if coverage has been checked. |
| P0 | **How is an apartment address converted to a coordinate?** | Current collision geocoding is for *collision records*, not user input. | MVP accepts map pins / explicit coordinates; add a separate `resolve_location` adapter before accepting free-form rental addresses. |
| P0 | **What dataset and geography does CD1 cover?** | An apparent zero can reflect an excluded area, not absence of reports. | Publish a data catalog with source URL, source-specific region/subset, observed date span, update policy, and verification status. Until verified, label CD1 results as *imported-subset counts*. |
| P0 | **How complete is collision geocoding?** | Spatial queries silently exclude unlocated reports. | Add query-level coverage metadata: dated, located, excluded-unlocated, and geocode-status counts where legitimately computable. Do not present global coverage as neighborhood-specific coverage. |
| P0 | **What counts as “better”?** | An LLM may turn raw report counts into a fabricated safety ranking. | MVP uses dimension-by-dimension comparisons only. “Preferred” may be conditional on explicit user priorities; do not infer absolute safety. |
| P0 | **How can a claim cite its source?** | A raw count is not user-auditable by itself. | Introduce `EvidenceRecord` with reproducible filters, source version/import, precise metric definition, and linked report IDs where appropriate. |
| P1 | **Which service categories/statuses are relevant?** | A single count mixes types of complaints; e.g., sidewalk reports and graffiti reports are not equivalent. | Add `aggregate_service_requests` grouped by source `service_name` / `status`, plus a controlled taxonomy with an explicit `other/unknown` bucket. Do not invent mappings. |
| P1 | **What is the meaning of report `status`?** | `Closed` need not mean repaired or resolved. | Preserve raw status and document valid categories; do not convert it into repair duration until lifecycle timestamps exist and are verified. |
| P1 | **What happens at DST repeated-hour times?** | Ambiguous wall times can produce different instants. | Record current PostgreSQL behavior as an assumption; flag ambiguous source times and exclude from strict time-boundary evaluation unless a verified rule exists. |
| P1 | **What is a “neighborhood”?** | A circular radius around a pin is not a neighborhood boundary. | Say “within X meters of the selected point”; add official neighborhood polygons only in a separate phase. |
| P2 | **What is a fair overall ranking?** | Traffic counts depend on exposure; complaint counts depend on reporting behavior. | Defer weighted scores until denominators, missingness handling, and validated benchmarks exist. |

## 4. System architecture

```text
Frontend / Client
      |
      v
FastAPI: POST /api/v1/agent/query
      |
      v
Input validation + intent / location / timeframe normalization
      |
      v
LangGraph orchestration (single LLM planner, bounded steps)
      |                    \
      |                     -> unsupported-scope handling / clarification
      v
Tool registry: typed, allowlisted, bounded, read-only adapters
      |
      +-- count_service_requests ----------> PostgreSQL/PostGIS
      +-- get_traffic_collisions ----------> PostgreSQL/PostGIS
      +-- aggregate_traffic_collisions ----> PostgreSQL/PostGIS
      +-- get_dataset_metadata (proposed) -> database/catalog
      +-- resolve_location (proposed) -----> location resolver
      +-- aggregate_service_requests (P1) -> PostgreSQL/PostGIS
      |
      v
Evidence normalization + deterministic comparison/calculation
      |
      v
Claim/evidence verification + coverage / limitations checks
      |  | failed verification
      |  +------------------> revise / explicitly abstain
      v
LLM answer composer (no new numeric facts)
      |
      v
Structured JSON: answer, comparisons, claims, evidence, limitations
```

### 4.1 Responsibility boundaries

**LLM planner** MAY classify intent, select approved tools, pick justified filters, and decide whether more evidence is needed. It MUST NOT write arbitrary SQL, fabricate numeric results, invent addresses/coordinates, or perform arithmetic that can be done deterministically.

**Tool adapters** MUST validate typed inputs, invoke existing parameterized SQL/Python functions with a dedicated read-only database role, and return query metadata with every result.

**Comparison engine** MUST normalize units and calendar windows, verify comparable inputs, compute totals/trends from authoritative aggregations, and produce typed comparison facts. It SHOULD be ordinary Python, not another LLM prompt.

**Claim verifier** MUST ensure each numeric, comparative, or source-specific factual claim is backed by matching evidence and appropriate caveats. It MUST reject unsupported superlatives or safety/causal assertions.

### 4.2 Suggested LangGraph nodes

`parse_request` → `resolve_scope` → `build_plan` → `execute_tools` → `normalize_evidence` → `compute_comparisons` → `verify_claims` → `compose_answer` → `final_validate`.

Use conditional edges for incomplete location, unsupported intent, tool failure, and failed validation. A single controlled tool-calling Agent is sufficient; there is no requirement for multiple autonomous agents. Bound retries and tool calls.

## 5. Request contract

The API should separate *structured user inputs* from the free-form question, rather than asking the model to reconstruct coordinates already supplied by the frontend.

```json
{
  "question": "Compare these two locations using Get It Done and collision records.",
  "locations": [
    { "id": "A", "label": "Apartment A", "latitude": 32.84, "longitude": -117.25 },
    { "id": "B", "label": "Apartment B", "latitude": 32.85, "longitude": -117.24 }
  ],
  "radius_meters": 800,
  "time_range": {
    "start": "2026-01-01T00:00:00-08:00",
    "end": "2026-07-01T00:00:00-07:00",
    "calendar_timezone": "America/Los_Angeles"
  },
  "preferences": ["traffic_collisions", "service_requests"],
  "conversation_id": null
}
```

The shown coordinates are **illustrative inputs**, not actual rental addresses. The client or location resolver is responsible for coordinates, not the collision-geocoding worker. Validate latitude/longitude ranges, supported geography, nonnegative radius, bounded date range, and maximum number of locations. A free-text address without verified coordinates is a non-resolved location, not a guessed point.

## 6. Tool interface specification

### 6.1 Common requirements for every Agent-exposed tool

Each response MUST include:

- `tool_name`, `tool_version`, `query_id`, and `executed_at`.
- Exact validated `filters`, timezone, inclusion/exclusion conventions, and metric definition.
- `data_domain`, source/catalog metadata or a stable metadata reference, `data_as_of` if verifiable, and `data_quality`.
- A typed result payload; an explicit `status` of `ok`, `partial`, `unavailable`, or `error`.
- Limit/pagination information where retrieval is bounded.
- Whether the result is spatially filtered; the relevant location precision caveat.

The adapter MUST NOT copy arbitrary instructions from `raw_record` into the Agent's instructions. Raw civic fields are data, not trusted prompts.

### 6.2 Existing tools (MVP)

**`count_service_requests`**

- Inputs: `latitude`, `longitude`, `radius_meters`, `start`, `end`, `exclude_linked_duplicates` (boolean).
- Output: `request_id_count` (integer), filters, scope metadata, limitations.
- Default for comparison: `exclude_linked_duplicates=false`, applied consistently. This measures report IDs, not separate underlying problems.
- Error handling: do not replace unavailable data with zero.

**`aggregate_traffic_collisions`**

- Inputs: `start`, `end`, optional complete spatial triplet, `period`, `calendar_timezone`, and documented optional incident filters.
- Output: ordered `[period_start, period_end, report_count, located_count, unknown_injury_count, unknown_fatality_count]` where supported; specify actual wrapper field names in code when implemented.
- For a spatial query, all matched reports with a point are already located; `located_count` within those results is **not** a sufficient denominator for overall local geocode coverage.
- MUST generate zero-valued missing calendar buckets in the deterministic normalization layer **only for the requested interval**; preserve “no bucket in source result” separately from evidence of source coverage.

**`get_traffic_collisions`**

- Inputs: `start`, `end`, optional complete spatial triplet, documented incident/street filters, `limit`, `offset`.
- Output: bounded reports, IDs, occurred-at time, normalized street information, source values, approximation/validation flags, pagination.
- Use for traceable examples/drilldown; do not count a first page as the total when more pages may exist.
- Never show a block-level coordinate as an exact incident point.

### 6.3 Proposed new tools

| Name | Priority | Inputs | Returns | Notes |
|---|---|---|---|---|
| `get_dataset_metadata` | P0 | `domain`, optional `import_id` | Source/subset, observation bounds, load version, current-vs-snapshot policy, coverage checks, freshness verification status | Required to interpret zero and stale data. These fields may initially say `unknown`. |
| `get_spatial_coverage` | P0 | `domain`, `start`, `end`, optional geography | Total dated records, located records, unlocated records **where scope can be evaluated without coordinates**, plus status breakdown | A local circle cannot include unknown-position reports; label that denominator unavailable instead of inventing one. |
| `resolve_location` | P1 (required for address UI) | User-entered address or selected map place | Normalized address, verified coordinate, provider, match confidence/precision, ambiguity | Separate from collision-specific geocoding; ambiguity requires user selection. |
| `aggregate_service_requests` | P1 | Spatial triplet, `start`, `end`, grouping dimension (`service_name`, `status`, or month), duplicate policy | Count by category/status/time with NULL/unknown explicit | Must use the same source-time and parent-duplicate rules as existing count. |
| `get_service_requests` | P1 | Spatial triplet, time range, filters, `limit`, cursor | Bounded source request IDs and fields, with provenance | Enables inspection/citation beyond an aggregate count. |
| `compare_locations` | Application helper | Validated query outputs for two locations | Comparable facts and guardrails | **Not a direct SQL tool**; deterministic Python calculation, not independently sourced data. |

Do not expose SQL schema changes, ingestion commands, the geocoding worker, or freeform database execution to the LLM.

## 7. Evidence record and citation model

Every evidence item must identify **what was measured**, **where**, **when**, **how**, and **which dataset version** was consulted. Suggested normalized form:

```json
{
  "evidence_id": "ev_example_001",
  "query_id": "q_example_001",
  "domain": "traffic_collisions",
  "source_table": "citylens.traffic_collisions",
  "source_dataset": "police_recorded_traffic_collisions",
  "tool": "aggregate_traffic_collisions",
  "tool_version": "1",
  "metric": "geocoded_collision_report_count",
  "value": 12,
  "unit": "reports",
  "location_id": "A",
  "scope": {
    "latitude": 32.84,
    "longitude": -117.25,
    "radius_meters": 800,
    "start_inclusive": "2026-01-01T00:00:00-08:00",
    "end_exclusive": "2026-07-01T00:00:00-07:00"
  },
  "provenance": {
    "source_file_sha256": null,
    "import_ids": [],
    "source_freshness_verified": false
  },
  "quality": {
    "coordinate_precision": "block_approximate",
    "unlocated_within_radius": "not_determinable",
    "completeness": "unknown"
  },
  "caveats": [
    "Police-recorded reports only",
    "Unknown-position reports are excluded from spatial counts",
    "Radius membership is approximate near the boundary"
  ]
}
```

**The number 12 and all example IDs above are synthetic schema fixtures, not observed CityLens findings.** In production, the adapter populates these values from executed queries. Do not imply a single file SHA is the source of a result when multiple retained rows from different imports contribute; reference the actual provenance set or a reproducible dataset snapshot identifier. Some metadata is unavailable from the current SQL interfaces and may remain `null` or `unknown` until implemented.

### 7.1 Claim contract

Each final fact or comparison should be represented as:

```json
{
  "claim_id": "claim_001",
  "text": "In the imported, geocoded police reports, A has more records within the selected radius than B.",
  "claim_type": "descriptive_comparison",
  "evidence_ids": ["ev_for_A", "ev_for_B"],
  "limitations": ["Not a collision-risk or safety ranking"]
}
```

The verifier MUST check that the referenced evidence exists and supports the stated metric, direction of comparison, interval, and units. Plain source URLs alone are **not** sufficient citations for local numerical claims; the query and filters must also be retained.

## 8. Agent execution policy

### 8.1 Planning

1. Extract requested locations, dimensions, radius, period, filters, and comparison intent.
2. Check whether each requested dimension exists in the registry and whether each location is resolved.
3. Fetch dataset metadata and verify the intended geographic/temporal scope as far as currently possible.
4. Build a small, explicit tool plan. For comparisons, prepare symmetric calls for A and B.
5. Reject contradictory filters, invalid dates, unbounded requests, and attempts to infer unsupported dimensions.

### 8.2 Execution

1. Call the allowlisted tools with validated typed arguments.
2. Record query ID, status, elapsed time, and raw tool result for internal replay.
3. Convert outputs into typed evidence records. `NULL`, `unknown`, `unavailable`, and `0` MUST remain distinct.
4. Aggregate totals from authoritative aggregate results, not truncated retrieval lists.
5. Avoid repeated queries with identical normalized arguments and unchanged dataset version; cache appropriately.

### 8.3 Comparison

The deterministic comparison helper MUST verify:

- Identical radii, compatible geographies, time intervals, filters, timezone, and duplicate-handling rules for A/B within each domain.
- Same metric definitions (e.g., *service-request IDs* compared with *service-request IDs*, not requests compared with physical issues).
- Geocoding quality and temporal data availability sufficient for the strength of any comparison.
- Non-overlapping or overlapping query circles are explicitly labeled. Overlap is allowed for descriptive counts, but those counts are correlated and should not be presented as independent samples.

MVP can say: “Among records in the imported dataset that have usable locations, A had more recorded collisions within the selected radius than B.” It MUST NOT leap to: “Therefore B is safer.”

### 8.4 Answer and abstention

- Lead with a concise **evidence-bounded** comparison and relevant trade-offs.
- Show the metric, time interval, radius, and source domain for each number.
- Provide clickable evidence details in the frontend using `evidence_id`; each detail must reveal executed filters and provenance, not just raw source names.
- Display limitations near any potentially misleading conclusion, not buried in a generic disclaimer.
- If coverage is unknown or a required domain fails, return `partial` with surviving results; do not fill the missing value with zero.
- If the user asks for a global “best apartment,” explain which requested criteria cannot yet be evaluated and make only a conditional recommendation based on observed dimensions and user preferences.

## 9. Worked example: compare two candidate rentals

**Illustrative user query:** “Compare A and B for traffic collisions and public service issues within 800 m from January through June 2026. Which would you prefer?”

**Tool sequence (values here are examples of arguments, not database findings):**

1. Verify A/B are already provided as valid map pins (otherwise call `resolve_location` when implemented).
2. Call `get_dataset_metadata` for both domains. If metadata is not implemented, retain `coverage=unknown` and explicitly limit conclusions.
3. Call `count_service_requests` for A and for B using `[2026-01-01T00:00:00-08:00, 2026-07-01T00:00:00-07:00)`, radius 800 m, and the same duplicate policy.
4. Call `aggregate_traffic_collisions` twice with identical period `'month'` and the matching temporal/spatial settings.
5. If a user asks *which incidents*, call `get_traffic_collisions` for a bounded drilldown rather than using retrieval as the total.
6. Normalize evidence. Deterministically calculate comparative count differences or monthly trends. Track incomplete scope and geocoding limitations.
7. Verify each generated claim's evidence references and limitations.
8. Respond with separate service-request and collision sections. Make a **conditional** preference statement only if the user's priorities permit it and the evidence supports it.

**Permitted answer pattern:**

> “Within the same 800 m radius and January–June 2026 window, the imported Get It Done records show [count A] reports near A and [count B] near B. The geocoded police-collision records show [count A] near A and [count B] near B. These are *record counts*, not direct measures of repair quality or travel safety. The Get It Done import's verified geographic completeness is [status], and collision locations are block-level approximations. Based on your preference for [specified criterion], [conditional interpretation]; other dimensions remain unassessed.”

If a dataset has no verified observation coverage for the window, the answer must say so rather than generating a misleading zero comparison.

## 10. Structured response contract

```json
{
  "status": "ok",
  "intent": "compare_locations",
  "answer": "Natural-language, evidence-bounded synthesis goes here.",
  "comparison": {
    "locations": ["A", "B"],
    "radius_meters": 800,
    "time_range": {
      "start": "2026-01-01T00:00:00-08:00",
      "end": "2026-07-01T00:00:00-07:00"
    },
    "metrics": []
  },
  "claims": [],
  "evidence": [],
  "limitations": [
    "Imported Get It Done CD1 subset: completeness not verified",
    "Collision spatial results exclude reports without coordinates"
  ],
  "unsupported_dimensions": [],
  "tool_trace_id": "trace_example_001"
}
```

`status` is one of `ok`, `partial`, `needs_location`, `unsupported`, or `error`. An empty `metrics` array indicates that numerical evidence has not been collected yet; it must not be interpreted as zero counts. The response should include machine-readable error/limitation codes for frontend display and evaluation.

## 11. Safety, performance, and reliability requirements

- **Database access:** dedicated least-privilege DB role with `SELECT` / narrowly scoped `EXECUTE` only; no schema modification, ingest, or geocoding side effects from Agent credentials. A read-only transaction alone is insufficient authorization.
- **Tools:** explicit allowlist and JSON-schema/Pydantic validation; parameter binding; no arbitrary LLM-generated SQL; no direct exposure of the uncapped filter function.
- **Budgets:** configurable per-request maximum tool calls, retries, rows, execution time, and token budget. Keep the documented 10-second query timeout for read-only wrappers unless deliberately revised.
- **Data handling:** validate all user-controlled inputs; treat `raw_record` text as untrusted; restrict personal/precise location logs as appropriate. Public dataset content must never override system/tool policies.
- **Failure handling:** timeouts, missing metadata, and database exceptions yield typed `partial`/`error` statuses and preserve successful independent evidence.
- **Versioning:** version tool adapters, metric definitions, source snapshots, and prompts. A repeated answer can change after data updates; store sufficient provenance to replay it.
- **Presentation:** block-approximate collision positions should be shown with an appropriate map style or uncertainty label; do not display them as exact collision pins.

## 12. Evaluation and acceptance criteria

Use deterministic, synthetic fixtures and a separate test DB; do not use production imports as a writeable test target.

| Test | Pass criterion |
|---|---|
| Correct tool routing | Prompt requiring a time trend calls aggregation; incident drilldown calls retrieval; report count calls service counter. |
| Symmetric comparison | A/B queries use the same interval, radius, filters, and duplicate rules. |
| Source-grounded numeric claims | 100% of rendered numerical claims match referenced tool output or deterministic calculations. |
| Provenance | 100% of substantive data claims have valid `evidence_ids` or are explicitly marked unsupported. |
| Coverage uncertainty | Zero located collision reports is **not** interpreted as zero total collisions; CD1 is not asserted citywide. |
| Injury NULL handling | Missing injury/fatality counts are never converted to 0; filters do not silently include unknowns. |
| Calendar correctness | `[start, end)` and Los Angeles DST boundaries are handled consistently; empty aggregation buckets are treated carefully. |
| Pagination | A retrieval page of 100 never becomes “the total” when aggregation indicates more records. |
| Unsupported request | “Which apartment is safer / closer to work?” is not answered using unsupported proxies. |
| Agent permissions | Agent DB role cannot write tables or call administrative functions. |
| Failure isolation | One failed data source produces `partial` output with no fabricated replacement. |
| Prompt injection | Malicious text inside source `raw_record` does not change tool access or answer policy. |

**Recommended evaluation metrics:** tool-selection accuracy, tool-argument validity, result/count accuracy, evidence-claim support rate, unsupported-claim rate, limitation recall, task completion rate, and mean tool calls/latency. Report scores separately for normal, empty, incomplete, ambiguous, and tool-failure scenarios. Targets beyond the pass criteria above should be established after collecting a representative test set.

## 13. Implementation order

**Phase 0 — make existing data safe to query (smallest useful vertical slice)**

1. Confirm tool SQL/Python wrapper signatures and runtime behavior with integration tests.
2. Create a dedicated Agent read-only database role and an allowlisted Python adapter registry.
3. Implement `POST /api/v1/agent/query` with pre-resolved map pins, typed inputs, and source metadata marked `unknown` where necessary.
4. Add evidence normalization, comparison checks, and an independent claim validator.
5. Wire a bounded LangGraph planner to the three existing Agent-facing query tools.
6. Implement an A/B comparison response with numerical evidence, coverage warnings, and test fixtures.

**Phase 1 — make the analysis meaningful to renters**

1. Define and test `get_dataset_metadata` and query-specific coverage reporting.
2. Implement `aggregate_service_requests` and bounded `get_service_requests` to distinguish service categories/statuses and cite reports.
3. Implement `resolve_location` for apartment addresses with ambiguity handling.
4. Add category- and trend-aware comparison without collapsing dimensions into an unsupported safety score.

**Phase 2 — expand value beyond the two existing datasets**

1. Add commute/accessibility data and independently validated tools.
2. Add amenities and development/project records with source/version metadata.
3. Introduce user-configurable preferences and explicit scoring rules only after adequate benchmarks, normalization, denominators, and missingness checks exist.
4. Evaluate ranking quality against human-labeled scenarios and task-oriented evaluations.

## 14. Decisions to record before implementation

- What precisely does the source file label `CD1` signify, and what geographic/time coverage can be independently verified?
- Will Phase 0 accept only map pins/coordinates, or is an apartment-address geocoder mandatory for launch?
- Which radius and time window should the UI choose by default? (Provisional: 800 m; time window only after coverage verification.)
- Should linked Get It Done reports be included by default? (Provisional: yes; display policy and support alternate view.)
- How will the frontend display approximate crash locations and missing coverage?
- Which user preference question is required before describing one candidate as preferable to another?
- What exportable provenance key identifies a query against a reproducible set of import records?

---

### Reference material supplied for this specification

- `README(1).md` — local database structure, query-tool summaries, Get It Done semantics and limitations.
- `TRAFFIC_COLLISIONS.md` — collision ingestion, geocoding, Python queries, filters and limitations.
- `get_it_done.sql` — Get It Done current-record and import schemas.
- `traffic_collisions.sql` — traffic current-record, archive, import, and approximate-geocode schemas.

No code beyond these provided documents was inspected, and all Agent architecture, new adapters, contracts, defaults, and acceptance criteria in this document are **proposals**, not statements of completed implementation.
