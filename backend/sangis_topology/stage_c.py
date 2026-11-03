"""Stage C: strict validation of every Stage B accepted pair and deployment prep.

Inputs are the hashed Stage B artifacts, the immutable local SanGIS snapshot
(read-only Agent connection) and bounded, cached TIGERweb transportation
evidence. Outputs are deterministic for the same inputs: no wall-clock value
enters a hashed artifact except the frozen plan timestamp and capture times.

Nothing here writes collision rows, constraints, journals or approvals.
"""
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path

from backend.collision_addresses import normalize_collision_address
from backend.verification.intersection_feasibility.study import connection
from .acquire import EvidenceError, digest, now, read, save, sha
from .batch import DEFAULT_OUTPUT as STAGE_B_OUTPUT, METHOD_VERSION, LocalResolver, audit, source_fingerprint
from .database import reference_reader_guard
from . import stage_c_evidence as ev

VALIDATION_VERSION = 'sangis-stage-c-strict-v1'
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STAGE_C = ROOT / 'verification' / 'intersection_stage_c'
DEFAULT_CACHE = ROOT / 'reference_data' / 'intersection_stage_c'
DEFAULT_BUDGET = 400
ALIGNMENT_M = .01
US_FOOT_M = 1200 / 3937
SEVERITY = {'pass': 0, 'unknown': 1, 'provider_error': 2, 'fail': 3}
SUPPORTED_STATUS = {'C', 'M'}
SUPPORTED_CLASS = {'1', '2', '3', '4', '5', '6', '7', '8', '9', 'A', 'M', 'Z'}
CLASS_GROUP = {'1': 'freeway_highway_ramp', '2': 'freeway_highway_ramp', '8': 'freeway_highway_ramp', '9': 'freeway_highway_ramp',
               '3': 'major_arterial_collector', '4': 'major_arterial_collector', '5': 'local', '6': 'local',
               '7': 'private_alley_military', 'A': 'private_alley_military', 'M': 'private_alley_military', 'Z': 'private_alley_military'}
POLICE_DIVISIONS = {'1': 'Northern', '2': 'Northeastern', '3': 'Eastern', '4': 'Southeastern', '5': 'Central',
                    '6': 'Western', '7': 'Southern', '8': 'Mid-City', '9': 'Northwestern'}
PRECISION = 'intersection_approximate'
QUALITY_SAMPLE_TARGET = 64

ALLOWLIST_FIELDS = ['report_id', 'source_address_fingerprint', 'normalized_primary_road', 'normalized_cross_road', 'pair_id',
                    'sangis_node_id', 'primary_segment_ids', 'cross_segment_ids', 'incident_segment_ids', 'longitude', 'latitude',
                    'precision', 'snapshot_id', 'method_version', 'validation_version', 'topology_status', 'independent_status',
                    'provenance_status', 'validation_status', 'acceptance_reasons', 'independent_evidence_sha256',
                    'independent_evidence_origin', 'independent_evidence_captured_at', 'topology_evidence_sha256', 'validated_at']
RESULT_FIELDS = ALLOWLIST_FIELDS[:-1] + ['exclusion_reasons', 'occurred_at', 'police_division', 'independent_reason',
                                         'primary_distance_m', 'cross_distance_m', 'validated_at']


# ---------------------------------------------------------------- inputs

