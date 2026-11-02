# Phase 1.1 pre-implementation data audit ? 2026-11-01

Observed in a repeatable-read, read-only transaction against the accessible existing database. No data, role or schema changes. Aggregate observations are in database-audit.json; audit.sql reproduces the inspection.

## Get It Done

- 303,225 stored rows and distinct IDs; 303,226 raw CSV rows (one repeated ID).
- 44 nonblank service names plus 424 blank service values; 4 statuses.
- No missing stored IDs, timestamps, statuses or locations. No missing raw lat/lng values. No empty points or points outside the existing approximate admission envelope.
- Source columns: service_name, service_name_detail, status, service_request_id, service_request_parent_id, date_requested, date_closed, lat, lng. Database: service_name, status, service_request_id, parent_id, requested_at, location. date_closed remains in raw_record, not a repair-completion field.
- Observed request span: 2016-05-20T09:10:00-07:00 through 2026-10-26T22:47:39-07:00 (Los Angeles).
- No leading/trailing/repeated whitespace changes or case-only collisions were found. Case formatting itself varies by title/acronym (e.g. ROW Maintenance); a formatting-only uppercase canonical key is safe for comparison. No spelling/synonym equivalence is verified; Parking/Parking Violation/Parking - 72-Hours and the Graffiti variants remain separate.
- Raw district field is 1 on every CSV row. The official portal identifies the CD1 download as Council District 1. This is an imported subset, not verified citywide coverage.

Most frequent service names:

- Missed Collection: 39,729
- Illegal Dumping: 34,098
- Parking: 26,415
- Pothole: 21,883
- Parking Violation: 18,276
- Encampment: 17,731
- Other: 13,292
- Shared Mobility Device: 11,614

Statuses:

- Closed: 257,854
- Referred: 35,097
- In Process: 9,324
- New: 950

## Traffic collisions / geocoding

- 78,494 reports, all pending; 0 located, 0 attempted (geocoded_at), 0 matched. Existing schema has actual statuses pending/matched/no_match/ambiguous/unsupported/error; no resolved or failed labels were invented.
- No pending row has a location and no matched row is missing one. All missing coordinates are genuinely NULL, not approximate points.
- Observed collision span: 2015-03-19T12:00:00-07:00 through 2026-10-26T16:52:00-07:00. No missing stored dates, injury counts or fatality counts in this snapshot.
- Geocoding is implemented (Census adapter, protocol, bounded offline worker, input hash and optimistic invalidation), but not completed on this dataset. No recorded attempts/errors support blaming a provider failure; pending here means unprocessed or reset, not proven failed.
- Independent raw-input inspection found 70,144 addresses eligible for the existing Census adapter and 8,350 unsupported inputs. This does not establish match success.
- 6,840 records include an intersecting street, and one lacks a primary street. Current Census adapter deliberately declines intersections and nonpositive/missing block numbers rather than guessing. These are future capability gaps, not proof of why every pending row remains pending.
- Geography GiST, time B-tree and primary/cross-street indexes exist. There is no pending-queue partial index; defer one until worker performance warrants it.
- Small code risks: concurrent workers can overwrite a result for an unchanged address after another worker completes it; unmatched results can currently retain an approximation precision label. Fixes will use pending-status compare-and-set and stricter unmatched validation. No external geocoding will run.

## Ingestion/freshness

- Last stored Get It Done import: 2026-10-28T06:19:28.788577+00:00; collision import: 2026-10-28T09:15:49.310466+00:00.
- Both import tables already retain successful local transaction timestamps, filenames and SHA-256. No source publication/update timestamp is recorded. Existing current-row joins alone would miss a successful empty import; new metadata must use the complete committed import table for last ingestion.
- Official portal pages currently display a later update date, but that is not evidence of when these older local files were published. The runtime metadata must leave source_updated_at/source_publication_at NULL. No historical values will be fabricated.
- No table migration is needed for reliable current metadata. Future automated downloads should retain provider timestamp/HTTP evidence tied to the downloaded file hash before asserting source freshness.

Sources: [Get It Done](https://data.sandiego.gov/datasets/get-it-done-reports/), [Traffic collisions](https://data.sandiego.gov/datasets/traffic-collisions/). The website observation and local import provenance are separate.
