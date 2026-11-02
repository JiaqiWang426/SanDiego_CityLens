-- PREPARED ONLY: review before running. Effective EXECUTE currently comes from PUBLIC.
-- No ownership, write, membership or PUBLIC changes.
GRANT EXECUTE ON FUNCTION citylens.aggregate_service_requests(double precision,double precision,double precision,timestamptz,timestamptz,boolean,text,text,text) TO citylens_agent_reader;
GRANT EXECUTE ON FUNCTION citylens.get_dataset_metadata(text) TO citylens_agent_reader;
GRANT EXECUTE ON FUNCTION citylens.normalize_request_category(text) TO citylens_agent_reader;
