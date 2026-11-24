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
