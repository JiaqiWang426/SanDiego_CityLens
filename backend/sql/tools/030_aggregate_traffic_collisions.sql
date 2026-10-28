-- Counts use all filtered records, not a page from get_traffic_collisions.
-- Calendar boundaries use an explicit timezone, independent of DB session time.
CREATE OR REPLACE FUNCTION citylens.aggregate_traffic_collisions(
    start_time timestamptz, end_time timestamptz,
    latitude float8 DEFAULT NULL, longitude float8 DEFAULT NULL,
    radius_meters float8 DEFAULT NULL,
    min_injured integer DEFAULT NULL, max_injured integer DEFAULT NULL,
    min_killed integer DEFAULT NULL, max_killed integer DEFAULT NULL,
    hit_run boolean DEFAULT NULL, street_name text DEFAULT NULL,
    hit_run_level_filter text DEFAULT NULL,
    period text DEFAULT 'month', calendar_timezone text DEFAULT 'America/Los_Angeles'
) RETURNS TABLE(period_start timestamptz, collision_count bigint,
    located_count bigint, unknown_injured_count bigint, unknown_killed_count bigint)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF period IS NULL OR period NOT IN ('day','week','month','quarter','year')
        OR calendar_timezone IS NULL OR NOT EXISTS (
            SELECT 1 FROM pg_timezone_names WHERE name=calendar_timezone) THEN
        RAISE EXCEPTION 'Invalid calendar period or timezone' USING ERRCODE='22023';
    END IF;
    RETURN QUERY SELECT date_trunc(period,c.occurred_at,calendar_timezone),
        count(*),count(c.location),count(*) FILTER (WHERE c.injured IS NULL),
        count(*) FILTER (WHERE c.killed IS NULL)
    FROM citylens.filter_traffic_collisions(start_time,end_time,latitude,longitude,
        radius_meters,min_injured,max_injured,min_killed,max_killed,
        hit_run,street_name,hit_run_level_filter) c
    GROUP BY 1 ORDER BY 1;
END;
$$;
