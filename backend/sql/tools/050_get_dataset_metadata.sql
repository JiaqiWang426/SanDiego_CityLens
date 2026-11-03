-- Dataset-wide diagnostics, never a denominator for a local circle.
-- Publication/update timestamps are not recorded for these local CSV imports.
-- Do not substitute event dates, file mtime, or the portal's current page date.
CREATE OR REPLACE FUNCTION citylens.get_dataset_metadata(dataset_domain text)
RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
DECLARE
    stats jsonb;
    imports jsonb;
    last_ingested timestamptz;
    statuses jsonb := NULL;
    precision_counts jsonb := NULL;
    latest_import_id bigint;
BEGIN
    IF dataset_domain IS NULL OR dataset_domain NOT IN ('service_requests','traffic_collisions') THEN
        RAISE EXCEPTION 'Unsupported dataset domain' USING ERRCODE='22023';
    END IF;
    IF dataset_domain='service_requests' THEN
        SELECT jsonb_build_object(
            'dataset_records',count(*),'dataset_located_records',count(*) FILTER(WHERE location IS NOT NULL AND NOT ST_IsEmpty(location::geometry)),
            'dataset_unlocated_records',count(*) FILTER(WHERE location IS NULL OR ST_IsEmpty(location::geometry)),
            'dataset_dated_records',count(requested_at),'dataset_observed_start',min(requested_at),'dataset_observed_end',max(requested_at))
        INTO stats FROM citylens.service_requests;
        SELECT imported_at,id INTO last_ingested,latest_import_id FROM citylens.ingestions
            ORDER BY imported_at DESC NULLS LAST,id DESC LIMIT 1;
        SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id DESC),'[]'::jsonb) INTO imports FROM (
            SELECT DISTINCT i.id,i.source_file,i.sha256,i.imported_at
            FROM citylens.ingestions i JOIN citylens.service_requests r ON r.ingestion_id=i.id
            ORDER BY i.id DESC LIMIT 101) t;
    ELSE
        SELECT jsonb_build_object(
            'dataset_records',count(*),'dataset_located_records',count(*) FILTER(WHERE location IS NOT NULL AND NOT ST_IsEmpty(location::geometry)),
            'dataset_unlocated_records',count(*) FILTER(WHERE location IS NULL OR ST_IsEmpty(location::geometry)),
            'dataset_dated_records',count(occurred_at),'dataset_observed_start',min(occurred_at),'dataset_observed_end',max(occurred_at))
        INTO stats FROM citylens.traffic_collisions;
        SELECT imported_at,id INTO last_ingested,latest_import_id FROM citylens.collision_imports
            ORDER BY imported_at DESC NULLS LAST,id DESC LIMIT 1;
        SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id DESC),'[]'::jsonb) INTO imports FROM (
            SELECT DISTINCT i.id,i.source_file,i.sha256,i.imported_at
            FROM citylens.collision_imports i JOIN citylens.traffic_collisions c ON c.import_id=i.id
            ORDER BY i.id DESC LIMIT 101) t;
        SELECT coalesce(jsonb_object_agg(t.geocode_status,t.records),'{}'::jsonb) INTO statuses FROM (
            SELECT geocode_status,count(*) AS records FROM citylens.traffic_collisions GROUP BY 1) t;
        SELECT coalesce(jsonb_object_agg(t.label,t.records),'{}'::jsonb) INTO precision_counts FROM (
            SELECT coalesce(geocode_precision,'not_recorded') AS label,count(*) AS records
            FROM citylens.traffic_collisions GROUP BY 1) t;
    END IF;
    RETURN stats || jsonb_build_object(
        'domain',dataset_domain,
        'dataset_id',CASE WHEN dataset_domain='service_requests' THEN 'get_it_done_requests' ELSE 'police_recorded_traffic_collisions' END,
        'source_url',CASE WHEN dataset_domain='service_requests' THEN 'https://data.sandiego.gov/datasets/get-it-done-reports/' ELSE 'https://data.sandiego.gov/datasets/traffic-collisions/' END,
        'last_successful_local_ingestion_at',last_ingested,
        'source_updated_at',NULL,'source_publication_at',NULL,'source_freshness_verified',false,
        'coverage_verified',false,'coverage_scope','dataset_wide_not_neighborhood',
        'source_timestamp_status','not_recorded_for_imported_files',
        'coordinate_presence_definition','non_null_non_empty_WGS84_point; does_not_verify_city_boundary_or_accuracy',
        'geocoding_coverage',CASE WHEN dataset_domain='traffic_collisions' AND (stats->>'dataset_records')::bigint>0
            THEN (stats->>'dataset_located_records')::numeric/(stats->>'dataset_records')::numeric ELSE NULL END,
        'coordinate_coverage',CASE WHEN (stats->>'dataset_records')::bigint>0
            THEN (stats->>'dataset_located_records')::numeric/(stats->>'dataset_records')::numeric ELSE NULL END,
        'geocode_status_breakdown',statuses,'geocode_precision_breakdown',precision_counts,
        'spatial_accuracy',CASE WHEN dataset_domain='traffic_collisions' THEN 'block_approximate (Census or SanGIS road block) or intersection_approximate (SanGIS junction); source numbers anonymized to blocks; not exact collision locations; see geocode_precision_breakdown'
            ELSE 'source supplied coordinates; positional accuracy not independently verified' END,
        'normalization_policy','current_records_upsert; absent rows retained; historical snapshots not complete',
        'local_ingestion_timestamp_definition','max committed imported_at (transaction timestamp, not source update or exact completion time)',
        'latest_import_id',latest_import_id,
        'dataset_imports',(SELECT coalesce(jsonb_agg(value ORDER BY ord),'[]'::jsonb)
            FROM jsonb_array_elements(imports) WITH ORDINALITY AS item(value,ord) WHERE ord<=100),
        'dataset_imports_limit',100,'dataset_imports_truncated',jsonb_array_length(imports)>100,
        'limitations',CASE WHEN dataset_domain='service_requests' THEN jsonb_build_array(
            'Imported coverage must be checked per source file; no citywide completeness guarantee.',
            'Counts are user-submitted reports, not maintenance work or completed repairs.',
            'Source timezone assumed America/Los_Angeles; import dates and event dates are not source publication dates.')
            ELSE jsonb_build_array('Police-recorded collisions only; many property-damage and freeway incidents absent.',
            'Unlocated reports cannot be assigned to a radius; global coverage is not local coverage.',
            'Approximate block or intersection locations make radius membership uncertain; event dates and local imports are not source updates.') END
    );
END;
$$;
