-- Additive overload: the existing 9-argument function remains unchanged.
-- Reuse its geographic/time/duplicate rules and both-dimensional groups, then
-- combine disjoint request-ID groups. One stored request ID has one category.
CREATE OR REPLACE FUNCTION citylens.aggregate_service_requests(
    latitude double precision, longitude double precision,
    radius_meters double precision, start_time timestamptz, end_time timestamptz,
    exclude_linked_duplicates boolean, group_by text,
    service_name_filter text, status_filter text, service_names_filter text[]
) RETURNS TABLE(service_name text,status text,request_id_count bigint)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF service_names_filter IS NULL THEN
        RETURN QUERY SELECT * FROM citylens.aggregate_service_requests(latitude,longitude,
            radius_meters,start_time,end_time,exclude_linked_duplicates,group_by,service_name_filter,status_filter);
        RETURN;
    END IF;
    IF service_name_filter IS NOT NULL OR cardinality(service_names_filter) NOT BETWEEN 1 AND 20
        OR array_ndims(service_names_filter)<>1 OR array_position(service_names_filter,NULL) IS NOT NULL
        OR group_by IS NULL OR group_by NOT IN ('service_name','status','both') THEN
        RAISE EXCEPTION 'Provide 1..20 category labels or one scalar filter, with valid grouping'
            USING ERRCODE='22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(service_names_filter) selected(value)
        WHERE NOT EXISTS (SELECT 1 FROM citylens.service_requests r
            WHERE citylens.normalize_request_category(r.service_name)
                IS NOT DISTINCT FROM citylens.normalize_request_category(selected.value))
    ) THEN
        RAISE EXCEPTION 'Unsupported service category; select a stored service_name label'
            USING ERRCODE='22023';
    END IF;
    RETURN QUERY
    WITH selected AS (
        SELECT DISTINCT citylens.normalize_request_category(value) AS category
        FROM unnest(service_names_filter) names(value)
    ), matching AS (
        SELECT CASE WHEN group_by IN ('service_name','both') THEN g.service_name END AS service,
               CASE WHEN group_by IN ('status','both') THEN g.status END AS state,g.request_id_count
        FROM citylens.aggregate_service_requests(latitude,longitude,radius_meters,start_time,end_time,
            exclude_linked_duplicates,'both',NULL::text,status_filter) g
        WHERE EXISTS (SELECT 1 FROM selected s WHERE s.category IS NOT DISTINCT FROM g.service_name)
    )
    SELECT m.service,m.state,sum(m.request_id_count)::bigint FROM matching m
    GROUP BY m.service,m.state
    ORDER BY m.service COLLATE "C" NULLS FIRST,m.state COLLATE "C" NULLS FIRST;
END;
$$;
