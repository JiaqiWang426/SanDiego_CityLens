# Explorer classification API

The Explorer backend applies `backend/category_mapping.csv` to the supplied
2026 District 1 snapshot. It represents 31,667 distinct request IDs with nine
main categories and 66 designed subcategories. The approved mapping and the
source CSV are unchanged. The frontend is unchanged.

## Run and validate

From the repository root, use the existing environment:

```bash
.venv/bin/python -m uvicorn backend.explorer_app:app --host 127.0.0.1 --port 8011
.venv/bin/python -m pytest backend/explorer_tests -q
```

OpenAPI documentation is available at `http://127.0.0.1:8011/docs`.
The API prefix is `/api/v1/explorer`.

## Existing behavior and additive fields

Existing routes remain: `/metadata`, `/summary`, `/map`, `/requests`, and
`/requests/{request_id}`. Existing response keys and source-category semantics
are preserved. `category` remains the normalized original `service_name`; it
has not been replaced by `main_category`.

Request list items, request details and individual map points now also contain:

| Field | Meaning |
| --- | --- |
| `main_category` | Issue-type main category from the approved mapping |
| `sub_category` | Rule-selected subcategory |
| `classification_method` | `detail_match`, `service_default`, or `needs_review` |

Map clusters retain their existing aggregate shape because a cluster can contain
several categories. The existing summary `categories` array still groups by
legacy `category`. Use the new comparison endpoints for hierarchical groups.
Public descriptions remain outside the Explorer projection and API.

## Metadata and hierarchy

`GET /api/v1/explorer/metadata` adds `category_hierarchy`, generated from the
actual mapping and deduplicated in first-appearance CSV order:

```json
{
  "main_category": "Graffiti",
  "subcategories": [
    "Park graffiti",
    "Graffiti removal - unspecified",
    "Public graffiti",
    "Private graffiti",
    "Graffiti enforcement"
  ]
}
```

The object above is the actual Graffiti entry in the full `category_hierarchy`
array. The full array contains all nine main categories and all 66 designed
subcategories, including categories with zero records in this snapshot.
Existing facets remain; `facets.main_category` and `facets.sub_category` add
observed counts. Metadata and facets describe the complete cached dataset and
are not changed by request filters. `mapping_sha256` identifies the mapping used
to build that cache. Hierarchy and records always come from the same cache build.

## Shared filters

`/summary`, `/map`, `/requests` and both comparison routes use the same filters.

| Query parameter | Default and semantics |
| --- | --- |
| `start` | `2026-01-01`; inclusive source-local date |
| `end` | `2026-12-31`; inclusive source-local date |
| `category` | Optional exact legacy service category |
| `main_category` | Optional exact main-category name |
| `sub_category` | Optional exact subcategory name |
| `status` | Optional exact source status |
| `community` | Optional exact community planning area |
| `zipcode` | Optional exact source ZIP code |
| `exclude_linked` | `false`; `true` removes requests with a parent ID |

Filters combine with AND. Main and subcategory filters can be used separately or
together. Unknown names or incompatible main/subcategory pairs return empty
results, rather than silently ignoring a filter. Preserve capitalization and
encode spaces and ampersands with `URLSearchParams`.

The `__missing__` value selects SQL NULL for a text filter. For example,
`community=__missing__` selects unknown communities, and `category=__missing__`
selects missing original service names. Derived categories are always assigned;
use `main_category=Needs Review` for uncertain issue types. Date ranges outside
2026 or with `start > end` return HTTP 422. The supplied data ends on October 26;
the full-year defaults are preserved for compatibility.

## Monthly category comparison

`GET /api/v1/explorer/comparisons/monthly`

Each item contains exactly `month` (`YYYY-MM`), `main_category`, `sub_category`
and integer `count`. Groups are ordered by month, main category and subcategory.
Counts use all matching records, including records without coordinates.

Actual snapshot request:

```text
/api/v1/explorer/comparisons/monthly?start=2026-01-01&end=2026-01-31&main_category=Graffiti&sub_category=Park%20graffiti
```

