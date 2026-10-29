-- [010_filter_traffic_collisions.sql] finds rows that meet the given requirements

-- Internal uncapped filtering primitive shared by retrieval and aggregation.
-- Agent callers should use the bounded public functions below, not this helper.
-- Date interval is required and half-open; query timestamps should have offsets.
-- Radius is optional, but latitude/longitude/radius must be supplied together.
-- Spatial results include only geocoded approximate points; NULL locations do
-- not match. Nonspatial results still include unresolved collision records.
CREATE OR REPLACE FUNCTION citylens.filter_traffic_collisions(
    start_time timestamptz, end_time timestamptz, -- Required date interval: start included, end excluded
    latitude float8 DEFAULT NULL, longitude float8 DEFAULT NULL, -- geographic filter; supply all three together
    radius_meters float8 DEFAULT NULL,
    min_injured integer DEFAULT NULL, max_injured integer DEFAULT NULL,
    min_killed integer DEFAULT NULL, max_killed integer DEFAULT NULL,
    hit_run boolean DEFAULT NULL, street_name text DEFAULT NULL,
    hit_run_level_filter text DEFAULT NULL
) RETURNS SETOF citylens.traffic_collisions LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF start_time IS NULL OR end_time IS NULL OR start_time >= end_time
        OR NOT isfinite(start_time) OR NOT isfinite(end_time) THEN
        RAISE EXCEPTION 'An increasing finite time interval is required' USING ERRCODE='22023';
    END IF;
    IF num_nonnulls(latitude,longitude,radius_meters) NOT IN (0,3) -- This checks that the geographic parameters are either all omitted or all supplied.
        OR (latitude IS NOT NULL AND NOT latitude BETWEEN -90 AND 90)
        OR (longitude IS NOT NULL AND NOT longitude BETWEEN -180 AND 180)
        OR (radius_meters IS NOT NULL AND NOT radius_meters BETWEEN 0 AND 50000)
        OR min_injured < 0 OR max_injured < 0 OR min_killed < 0 OR max_killed < 0
        OR min_injured > max_injured OR min_killed > max_killed THEN
        RAISE EXCEPTION 'Invalid radius/coordinates or count bounds' USING ERRCODE='22023';
    END IF;
    RETURN QUERY SELECT c.* FROM citylens.traffic_collisions c
    WHERE c.occurred_at >= start_time AND c.occurred_at < end_time
        AND (radius_meters IS NULL OR ST_DWithin(c.location,
            ST_SetSRID(ST_MakePoint(longitude,latitude),4326)::geography,radius_meters))
        -- NULL counts are retained in storage but cannot satisfy numeric bounds.
        AND (min_injured IS NULL OR c.injured >= min_injured)
        AND (max_injured IS NULL OR c.injured <= max_injured)
        AND (min_killed IS NULL OR c.killed >= min_killed)
        AND (max_killed IS NULL OR c.killed <= max_killed)
        AND (hit_run IS NULL OR c.hit_and_run=hit_run)
        AND (hit_run_level_filter IS NULL OR c.hit_run_level=hit_run_level_filter)
        -- The Python wrapper supplies a normalized full street name. Either
        -- the primary street or the explicitly reported cross street can match.
        AND (street_name IS NULL OR c.primary_street=street_name OR c.intersecting_street=street_name);
END;
$$;
