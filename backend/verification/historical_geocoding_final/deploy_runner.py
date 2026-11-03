"""Run approved batches through the EXISTING CLI, verifying after each one.
Credentials are read from .env into the child environment only; never printed."""
import csv, json, os, subprocess, sys
from collections import Counter
from pathlib import Path

ROOT = Path('E:/Github Projects/SanDiego_CityLens')
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from psycopg.conninfo import make_conninfo
from backend.verification.intersection_feasibility.study import connection
from backend.sangis_topology import rollout, batch, database
from backend.sangis_topology.acquire import read, sha

REVIEW = Path('E:/review/citylens-geocoding-2026-11')
LOG = Path(sys.argv[1])
FILES = sys.argv[2:]
C = ROOT / 'backend/verification/intersection_stage_c'
F = ROOT / 'backend/verification/historical_geocoding_final'
PY = str(ROOT / '.venv/Scripts/python.exe')


def env_from_dotenv():
    values = {}
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


dot = env_from_dotenv()
MAINT = make_conninfo(dot['AGENT_DATABASE_URL'], user='citylens', password=dot['CITYLENS_DB_PASSWORD'])
SNAP = rollout.load_snapshot(F / 'before-row-digests.json.gz', read(F / 'validation-manifest.json')['files']['before-row-digests.json.gz'])
ORIGINAL_MATCHED = None
STAGE_C_IDS = {r['report_id'] for r in csv.DictReader(open(C / 'deployment_allowlist.csv', encoding='utf-8'))}
FINAL = {r['report_id']: r for r in csv.DictReader(open(F / 'deployment_allowlist.csv', encoding='utf-8'))}


def cli(*args, maintenance=False):
    env = dict(os.environ)
    env.pop('SANGIS_MAINTENANCE_DATABASE_URL', None)
    if maintenance:
        env['SANGIS_MAINTENANCE_DATABASE_URL'] = MAINT
    p = subprocess.run([PY, '-m', 'backend.sangis_topology', *args], capture_output=True, text=True, env=env, cwd=ROOT)
    last = (p.stdout.strip().splitlines() or [''])[-1]
    try:
        out = json.loads(last)
    except ValueError:
        out = {'error': (p.stderr.strip().splitlines() or ['no output'])[-1][:300]}
    return p.returncode, out


def verify():
    with connection() as c:
        audit = batch.audit(c)
        drift = rollout.check(c, SNAP, audit)  # Raises on any unjournaled change or count mismatch.
        current = {r['report_id']: r['d'] for r in c.execute("SELECT report_id, md5(to_jsonb(t)::text) AS d FROM citylens.traffic_collisions t")}
        hist = c.execute("SELECT run_id, report_id, outcome, reason FROM citylens_ref.geocode_apply_history").fetchall()
        updated = [h['report_id'] for h in hist if h['outcome'] == 'updated']
        rejected = [dict(h) for h in hist if h['outcome'] != 'updated']
        bad = c.execute("""SELECT count(*) AS n FROM citylens.traffic_collisions WHERE report_id=ANY(%s) AND (
              location IS NULL OR NOT ST_IsValid(location::geometry) OR geocode_status<>'matched'
              OR ST_X(location::geometry) NOT BETWEEN -117.6 AND -116.8 OR ST_Y(location::geometry) NOT BETWEEN 32.5 AND 33.2
              OR geocode_precision IS NULL OR geocode_provider IS NULL
              OR NOT ((geocode_provider='SanGIS local topology' AND geocode_precision='intersection_approximate'
                       AND (geocode_details->'stage_c'->>'validation_status'='pass' OR geocode_details->'final_geocoding'->>'validation_status'='pass'))
                   OR (geocode_provider='SanGIS road centerline' AND geocode_precision='block_approximate'
                       AND geocode_details->'final_geocoding'->>'validation_status'='pass')))""", (updated,)).fetchone()['n']
        prov = c.execute("SELECT geocode_provider p, geocode_precision q, count(*) n FROM citylens.traffic_collisions WHERE location IS NOT NULL GROUP BY 1,2 ORDER BY 1,2").fetchall()
        guard = database.reference_reader_guard(c)
        writes = c.execute("SELECT has_table_privilege(current_user,'citylens.traffic_collisions','UPDATE') OR has_table_privilege(current_user,'citylens.traffic_collisions','INSERT') OR has_table_privilege(current_user,'citylens.traffic_collisions','DELETE') AS w").fetchone()['w']
    census_ids = [i for i in SNAP['rows'] if i not in set(updated)]
    census_unchanged = all(current[i] == SNAP['rows'][i] for i in census_ids)
    outside = [i for i in updated if i not in STAGE_C_IDS and i not in FINAL]
    checks = {'rollout_drift_free': True, 'records': audit['records'], 'statuses': audit['statuses'], 'located': audit['located'],
              'source_fields_unchanged': audit['source_fields'] == SNAP['collision_audit']['source_fields'],
              'journaled_updates': len(updated), 'duplicate_journal_updates': len(updated) - len(set(updated)),
              'rejected_or_other': rejected, 'invalid_geometry_or_provenance': bad, 'updated_outside_allowlists': len(outside),
              'non_updated_rows_byte_identical': census_unchanged, 'provider_precision': [dict(r) for r in prov],
              'agent_reference_permissions': guard, 'agent_collision_write': writes, 'by_original_status': drift['by_original_status']}
    ok = (checks['source_fields_unchanged'] and not checks['duplicate_journal_updates'] and not rejected and bad == 0 and not outside
          and census_unchanged and not any(guard.values()) and not writes and audit['records'] == 78494)
    return ok, checks


def drift_ids(c):
    return set(rollout.journaled_updates(c))


log = json.loads(LOG.read_text()) if LOG.exists() else []
for name in FILES:
    path = REVIEW / name
    is_final = name.startswith('final')
    n = len(read(path)['report_ids'])
    if is_final:
        allowed = set(read(path)['allowed_original_statuses'])
        assert all(FINAL[i]['original_status'] in allowed for i in read(path)['report_ids'])
    cmd = 'final-deployment' if is_final else 'deployment'
    entry = {'file': name, 'approval_sha256': sha(path), 'reports': n}
    code, pre = cli(cmd, '--approval', str(path))
    entry['preflight'] = pre
    eligible = (pre.get('counts') == {'eligible': n})
    if code or not eligible:
        entry['stopped'] = 'preflight_failed'
        log.append(entry); LOG.write_text(json.dumps(log, indent=2, default=str)); print(json.dumps(entry, default=str)); sys.exit(1)
    code, res = cli(cmd, '--approval', str(path), '--apply', '--batch-size', '50', maintenance=True)
    entry['apply'] = res
    counts = res.get('counts', {})
    try:
        ok, checks = verify()
    except Exception as exc:
        ok, checks = False, {'verification_error': f'{type(exc).__name__}: {exc}'}
    entry['verification'] = checks
    applied_ok = code == 0 and counts.get('updated') == n and counts.get('rejected') == 0
    entry['passed'] = bool(ok and applied_ok)
    log.append(entry); LOG.write_text(json.dumps(log, indent=2, default=str))
    print(json.dumps({'file': name, 'run_id': res.get('run_id'), 'counts': counts, 'passed': entry['passed'],
                      'statuses': checks.get('statuses'), 'located': checks.get('located')}, default=str), flush=True)
    if not entry['passed']:
        print('STOPPING: verification or apply failed', flush=True)
        sys.exit(1)
print('ALL_REQUESTED_BATCHES_PASSED')
