"""Read-only local deployment baseline; writes only sanitized audit artifacts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

from dotenv import dotenv_values
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]


def local_admin_url():
    values = dotenv_values(ROOT/'.env', encoding='utf-8-sig')
    return make_conninfo(host='127.0.0.1', port=54329, dbname='citylens', user='citylens',
                         password=values['CITYLENS_DB_PASSWORD'])


def snapshot():
    url = local_admin_url()
    with psycopg.connect(url, row_factory=dict_row, options='-c default_transaction_read_only=on') as conn:
        tables = conn.execute("""SELECT n.nspname AS schema,c.relname AS name,c.relacl::text AS acl,
            pg_get_userbyid(c.relowner) AS owner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname IN ('citylens','citylens_ref') AND c.relkind='r' AND c.relname<>'sessions'
            ORDER BY 1,2""").fetchall()
        roles = conn.execute("""SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,
            rolcanlogin,rolconfig FROM pg_roles WHERE rolname LIKE 'citylens%' ORDER BY rolname""").fetchall()
        memberships = conn.execute("""SELECT member.rolname AS member,parent.rolname AS parent FROM pg_auth_members m
            JOIN pg_roles member ON member.oid=m.member JOIN pg_roles parent ON parent.oid=m.roleid
            WHERE member.rolname LIKE 'citylens%' ORDER BY 1,2""").fetchall()
        functions = conn.execute("""SELECT p.oid::regprocedure::text AS signature,pg_get_functiondef(p.oid) AS definition,
            p.proacl::text AS acl,pg_get_userbyid(p.proowner) AS owner FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='citylens' ORDER BY 1""").fetchall()
        sessions_exist = bool(conn.execute("SELECT to_regclass('citylens.sessions') AS found").fetchone()['found'])
        sessions = conn.execute('SELECT session_id,title,created_at,updated_at FROM citylens.sessions ORDER BY session_id').fetchall() if sessions_exist else []
    def digest(table):
        with psycopg.connect(url, row_factory=dict_row,
                options='-c default_transaction_read_only=on -c statement_timeout=60000 -c lock_timeout=2000') as conn:
            found = conn.execute(sql.SQL('SELECT count(*) AS rows, sum(hashtextextended(t::text,0)::numeric)::text AS content_hash_sum FROM {}.{} t')
                                 .format(sql.Identifier(table['schema']), sql.Identifier(table['name']))).fetchone()
            return {**table, **found}
    with ThreadPoolExecutor(max_workers=3) as executor:
        checked = list(executor.map(digest, tables))
    for function in functions:
        function['definition_sha256'] = hashlib.sha256(function.pop('definition').encode()).hexdigest()
    return {'tables': checked, 'roles': roles, 'memberships': memberships, 'functions': functions,
            'session_table_existed': sessions_exist, 'sessions': sessions}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = snapshot()
    except Exception as exc:
        raise SystemExit('Read-only database audit failed: '+type(exc).__name__) from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, default=str, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'audited_tables': len(result['tables']), 'rows': sum(t['rows'] for t in result['tables']),
                      'existing_sessions': len(result['sessions']), 'artifact': str(args.output)}))
