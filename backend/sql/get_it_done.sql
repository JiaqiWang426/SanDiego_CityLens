-- Defines the storage structure for Get it Done data

-- Enable geographic types/functions and keep application objects in a schema.
-- Run with an account permitted to install PostGIS in this database.
-- These statements initialize objects; they are not a table migration system.
CREATE EXTENSION IF NOT EXISTS postgis; --PostGIS adds geographic types and operations, such as finding points within 800 meters
CREATE SCHEMA IF NOT EXISTS citylens;

CREATE TABLE IF NOT EXISTS citylens.ingestions (
    -- One committed entry per successful import. Failed imports roll back.
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, --automatically generated unique import ID
    source_file text NOT NULL, --source CSV filename
    sha256 text NOT NULL, -- File-byte fingerprint; does not prove source freshness.
    imported_at timestamptz NOT NULL DEFAULT now(),
    source_rows bigint NOT NULL, -- Total CSV rows, including repeated timestamp.
    distinct_requests bigint NOT NULL -- Number of unique request IDs within that imported CSV only.
);

CREATE TABLE IF NOT EXISTS citylens.service_requests (
    service_request_id text PRIMARY KEY, -- One current row per source request ID.
    -- No foreign key: a parent can be outside the imported subset.
    parent_id text,    -- parent service request id
    requested_at timestamptz NOT NULL, -- An instant; display timezone is session-dependent.
    service_name text,
    status text, -- Source status; Closed does not establish completed repair.
    location geography(Point, 4326), -- Geographic point, or NULL when coordinateds are unavailable
    raw_record jsonb NOT NULL, -- Original field values from the current source row, stored as JSON.
    ingestion_id bigint NOT NULL REFERENCES citylens.ingestions(id)
);
-- GiST supports geographic candidate filtering; the B-tree index supports
-- creation-time intervals. PostgreSQL decides which indexes to use per query.
CREATE INDEX IF NOT EXISTS service_requests_location_idx
    ON citylens.service_requests USING gist(location);
CREATE INDEX IF NOT EXISTS service_requests_requested_idx
    ON citylens.service_requests(requested_at);
