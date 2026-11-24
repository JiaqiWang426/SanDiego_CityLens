"""Apply the service-request classification migration, mapping sync and backfill.

One idempotent operation, used by ``db.py init``, ``db.py migrate`` and
``db.py load`` (after every upsert):

1. Run ``sql/migrations/*.sql`` in filename order (additive DDL only).
2. Validate ``category_mapping.csv`` with ``category_rules.CategoryRules`` and
   synchronize its compiled rules into ``citylens.service_category_rules``.
   A new mapping version is recorded in ``citylens.service_category_mappings``
   and becomes the single active version; older versions stay as history.
3. Call ``citylens.backfill_service_request_classification()``, which updates
   only rows whose stored classification differs from the active mapping.

Repeating the operation with an unchanged mapping changes nothing and reports
``changed_rows = 0``. Nothing is deleted from service_requests, and geocoding or
location data are never read or written.

Usage (repository root; DATABASE_URL must be a table-owner connection)::

    python -m backend.db migrate
    python -m backend.service_classification status
    # Without host credentials, render the same transaction for psql:
    python -m backend.service_classification sql | docker exec -i <db> psql -U citylens -d citylens -v ON_ERROR_STOP=1
"""
import argparse
import json
import os
from pathlib import Path

if __package__:
    from .category_rules import MAPPING, load_category_rules
else:  # pragma: no cover - script execution from backend/
    from category_rules import MAPPING, load_category_rules

MIGRATIONS = Path(__file__).resolve().parent / 'sql' / 'migrations'
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '15s'"


def migration_files():
    return sorted(MIGRATIONS.glob('*.sql'))


def sync_statements(rules, source_file):
    """Parameterized statements that make ``rules`` the single active mapping."""
    sha = rules.sha256
    rows = rules.rule_rows()
    statements = [
        ('''INSERT INTO citylens.service_category_mappings (sha256, source_file, rule_count, hierarchy)
            VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (sha256) DO NOTHING''',
         (sha, source_file, len(rows), json.dumps(rules.hierarchy()))),
        # Two statements: the single-active unique index is checked per row.
        ('UPDATE citylens.service_category_mappings SET is_active = false WHERE is_active AND sha256 <> %s', (sha,)),
        ('DELETE FROM citylens.service_category_rules WHERE mapping_sha256 <> %s', (sha,)),
    ]
    for ordinal, service, detail, main, sub in rows:
        statements.append(('''INSERT INTO citylens.service_category_rules
            (ordinal, service_name, detail, main_category, sub_category, mapping_sha256)
            VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (ordinal) DO NOTHING''',
            (ordinal, service, detail, main, sub, sha)))
    statements.append(('UPDATE citylens.service_category_mappings SET is_active = true WHERE sha256 = %s AND NOT is_active', (sha,)))
    # Fail the whole transaction if the stored rules are not exactly this mapping.
    statements.append(('''DO $$ BEGIN
        IF (SELECT count(*) FROM citylens.service_category_rules) <> %s
           OR EXISTS (SELECT 1 FROM citylens.service_category_rules WHERE mapping_sha256 <> %s)
           OR (SELECT count(*) FROM citylens.service_category_mappings WHERE is_active AND sha256 = %s) <> 1 THEN
            RAISE EXCEPTION 'Service category rules do not match the requested mapping';
        END IF; END $$''', (len(rows), sha, sha)))
    return statements


def status(conn):
    """Classification coverage, by method and mapping version (read-only)."""
    row = conn.execute('''SELECT count(*), count(*) FILTER (WHERE classification_method IS NULL),
        count(*) FILTER (WHERE classification_method = 'unmapped'),
        count(*) FILTER (WHERE classification_version IS DISTINCT FROM
            (SELECT sha256 FROM citylens.service_category_mappings WHERE is_active)),
        (SELECT sha256 FROM citylens.service_category_mappings WHERE is_active)
        FROM citylens.service_requests''').fetchone()
    methods = dict(conn.execute('''SELECT coalesce(classification_method, 'unclassified'), count(*)
        FROM citylens.service_requests GROUP BY 1 ORDER BY 1''').fetchall())
    return dict(records=row[0], unclassified=row[1], unmapped=row[2], not_current_version=row[3],
                active_mapping_sha256=row[4], methods=methods)


def apply(conn, mapping=MAPPING):
    """Run migrations, sync the mapping and backfill inside the caller's transaction."""
    rules = load_category_rules(mapping)
    conn.execute(LOCK_TIMEOUT)
    for path in migration_files():
        conn.execute(path.read_text(encoding='utf-8'))
    for statement, params in sync_statements(rules, Path(mapping).name):
        # DO blocks cannot take bind parameters; render that one as literals.
        conn.execute(render(statement, params) if statement.lstrip().startswith('DO') else statement,
                     None if statement.lstrip().startswith('DO') else params)
    changed = conn.execute('SELECT citylens.backfill_service_request_classification()').fetchone()[0]
    return dict(mapping_sha256=rules.sha256, rules=len(rules.rule_rows()), changed_rows=changed, **status(conn))


def render(statement, params):
    """Inline literals with PostgreSQL-aware escaping (psql/script output only)."""
    from psycopg import sql
    pieces = statement.split('%s')
    if len(pieces) != len(params) + 1:
        raise ValueError('Parameter count mismatch')
    return pieces[0] + ''.join(sql.Literal(value).as_string() + tail for value, tail in zip(params, pieces[1:]))


def script(mapping=MAPPING):
    """The same transaction as ``apply`` as one psql script."""
    rules = load_category_rules(mapping)
    parts = ['BEGIN;', LOCK_TIMEOUT + ';']
    parts += [path.read_text(encoding='utf-8') for path in migration_files()]
    parts += [render(statement, params) + ';' for statement, params in sync_statements(rules, Path(mapping).name)]
    parts += ['SELECT citylens.backfill_service_request_classification() AS changed_rows;', 'COMMIT;']
    return '\n'.join(parts) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['migrate', 'status', 'sql'])
    parser.add_argument('--mapping', type=Path, default=MAPPING)
    args = parser.parse_args()
    if args.command == 'sql':
        print(script(args.mapping), end='')
        return
    import psycopg
    if __package__:
        from .config import load_environment
    else:  # pragma: no cover
        from config import load_environment
    load_environment()
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
        result = apply(conn, args.mapping) if args.command == 'migrate' else status(conn)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
