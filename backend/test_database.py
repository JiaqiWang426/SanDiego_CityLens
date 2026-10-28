"""Integration checks against a disposable transaction in the configured database."""
import os
import psycopg


def main():
    """Check SQL behavior using controlled records, then roll back data changes.

    Intended for a local development database: DELETE takes locks and can
    interfere with concurrent work. Rollback restores table data, but identity
    sequence values consumed by the test are not restored by PostgreSQL.
    """
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        try:
            # Temporarily hide real records; rollback restores them after verification.
            conn.execute('DELETE FROM citylens.service_requests')
            run = conn.execute("""INSERT INTO citylens.ingestions
                (source_file,sha256,source_rows,distinct_requests)
                VALUES ('test','test',5,5) RETURNING id""").fetchone()[0]
            # Five cases isolate time start/end, parent exclusion, distance,
            # and missing location. Explicit UTC offsets avoid session timezone
            # dependencies. Only start and child should match the base query.
            conn.execute("""INSERT INTO citylens.service_requests
                (service_request_id,parent_id,requested_at,location,raw_record,ingestion_id)
                SELECT id,parent,ts::timestamptz,
                    CASE WHEN lng IS NOT NULL THEN
                        ST_SetSRID(ST_MakePoint(lng,32.8),4326)::geography END,
                    '{}'::jsonb,%s
                FROM (VALUES
                    ('test-start',NULL,'2026-01-01T00:00:00Z',-117.2),
                    ('test-child','test-start','2026-01-01T12:00:00Z',-117.2),
                    ('test-end',NULL,'2026-01-02T00:00:00Z',-117.2),
                    ('test-far',NULL,'2026-01-01T12:00:00Z',-118.2),
                    ('test-missing',NULL,'2026-01-01T12:00:00Z',NULL)
                ) AS fixture(id,parent,ts,lng)""", (run,))
            def count(exclude=False, radius=800):
                return conn.execute("""SELECT citylens.count_service_requests(
                    32.8,-117.2,%s,'2026-01-01T00:00:00Z',
                    '2026-01-02T00:00:00Z',%s)""", (radius,exclude)).fetchone()[0]
            assert count() == 2, 'Check inclusive start, exclusive end, distance, missing location'
            assert count(True) == 1, 'Check exclusion of linked submissions'
            assert count(radius=0) == 2, 'Check exact coordinate'
            # Each nested transaction creates a savepoint. An expected SQL
            # error rolls back to it so subsequent checks can still execute.
            for params in [(91,-117,800), (32,-181,800), (32,-117,-1)]:
                try:
                    with conn.transaction():
                        conn.execute("""SELECT citylens.count_service_requests(
                            %s,%s,%s,'2026-01-01T00:00:00Z',
                            '2026-01-02T00:00:00Z')""", params)
                except psycopg.errors.InvalidParameterValue:
                    pass
                else:
                    raise AssertionError('Expected invalid parameter rejection')
            print('Database integration checks passed (rolled back).')
        finally:
            # Restore the original records even when an assertion fails.
            conn.rollback()


if __name__ == '__main__':
    main()
