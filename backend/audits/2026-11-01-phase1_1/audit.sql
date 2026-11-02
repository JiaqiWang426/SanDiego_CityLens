-- Aggregate-only inspection of existing development data; no writes.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='30s';
SELECT jsonb_build_object(
 'observed_at',now(),
 'schema',(SELECT jsonb_agg(to_jsonb(c)) FROM
    (SELECT table_name,column_name,data_type,is_nullable FROM information_schema.columns
     WHERE table_schema='citylens' ORDER BY table_name,ordinal_position) c),
 'functions',(SELECT jsonb_agg(jsonb_build_object('name',p.proname,'arguments',pg_get_function_identity_arguments(p.oid)))
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='citylens'),
 'gid',(SELECT jsonb_build_object('total',count(*),'distinct_ids',count(DISTINCT service_request_id),
    'missing_id',count(*) FILTER(WHERE nullif(btrim(service_request_id),'') IS NULL),
    'missing_service',count(*) FILTER(WHERE nullif(regexp_replace(service_name,'\s','','g'),'') IS NULL),
    'missing_status',count(*) FILTER(WHERE nullif(regexp_replace(status,'\s','','g'),'') IS NULL),
    'missing_timestamp',count(*) FILTER(WHERE requested_at IS NULL),
    'missing_location',count(*) FILTER(WHERE location IS NULL),
    'empty_location',count(*) FILTER(WHERE ST_IsEmpty(location::geometry)),
    'outside_admission_envelope',count(*) FILTER(WHERE NOT (ST_Y(location::geometry) BETWEEN 32.5 AND 33.2 AND ST_X(location::geometry) BETWEEN -117.6 AND -116.8)),
    'raw_missing_lat',count(*) FILTER(WHERE nullif(btrim(raw_record->>'lat'),'') IS NULL),
    'raw_missing_lng',count(*) FILTER(WHERE nullif(btrim(raw_record->>'lng'),'') IS NULL),
    'earliest',min(requested_at),'latest',max(requested_at),'linked_parent',count(parent_id))
    FROM citylens.service_requests),
 'service_values',(SELECT jsonb_agg(to_jsonb(t)) FROM
    (SELECT service_name,count(*) AS records FROM citylens.service_requests GROUP BY 1 ORDER BY 2 DESC,1) t),
 'status_values',(SELECT jsonb_agg(to_jsonb(t)) FROM
    (SELECT status,count(*) AS records FROM citylens.service_requests GROUP BY 1 ORDER BY 2 DESC,1) t),
 'format_variants',(SELECT jsonb_agg(to_jsonb(t)) FROM
    (SELECT 'service_name' AS field,upper(btrim(regexp_replace(service_name,'\s+',' ','g'))) AS normalized,
        array_agg(DISTINCT service_name) AS originals,count(*) AS records
     FROM citylens.service_requests GROUP BY 2 HAVING count(DISTINCT service_name)>1
     UNION ALL SELECT 'status',upper(btrim(regexp_replace(status,'\s+',' ','g'))),
        array_agg(DISTINCT status),count(*) FROM citylens.service_requests
     GROUP BY 2 HAVING count(DISTINCT status)>1) t),
 'format_changes',(SELECT jsonb_build_object('service_rows',count(*) FILTER(WHERE service_name IS DISTINCT FROM btrim(regexp_replace(service_name,'\s+',' ','g'))),
     'status_rows',count(*) FILTER(WHERE status IS DISTINCT FROM btrim(regexp_replace(status,'\s+',' ','g')))) FROM citylens.service_requests),
 'collisions',(SELECT jsonb_build_object('total',count(*),'missing_timestamp',count(*) FILTER(WHERE occurred_at IS NULL),
    'missing_injured',count(*) FILTER(WHERE injured IS NULL),'missing_killed',count(*) FILTER(WHERE killed IS NULL),
    'located',count(location),'missing_location',count(*) FILTER(WHERE location IS NULL),
    'empty_location',count(*) FILTER(WHERE ST_IsEmpty(location::geometry)),
    'pending_with_location',count(*) FILTER(WHERE geocode_status='pending' AND location IS NOT NULL),
    'matched_without_location',count(*) FILTER(WHERE geocode_status='matched' AND location IS NULL),
    'attempted',count(geocoded_at),'missing_primary_street',count(*) FILTER(WHERE primary_street=''),
    'has_intersection',count(*) FILTER(WHERE intersecting_street<>''),
    'earliest',min(occurred_at),'latest',max(occurred_at)) FROM citylens.traffic_collisions),
 'geocode_statuses',(SELECT jsonb_agg(to_jsonb(t)) FROM
    (SELECT geocode_status,geocode_precision,count(*) AS records FROM citylens.traffic_collisions GROUP BY 1,2 ORDER BY 1,2) t),
 'gid_imports',(SELECT jsonb_agg(to_jsonb(i)) FROM citylens.ingestions i),
 'collision_imports',(SELECT jsonb_agg(to_jsonb(i)) FROM citylens.collision_imports i),
 'indexes',(SELECT jsonb_agg(to_jsonb(i)) FROM pg_indexes i WHERE schemaname='citylens')
);
ROLLBACK;
