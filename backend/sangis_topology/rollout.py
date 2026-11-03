"""Per-row rollout baseline shared by the Stage C and final deployments.

A frozen md5 of every collision row (captured read-only before any deployment)
lets several approved allowlists roll out in any order: the live table may
differ from the snapshot ONLY in rows journaled as `updated` by a guarded run,
and status counts must reconcile with those journaled original statuses.
"""
from collections import Counter
import gzip
import json
from pathlib import Path

from .acquire import EvidenceError, sha


def load_snapshot(path, expected_sha256=None):
    path = Path(path)
    if expected_sha256 is not None and sha(path) != expected_sha256:
        raise EvidenceError('Rollout baseline snapshot changed after validation')
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        snapshot = json.load(stream)
    if len(snapshot['rows']) != snapshot['collision_audit']['records']:
        raise EvidenceError('Rollout baseline snapshot is incomplete')
    return snapshot


def journaled_updates(conn):
    if not conn.execute("SELECT to_regclass('citylens_ref.geocode_apply_history') AS t").fetchone()['t']:
        return {}
    return {r['report_id']: r['old_status'] for r in conn.execute(
        "SELECT report_id, old_geocoding->>'geocode_status' AS old_status FROM citylens_ref.geocode_apply_history WHERE outcome='updated'")}


def check(conn, snapshot, current_audit):
    """Raise unless every difference is a journaled guarded update."""
    rows = snapshot['rows']
    current = {r['report_id']: r['d'] for r in conn.execute("SELECT report_id, md5(to_jsonb(c)::text) AS d FROM citylens.traffic_collisions c")}
    if current.keys() != rows.keys():
        raise EvidenceError('Collision report set differs from the rollout baseline')
    updated = journaled_updates(conn)
    drift = sorted(i for i, d in current.items() if d != rows[i] and i not in updated)
    if drift:
        raise EvidenceError(f'{len(drift)} collision rows changed outside guarded deployments; stop and review')
    base = snapshot['collision_audit']
    if current_audit['records'] != base['records'] or current_audit['source_fields'] != base['source_fields']:
        raise EvidenceError('Collision records or original fields changed since the rollout baseline')
    expected = Counter(base['statuses'])
    for old in updated.values():
        expected[old] -= 1
        expected['matched'] += 1
    if current_audit['statuses'] != {k: v for k, v in expected.items() if v} or current_audit['located'] != base['located'] + len(updated):
        raise EvidenceError('Status counts are not explained by journaled guarded updates')
    unlocated = conn.execute('''SELECT count(*) AS n FROM citylens.traffic_collisions WHERE report_id=ANY(%s)
                                AND (geocode_status<>'matched' OR location IS NULL)''', (sorted(updated),)).fetchone()['n']
    if unlocated:
        raise EvidenceError('A journaled update no longer holds its coordinate')
    return {'journaled_updates': len(updated), 'by_original_status': dict(Counter(updated.values()))}