def load_stage_b(stage_b):
    stage_b = Path(stage_b)
    run = read(stage_b / 'run-manifest.json')
    for name, checksum in run['files'].items():
        if sha(stage_b / name) != checksum:
            raise EvidenceError('Stage B artifact changed after its run manifest: ' + name)
    if not read(stage_b / 'stage-a-replay.json')['passed'] or not read(stage_b / 'rest-archive-comparison.json')['passed']:
        raise EvidenceError('Stage B Stage A replay/archive gate did not pass')
    pairs = [p for p in read(stage_b / 'pair-results.json') if p['status'] == 'accepted_candidate']
    candidates = {}
    with (stage_b / 'candidates.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            c = json.loads(line)
            if c['classification'] == 'accepted_candidate' and c['topology_outcome'] == 'supported':
                if c['report_id'] in candidates:
                    raise EvidenceError('Duplicate supported proposal for one report')
                candidates[c['report_id']] = c
    records = {r['report_id']: r for r in read(stage_b / 'input.json')['records']}
    return {'directory': stage_b, 'run': run, 'run_sha256': sha(stage_b / 'run-manifest.json'),
            'candidate_sha256': sha(stage_b / 'candidates.jsonl'), 'summary': read(stage_b / 'summary.json'),
            'pairs': sorted(pairs, key=lambda p: p['pair_id']), 'candidates': candidates, 'records': records}


def supported_node(pair):
    nodes = [c for c in pair['candidates'] if c['status'] == 'supported']
    return nodes[0] if len(nodes) == 1 else None


def default_sources(stage_b, cache):
    return [ev.LedgerSource('stage_b_independent_evidence', Path(stage_b) / 'independent-evidence', 'requests.json'),
            ev.LedgerSource('prior_interrupted_stage_c', Path(cache) / 'prior-interrupted-run', 'request-ledger.json')]


# ---------------------------------------------------------------- topology

NODE_SQL = '''SELECT n.interid,n.objectid,n.globalid,n.node_type,ST_IsValid(n.geom) AS valid,ST_IsEmpty(n.geom) AS empty,
  (SELECT count(*) FROM citylens_ref.road_nodes x WHERE x.snapshot_id=n.snapshot_id AND x.objectid<>n.objectid
     AND ST_DWithin(x.geom,n.geom,%s)) AS other_nodes_within_1cm
  FROM citylens_ref.road_nodes n WHERE n.snapshot_id=%s AND n.interid=ANY(%s) ORDER BY n.interid,n.objectid'''

SEGMENT_SQL = '''SELECT s.roadsegid,s.fnode,s.tnode,s.segstat,s.segclass,s.f_level,s.t_level,
  ST_IsValid(s.geom) AS valid,ST_IsSimple(s.geom) AS simple,ST_NumGeometries(s.geom) AS parts,
  (SELECT count(*) FROM citylens_ref.road_nodes x WHERE x.snapshot_id=s.snapshot_id AND x.interid=s.fnode) AS fnode_records,
  (SELECT count(*) FROM citylens_ref.road_nodes x WHERE x.snapshot_id=s.snapshot_id AND x.interid=s.tnode) AS tnode_records,
  (SELECT min(ST_Distance(ST_Force2D(ST_StartPoint(ST_GeometryN(s.geom,1))),ST_Force2D(x.geom)))
     FROM citylens_ref.road_nodes x WHERE x.snapshot_id=s.snapshot_id AND x.interid=s.fnode) AS f_distance_ft,
  (SELECT min(ST_Distance(ST_Force2D(ST_EndPoint(ST_GeometryN(s.geom,ST_NumGeometries(s.geom)))),ST_Force2D(x.geom)))
     FROM citylens_ref.road_nodes x WHERE x.snapshot_id=s.snapshot_id AND x.interid=s.tnode) AS t_distance_ft,
  EXISTS(SELECT 1 FROM citylens_ref.road_segments o WHERE o.snapshot_id=s.snapshot_id AND o.roadsegid<>s.roadsegid
     AND o.geom && s.geom AND ST_Relate(ST_Force2D(o.geom),ST_Force2D(s.geom),'1********')) AS overlaps_other_segment
  FROM citylens_ref.road_segments s WHERE s.snapshot_id=%s AND (s.fnode=ANY(%s) OR s.tnode=ANY(%s)) ORDER BY s.roadsegid'''


def network_facts(conn, sid, node_ids):
    """Direct PostGIS facts for each node and its complete incident segment set."""
    nodes, segments = defaultdict(list), defaultdict(list)
    tolerance_ft = ALIGNMENT_M / US_FOOT_M
    for row in conn.execute(NODE_SQL, (tolerance_ft, sid, node_ids)):
        nodes[row['interid']].append(row)
    wanted = set(node_ids)
    for row in conn.execute(SEGMENT_SQL, (sid, node_ids, node_ids)):
        for node in {row['fnode'], row['tnode']} & wanted:
            segments[node].append(row)
    return nodes, segments


def strict_topology(pair, stage_b_node, replay, node_rows, segment_rows):
    """Every mandatory SanGIS check. Any material defect is a `fail`."""
    reasons = []
    replay_nodes = [c for c in replay['candidates'] if c['status'] == 'supported']
    if replay['status'] != 'accepted_candidate' or len(replay_nodes) != 1:
        reasons.append('local_replay_not_uniquely_supported')
    else:
        n = replay_nodes[0]
        if (n['node_id'] != stage_b_node['node_id'] or abs(n['longitude'] - stage_b_node['longitude']) > 1e-10
                or abs(n['latitude'] - stage_b_node['latitude']) > 1e-10
                or any(n[k] != stage_b_node[k] for k in ('primary_segment_ids', 'cross_segment_ids', 'incident_segment_ids', 'endpoint_levels', 'reasons'))):
            reasons.append('local_replay_differs_from_stage_b')
    if replay.get('diagnostics') or pair['diagnostics']:
        reasons.append('pair_topology_diagnostics_present')
    if pair['grade_separated_conflicts'] or replay.get('grade_separated_conflicts'):
        reasons.append('grade_separation_evidence_in_pair')
    if len(pair['candidates']) != 1:
        reasons.append('additional_nonsupported_shared_node')
    if stage_b_node['reasons'] or stage_b_node['node_type'] != 'I':
        reasons.append('node_type_or_reasons_not_supported')
    if len(stage_b_node['endpoint_levels']) != 1:
        reasons.append('incompatible_endpoint_levels')
    if not stage_b_node['primary_segment_ids'] or not stage_b_node['cross_segment_ids']:
        reasons.append('both_roads_not_incident')
    if len(node_rows) != 1:
        reasons.append('node_identity_not_unique')
    else:
        row = node_rows[0]
        if row['interid'] <= 0 or not row['globalid'] or row['node_type'] != 'I':
            reasons.append('node_identity_or_type_invalid')
        if not row['valid'] or row['empty']:
            reasons.append('node_geometry_invalid')
        if row['other_nodes_within_1cm']:
            reasons.append('other_node_record_within_1cm')
    node = stage_b_node['node_id']
    ids = sorted(r['roadsegid'] for r in segment_rows)
    if ids != stage_b_node['incident_segment_ids'] or len(ids) < 3:
        reasons.append('incident_segment_set_mismatch')
    distances = []
    for s in segment_rows:
        if s['fnode'] == s['tnode']:
            reasons.append('self_loop_incident_segment')
        if not s['valid'] or not s['simple']:
            reasons.append('incident_segment_invalid_or_nonsimple')
        if s['parts'] != 1:
            reasons.append('incident_segment_multipart')
        if s['overlaps_other_segment']:
            reasons.append('incident_segment_overlaps_another_segment')
        if s['segstat'] not in SUPPORTED_STATUS:
            reasons.append('incident_road_status_not_constructed_or_maintained')
        if s['segclass'] not in SUPPORTED_CLASS:
            reasons.append('incident_road_class_not_supported')
        for end, other in (('f', 't'), ('t', 'f')):
            if s[end + 'node'] == node:
                value = s[end + '_distance_ft']
                distances.append(None if value is None else value * US_FOOT_M)
                if s[other + 'node'] is None or s[other + 'node'] <= 0 or s[other + 'node_records'] != 1:
                    reasons.append('incident_segment_far_endpoint_unresolved')
    if not distances or any(d is None or d > ALIGNMENT_M for d in distances):
        reasons.append('endpoint_alignment_exceeds_1cm')
    facts = {'node_records': [{k: r[k] for k in ('objectid', 'globalid', 'node_type', 'valid', 'empty', 'other_nodes_within_1cm')} for r in node_rows],
             'incident_segments': [{k: s[k] for k in ('roadsegid', 'fnode', 'tnode', 'segstat', 'segclass', 'f_level', 't_level', 'valid',
                                                      'simple', 'parts', 'fnode_records', 'tnode_records', 'overlaps_other_segment')} for s in segment_rows],
             'max_junction_endpoint_distance_m': round(max((d for d in distances if d is not None), default=0), 6),
             'replay_status': replay['status']}
    reasons = sorted(set(reasons))
    return {'status': 'fail' if reasons else 'pass', 'reasons': reasons, 'facts': facts}


# ---------------------------------------------------------------- provenance

def provenance(candidate, record, current, snapshot_id):
    reasons = []
    if candidate is None:
        return {'status': 'unknown', 'reasons': ['stage_b_candidate_evidence_missing']}
    complete = (isinstance(candidate.get('longitude'), (int, float)) and isinstance(candidate.get('latitude'), (int, float))
                and math.isfinite(candidate['longitude']) and math.isfinite(candidate['latitude'])
                and -117.6 <= candidate['longitude'] <= -116.8 and 32.5 <= candidate['latitude'] <= 33.2
                and candidate.get('primary_segment_ids') and candidate.get('cross_segment_ids') and candidate.get('road_segment_evidence')
                and isinstance(candidate.get('source_address_fingerprint'), str) and len(candidate['source_address_fingerprint']) == 64)
    if not complete:
        reasons.append('incomplete_stage_b_road_node_evidence')
    if (candidate.get('snapshot_id') != snapshot_id or candidate.get('method_version') != METHOD_VERSION
            or candidate.get('precision') != PRECISION or candidate.get('srid') != 4326 or candidate.get('distinct_valid_candidate_nodes') != 1):
        reasons.append('snapshot_method_or_precision_mismatch')
    if record is None or not record.get('source_components_consistent') or record['source_address_fingerprint'] != candidate['source_address_fingerprint']:
        reasons.append('stage_b_source_fingerprint_not_preserved')
    elif str(record['raw_record'].get('ADDRESS_NO_PRIMARY') or '').strip() not in {'', '0'}:
        reasons.append('unresolved_block_number_qualifier')
    if current is None:
        reasons.append('collision_record_missing')
    else:
        if current['geocode_status'] != 'pending' or current['has_location']:
            reasons.append('record_no_longer_pending_without_location')
        if source_fingerprint(current) != candidate['source_address_fingerprint']:
            reasons.append('source_address_changed_since_stage_b')
        normalized = normalize_collision_address(current['raw_record'])
        if (normalized.primary_street, normalized.intersecting_street, normalized.address_key) != (
                current['primary_street'], current['intersecting_street'], current['address_key']):
            reasons.append('stored_streets_disagree_with_original_components')
    return {'status': 'fail' if reasons else 'pass', 'reasons': sorted(set(reasons))}


def combine(*checks):
    return max((c['status'] for c in checks), key=SEVERITY.__getitem__)


# ---------------------------------------------------------------- outputs

def write_csv(path, fields, rows):
    temporary = Path(str(path) + '.tmp')
    with temporary.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(row.get(k), separators=(',', ':')) if isinstance(row.get(k), (list, dict)) else row.get(k) for k in fields})
    temporary.replace(path)


