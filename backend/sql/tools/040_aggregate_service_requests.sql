-- Formatting only: uppercase, trim and collapse whitespace. No synonym mapping.
-- NULL and blank are the same unknown category; literal 'Unknown' stays UNKNOWN.
CREATE OR REPLACE FUNCTION citylens.normalize_request_category(value text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT nullif(upper(btrim(regexp_replace(value,'\s+',' ','g'))),'')
$$;

-- Same geographic/time/parent policy as count_service_requests. Filter NULL
-- means no filter; an explicitly blank filter selects the unknown category.
-- Inactive grouping columns are NULL: use group_by to distinguish these from
-- an unknown active category. All groups are returned, never a truncated page.
CREATE OR REPLACE FUNCTION citylens.aggregate_service_requests(
    latitude double precision, longitude double precision,
    radius_meters double precision, start_time timestamptz, end_time timestamptz,
    exclude_linked_duplicates boolean DEFAULT false,
    group_by text DEFAULT 'service_name',
    service_name_filter text DEFAULT NULL, status_filter text DEFAULT NULL
) RETURNS TABLE(service_name text,status text,request_id_count bigint)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF latitude IS NULL OR NOT latitude BETWEEN -90 AND 90
        OR longitude IS NULL OR NOT longitude BETWEEN -180 AND 180
        OR radius_meters IS NULL OR NOT radius_meters BETWEEN 0 AND 50000
        OR start_time IS NULL OR end_time IS NULL OR start_time >= end_time
        OR NOT isfinite(start_time) OR NOT isfinite(end_time)
        OR exclude_linked_duplicates IS NULL
        OR group_by IS NULL OR group_by NOT IN ('service_name','status','both') THEN
        RAISE EXCEPTION 'Invalid coordinate, radius, time interval, duplicate policy or grouping'
            USING ERRCODE='22023';
    END IF;
    RETURN QUERY
    WITH normalized AS (
        SELECT r.service_request_id,
            citylens.normalize_request_category(r.service_name) AS service,
            citylens.normalize_request_category(r.status) AS state
        FROM citylens.service_requests r
        WHERE r.requested_at>=start_time AND r.requested_at<end_time
            AND (NOT exclude_linked_duplicates OR r.parent_id IS NULL)
            AND ST_DWithin(r.location,
                ST_SetSRID(ST_MakePoint(longitude,latitude),4326)::geography,radius_meters)
    ), matching AS (
        SELECT n.service_request_id,
            CASE WHEN group_by IN ('service_name','both') THEN n.service END AS service,
            CASE WHEN group_by IN ('status','both') THEN n.state END AS state
        FROM normalized n
        WHERE (service_name_filter IS NULL OR n.service IS NOT DISTINCT FROM citylens.normalize_request_category(service_name_filter))
          AND (status_filter IS NULL OR n.state IS NOT DISTINCT FROM citylens.normalize_request_category(status_filter))
    )
    SELECT m.service,m.state,count(DISTINCT m.service_request_id)
    FROM matching m GROUP BY m.service,m.state
    ORDER BY m.service COLLATE "C" NULLS FIRST,m.state COLLATE "C" NULLS FIRST;
END;
$$;
