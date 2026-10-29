# Coverage matrix

Each documented tool is linked to the tests that cover it. **Strict tests**
assert documented behavior. **Probe tests** record behavior the docs don't
define (see [AMBIGUITIES.md](AMBIGUITIES.md)). **Untested** means no test
exercises the item.

Prefixes: `U` = `test_reference_unit.py`, `S` = `test_db_schema.py`,
`I` = `test_db_ingest.py`, `D` = `test_differential.py`,
`L` = `test_db_limits.py`, `R` = `test_realdata.py`.

## Get It Done

| Tool / requirement | Strict tests | Probes | Gaps |
| --- | --- | --- | --- |
| `sql/get_it_done.sql`: tables, types, keys | `S::test_column_types_and_nullability[ingestions, service_requests]`, `S::test_primary_keys`, `S::test_foreign_keys`, `S::test_service_request_parent_has_no_foreign_key`, `S::test_indexes`, `S::test_service_request_constraints`, `S::test_parent_outside_subset_allowed`, `S::test_duplicate_primary_keys_rejected` | | |
| `db.py init`: repeatable | `S::test_init_is_repeatable`, `S::test_postgis_and_schema_installed` | | Removing a tool function isn't tested (README says installed functions persist). |
| `db.py load`: upsert by ID, last row wins | `I::test_gid_load_matches_reference`, `I::test_gid_last_occurrence_wins_including_raw_record`, `I::test_gid_reimport_upserts_and_retains_absent_ids`, `R::test_gid_stored_rows_match_reference`, `R::test_gid_duplicate_5449467_last_wins` | `I::test_probe_gid_ingestion_id_after_upsert` | |
| `db.py load`: provenance (file name, SHA-256, row counts) | `I::test_gid_ingestion_provenance` | | `imported_at` is checked only for not-NULL. |
| `db.py load`: missing coordinates kept | `I::test_gid_missing_coordinates_retained` | | |
| `db.py load`: invalid coordinates or date fail and roll back | `I::test_gid_invalid_file_fails_and_rolls_back[lat_text, lat_range, invalid_date, missing_date]`, `U::test_gid_invalid_rows_fail_whole_import` | `I::test_probe_gid_undocumented_inputs[one_sided, zero_coord, empty_id, dst_gap]` | |
| `db.py load`: repeated identical import | `I::test_gid_repeated_identical_import_is_idempotent` | | |
| `count_service_requests`: radius, inclusive start, exclusive end | `D::test_gid_count_matches_reference` (6 cases), `R::test_gid_count_grid` (160 queries), `U::test_gid_count_reference` | | |
| `count_service_requests`: exclude linked duplicates | `D::test_gid_count_matches_reference[jan_800m_exclude_linked]`, `R::test_gid_count_grid` | | |
| `count_service_requests`: radius 0 and 50,000 m | `D::…[jan_radius_0]`, `D::…[jan_50km]` | `D::test_probe_gid_count_invalid_arguments` | |
| `count_service_requests`: DST overlap | `D::test_gid_count_dst_overlap` (3 windows) | | |
| Geodesic correctness | `D::test_postgis_distance_agrees_with_independent_geodesic`, `U::test_geodesic_distances_of_fixture_points` | | |
| `db.py count` CLI | `D::test_gid_cli_count_matches_sql`, `D::test_gid_cli_rejects_timestamp_without_offset` | | The default `--radius` isn't tested. |
| Category / status filters | `U::test_gid_category_status_reference` (reference only) | | **No production tool exists, so these can't be compared.** |

## Traffic collisions

| Tool / requirement | Strict tests | Probes | Gaps |
| --- | --- | --- | --- |
| `sql/traffic_collisions.sql`: tables, keys, CHECK constraints | `S::test_column_types_and_nullability[collision_*, traffic_collisions]`, `S::test_primary_keys`, `S::test_foreign_keys`, `S::test_indexes`, `S::test_collision_constraints` (8), `S::test_location_requires_matched_and_precision`, `S::test_zero_counts_allowed_and_default_status_pending`, `S::test_source_row_archive_key` | | |
| Transaction rollback | `S::test_transaction_rollback_discards_all_writes`, `I::test_collision_structural_error_rolls_back` | | |
| `collision_ingest.py`: cleaning (counts NULL vs 0, hit-and-run, dates) | `I::test_collision_load_matches_reference`, `I::test_collision_validation_issues`, `U::test_collision_field_cleaning`, `R::test_collision_stored_rows_match_reference` | `I::test_probe_collision_undocumented_values` | |
| `collision_ingest.py`: missing ID rejected; repeated ID, last row wins | `I::test_collision_import_counts_and_archive`, `I::test_collision_current_row_and_geocode_defaults`, `R::test_collision_import_counts` | | |
| `collision_ingest.py`: archive of every row | `I::test_collision_import_counts_and_archive`, `I::test_collision_repeated_identical_import` | | |
| `collision_ingest.py`: missing header or bad column count fails the import | `I::test_collision_structural_error_rolls_back` (2) | | |
| `collision_ingest.py`: `--batch-size` | `I::test_collision_batch_size_does_not_change_result` | | |
| `collision_ingest.py`: reimport keeps or resets geocoding | `I::test_collision_reimport_upsert_and_geocode_preservation` | | |
| Street normalization | `I::test_collision_street_normalization`, `R::test_collision_stored_rows_match_reference` (documented suffixes) | USPS mapping in `R::…` observations | |
| `010 filter_traffic_collisions` | `D::test_filter_matches_reference` (18 cases), `R::test_collision_filter_grid` (4 windows × 13 filters) | `D::test_probe_collision_invalid_arguments` | |
| `020 get_traffic_collisions` | `D::test_get_matches_filter_set` (18), `D::test_pagination_order` (4), `D::test_get_returns_full_rows`, `L::test_limit_capped_at_1000` | `D::test_probe_collision_invalid_arguments` | |
| `030 aggregate_traffic_collisions` | `D::test_aggregate_matches_reference` (5), `D::test_aggregate_total_equals_filter_count` (16), `R::test_collision_aggregate_grid` (9) | `D::test_probe_aggregate_unsupported_period` | |
| DST overlap (filter and aggregate) | `D::test_dst_overlap_filter_and_aggregate`, `I::test_collision_dst_overlap_preserved` | | |
| `collision_queries.py` wrappers | `D::test_wrapper_street_name_normalized`, `D::test_wrapper_rejects_naive_timestamps`, `D::test_wrapper_aggregate_matches_sql` | | The 10-second timeout and read-only mode aren't tested. |
| `collision_geocoding.py` | — | — | **Untested.** It calls an external service and the instructions forbid network geocoding. Only how reimport treats geocoded rows is covered. |
| `address_key` / `normalized_address` values | `I::test_collision_reimport_…` (only stable vs changed) | `I::test_probe_collision_undocumented_values` | The algorithm isn't documented. |
