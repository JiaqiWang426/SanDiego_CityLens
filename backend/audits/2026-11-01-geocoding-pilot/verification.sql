-- Read-only checks, usable with AGENT_DATABASE_URL. No migrations/grants/writes.
BEGIN READ ONLY;
SELECT current_user,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
FROM pg_roles WHERE rolname=current_user;
SELECT p.proname,pg_get_function_identity_arguments(p.oid) AS arguments,
    has_function_privilege(current_user,p.oid,'EXECUTE') AS effective_execute,
    EXISTS (SELECT FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE a.grantee=to_regrole('citylens_agent_reader') AND a.privilege_type='EXECUTE') AS explicit_reader_execute,
    EXISTS (SELECT FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE a.grantee=0 AND a.privilege_type='EXECUTE') AS public_execute
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='citylens' AND p.proname IN
    ('aggregate_service_requests','get_dataset_metadata','normalize_request_category')
ORDER BY p.proname;
SELECT has_table_privilege(current_user,'citylens.traffic_collisions','INSERT,UPDATE,DELETE,TRUNCATE') AS collision_writes,
    has_schema_privilege(current_user,'citylens','CREATE') AS schema_create;
SELECT geocode_status,count(*) AS records,count(location) AS located,count(geocoded_at) AS attempted
FROM citylens.traffic_collisions GROUP BY 1 ORDER BY 1;
SELECT citylens.count_service_requests(32.84,-117.25,800,
    '2026-01-01T00:00:00-08:00','2026-03-01T00:00:00-08:00',false) AS existing_count;
SELECT sum(request_id_count) AS grouped_count FROM citylens.aggregate_service_requests(
    32.84,-117.25,800,'2026-01-01T00:00:00-08:00','2026-03-01T00:00:00-08:00',false,'both');
SELECT citylens.get_dataset_metadata('service_requests');
SELECT citylens.get_dataset_metadata('traffic_collisions');
ROLLBACK;