def read_allowlist(path):
    rows = {}
    with Path(path).open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            for key in ('primary_segment_ids', 'cross_segment_ids', 'incident_segment_ids', 'acceptance_reasons'):
                row[key] = json.loads(row[key])
            row['sangis_node_id'] = int(row['sangis_node_id'])
            row['longitude'], row['latitude'] = float(row['longitude']), float(row['latitude'])
            if row['report_id'] in rows:
                raise EvidenceError('Duplicate report in Stage C allowlist')
            rows[row['report_id']] = row
    return rows


def class_groups(segment_facts):
    return sorted({CLASS_GROUP.get(s['segclass'], 'other') for s in segment_facts})


def pair_strata(pair, reports):
    names = (pair['primary'] + ' ' + pair['cross']).split()
    return {'division': sorted({r['police_division'] for r in reports}),
            'year': sorted({r['occurred_at'][:4] for r in reports if r['occurred_at']}),
            'road_class': pair['class_groups'],
            'directional_name': [any(t in ev.DIRECTIONS for t in names)],
            'complex_name': [max(len(pair['primary'].split()), len(pair['cross'].split())) >= 4]}


def quality_sample(pairs, reports_by_pair, target=QUALITY_SAMPLE_TARGET):
    """Deterministic round robin over every observed stratum value."""
    queues = defaultdict(list)
    for pair in sorted(pairs, key=lambda p: digest({'quality': VALIDATION_VERSION, 'pair': p['pair_id']})):
        for dimension, values in pair_strata(pair, reports_by_pair[pair['pair_id']]).items():
            for value in values:
                queues[(dimension, str(value))].append(pair)
    order = sorted(queues)
    chosen, ids = [], set()
    while len(chosen) < min(target, len(pairs)):
        progressed = False
        for key in order:
            queue = queues[key]
            while queue and queue[0]['pair_id'] in ids:
                queue.pop(0)
            if queue and len(chosen) < target:
                pair = queue.pop(0)
                chosen.append(pair)
                ids.add(pair['pair_id'])
                progressed = True
        if not progressed:
            break
    return chosen


