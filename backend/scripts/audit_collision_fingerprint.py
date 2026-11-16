"""Read-only traffic-collision preservation check.

Compares the current citylens.traffic_collisions table and protected geocoding
code against the baseline in .local/ux-collisions-before.json and
.local/ux-protected-code-before.json. Uses the project-standard digest
md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id)) (see
backend/sangis_topology/batch.py). Runs in a READ ONLY transaction; never writes.

    .\\.venv\\Scripts\\python.exe -m backend.scripts.audit_collision_fingerprint
"""
import hashlib
import json
import os
from pathlib import Path

import psycopg

from ..config import load_environment

ROOT = Path(__file__).resolve().parents[2]


def main():
    load_environment()
    out = {}
    code_path = ROOT / '.local/ux-protected-code-before.json'
    if code_path.exists():
        before_code = json.loads(code_path.read_text())
        out['protected_code_changed'] = [p for p, digest in before_code.items()
            if hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != digest]
    before_path = ROOT / '.local/ux-collisions-before.json'
    before = json.loads(before_path.read_text()) if before_path.exists() else None
    try:
        with psycopg.connect(os.environ['AGENT_DATABASE_URL'], connect_timeout=5) as conn:
            conn.read_only = True
            with conn.transaction():
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                records, digest, located = conn.execute("""SELECT count(*),
                    md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id)), count(location)
                    FROM citylens.traffic_collisions c""").fetchone()
                statuses = dict(conn.execute("""SELECT coalesce(geocode_status,'<null>'),count(*)
                    FROM citylens.traffic_collisions GROUP BY 1 ORDER BY 1""").fetchall())
        out.update(status='verified', records=records, fingerprint=digest, located=located, status_counts=statuses)
        if before:
            out.update(baseline=before, records_match=records == before['records'],
                       fingerprint_match=digest == before['fingerprint'])
    except Exception as exc:
        out.update(status='not_verified', error=type(exc).__name__)
    print(json.dumps(out, indent=2))


if __name__ == '__main__':
    main()
