-- Only the separately approved --apply operation may execute this migration.
-- Existing matched positions and their precision remain unchanged.
ALTER TABLE citylens.traffic_collisions DROP CONSTRAINT traffic_collisions_geocode_precision_check;
ALTER TABLE citylens.traffic_collisions DROP CONSTRAINT traffic_collisions_check1;
ALTER TABLE citylens.traffic_collisions ADD CONSTRAINT traffic_collisions_geocode_precision_check
 CHECK(geocode_precision IS NULL OR geocode_precision IN ('block_approximate','intersection_approximate'));
ALTER TABLE citylens.traffic_collisions ADD CONSTRAINT traffic_collisions_check1
 CHECK(location IS NULL OR (geocode_precision IS NOT NULL AND geocode_precision IN ('block_approximate','intersection_approximate')));
