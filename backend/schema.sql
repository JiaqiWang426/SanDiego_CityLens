CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS citylens;

CREATE TABLE IF NOT EXISTS citylens.ingestions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_file text NOT NULL,
    sha256 text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    source_rows bigint NOT NULL,
    distinct_requests bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS citylens.service_requests (
    service_request_id text PRIMARY KEY,
    parent_id text,
    requested_at timestamptz NOT NULL,
    service_name text,
    status text,
    location geography(Point, 4326),
    raw_record jsonb NOT NULL,
    ingestion_id bigint NOT NULL REFERENCES citylens.ingestions(id)
);
CREATE INDEX IF NOT EXISTS service_requests_location_idx
    ON citylens.service_requests USING gist(location);
CREATE INDEX IF NOT EXISTS service_requests_requested_idx
    ON citylens.service_requests(requested_at);

-- Counts submitted request IDs, including explicitly linked duplicates by default.
-- Radius includes its boundary; time interval is [start_time, end_time).
CREATE OR REPLACE FUNCTION citylens.count_service_requests(
    latitude double precision,
    longitude double precision,
    radius_meters double precision,
    start_time timestamptz,
    end_time timestamptz,
    exclude_linked_duplicates boolean DEFAULT false
) RETURNS bigint LANGUAGE plpgsql STABLE AS $$
BEGIN
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
        SELECT count(*) FROM citylens.service_requests r
        WHERE r.requested_at >= start_time AND r.requested_at < end_time
          AND (NOT exclude_linked_duplicates OR r.parent_id IS NULL)
          AND ST_DWithin(r.location,
              ST_SetSRID(ST_MakePoint(longitude, latitude),4326)::geography,
              radius_meters)
    );
END;
$$;