def screening_flags(pair):
    flags = []
    mtfcc = {m for r in pair['independent']['road_checks'].values() for m in r.get('mtfcc', [])}
    diagnostics = pair['independent']['diagnostics']
    if 'S1100' in mtfcc or 'freeway_highway_ramp' in pair['class_groups']:
        flags.append('limited_access_or_ramp_context')
    if 'S1630' in mtfcc:
        flags.append('tiger_ramp_feature')
    if diagnostics.get('named_lines_min_separation_m', 0) > 5:
        flags.append('tiger_named_lines_separated_over_5m')
    if diagnostics.get('crossing_angle_degrees', 90) < 20:
        flags.append('near_parallel_named_lines')
    if max(r['distance_m'] or 0 for r in pair['independent']['road_checks'].values()) > 15:
        flags.append('named_road_15_to_30m_from_node')
    return flags


def distribution(rows, key):
    table = defaultdict(Counter)
    for row in rows:
        table[key(row)][row['validation_status']] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(table.items())}


# ---------------------------------------------------------------- pipeline

def run(stage_b=STAGE_B_OUTPUT, output=DEFAULT_STAGE_C, cache=DEFAULT_CACHE, *, live=False, budget=DEFAULT_BUDGET,
        fetch=None, sources=None, sleep=None):
    output, cache = Path(output), Path(cache)
    output.mkdir(parents=True, exist_ok=True)
    b = load_stage_b(stage_b)
    sid = b['run']['snapshot_id']
    pairs = b['pairs']
    reports = {i for p in pairs for i in p['report_ids']}
    nodes = {p['pair_id']: supported_node(p) for p in pairs}
    if any(n is None or n['longitude'] is None for n in nodes.values()):
        raise EvidenceError('Accepted Stage B pair lacks exactly one supported positioned node')
    scope = {p['pair_id']: ev.scope_url(nodes[p['pair_id']]['longitude'], nodes[p['pair_id']]['latitude']) for p in pairs}
    store_options = {'budget': budget, 'live': live, 'reuse': default_sources(stage_b, cache) if sources is None else sources, 'fetch': fetch}
    if sleep:
        store_options['sleep'] = sleep
    store = ev.EvidenceStore(cache, **store_options)
    urls = sorted(set(scope.values()))

    plan_path = output / 'verification-plan.json'
    identity = {'stage_b_run_manifest_sha256': b['run_sha256'], 'stage_b_candidate_sha256': b['candidate_sha256'],
                'snapshot_id': sid, 'method_version': METHOD_VERSION, 'validation_version': VALIDATION_VERSION,
                'accepted_pairs': len(pairs), 'accepted_reports': len(reports), 'unique_geographic_scopes': len(urls),
                'new_request_budget': budget}
    if plan_path.exists():
        plan = read(plan_path)
        if {k: plan.get(k) for k in identity} != identity:
            raise EvidenceError('Stage C plan belongs to different inputs or budget; use a new output directory')
    else:
        sources_now = Counter(store.peek(u) for u in urls)
        plan = dict(identity, created_at=now(), evidence_sources_at_planning=dict(sorted(sources_now.items())),
                    expected_new_requests=sources_now.get('new_request', 0),
                    expected_worst_case_attempts=sources_now.get('new_request', 0) * ev.MAX_ATTEMPTS_PER_SCOPE,
                    maximum_attempts_per_scope=ev.MAX_ATTEMPTS_PER_SCOPE, interval_seconds=store.interval,
                    timeout_seconds=store.timeout, max_response_bytes=ev.MAX_BYTES,
                    deduplication='One TIGERweb identify per distinct proposed node position (exact Stage A/B URL); pairs sharing a node share evidence.',
                    budget_policy='Budget counts live attempts across restarts. When exhausted, remaining scopes stay unknown, never failed.',
                    limit_policy='Small 0.004-degree WGS84 envelope, road layers 2/6/8. Transfer flags or >=1000 results are incomplete (unknown). No service-wide download, no pagination workaround.',
                    thirty_meter_policy='Acceptance requires BOTH exact canonical named-road TIGER lines within 30 m of the SanGIS node. Inherited diagnostic: it corroborates names near the node; it does not replace SanGIS topology, test grade separation or measure collision-location error.')
        save(plan_path, plan)
    if plan['expected_new_requests'] > budget:
        print(f"Warning: {plan['expected_new_requests']} new scopes exceed budget {budget}; the rest will stay unknown", flush=True)

    evidence = {}
    for index, url in enumerate(urls):
        evidence[url] = store.get(url)
        if index % 250 == 0 or index == len(urls) - 1:
            print(f'Independent evidence {index + 1}/{len(urls)} scopes; live attempts {store.live_attempts}/{budget}', flush=True)
    store.flush()

    with connection() as conn:
        guard = reference_reader_guard(conn)
        before = audit(conn)
        current = {r['report_id']: r for r in conn.execute(
            '''SELECT report_id,geocode_status,location IS NOT NULL AS has_location,raw_record,address_key,primary_street,intersecting_street
               FROM citylens.traffic_collisions WHERE report_id=ANY(%s)''', (sorted(reports),))}
        keys = [(p['primary'], p['cross']) for p in pairs]
        resolver = LocalResolver(conn, sid, keys)
        replays = {p['pair_id']: resolver.resolve(p['primary'], p['cross']) for p in pairs}
        node_rows, segment_rows = network_facts(conn, sid, sorted({n['node_id'] for n in nodes.values()}))

        pair_results, rows = [], []
        for p in pairs:
            node = nodes[p['pair_id']]
            topology = strict_topology(p, node, replays[p['pair_id']], node_rows.get(node['node_id'], []), segment_rows.get(node['node_id'], []))
            topology['evidence_sha256'] = digest(topology)
            reference = evidence[scope[p['pair_id']]]['reference']
            independent = ev.assess(p['primary'], p['cross'], node['longitude'], node['latitude'], evidence[scope[p['pair_id']]])
            independent['evidence'] = reference
            result = {'pair_id': p['pair_id'], 'primary': p['primary'], 'cross': p['cross'], 'node_id': node['node_id'],
                      'longitude': node['longitude'], 'latitude': node['latitude'], 'report_ids': sorted(p['report_ids']),
                      'topology': topology, 'independent': independent,
                      'class_groups': class_groups(topology['facts']['incident_segments'])}
            result['pair_status'] = combine(topology, independent)
            pair_results.append(result)
            for report_id in result['report_ids']:
                candidate, record = b['candidates'].get(report_id), b['records'].get(report_id)
                check = provenance(candidate, record, current.get(report_id), sid)
                if candidate and candidate['node_id'] != node['node_id']:
                    check = {'status': 'fail', 'reasons': sorted(set(check['reasons'] + ['candidate_node_differs_from_pair']))}
                status = combine(topology, independent, check)
                exclusions = ([f'topology:{r}' for r in topology['reasons']] +
                              ([f"independent:{independent['reason']}"] if independent['status'] != 'pass' else []) +
                              [f'provenance:{r}' for r in check['reasons']])
                beat = (record or {}).get('police_beat') or ''
                checks = independent['road_checks']
                rows.append({'report_id': report_id, 'source_address_fingerprint': (candidate or {}).get('source_address_fingerprint'),
                             'normalized_primary_road': (candidate or {}).get('normalized_primary_road'),
                             'normalized_cross_road': (candidate or {}).get('normalized_cross_road'),
                             'pair_id': p['pair_id'], 'sangis_node_id': node['node_id'],
                             'primary_segment_ids': (candidate or {}).get('primary_segment_ids'),
                             'cross_segment_ids': (candidate or {}).get('cross_segment_ids'),
                             'incident_segment_ids': (candidate or {}).get('incident_segment_ids'),
                             'longitude': (candidate or {}).get('longitude'), 'latitude': (candidate or {}).get('latitude'),
                             'precision': PRECISION, 'snapshot_id': sid, 'method_version': METHOD_VERSION,
                             'validation_version': VALIDATION_VERSION, 'topology_status': topology['status'],
                             'independent_status': independent['status'], 'provenance_status': check['status'],
                             'validation_status': status,
                             'acceptance_reasons': ['sangis_unique_supported_type_i_node_strict_checks_passed',
                                                    'tiger_both_exact_named_roads_within_30m',
                                                    'source_fingerprint_and_pending_state_preserved'] if status == 'pass' else [],
                             'exclusion_reasons': exclusions,
                             'independent_reason': independent['reason'],
                             'primary_distance_m': checks.get(p['primary'], {}).get('distance_m'),
                             'cross_distance_m': checks.get(p['cross'], {}).get('distance_m'),
                             'independent_evidence_sha256': (reference or {}).get('sha256'),
                             'independent_evidence_origin': (reference or {}).get('origin'),
                             'independent_evidence_captured_at': (reference or {}).get('captured_at'),
                             'topology_evidence_sha256': topology['evidence_sha256'],
                             'occurred_at': (record or {}).get('occurred_at'),
                             'police_division': POLICE_DIVISIONS.get(beat[:1], 'unknown'),
                             'validated_at': plan['created_at']})
        rows.sort(key=lambda r: r['report_id'])
        allow_ids = sorted(r['report_id'] for r in rows if r['validation_status'] == 'pass')
        before_state = {'collision_audit': before, 'agent_reference_permissions': guard,
                        'non_allowlist_rows': conn.execute(
                            "SELECT md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id)) AS digest FROM citylens.traffic_collisions c WHERE NOT(report_id=ANY(%s))",
                            (allow_ids,)).fetchone()['digest'],
                        'allowlist_rows': conn.execute(
                            "SELECT md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id)) AS digest FROM citylens.traffic_collisions c WHERE report_id=ANY(%s)",
                            (allow_ids,)).fetchone()['digest'],
                        'allowlist_reports': len(allow_ids),
                        'matches_stage_b_dry_run_after_state': before == b['summary']['after']}
        if audit(conn) != before:
            raise EvidenceError('Collision data changed during Stage C validation')

    # ------------------------------------------------------------ artifacts
    by_pair = defaultdict(list)
    for row in rows:
        by_pair[row['pair_id']].append(row)
    for result in pair_results:
        result['report_statuses'] = dict(Counter(r['validation_status'] for r in by_pair[result['pair_id']]))
        result['screening_flags'] = screening_flags(result) if result['pair_status'] == 'pass' else []
    passed_pairs = [p for p in pair_results if p['pair_status'] == 'pass' and any(r['validation_status'] == 'pass' for r in by_pair[p['pair_id']])]
    sample = quality_sample(passed_pairs, by_pair)
    sample_rows = []
    for p in sample:
        strata = pair_strata(p, by_pair[p['pair_id']])
        sample_rows.append({'pair_id': p['pair_id'], 'primary': p['primary'], 'cross': p['cross'], 'node_id': p['node_id'],
                            'longitude': p['longitude'], 'latitude': p['latitude'], 'reports': len(p['report_ids']), 'strata': strata,
                            'incident_segments': [{k: s[k] for k in ('roadsegid', 'segclass', 'segstat', 'f_level', 't_level')} for s in p['topology']['facts']['incident_segments']],
                            'tiger_road_checks': p['independent']['road_checks'], 'tiger_diagnostics': p['independent']['diagnostics'],
                            'screening_flags': p['screening_flags'], 'evidence': p['independent']['evidence']})
    save(output / 'quality-sample.json', {'validation_version': VALIDATION_VERSION, 'target': QUALITY_SAMPLE_TARGET,
                                          'selection': 'Deterministic round robin over every observed value of police division, collision year, SanGIS incident road-class group, directional names and complex (>=4 token) names, among pairs passing all checks. Not a random sample; no population rate is inferred.',
                                          'pairs': sample_rows})
    review_path = output / 'quality-review.json'
    review = read(review_path) if review_path.exists() else None
    review_current = bool(review and review.get('quality_sample_sha256') == sha(output / 'quality-sample.json'))

    allow_rows = [{k: r[k] for k in ALLOWLIST_FIELDS} for r in rows if r['validation_status'] == 'pass']
    excluded = [r for r in rows if r['validation_status'] != 'pass']
    write_csv(output / 'validation_results.csv', RESULT_FIELDS, rows)
    write_csv(output / 'deployment_allowlist.csv', ALLOWLIST_FIELDS, allow_rows)
    write_csv(output / 'excluded_candidates.csv', RESULT_FIELDS, excluded)
    (output / 'deployment_allowlist.sha256').write_text(sha(output / 'deployment_allowlist.csv') + '  deployment_allowlist.csv\n', encoding='utf-8')
    save(output / 'pair-validation.json', pair_results)
    save(output / 'evidence-index.json', {'scopes': [{'url': u, 'status': evidence[u]['status'], 'reason': evidence[u]['reason'],
                                                      'reference': evidence[u]['reference']} for u in urls],
                                          'reuse_policy': 'Earlier responses reused only for identical URL plus matching original ledger SHA-256; see run-log.json for rejected reuse attempts.'})
    save(output / 'before-state.json', before_state)

    pair_status = Counter(p['pair_status'] for p in pair_results)
    report_status = Counter(r['validation_status'] for r in rows)
    reasons_pairs, reasons_reports = Counter(), Counter()
    for p in pair_results:
        for reason in set([f'topology:{r}' for r in p['topology']['reasons']] + ([f"independent:{p['independent']['reason']}"] if p['independent']['status'] != 'pass' else [])):
            reasons_pairs[reason] += 1
    for r in excluded:
        for reason in r['exclusion_reasons']:
            reasons_reports[reason] += 1
    denominator, matched = before['records'], before['matched_count']
    flags_population = Counter(f for p in passed_pairs for f in p['screening_flags'])
    flags_sample = Counter(f for p in sample for f in p['screening_flags'])
    summary = {
        'validation_version': VALIDATION_VERSION, 'snapshot_id': sid, 'method_version': METHOD_VERSION,
        'validated_at': plan['created_at'],
        'stage_b': {'run_manifest_sha256': b['run_sha256'], 'candidate_sha256': b['candidate_sha256'],
                    'accepted_pairs': len(pairs), 'accepted_reports': len(reports)},
        'pairs': {'checked': len(pair_results), **{k: pair_status.get(k, 0) for k in SEVERITY}},
        'reports': {'checked': len(rows), **{k: report_status.get(k, 0) for k in SEVERITY}},
        'check_outcomes_pairs': {'topology': dict(Counter(p['topology']['status'] for p in pair_results)),
                                 'independent': dict(Counter(p['independent']['status'] for p in pair_results))},
        'check_outcomes_reports': {'provenance': dict(Counter(r['provenance_status'] for r in rows))},
        'exclusion_reasons_pairs': dict(reasons_pairs.most_common()),
        'exclusion_reasons_reports': dict(reasons_reports.most_common()),
        'independent_evidence': {'unique_scopes': len(urls), 'scope_statuses': dict(Counter(e['status'] for e in evidence.values())),
                                 'origins': dict(Counter((e['reference'] or {}).get('origin', 'none') for e in evidence.values())),
                                 'live_attempts': store.live_attempts, 'budget': budget},
        'coverage': {'denominator': denominator, 'existing_matched': matched,
                     'existing_coverage_percent': round(100 * matched / denominator, 4),
                     'eligible_allowlist_reports': len(allow_ids),
                     'projected_matched_if_entire_allowlist_deployed': matched + len(allow_ids),
                     'projected_coverage_percent': round(100 * (matched + len(allow_ids)) / denominator, 4),
                     'projected_gain_percentage_points': round(100 * len(allow_ids) / denominator, 4),
                     'stage_b_proposal_ceiling_percent': round(100 * (matched + len(reports)) / denominator, 4),
                     'deployed_by_stage_c': 0},
        'distribution': {'by_year': distribution(rows, lambda r: (r['occurred_at'] or 'unknown')[:4]),
                         'by_police_division': distribution(rows, lambda r: r['police_division'])},
        'quality': {'sample_pairs': len(sample), 'sample_reports': sum(len(p['report_ids']) for p in sample),
                    'sample_screening_flags': dict(sorted(flags_sample.items())),
                    'population_screening_flags_passed_pairs': dict(sorted(flags_population.items())),
                    'review_recorded': review_current,
                    'systematic_issue_detected': review.get('systematic_issue_detected') if review_current else None},
        'collision_updates': 0,
        'before_state_matches_stage_b': before_state['matches_stage_b_dry_run_after_state'],
        'agent_reference_permissions': guard,
    }
    ready = (review_current and review.get('systematic_issue_detected') is False and bool(allow_ids)
             and before_state['matches_stage_b_dry_run_after_state'] and not any(guard.values()))
    summary['deployment_gate'] = {'allowlist_ready_for_review': ready,
                                  'requires_separate_explicit_approval': True,
                                  'blocking_reasons': [] if ready else [x for x, bad in [
                                      ('quality_review_missing_or_stale', not review_current),
                                      ('systematic_issue_detected', bool(review_current and review.get('systematic_issue_detected') is not False)),
                                      ('empty_allowlist', not allow_ids),
                                      ('database_changed_since_stage_b', not before_state['matches_stage_b_dry_run_after_state']),
                                      ('agent_reference_write_privilege', any(guard.values()))] if bad]}
    save(output / 'summary.json', summary)

    manifest_files = ['verification-plan.json', 'validation_results.csv', 'deployment_allowlist.csv', 'deployment_allowlist.sha256',
                      'excluded_candidates.csv', 'pair-validation.json', 'evidence-index.json', 'before-state.json',
                      'quality-sample.json', 'summary.json'] + (['quality-review.json'] if review_current else [])
    implementation = {name: sha(Path(__file__).parent / name) for name in
                      ('stage_c.py', 'stage_c_evidence.py', 'batch.py', 'apply.py', 'deploy.sql')}
    save(output / 'validation-manifest.json', {'validation_version': VALIDATION_VERSION, 'snapshot_id': sid,
                                               'method_version': METHOD_VERSION, 'files': {n: sha(output / n) for n in manifest_files}})
    save(output / 'provenance-manifest.json', {
        'validation_version': VALIDATION_VERSION,
        'stage_b': {'directory': 'backend/verification/intersection_batch', 'run_manifest_sha256': b['run_sha256'],
                    'files': b['run']['files']},
        'sangis_snapshot_id': sid,
        'independent_source': {'name': 'US Census TIGERweb Transportation MapServer identify (layers 2, 6, 8)', 'url': ev.TIGER,
                               'not_used': ['Google Geocoding/Places', 'Census address geocoder', 'LLM coordinates'],
                               'cache_directory': 'backend/reference_data/intersection_stage_c (ignored)',
                               'request_ledger_sha256': sha(cache / 'requests.json') if (cache / 'requests.json').exists() else None},
        'implementation_sha256': implementation,
        'collision_reader': 'Agent read-only connection (REPEATABLE READ READ ONLY)'})
    log = {'finished_at': now(), 'live': live, 'live_attempts_total': store.live_attempts, 'budget': budget,
           'reuse_rejections_this_invocation': {s.name: s.rejected for s in store.reuse}}
    save(output / 'run-log.json', log)
    return summary


