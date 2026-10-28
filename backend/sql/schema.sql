-- Enable geographic types/functions and keep application objects in a schema.
-- Run with an account permitted to install PostGIS in this database.
-- These statements initialize objects; they are not a table migration system.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS citylens;

CREATE TABLE IF NOT EXISTS citylens.ingestions (
    -- One committed entry per successful import. Failed imports roll back.
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_file text NOT NULL,
    sha256 text NOT NULL, -- File-byte fingerprint; does not prove source freshness.
    imported_at timestamptz NOT NULL DEFAULT now(),
    source_rows bigint NOT NULL, -- Includes repeated request IDs in the CSV.
    distinct_requests bigint NOT NULL -- Unique IDs within that import only.
);

CREATE TABLE IF NOT EXISTS citylens.service_requests (
    service_request_id text PRIMARY KEY, -- One current row per source request ID.
    -- No foreign key: a parent can be outside the imported subset.
    parent_id text,
    requested_at timestamptz NOT NULL, -- An instant; display timezone is session-dependent.
    service_name text,
    status text, -- Source status; Closed does not establish completed repair.
    location geography(Point, 4326), -- Nullable WGS84 point for meter-based distances.
    raw_record jsonb NOT NULL, -- Current source row, including unnormalized fields.
    ingestion_id bigint NOT NULL REFERENCES citylens.ingestions(id)
);
-- GiST supports geographic candidate filtering; the B-tree index supports
-- creation-time intervals. PostgreSQL decides which indexes to use per query.
CREATE INDEX IF NOT EXISTS service_requests_location_idx
    ON citylens.service_requests USING gist(location);
CREATE INDEX IF NOT EXISTS service_requests_requested_idx
    ON citylens.service_requests(requested_at);
