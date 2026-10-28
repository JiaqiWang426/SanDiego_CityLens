-- Additional domain in the existing CityLens database. No Get It Done changes.
CREATE TABLE IF NOT EXISTS citylens.collision_imports (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_file text NOT NULL,
    sha256 text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    source_rows bigint NOT NULL DEFAULT 0,
    accepted_rows bigint NOT NULL DEFAULT 0,
    rejected_rows bigint NOT NULL DEFAULT 0
);

-- An immutable per-import archive keeps every parsed CSV row, including
-- repeated IDs, rejected records, original whitespace, and extra columns.
CREATE TABLE IF NOT EXISTS citylens.collision_source_rows (
    import_id bigint NOT NULL REFERENCES citylens.collision_imports(id),
    row_number bigint NOT NULL,
    raw_record jsonb NOT NULL,
    validation_issues jsonb NOT NULL,
    PRIMARY KEY (import_id, row_number)
);

CREATE TABLE IF NOT EXISTS citylens.traffic_collisions (
    report_id text PRIMARY KEY,
    occurred_at timestamptz,
    police_beat text,
    primary_street text NOT NULL,
    intersecting_street text NOT NULL,
    normalized_address text NOT NULL,
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
CREATE INDEX IF NOT EXISTS collision_location_idx ON citylens.traffic_collisions USING gist(location);
CREATE INDEX IF NOT EXISTS collision_time_idx ON citylens.traffic_collisions(occurred_at);
CREATE INDEX IF NOT EXISTS collision_primary_street_idx ON citylens.traffic_collisions(primary_street);
CREATE INDEX IF NOT EXISTS collision_cross_street_idx ON citylens.traffic_collisions(intersecting_street);