# ---------------------------------------------------------------- deployment gate

def approval_template(output=DEFAULT_STAGE_C, stage_b=STAGE_B_OUTPUT):
    """Unsigned template. Its decision value is deliberately invalid."""
    output, stage_b = Path(output), Path(stage_b)
    summary = read(output / 'summary.json')
    template = {
        'decision': 'REVIEWER_MUST_REPLACE_WITH_approve_apply',
        'reviewer': '', 'approved_at': '',
        'candidate_sha256': sha(stage_b / 'candidates.jsonl'), 'run_manifest_sha256': sha(stage_b / 'run-manifest.json'),
        'independent_summary_sha256': sha(stage_b / 'independent-summary.json'),
        'snapshot_id': summary['snapshot_id'], 'method_version': METHOD_VERSION,
        'validation_version': VALIDATION_VERSION,
        'stage_c_validation_manifest_sha256': sha(output / 'validation-manifest.json'),
        'stage_c_allowlist_sha256': sha(output / 'deployment_allowlist.csv'),
        'report_ids': [],
        'acknowledge_temporal_uncertainty': False,
        'acknowledge_independent_validation_limitations': False,
        'acknowledge_intersection_approximate_precision': False,
        'historical_review_basis': '',
        'allow_precision_migration': False,
    }
    save(output / 'approval-template.json', template)
    return template


