-- 010 defines which records match. 020 controls how many matching records are returned and in what order.
CREATE OR REPLACE FUNCTION citylens.get_traffic_collisions(
    start_time timestamptz, end_time timestamptz,
    latitude float8 DEFAULT NULL, longitude float8 DEFAULT NULL,
    radius_meters float8 DEFAULT NULL,
    min_injured integer DEFAULT NULL, max_injured integer DEFAULT NULL,
    min_killed integer DEFAULT NULL, max_killed integer DEFAULT NULL,
    hit_run boolean DEFAULT NULL, street_name text DEFAULT NULL,
    hit_run_level_filter text DEFAULT NULL,
    result_limit integer DEFAULT 100, result_offset integer DEFAULT 0
) RETURNS SETOF citylens.traffic_collisions LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF result_limit IS NULL OR NOT result_limit BETWEEN 1 AND 1000
        OR result_offset IS NULL OR NOT result_offset BETWEEN 0 AND 100000 THEN
        RAISE EXCEPTION 'limit must be 1..1000; offset 0..100000' USING ERRCODE='22023';
    END IF;
    RETURN QUERY SELECT * FROM citylens.filter_traffic_collisions(
        start_time,end_time,latitude,longitude,radius_meters,
        min_injured,max_injured,min_killed,max_killed,hit_run,street_name,hit_run_level_filter)
    ORDER BY occurred_at, report_id LIMIT result_limit OFFSET result_offset;
END;
$$;
