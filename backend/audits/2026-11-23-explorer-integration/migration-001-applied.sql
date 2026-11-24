BEGIN;
SET LOCAL lock_timeout = '15s';
-- Migration 001: service request classification (main/sub category).
--
-- Additive and idempotent. Run with the table owner, normally through
--   python -m backend.db migrate
-- which applies this file, synchronizes backend/category_mapping.csv into
-- citylens.service_category_rules and backfills changed rows in one transaction.
--
-- Nothing here deletes, rewrites or re-geocodes existing data:
-- * New columns are nullable without defaults (a catalog-only change; no table
--   rewrite). NULL means "not yet classified", never a guessed category.
-- * Original service_name, raw_record, location, IDs and indexes are untouched.
-- * Existing SQL tools keep their signatures; the legacy category stays
--   service_name for backward compatibility.

ALTER TABLE citylens.service_requests
    ADD COLUMN IF NOT EXISTS main_category text,
    ADD COLUMN IF NOT EXISTS sub_category text,
    ADD COLUMN IF NOT EXISTS classification_method text,
    ADD COLUMN IF NOT EXISTS classification_version text; -- mapping SHA-256

-- Either all four derived fields are present, or none are.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'service_requests_classification_check'
                     AND conrelid = 'citylens.service_requests'::regclass) THEN
        ALTER TABLE citylens.service_requests ADD CONSTRAINT service_requests_classification_check CHECK (
            (main_category IS NULL AND sub_category IS NULL
             AND classification_method IS NULL AND classification_version IS NULL)
            OR (main_category IS NOT NULL AND sub_category IS NOT NULL
                AND classification_version IS NOT NULL
                AND classification_method IN ('detail_match', 'service_default', 'needs_review', 'unmapped')));
    END IF;
END $$;

-- Filtering and grouping support. requested_at keeps date-range scans selective.
CREATE INDEX IF NOT EXISTS service_requests_main_sub_requested_idx
    ON citylens.service_requests (main_category, sub_category, requested_at);
CREATE INDEX IF NOT EXISTS service_requests_sub_requested_idx
    ON citylens.service_requests (sub_category, requested_at);

