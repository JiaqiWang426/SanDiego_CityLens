-- Counts submitted request IDs, including explicitly linked duplicates by default.
-- Radius includes its boundary; time interval is [start_time, end_time).
-- Public argument order is latitude, longitude; ST_MakePoint uses the reverse.
-- A radius cap of 50 km bounds query scope. Zero permits an exact-point query.
-- STABLE describes a read-only function whose result can depend on DB contents
-- within the statement; it does not cache answers across changing datasets.
-- bigint accommodates large counts. This counts submitted records, not issues.
CREATE OR REPLACE FUNCTION citylens.count_service_requests(
    latitude double precision,
    longitude double precision,
    radius_meters double precision,
    start_time timestamptz,
    end_time timestamptz,
    exclude_linked_duplicates boolean DEFAULT false
) RETURNS bigint LANGUAGE plpgsql STABLE AS $$
BEGIN
    -- Reject NULLs, invalid numeric ranges, infinite dates, and empty/reversed
    -- intervals. SQLSTATE 22023 means invalid_parameter_value for callers/tests.
    IF latitude IS NULL OR NOT latitude BETWEEN -90 AND 90
       OR longitude IS NULL OR NOT longitude BETWEEN -180 AND 180
       OR radius_meters IS NULL OR NOT radius_meters BETWEEN 0 AND 50000
       OR start_time IS NULL OR end_time IS NULL OR start_time >= end_time
       OR NOT isfinite(start_time) OR NOT isfinite(end_time)
       OR exclude_linked_duplicates IS NULL THEN
        RAISE EXCEPTION 'Invalid coordinates, radius (0..50000 meters), or time interval'
            USING ERRCODE = '22023';
    END IF;
    RETURN (
        -- The primary key already ensures one row per request ID, so count(*)
        -- cannot double-count repeated CSV occurrences after ingestion.
        SELECT count(*) FROM citylens.service_requests r
        -- Half-open intervals avoid counting a boundary timestamp twice when
        -- querying adjacent periods, such as January followed by February.
        WHERE r.requested_at >= start_time AND r.requested_at < end_time
          -- Flag false: NOT false is true, so every parent-link state passes.
          -- Flag true: only rows with no explicit parent link pass. This does
          -- not discover unlinked duplicates or merge records into real issues.
          AND (NOT exclude_linked_duplicates OR r.parent_id IS NULL)
          -- geography uses spheroidal distance in meters by default. Assigning
          -- SRID labels the coordinate system; it does not transform coordinates.
          -- NULL locations cannot satisfy this filter and are not counted.
          AND ST_DWithin(r.location,
              ST_SetSRID(ST_MakePoint(longitude, latitude),4326)::geography,
              radius_meters)
    );
END;
$$;