def validate_stage_c(stage_c, approval, proposed):
    """Bind an approval to the Stage C allowlist. Raises on any mismatch."""
    stage_c = Path(stage_c)
    manifest = read(stage_c / 'validation-manifest.json')
    for name, checksum in manifest['files'].items():
        if sha(stage_c / name) != checksum:
            raise EvidenceError('Stage C artifact changed after validation: ' + name)
    summary = read(stage_c / 'summary.json')
    if (approval.get('validation_version') != VALIDATION_VERSION or manifest['validation_version'] != VALIDATION_VERSION
            or approval.get('stage_c_validation_manifest_sha256') != sha(stage_c / 'validation-manifest.json')
            or approval.get('stage_c_allowlist_sha256') != sha(stage_c / 'deployment_allowlist.csv')
            or approval.get('acknowledge_intersection_approximate_precision') is not True
            or summary['snapshot_id'] != approval.get('snapshot_id')):
        raise EvidenceError('Approval is not bound to the current Stage C allowlist and manifest')
    gate = summary['deployment_gate']
    if not gate['allowlist_ready_for_review'] or summary['quality']['systematic_issue_detected'] is not False:
        raise EvidenceError('Stage C quality gate is not satisfied')
    allow = read_allowlist(stage_c / 'deployment_allowlist.csv')
    for report_id, c in proposed.items():
        row = allow.get(report_id)
        if (row is None or row['validation_status'] != 'pass' or row['sangis_node_id'] != c['node_id']
                or row['source_address_fingerprint'] != c['source_address_fingerprint']
                or row['longitude'] != c['longitude'] or row['latitude'] != c['latitude']
                or row['primary_segment_ids'] != c['primary_segment_ids'] or row['cross_segment_ids'] != c['cross_segment_ids']
                or row['snapshot_id'] != c['snapshot_id'] or row['precision'] != PRECISION):
            raise EvidenceError('Approved report is not an identical Stage C allowlist entry')
    return {'summary': summary, 'allowlist': allow, 'before_state': read(stage_c / 'before-state.json'),
            'manifest_sha256': sha(stage_c / 'validation-manifest.json')}


