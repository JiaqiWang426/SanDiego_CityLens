-- Defines the storage structure for Traffic Collisions data

-- Additional domain in the existing CityLens database. No Get It Done changes.
CREATE TABLE IF NOT EXISTS citylens.collision_imports (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, -- Automatically generated import ID
    source_file text NOT NULL, -- CSV filename
    sha256 text NOT NULL, -- Fingerprint of the original file contents
    imported_at timestamptz NOT NULL DEFAULT now(), -- Import timestamp
    source_rows bigint NOT NULL DEFAULT 0, -- Total CSV rows
    accepted_rows bigint NOT NULL DEFAULT 0, -- Rows accepted for normalized storage, including repeated IDs
    rejected_rows bigint NOT NULL DEFAULT 0 -- Rows excluded from normalized storage, such as those missing report IDs
);

-- This archives every parsed CSV row, including rejected rows and repeated report IDs.
-- repeated IDs, rejected records, original whitespace, and extra columns.
CREATE TABLE IF NOT EXISTS citylens.collision_source_rows (
    import_id bigint NOT NULL REFERENCES citylens.collision_imports(id),
    row_number bigint NOT NULL,
    raw_record jsonb NOT NULL, -- all original field values stored as JSON.
    validation_issues jsonb NOT NULL, -- a JSON list explaining problems found during parsing.
    PRIMARY KEY (import_id, row_number) -- links the row to its import.
);

-- Geocoding: Turn address texts into geographic coordinates (latitude, longitude)
CREATE TABLE IF NOT EXISTS citylens.traffic_collisions (
    report_id text PRIMARY KEY,
    occurred_at timestamptz,
    police_beat text,
    primary_street text NOT NULL,
    intersecting_street text NOT NULL,
    normalized_address text NOT NULL, -- normalized for geocoding, example: 8100 LA JOLLA SCENIC DR, SAN DIEGO, CA
    address_key text NOT NULL, -- Versioned hash of normalized source components.
    violation_section text,
    violation_type text,
    charge_desc text,
    injured integer CHECK (injured >= 0), -- NULL is unknown, never implicit zero.
    killed integer CHECK (killed >= 0),
    hit_run_level text,
    hit_and_run boolean, -- Unknown labels remain NULL; source blank means no H&R.
    raw_record jsonb NOT NULL,
    validation_issues jsonb NOT NULL,
    import_id bigint NOT NULL REFERENCES citylens.collision_imports(id),
    location geography(Point,4326), -- Always approximate if populated.
    geocode_status text NOT NULL DEFAULT 'pending'
        CHECK (geocode_status IN ('pending','matched','no_match','ambiguous','unsupported','error')),
    geocode_provider text,
    geocode_precision text CHECK (geocode_precision IS NULL OR geocode_precision = 'block_approximate'),
    geocoded_at timestamptz,
    geocode_details jsonb,
    CHECK ((location IS NOT NULL) = (geocode_status = 'matched')),
    CHECK (location IS NULL OR (geocode_precision IS NOT NULL AND geocode_precision = 'block_approximate'))
);
CREATE INDEX IF NOT EXISTS collision_location_idx ON citylens.traffic_collisions USING gist(location); -- supports Nearby collisions within a radius
CREATE INDEX IF NOT EXISTS collision_time_idx ON citylens.traffic_collisions(occurred_at); -- supports data-range filtering
CREATE INDEX IF NOT EXISTS collision_primary_street_idx ON citylens.traffic_collisions(primary_street); -- supports primary street lookup
CREATE INDEX IF NOT EXISTS collision_cross_street_idx ON citylens.traffic_collisions(intersecting_street); -- supports cross-street lookup