-- One row per mapping file version that has been applied. Exactly one is active.
CREATE TABLE IF NOT EXISTS citylens.service_category_mappings (
    sha256 text PRIMARY KEY,               -- SHA-256 of category_mapping.csv bytes
    source_file text NOT NULL,
    rule_count integer NOT NULL CHECK (rule_count > 0),
    hierarchy jsonb NOT NULL,              -- designed main -> subcategories, CSV order
    applied_at timestamptz NOT NULL DEFAULT now(),
    is_active boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX IF NOT EXISTS service_category_mappings_single_active_idx
    ON citylens.service_category_mappings ((true)) WHERE is_active;

-- Compiled rules of the ACTIVE mapping only. detail NULL = the service default
-- ("any"/"fallback" rows); otherwise one exact normalized detail condition.
-- service_name '' represents a missing source service name.
CREATE TABLE IF NOT EXISTS citylens.service_category_rules (
    ordinal integer PRIMARY KEY,
    service_name text NOT NULL,
    detail text CHECK (detail IS NULL OR detail <> ''),
    main_category text NOT NULL CHECK (main_category <> ''),
    sub_category text NOT NULL CHECK (sub_category <> ''),
    mapping_sha256 text NOT NULL REFERENCES citylens.service_category_mappings (sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS service_category_rules_condition_idx
    ON citylens.service_category_rules (service_name, detail) WHERE detail IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS service_category_rules_default_idx
    ON citylens.service_category_rules (service_name) WHERE detail IS NULL;

-- Same normalization as category_rules.normalize_text and the Explorer's
-- clean(): collapse whitespace runs, trim, blank -> NULL. Case is preserved.
CREATE OR REPLACE FUNCTION citylens.normalize_label(value text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT nullif(btrim(regexp_replace(value, '\s+', ' ', 'g')), '')
$$;

-- The single SQL classifier. It mirrors CategoryRules.classify_or_unmapped:
--   exact detail condition > service default; final Needs Review -> needs_review;
--   a service without any rule -> Needs Review / Unmapped service name / unmapped.
-- Set-based: only rows whose stored classification differs from the active
-- mapping are updated, so repeated runs return 0 and touch nothing. Ingestion
-- calls it after each upsert, which classifies new rows and rows whose service
-- name/detail changed, in the same transaction as the import.
CREATE OR REPLACE FUNCTION citylens.backfill_service_request_classification()
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    active text;
    changed bigint;
BEGIN
    SELECT sha256 INTO active FROM citylens.service_category_mappings WHERE is_active;
    IF active IS NULL THEN
        RAISE EXCEPTION 'No active service category mapping; run python -m backend.db migrate'
            USING ERRCODE = '55000';
    END IF;
    WITH normalized AS (
        SELECT s.service_request_id,
               coalesce(citylens.normalize_label(s.service_name), '') AS service,
               coalesce(citylens.normalize_label(s.raw_record->>'service_name_detail'), '') AS detail
        FROM citylens.service_requests s
    ), computed AS (
        SELECT n.service_request_id,
               coalesce(c.main_category, d.main_category, 'Needs Review') AS main_category,
               coalesce(c.sub_category, d.sub_category, 'Unmapped service name') AS sub_category,
               CASE WHEN c.main_category IS NULL AND d.main_category IS NULL THEN 'unmapped'
                    WHEN coalesce(c.main_category, d.main_category) = 'Needs Review' THEN 'needs_review'
                    WHEN c.main_category IS NOT NULL THEN 'detail_match'
                    ELSE 'service_default' END AS classification_method
        FROM normalized n
        LEFT JOIN citylens.service_category_rules c ON c.service_name = n.service AND c.detail = n.detail
        LEFT JOIN citylens.service_category_rules d ON d.service_name = n.service AND d.detail IS NULL
    )
    UPDATE citylens.service_requests r
    SET main_category = x.main_category, sub_category = x.sub_category,
        classification_method = x.classification_method, classification_version = active
    FROM computed x
    WHERE r.service_request_id = x.service_request_id
      AND (r.main_category, r.sub_category, r.classification_method, r.classification_version)
          IS DISTINCT FROM (x.main_category, x.sub_category, x.classification_method, active);
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed;
END $$;
REVOKE ALL ON FUNCTION citylens.backfill_service_request_classification() FROM PUBLIC;

-- Read access for the restricted Agent/Explorer reader, if it is installed.
-- (sql/setup_agent_reader.sql grants the same for databases set up later.)
DO $$
BEGIN
    IF to_regrole('citylens_agent_reader') IS NOT NULL THEN
        GRANT SELECT ON citylens.service_category_mappings, citylens.service_category_rules
            TO citylens_agent_reader;
    END IF;
END $$;

INSERT INTO citylens.service_category_mappings (sha256, source_file, rule_count, hierarchy)
            VALUES ('b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6', 'category_mapping.csv', 168, '[{"main_category": "Sanitation & Waste", "subcategories": ["Missed collection", "Delayed collection", "Potential missed collection", "Illegal dumping", "Litter", "Shopping cart", "Waste on private property", "Park litter and bins", "Environmental services compliance", "ESD collections - unspecified", "Trash and recycling collection", "Container request", "Dead animal removal", "Street sweeping", "Biohazard report"]}, {"main_category": "Parking & Vehicles", "subcategories": ["Parking - unspecified", "Parking - 72 hours", "Oversized vehicle", "Parking obstruction", "Parking zone or time restriction", "Residential parking permit"]}, {"main_category": "Traffic & Mobility", "subcategories": ["Shared mobility device", "Bicycle-related issue", "Road markings", "Traffic devices and delineators", "Traffic maintenance - unspecified", "Traffic sign maintenance", "Bicycle facility maintenance", "Street lighting", "Traffic signal fault", "Traffic signal timing", "Traffic engineering and evaluation"]}, {"main_category": "Roads & Sidewalks", "subcategories": ["Pothole", "Pavement maintenance", "Sidewalk repair", "Right-of-way maintenance - unspecified", "Roadside barriers and structures", "Right-of-way enforcement - unspecified", "Trenches and right-of-way enforcement"]}, {"main_category": "Water & Drainage", "subcategories": ["Surface water flow", "Street flooding and ponding", "Stormwater infrastructure", "Stormwater compliance"]}, {"main_category": "Parks & Vegetation", "subcategories": ["Vegetation encroachment", "Parks - other or unspecified", "Park feedback and improvements", "Park facilities", "Park vegetation and grounds", "Park irrigation and drainage", "Park activity and enforcement", "Park paths and pavement", "Tree maintenance", "Weed cleanup"]}, {"main_category": "Graffiti", "subcategories": ["Park graffiti", "Graffiti removal - unspecified", "Public graffiti", "Private graffiti", "Graffiti enforcement"]}, {"main_category": "Community & Property Services", "subcategories": ["Encampment report", "Homeless outreach", "STRO violation report", "Building and land-use enforcement", "Development services enforcement", "COVID-19 service report"]}, {"main_category": "Needs Review", "subcategories": ["Source Other - unresolved", "Missing service name"]}]'::jsonb) ON CONFLICT (sha256) DO NOTHING;
UPDATE citylens.service_category_mappings SET is_active = false WHERE is_active AND sha256 <> 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6';
DELETE FROM citylens.service_category_rules WHERE mapping_sha256 <> 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6';
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (1, 'Missed Collection', NULL, 'Sanitation & Waste', 'Missed collection', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (2, 'Missed Collection', 'Delayed Collection', 'Sanitation & Waste', 'Delayed collection', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (3, 'Missed Collection', 'Potential Missed Collection', 'Sanitation & Waste', 'Potential missed collection', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (4, 'Illegal Dumping', NULL, 'Sanitation & Waste', 'Illegal dumping', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (5, 'Illegal Dumping', 'Litter', 'Sanitation & Waste', 'Litter', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (6, 'Illegal Dumping', 'Shopping Cart', 'Sanitation & Waste', 'Shopping cart', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (7, 'Parking', NULL, 'Parking & Vehicles', 'Parking - unspecified', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (8, 'Parking', '72 Hour Violation', 'Parking & Vehicles', 'Parking - 72 hours', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (9, 'Parking', 'Oversized Vehicle Complaints', 'Parking & Vehicles', 'Oversized vehicle', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (10, 'Parking', 'Alley', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (11, 'Parking', 'Bike Lane', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (12, 'Parking', 'Lane of Traffic', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (13, 'Parking', 'Sidewalk', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (14, 'Parking', 'Blue Disabled Zone', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (15, 'Parking', 'Fire Hydrant', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (16, 'Parking', 'Green/Time Limited', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (17, 'Parking', 'Parking Meter', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (18, 'Parking', 'Parking Zone Violation', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (19, 'Parking', 'Passenger Zone (White)', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (20, 'Parking', 'Red Zone', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (21, 'Parking', 'Yellow Commercial Zone', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (22, 'Parking', 'No Residential Permit', 'Parking & Vehicles', 'Residential parking permit', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (23, 'Parking Violation', NULL, 'Parking & Vehicles', 'Parking - unspecified', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (24, 'Parking Violation', '72 Hour Violation', 'Parking & Vehicles', 'Parking - 72 hours', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (25, 'Parking Violation', 'Oversized Vehicle Complaints', 'Parking & Vehicles', 'Oversized vehicle', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (26, 'Parking Violation', 'Alley', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (27, 'Parking Violation', 'Bike Lane', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (28, 'Parking Violation', 'Blocked Driveway', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (29, 'Parking Violation', 'Lane of Traffic', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (30, 'Parking Violation', 'Parking Within 20 Feet of Crosswalk', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (31, 'Parking Violation', 'Sidewalk', 'Parking & Vehicles', 'Parking obstruction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (32, 'Parking Violation', 'Blue Disabled Zone', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (33, 'Parking Violation', 'EV Charger', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (34, 'Parking Violation', 'Fire Hydrant', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (35, 'Parking Violation', 'Green/Time Limited', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (36, 'Parking Violation', 'Parking Zone Violation', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (37, 'Parking Violation', 'Passenger Zone (White)', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (38, 'Parking Violation', 'Red Zone', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (39, 'Parking Violation', 'Timed Zone Violation', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (40, 'Parking Violation', 'Yellow Commercial Zone', 'Parking & Vehicles', 'Parking zone or time restriction', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (41, 'Parking Violation', 'No Residential Permit', 'Parking & Vehicles', 'Residential parking permit', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (42, 'Parking - 72-Hours', NULL, 'Parking & Vehicles', 'Parking - 72 hours', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (43, 'Oversized Vehicle', NULL, 'Parking & Vehicles', 'Oversized vehicle', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (44, 'Shared Mobility Device', NULL, 'Traffic & Mobility', 'Shared mobility device', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (45, 'Bicycle-Related Issue', NULL, 'Traffic & Mobility', 'Bicycle-related issue', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (46, 'Pothole', NULL, 'Roads & Sidewalks', 'Pothole', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (47, 'Pavement Maintenance', NULL, 'Roads & Sidewalks', 'Pavement maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (48, 'Pavement Maintenance', 'SURFACE FLOW ISSUE', 'Water & Drainage', 'Surface water flow', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (49, 'Sidewalk Repair Issue', NULL, 'Roads & Sidewalks', 'Sidewalk repair', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (50, 'ROW Maintenance', NULL, 'Roads & Sidewalks', 'Right-of-way maintenance - unspecified', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (51, 'ROW Maintenance', 'PAINT CURB - MAINTENANCE', 'Traffic & Mobility', 'Road markings', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (52, 'ROW Maintenance', 'PAINT LEGEND - MAINTENANCE', 'Traffic & Mobility', 'Road markings', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (53, 'ROW Maintenance', 'PAINT STRIPING - MAINTENANCE', 'Traffic & Mobility', 'Road markings', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (54, 'ROW Maintenance', 'TRAFFIC DEVICES', 'Traffic & Mobility', 'Traffic devices and delineators', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (55, 'ROW Maintenance', 'MARKERS/DELINIATORS - MAINTENANCE', 'Traffic & Mobility', 'Traffic devices and delineators', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (56, 'ROW Maintenance', 'TRAFFIC MAINTENANCE - OTHER', 'Traffic & Mobility', 'Traffic maintenance - unspecified', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (57, 'ROW Maintenance', 'SIGN - MAINT - BIKE', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (58, 'ROW Maintenance', 'SIGN - MAINT - DO NOT ENTER', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (59, 'ROW Maintenance', 'SIGN - MAINT - OTHER', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (60, 'ROW Maintenance', 'SIGN - MAINT - PARKING', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (61, 'ROW Maintenance', 'SIGN - MAINT - PED', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (62, 'ROW Maintenance', 'SIGN - MAINT - SCHOOL', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (63, 'ROW Maintenance', 'SIGN - MAINT - SPEED', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (64, 'ROW Maintenance', 'SIGN - MAINT - STOP', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (65, 'ROW Maintenance', 'SIGN - MAINT - STREET NAME', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (66, 'ROW Maintenance', 'SIGN - MAINT - TRAFFIC', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (67, 'ROW Maintenance', 'SIGN - MAINT - WRONG WAY', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (68, 'ROW Maintenance', 'SIGN - MAINT - YIELD', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (69, 'ROW Maintenance', 'TRAFFIC SIGN - NEW AND MODIFICATIONS', 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (70, 'ROW Maintenance', 'BRIDGE/RAILINGS/STAIRS REPAIR', 'Roads & Sidewalks', 'Roadside barriers and structures', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (71, 'ROW Maintenance', 'CHAIN LINK FENCE REPAIR', 'Roads & Sidewalks', 'Roadside barriers and structures', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (72, 'ROW Maintenance', 'GUARDRAILS/BARRIER METAL/WOODEN', 'Roads & Sidewalks', 'Roadside barriers and structures', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (73, 'ROW Maintenance', 'GUARDRAILS/BARRIER METAL/WOODEN MAINT', 'Roads & Sidewalks', 'Roadside barriers and structures', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (74, 'ROW Maintenance', 'BICYCLE FACILITIES MAINTENANCE', 'Traffic & Mobility', 'Bicycle facility maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (75, 'ROW Maintenance', 'BIKE MAINT-SIGNAGE', 'Traffic & Mobility', 'Bicycle facility maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (76, 'ROW Maintenance', 'BIKE MAINT-SIGNAL LOOP DETECTION', 'Traffic & Mobility', 'Bicycle facility maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (77, 'ROW Maintenance', 'BIKE MAINT-STRIPING/MARKINGS', 'Traffic & Mobility', 'Bicycle facility maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (78, 'ROW Maintenance', 'FLOODED', 'Water & Drainage', 'Street flooding and ponding', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (79, 'ROW Maintenance', 'PONDING WATER IN GUTTER/STREET', 'Water & Drainage', 'Street flooding and ponding', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (80, 'ROW Maintenance', 'Illegal Dumping', 'Sanitation & Waste', 'Illegal dumping', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (81, 'ROW Maintenance', 'Waste on Private Property', 'Sanitation & Waste', 'Waste on private property', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (82, 'Right-of-Way Code Enforcement', NULL, 'Roads & Sidewalks', 'Right-of-way enforcement - unspecified', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (83, 'Right-of-Way Code Enforcement', 'Trenches', 'Roads & Sidewalks', 'Trenches and right-of-way enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (84, 'Right-of-Way Code Enforcement', 'Vegetation Encroachment', 'Parks & Vegetation', 'Vegetation encroachment', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (85, 'Street Light Maintenance', NULL, 'Traffic & Mobility', 'Street lighting', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (86, 'Traffic Sign Maintenance', NULL, 'Traffic & Mobility', 'Traffic sign maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (87, 'Traffic Signal Issue', NULL, 'Traffic & Mobility', 'Traffic signal fault', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (88, 'Traffic Signal Timing', NULL, 'Traffic & Mobility', 'Traffic signal timing', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (89, 'Traffic Engineering', NULL, 'Traffic & Mobility', 'Traffic engineering and evaluation', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (90, 'Parks Issue', NULL, 'Parks & Vegetation', 'Parks - other or unspecified', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (91, 'Parks Issue', 'Feedback - General Feedback', 'Parks & Vegetation', 'Park feedback and improvements', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (92, 'Parks Issue', 'Feedback - Requested Park Improvement', 'Parks & Vegetation', 'Park feedback and improvements', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (93, 'Parks Issue', 'Beach Shower Issue', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (94, 'Parks Issue', 'Broken Playground Equipment', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (95, 'Parks Issue', 'Drinking fountain', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (96, 'Parks Issue', 'Electrical Issue', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (97, 'Parks Issue', 'Fencing', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (98, 'Parks Issue', 'Gate/Access Closure', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (99, 'Parks Issue', 'Grill/Hot Coal Container', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (100, 'Parks Issue', 'Park Equipment', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (101, 'Parks Issue', 'Park Equipment (missing from recent change request)', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (102, 'Parks Issue', 'Park Furniture/Bench/Table', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (103, 'Parks Issue', 'Park Lighting Issue', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (104, 'Parks Issue', 'Park Sign', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (105, 'Parks Issue', 'Pet Waste Bags', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (106, 'Parks Issue', 'Portable Restroom', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (107, 'Parks Issue', 'Restroom', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (108, 'Parks Issue', 'Restroom - Litter/Cleanliness', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (109, 'Parks Issue', 'Restroom - Sink', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (110, 'Parks Issue', 'Restroom - Supplies', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (111, 'Parks Issue', 'Restroom - Urinal/Toilet', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (112, 'Parks Issue', 'Restroom/Comfort Station', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (113, 'Parks Issue', 'Swing Set', 'Parks & Vegetation', 'Park facilities', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (114, 'Parks Issue', 'Beach/Sand Maintenance', 'Parks & Vegetation', 'Park vegetation and grounds', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (115, 'Parks Issue', 'Bee Hive', 'Parks & Vegetation', 'Park vegetation and grounds', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (116, 'Parks Issue', 'Brush Management', 'Parks & Vegetation', 'Park vegetation and grounds', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (117, 'Parks Issue', 'Brush Management/Weeds', 'Parks & Vegetation', 'Park vegetation and grounds', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (118, 'Parks Issue', 'Grass/Turf Issue', 'Parks & Vegetation', 'Park vegetation and grounds', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (119, 'Parks Issue', 'Potential Tree Hazard', 'Parks & Vegetation', 'Park vegetation and grounds', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (120, 'Parks Issue', 'Tree hazard/Brush management', 'Parks & Vegetation', 'Park vegetation and grounds', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (121, 'Parks Issue', 'Vegetation Maintenance', 'Parks & Vegetation', 'Park vegetation and grounds', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (122, 'Parks Issue', 'Weed Abatement', 'Parks & Vegetation', 'Park vegetation and grounds', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (123, 'Parks Issue', 'Broken Storm Drain', 'Parks & Vegetation', 'Park irrigation and drainage', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (124, 'Parks Issue', 'Sprinkler/Irrigation', 'Parks & Vegetation', 'Park irrigation and drainage', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (125, 'Parks Issue', 'Sprinkler/Irrigation problem', 'Parks & Vegetation', 'Park irrigation and drainage', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (126, 'Parks Issue', 'Graffiti', 'Graffiti', 'Park graffiti', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (127, 'Parks Issue', 'Trash Can/Overflowing', 'Sanitation & Waste', 'Park litter and bins', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (128, 'Parks Issue', 'Trash/Litter', 'Sanitation & Waste', 'Park litter and bins', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (129, 'Parks Issue', 'Dog Off Leash', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (130, 'Parks Issue', 'E-Bikes/Scooters/E-Boards', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (131, 'Parks Issue', 'Illegal Activity', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (132, 'Parks Issue', 'Illegal Fires', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (133, 'Parks Issue', 'Illegal Parking', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (134, 'Parks Issue', 'Park Enforcement', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (135, 'Parks Issue', 'Park Enforcement (missing from recent change request)', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (136, 'Parks Issue', 'Security Issue', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (137, 'Parks Issue', 'Sidewalk Vendor/Expressive Activity', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (138, 'Parks Issue', 'Unpermitted Activity/Event', 'Parks & Vegetation', 'Park activity and enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (139, 'Parks Issue', 'Path/Walkway/Road Pavement/Pothole', 'Parks & Vegetation', 'Park paths and pavement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (140, 'Parks Issue', 'Pavement Striping', 'Parks & Vegetation', 'Park paths and pavement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (141, 'Parks Issue', 'Pothole/Asphalt', 'Parks & Vegetation', 'Park paths and pavement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (142, 'Parks Issue', 'Trail Maintenance', 'Parks & Vegetation', 'Park paths and pavement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (143, 'Tree Maintenance', NULL, 'Parks & Vegetation', 'Tree maintenance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (144, 'Vegetation Encroachment', NULL, 'Parks & Vegetation', 'Vegetation encroachment', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (145, 'Weed Cleanup', NULL, 'Parks & Vegetation', 'Weed cleanup', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (146, 'Stormwater', NULL, 'Water & Drainage', 'Stormwater infrastructure', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (147, 'Stormwater Code Enforcement', NULL, 'Water & Drainage', 'Stormwater compliance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (148, 'Street Flooded', NULL, 'Water & Drainage', 'Street flooding and ponding', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (149, 'Environmental Services Code Compliance', NULL, 'Sanitation & Waste', 'Environmental services compliance', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (150, 'ESD Collections', NULL, 'Sanitation & Waste', 'ESD collections - unspecified', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (151, 'Trash/Recycling Collection', NULL, 'Sanitation & Waste', 'Trash and recycling collection', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (152, 'Container Request', NULL, 'Sanitation & Waste', 'Container request', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (153, 'Dead Animal', NULL, 'Sanitation & Waste', 'Dead animal removal', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (154, 'Street Sweeping', NULL, 'Sanitation & Waste', 'Street sweeping', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (155, 'Waste on Private Property', NULL, 'Sanitation & Waste', 'Waste on private property', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (156, 'Biohazard', NULL, 'Sanitation & Waste', 'Biohazard report', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (157, 'Graffiti', NULL, 'Graffiti', 'Graffiti removal - unspecified', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (158, 'Graffiti - Public', NULL, 'Graffiti', 'Public graffiti', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (159, 'Graffiti - Private', NULL, 'Graffiti', 'Private graffiti', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (160, 'Graffiti - Code Enforcement', NULL, 'Graffiti', 'Graffiti enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (161, 'Encampment', NULL, 'Community & Property Services', 'Encampment report', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (162, 'Homeless Outreach', NULL, 'Community & Property Services', 'Homeless outreach', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (163, 'STRO Violation', NULL, 'Community & Property Services', 'STRO violation report', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (164, 'Building and Land Use Enforcement', NULL, 'Community & Property Services', 'Building and land-use enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (165, 'Development Services - Code Enforcement', NULL, 'Community & Property Services', 'Development services enforcement', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (166, 'COVID-19', NULL, 'Community & Property Services', 'COVID-19 service report', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (167, 'Other', NULL, 'Needs Review', 'Source Other - unresolved', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (168, '', NULL, 'Needs Review', 'Missing service name', 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') ON CONFLICT (ordinal) DO NOTHING;
UPDATE citylens.service_category_mappings SET is_active = true WHERE sha256 = 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6' AND NOT is_active;
DO $$ BEGIN
        IF (SELECT count(*) FROM citylens.service_category_rules) <> 168
           OR EXISTS (SELECT 1 FROM citylens.service_category_rules WHERE mapping_sha256 <> 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6')
           OR (SELECT count(*) FROM citylens.service_category_mappings WHERE is_active AND sha256 = 'b148da2936c3834cfadbaa9020f9e90c4ac69bb3d62b92f848b6c79e741194c6') <> 1 THEN
            RAISE EXCEPTION 'Service category rules do not match the requested mapping';
        END IF; END $$;
SELECT citylens.backfill_service_request_classification() AS changed_rows;
COMMIT;
