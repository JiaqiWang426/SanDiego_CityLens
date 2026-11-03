-- Reference ingestion only. Never changes citylens traffic tables or constraints.
CREATE SCHEMA IF NOT EXISTS citylens_ref;
REVOKE ALL ON SCHEMA citylens_ref FROM PUBLIC;
CREATE TABLE IF NOT EXISTS citylens_ref.snapshots (
    snapshot_id text PRIMARY KEY, manifest jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(), validated_at timestamptz,
    integrity jsonb
);
CREATE TABLE IF NOT EXISTS citylens_ref.road_segments (
    snapshot_id text NOT NULL REFERENCES citylens_ref.snapshots(snapshot_id),
    roadsegid bigint NOT NULL, objectid bigint NOT NULL,
    rd30full text, rd30pred text, rd30name text, rd30sfx text, rd30postd text,
    canonical_name text NOT NULL,
    fnode bigint, tnode bigint, f_level integer, t_level integer,
    segclass text, segstat text,
    attributes jsonb NOT NULL, source_geometry jsonb,
    geom geometry, -- Original XY/Z, EPSG:2230, US survey feet; no repair.
    geom_4326 geometry(MultiLineString,4326), -- Explicit derived 2D transform.
    PRIMARY KEY(snapshot_id,roadsegid), UNIQUE(snapshot_id,objectid),
    CHECK(geom IS NULL OR ST_SRID(geom)=2230)
);
CREATE TABLE IF NOT EXISTS citylens_ref.road_nodes (
    snapshot_id text NOT NULL REFERENCES citylens_ref.snapshots(snapshot_id),
    interid bigint NOT NULL, objectid bigint NOT NULL, globalid text, node_type text,
    attributes jsonb NOT NULL, source_geometry jsonb,
    geom geometry, geom_4326 geometry(Point,4326),
    -- INTERID is a topology link, not reliably unique in the official snapshot.
    -- Preserve duplicate and zero INTERIDs; the resolver fails closed on them.
    PRIMARY KEY(snapshot_id,objectid), UNIQUE(snapshot_id,globalid),
    CHECK(geom IS NULL OR ST_SRID(geom)=2230)
);
CREATE INDEX IF NOT EXISTS ref_road_geom_idx ON citylens_ref.road_segments USING gist(geom);
CREATE INDEX IF NOT EXISTS ref_road_wgs_idx ON citylens_ref.road_segments USING gist(geom_4326);
CREATE INDEX IF NOT EXISTS ref_node_geom_idx ON citylens_ref.road_nodes USING gist(geom);
CREATE INDEX IF NOT EXISTS ref_node_wgs_idx ON citylens_ref.road_nodes USING gist(geom_4326);
CREATE INDEX IF NOT EXISTS ref_node_interid_idx ON citylens_ref.road_nodes(snapshot_id,interid);
CREATE INDEX IF NOT EXISTS ref_road_name_idx ON citylens_ref.road_segments(snapshot_id,canonical_name);
CREATE INDEX IF NOT EXISTS ref_road_fullname_idx ON citylens_ref.road_segments(snapshot_id,upper(rd30full));
CREATE INDEX IF NOT EXISTS ref_road_fnode_idx ON citylens_ref.road_segments(snapshot_id,fnode);
CREATE INDEX IF NOT EXISTS ref_road_tnode_idx ON citylens_ref.road_segments(snapshot_id,tnode);
REVOKE ALL ON ALL TABLES IN SCHEMA citylens_ref FROM PUBLIC;
GRANT USAGE ON SCHEMA citylens_ref TO citylens_agent_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA citylens_ref TO citylens_agent_reader;
