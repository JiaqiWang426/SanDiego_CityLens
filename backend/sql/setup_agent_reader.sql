-- OPTIONAL ADMIN SETUP: review and run manually after db.py init.
-- Not loaded by init, and never executed by the agent. No existing grants are
-- revoked or altered. If an existing role has extra privileges, audit it first;
-- the application fails closed on effective application write privileges.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='citylens_agent_reader') THEN
        CREATE ROLE citylens_agent_reader NOLOGIN NOSUPERUSER NOCREATEDB
            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END $$;
GRANT USAGE ON SCHEMA citylens TO citylens_agent_reader;
GRANT SELECT ON citylens.service_requests,citylens.ingestions,
    citylens.traffic_collisions,citylens.collision_imports TO citylens_agent_reader;
GRANT EXECUTE ON FUNCTION citylens.count_service_requests(
    double precision,double precision,double precision,timestamptz,timestamptz,boolean)
    TO citylens_agent_reader;
GRANT EXECUTE ON FUNCTION citylens.filter_traffic_collisions(
    timestamptz,timestamptz,double precision,double precision,double precision,
    integer,integer,integer,integer,boolean,text,text) TO citylens_agent_reader;
GRANT EXECUTE ON FUNCTION citylens.get_traffic_collisions(
    timestamptz,timestamptz,double precision,double precision,double precision,
    integer,integer,integer,integer,boolean,text,text,integer,integer) TO citylens_agent_reader;
GRANT EXECUTE ON FUNCTION citylens.aggregate_traffic_collisions(
    timestamptz,timestamptz,double precision,double precision,double precision,
    integer,integer,integer,integer,boolean,text,text,text,text) TO citylens_agent_reader;
-- The uncapped helper's EXECUTE privilege supports SECURITY INVOKER wrappers;
-- it is NOT in the agent tool registry. These functions do not elevate users.
-- Existing PostgreSQL PUBLIC grants remain inherited: review them separately
-- before deployment, especially when adding future SECURITY DEFINER routines.
-- Create a separate LOGIN using a securely entered password, then:
-- GRANT citylens_agent_reader TO your_agent_login;
-- Do not grant memberships/ownership/write rights beyond this reader group.