Actual response:

```json
{
  "data": [
    {
      "month": "2026-01",
      "main_category": "Graffiti",
      "sub_category": "Park graffiti",
      "count": 17
    }
  ]
}
```

Only observed groups are returned. Zero-count combinations and uncovered future
months are not generated. A partial-month date filter counts only records inside
that range. The sum of all returned counts equals the correspondingly filtered
`/summary.total` and `/requests.total`.

## Community comparison

`GET /api/v1/explorer/comparisons/community`

Each item contains exactly `community`, `main_category`, `sub_category` and
integer `count`. Groups are ordered by community, main category and subcategory.
Missing communities are explicit JSON `null`, not omitted or replaced with a
potentially conflicting source label such as `Unknown`.

Actual snapshot request:

```text
/api/v1/explorer/comparisons/community?community=LA%20JOLLA&main_category=Graffiti&sub_category=Park%20graffiti
```

Actual response:

```json
{
  "data": [
    {
      "community": "LA JOLLA",
      "main_category": "Graffiti",
      "sub_category": "Park graffiti",
      "count": 39
    }
  ]
}
```

The current snapshot includes two requests with missing communities. The request
`/api/v1/explorer/comparisons/community?community=__missing__` returns:

```json
{
  "data": [
    {
      "community": null,
      "main_category": "Community & Property Services",
      "sub_category": "Encampment report",
      "count": 1
    },
    {
      "community": null,
      "main_category": "Parks & Vegetation",
      "sub_category": "Park activity and enforcement",
      "count": 1
    }
  ]
}
```

Render a suitable display label for `null`, but use `__missing__` when filtering
the API. Community aggregation also preserves unlocated requests. It has no
pagination or top-N truncation. Empty selections return `{"data": []}`.

## Frontend request example

```javascript
const filters = new URLSearchParams({
  start: "2026-01-01",
  end: "2026-10-26",
  main_category: "Parking & Vehicles",
  sub_category: "Parking obstruction"
});
const response = await fetch(`/api/v1/explorer/comparisons/monthly?${filters}`);
if (!response.ok) throw new Error(`Explorer request failed: ${response.status}`);
const { data } = await response.json();
```

Hierarchy entries use the key `subcategories`; the query parameter and record
field use `sub_category`. No CORS or cross-origin deployment policy is changed;
use the existing same-origin setup or the frontend development proxy.

## Classification and cache lifecycle

`backend/category_rules.py` compiles exact detail conditions and service defaults
without importing pandas into the application. Detail conditions take priority.
Missing details use the service default; final Needs Review assignments always
use `classification_method=needs_review`. Unknown service names fail the rebuild
instead of being guessed. An unseen detail retains its service fallback.
Conditional overlaps, conflicting defaults and ambiguous subcategory parents
are rejected before replacing a cache.

Cache schema version 2 adds the three derived columns and category indexes.
Startup `ensure_cache()` checks the source path/size/mtime, schema version, mapping
path and mapping content SHA-256. A mapping content change invalidates the cache
even if its size and mtime are unchanged. A successful rebuild atomically replaces
the disposable cache. Invalid CSV data, invalid rules or source/mapping changes
during a build leave the old cache file intact and raise an error.

As with the original source-cache workflow, restart the server after a source or
mapping change, or rebuild explicitly while the server is stopped:

```bash
.venv/bin/python -m backend.explorer_data
```

There is no per-request mapping hot reload. Until restart, the existing records
and hierarchy remain a coherent snapshot. Do not delete the source CSV or edit
the approved mapping merely to refresh the cache.

The test suite compares every real scoped assignment against the independent
pandas audit; checks monthly/community sums across shared filters; exercises
missing communities and coordinates; and verifies version migration, content
hash invalidation, failed rebuild preservation and changes during a build.

See [the classification report](category_classification_report.md) for the full
hierarchy, counts, provenance and remaining classification ambiguities. Park
irrigation, park pavement, mixed park enforcement and street-light placement
remain unchanged pending further review.