def allowlist_preflight(stage_c=DEFAULT_STAGE_C):
    """Read-only check of the current database against the whole allowlist.
    Needs no approval and writes nothing to the database."""
    from .apply import eligibility
    stage_c = Path(stage_c)
    manifest = read(stage_c / 'validation-manifest.json')
    for name, checksum in manifest['files'].items():
        if sha(stage_c / name) != checksum:
            raise EvidenceError('Stage C artifact changed after validation: ' + name)
    allow = read_allowlist(stage_c / 'deployment_allowlist.csv')
    before = read(stage_c / 'before-state.json')
    ids = sorted(allow)
    with connection() as conn:
        guard = reference_reader_guard(conn)
        current_audit = audit(conn)
        rows = {r['report_id']: r for r in conn.execute(
            'SELECT *,location IS NOT NULL AS has_location FROM citylens.traffic_collisions WHERE report_id=ANY(%s)', (ids,))}
        outside = conn.execute("SELECT md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id)) AS digest FROM citylens.traffic_collisions c WHERE NOT(report_id=ANY(%s))",
                               (ids,)).fetchone()['digest']
    counts = Counter(eligibility(rows.get(i), allow[i]) or 'eligible' for i in ids)
    expected = before['collision_audit']
    result = {'mode': 'read_only_allowlist_preflight', 'collision_updates': 0, 'checked_at': now(),
              'validation_manifest_sha256': sha(stage_c / 'validation-manifest.json'),
              'allowlist_sha256': sha(stage_c / 'deployment_allowlist.csv'),
              'allowlist_reports': len(ids), 'eligibility': dict(sorted(counts.items())),
              'collision_records_unchanged': current_audit['records'] == expected['records'],
              'original_fields_unchanged': current_audit['source_fields'] == expected['source_fields'],
              'non_allowlist_rows_unchanged': outside == before['non_allowlist_rows'],
              'whole_table_unchanged_since_validation': current_audit == expected,
              'statuses': current_audit['statuses'], 'agent_reference_permissions': guard,
              'deployment_gate': read(stage_c / 'summary.json')['deployment_gate']}
    save(stage_c / 'allowlist-preflight.json', result)
    return result


def first_batch_proposal(stage_c=DEFAULT_STAGE_C):
    """Suggested first approval scope: allowlisted reports of the inspected
    quality-sample pairs. A proposal for a reviewer, never an approval."""
    stage_c = Path(stage_c)
    allow = read_allowlist(stage_c / 'deployment_allowlist.csv')
    sample = read(stage_c / 'quality-sample.json')['pairs']
    pair_ids = {p['pair_id'] for p in sample}
    ids = sorted(i for i, row in allow.items() if row['pair_id'] in pair_ids)
    proposal = {'status': 'proposal_only_not_approved', 'basis': 'All allowlisted reports at the 64 pairs inspected in the Stage C quality sample.',
                'allowlist_sha256': sha(stage_c / 'deployment_allowlist.csv'), 'pairs': len(pair_ids), 'report_ids': ids,
                'suggested_transaction_batch_size': 50}
    save(stage_c / 'first-batch-proposal.json', proposal)
    return proposal
