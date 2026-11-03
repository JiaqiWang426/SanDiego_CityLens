"""Final historical collision geocoding: read-only candidate generation.

Phase 2  no_match  -> SanGIS road-block candidates (roadblock.py)
Phase 3  ambiguous -> same road-block method + stored Census evidence relation
Phase 4  pending   -> reuse Stage B/C outcomes; only official-abbreviation
                      intersection pairs are re-resolved with Stage C's checks
Phase 5  consolidated results, strict allowlist, manifests and per-row baseline

Never writes collisions. Deterministic for the same database, snapshot and
cached evidence (only the frozen plan timestamp and capture times appear).
"""
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path

from backend.verification.intersection_feasibility.study import connection
from backend.verification.intersection_feasibility import topology as stage_a
from .acquire import EvidenceError, digest, now, read, save, sha
from .batch import DEFAULT_OUTPUT as STAGE_B, LocalResolver, audit, source_fingerprint, METHOD_VERSION as STAGE_B_METHOD
from .database import reference_reader_guard
from . import roadblock as rb, stage_c, stage_c_evidence as ev

METHOD_VERSION = 'citylens-final-historical-v1'
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / 'verification' / 'historical_geocoding_final'
DEFAULT_CACHE = ROOT / 'reference_data' / 'historical_geocoding_final'
DEFAULT_BUDGET = 4000
CENSUS_BLOCK_CONSISTENCY_M = 100
VALIDATION_SAMPLE = 3000
ROADBLOCK_PROVIDER = 'SanGIS road centerline'
INTERSECTION_PROVIDER = 'SanGIS local topology'
SEVERITY = stage_c.SEVERITY

RESULT_FIELDS = ['report_id', 'original_status', 'original_reason', 'phase', 'outcome', 'validation_status', 'deployable',
                 'deployment_route', 'proposed_status', 'source_address_fingerprint', 'longitude', 'latitude', 'precision',
                 'provider', 'method_version', 'snapshot_id', 'street', 'block', 'segment_ids', 'node_id', 'name_sources',
                 'block_extent_m', 'uncertainty_radius_m', 'address_ranges', 'police_beat', 'beat_check', 'census_relation',
                 'census_distance_m', 'independent_status', 'independent_reason', 'independent_evidence_sha256',
                 'exclusion_reasons', 'occurred_at', 'police_division', 'validated_at']
ALLOWLIST_FIELDS = ['report_id', 'original_status', 'source_address_fingerprint', 'longitude', 'latitude', 'precision',
                    'provider', 'method_version', 'snapshot_id', 'phase', 'outcome', 'street', 'block', 'segment_ids', 'node_id',
                    'name_sources', 'block_extent_m', 'uncertainty_radius_m', 'census_relation', 'independent_evidence_sha256',
                    'validation_status', 'validated_at']


# ---------------------------------------------------------------- independent road check

def assess_road(names, longitude, latitude, evidence):
    """One road: an exact official name within 30 m of the representative point."""
    if evidence['status'] != 'ok':
        return {'status': evidence['status'], 'reason': evidence['reason'], 'distance_m': None}
    status, reason, features = ev.parse_identify(evidence['payload'])
    if status != 'ok':
        return {'status': status, 'reason': reason, 'distance_m': None}
    point = stage_a.projected([longitude, latitude])
    lines = [f for f in features if f['name'] in names]
    distance = ev._distance(point, lines)
    if distance is not None and distance <= ev.PROXIMITY_M:
        return {'status': 'pass', 'reason': 'named_road_within_30m', 'distance_m': round(distance, 3)}
    if distance is not None:
        return {'status': 'fail', 'reason': 'named_road_geometry_far_from_block', 'distance_m': round(distance, 3)}
    return {'status': 'unknown', 'reason': 'road_name_not_identified_in_independent_source', 'distance_m': None}


# ---------------------------------------------------------------- helpers

def division(beat):
    return stage_c.POLICE_DIVISIONS.get(str(beat or '')[:1], 'unknown')


def write_csv(path, fields, rows):
    stage_c.write_csv(path, fields, rows)


def row_digests(conn):
    return {r['report_id']: r['d'] for r in conn.execute(
        "SELECT report_id, md5(to_jsonb(c)::text) AS d FROM citylens.traffic_collisions c ORDER BY report_id")}


def save_gzip_json(path, value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    temporary = Path(str(path) + '.tmp')
    with open(temporary, 'wb') as raw:
        # mtime=0 keeps the compressed bytes (and hence the hash) deterministic.
        with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as stream:
            stream.write(data)
    temporary.replace(path)


def load_gzip_json(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def method_validation(conn, sid, index, matched_rows):
    """Run the unchanged road-block method on records Census already matched and
    measure how far the Census point lies from the accepted SanGIS block."""
    sample = sorted(matched_rows, key=lambda r: hashlib.sha256(('validation:' + r['report_id']).encode()).hexdigest())[:VALIDATION_SAMPLE]
    cache, rows = {}, []
    for r in sample:
        why, street, block = rb.parse_source(r['primary_street'], r['number'], r['intersecting_street'])
        if why:
            rows.append({'report_id': r['report_id'], 'status': 'invalid_source'})
            continue
        if (street, block) not in cache:
            cache[(street, block)] = rb.resolve_block(conn, sid, index, street, block)
        res = cache[(street, block)]
        item = {'report_id': r['report_id'], 'status': res['status']}
        if res['status'] == 'accepted_candidate':
            d = conn.execute('''SELECT ST_Distance(ST_Transform(ST_SetSRID(ST_MakePoint(%s,%s),4326),2230),ST_Collect(ST_Force2D(geom))) AS ft
                FROM citylens_ref.road_segments WHERE snapshot_id=%s AND roadsegid=ANY(%s)''', (r['x'], r['y'], sid, res['segment_ids'])).fetchone()['ft']
            q = res['diagnostics']
            item.update(census_to_block_m=round(d * rb.US_FOOT_M, 2), extent_m=q['extent_m'], alias='official_rd20full_abbreviation' in res['name_sources'],
                        clusters_from_parallel_components=q['node_connected_components'] > 1,
                        census_to_point_m=None)
        rows.append(item)
    accepted = [r for r in rows if r['status'] == 'accepted_candidate']

    def stats(group):
        d = sorted(r['census_to_block_m'] for r in group)
        if not d:
            return {'n': 0}
        return {'n': len(d), 'median_m': d[len(d) // 2], 'p95_m': d[int(.95 * (len(d) - 1))], 'p99_m': d[int(.99 * (len(d) - 1))],
                'over_50m': sum(x > 50 for x in d), 'over_100m': sum(x > 100 for x in d), 'over_200m': sum(x > 200 for x in d),
                'share_over_100m': round(sum(x > 100 for x in d) / len(d), 4)}
    strata = {'all_accepted': stats(accepted),
              'official_abbreviation_names': stats([r for r in accepted if r['alias']]),
              'exact_names': stats([r for r in accepted if not r['alias']]),
              'parallel_components_joined': stats([r for r in accepted if r['clusters_from_parallel_components']]),
              'single_connected_block': stats([r for r in accepted if not r['clusters_from_parallel_components']]),
              'extent_under_200m': stats([r for r in accepted if r['extent_m'] < 200]),
              'extent_200_to_400m': stats([r for r in accepted if r['extent_m'] >= 200])}
    return {'sample_size': len(sample), 'selection': 'Deterministic SHA-256 ordering of all matched Census reports.',
            'outcomes': dict(Counter(r['status'] for r in rows)), 'strata': strata, 'rows': rows,
            'interpretation': 'Distance from the existing Census block point to the SanGIS block geometry the method accepts. Both are approximate; agreement shows the method reproduces the provider block, disagreement does not show which source is wrong.'}


# ---------------------------------------------------------------- pipeline

def run(output=DEFAULT_OUTPUT, cache=DEFAULT_CACHE, *, stage_b=STAGE_B, stage_c_dir=stage_c.DEFAULT_STAGE_C, live=False,
        budget=DEFAULT_BUDGET, fetch=None, sleep=None):
    output, cache, stage_b, stage_c_dir = Path(output), Path(cache), Path(stage_b), Path(stage_c_dir)
    output.mkdir(parents=True, exist_ok=True)
    sid = read(stage_b / 'run-manifest.json')['snapshot_id']
    stage_c_before = read(stage_c_dir / 'before-state.json')
    allow_c = stage_c.read_allowlist(stage_c_dir / 'deployment_allowlist.csv')
    with (stage_c_dir / 'validation_results.csv').open(encoding='utf-8', newline='') as stream:
        import csv
        stage_c_rows = {r['report_id']: r for r in csv.DictReader(stream)}
    stage_b_class = {}
    with (stage_b / 'candidates.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            c = json.loads(line)
            stage_b_class.setdefault(c['report_id'], c['classification'])
    stage_b_pairs = {p['pair_id']: p for p in read(stage_b / 'pair-results.json')}
    stage_b_report_pair = {i: p['pair_id'] for p in stage_b_pairs.values() for i in p['report_ids']}

    plan_path = output / 'plan.json'
    plan = read(plan_path) if plan_path.exists() else None
    store_options = {'budget': budget, 'live': live, 'reuse': [], 'fetch': fetch}
    if sleep:
        store_options['sleep'] = sleep
    store = ev.EvidenceStore(cache, **store_options)

    with connection() as conn:
        guard = reference_reader_guard(conn)
        before = audit(conn)
        digests = row_digests(conn)
        index = rb.load_index(conn, sid)
        rows = conn.execute('''SELECT report_id, occurred_at, police_beat, geocode_status, primary_street, intersecting_street, address_key,
                 raw_record, raw_record->>'ADDRESS_NO_PRIMARY' AS number, geocode_details, location IS NOT NULL AS has_location,
                 ST_X(location::geometry) AS x, ST_Y(location::geometry) AS y
                 FROM citylens.traffic_collisions ORDER BY report_id''').fetchall()
        matched = [r for r in rows if r['geocode_status'] == 'matched']
        targets = [r for r in rows if r['geocode_status'] in ('no_match', 'ambiguous', 'pending')]

        # Phase 2/3 road blocks, one resolution per distinct (street, block).
        blocks = {}
        parsed = {}
        for r in targets:
            parsed[r['report_id']] = rb.parse_source(r['primary_street'], r['number'], r['intersecting_street'])
            why, street, block = parsed[r['report_id']]
            if not why and r['geocode_status'] in ('no_match', 'ambiguous') and (street, block) not in blocks:
                blocks[(street, block)] = rb.resolve_block(conn, sid, index, street, block)
        # Census point to block geometry for ambiguous evidence that carries a point.
        census_distance = {}
        for r in targets:
            res = blocks.get(parsed[r['report_id']][1:])
            point = rb.census_point(r['geocode_details']) if r['geocode_status'] == 'ambiguous' else None
            if res and res['status'] == 'accepted_candidate' and point:
                census_distance[r['report_id']] = round(conn.execute(
                    '''SELECT ST_Distance(ST_Transform(ST_SetSRID(ST_MakePoint(%s,%s),4326),2230),ST_Collect(ST_Force2D(geom))) AS ft
                       FROM citylens_ref.road_segments WHERE snapshot_id=%s AND roadsegid=ANY(%s)''',
                    (point[0], point[1], sid, res['segment_ids'])).fetchone()['ft'] * rb.US_FOOT_M, 2)

        # Phase 4: official-abbreviation re-resolution of Stage B street_not_found pairs only.
        rd30_names = set(index.by_name) - {n for n, hits in index.by_name.items() if all(how != 'exact_rd30full' for _, how in hits)}
        alias_target = {}
        for name, hits in index.by_name.items():
            targets_ = {stage_a.canonical(s['rd30full']) for s, how in hits if how == 'official_rd20full_abbreviation'}
            if name not in rd30_names and len(targets_) == 1:
                alias_target[name] = next(iter(targets_))
        alias_pairs = {}
        for pair in stage_b_pairs.values():
            if pair['status'] != 'street_not_found':
                continue
            mapped = []
            for name in (pair['primary'], pair['cross']):
                mapped.append(name if name in rd30_names else alias_target.get(name))
            if None in mapped or tuple(mapped) == (pair['primary'], pair['cross']):
                continue
            alias_pairs[pair['pair_id']] = tuple(stage_a.pair_key(*mapped))
        alias_results = {}
        if alias_pairs:
            resolver = LocalResolver(conn, sid, sorted(set(alias_pairs.values())))
            replays = {pid: resolver.resolve(*key) for pid, key in alias_pairs.items()}
            nodes = sorted({n['node_id'] for r in replays.values() if r['status'] == 'accepted_candidate'
                            for n in r['candidates'] if n['status'] == 'supported'})
            node_rows, segment_rows = stage_c.network_facts(conn, sid, nodes) if nodes else ({}, {})
            for pid, replay in replays.items():
                supported = [n for n in replay['candidates'] if n['status'] == 'supported']
                if replay['status'] != 'accepted_candidate' or len(supported) != 1:
                    alias_results[pid] = {'status': 'fail', 'outcome': 'stage_b_' + replay['status'] + '_after_official_abbreviation',
                                          'replay': replay}
                    continue
                node = supported[0]
                topology = stage_c.strict_topology(replay, node, replay, node_rows.get(node['node_id'], []), segment_rows.get(node['node_id'], []))
                alias_results[pid] = {'status': topology['status'], 'outcome': 'official_abbreviation_intersection', 'replay': replay,
                                      'node': node, 'topology': topology, 'names': alias_pairs[pid]}
        current_audit_matches_stage_c = before == stage_c_before['collision_audit']

    if plan is None:
        plan = {'created_at': now(), 'method_version': METHOD_VERSION, 'snapshot_id': sid, 'new_request_budget': budget,
                'stage_c_before_state_sha256': sha(stage_c_dir / 'before-state.json'),
                'max_block_extent_m': rb.MAX_BLOCK_EXTENT_M, 'carriageway_gap_m': rb.CARRIAGEWAY_GAP_M,
                'census_block_consistency_m': CENSUS_BLOCK_CONSISTENCY_M,
                'independent_policy': 'One TIGERweb identify per distinct representative point (exact Stage A/B/C request). Pass requires an exact official road name within 30 m. Missing names are unknown; budget exhaustion leaves scopes unknown.'}
        save(plan_path, plan)
    elif plan['new_request_budget'] != budget or plan['snapshot_id'] != sid:
        raise EvidenceError('Final geocoding plan belongs to a different budget or snapshot; use a new output directory')

    # Independent evidence for every block that could still be deployable.
    scopes = {}
    for key, res in blocks.items():
        if res['status'] == 'accepted_candidate':
            scopes[ev.scope_url(res['longitude'], res['latitude'])] = None
    for pid, a in alias_results.items():
        if a['status'] == 'pass':
            scopes[ev.scope_url(a['node']['longitude'], a['node']['latitude'])] = None
    urls = sorted(scopes)
    if 'expected_new_requests' not in plan:
        plan['expected_new_requests'] = sum(store.peek(u) == 'new_request' for u in urls)
        plan['unique_independent_scopes'] = len(urls)
        save(plan_path, plan)
    for i, url in enumerate(urls):
        scopes[url] = store.get(url)
        if i % 250 == 0 or i == len(urls) - 1:
            print(f'Independent evidence {i + 1}/{len(urls)}; live attempts {store.live_attempts}/{budget}', flush=True)
    store.flush()

    # ------------------------------------------------------------ per-report decisions
    results = []
    for r in targets:
        status, rid = r['geocode_status'], r['report_id']
        base = {'report_id': rid, 'original_status': status, 'original_reason': (r['geocode_details'] or {}).get('reason'),
                'source_address_fingerprint': source_fingerprint(r), 'snapshot_id': sid, 'method_version': METHOD_VERSION,
                'occurred_at': r['occurred_at'].isoformat() if r['occurred_at'] else None, 'police_beat': r['police_beat'],
                'police_division': division(r['police_beat']), 'validated_at': plan['created_at'], 'deployable': False,
                'deployment_route': 'none', 'proposed_status': status, 'exclusion_reasons': []}
        why, street, block = parsed[rid]
        if status == 'pending':
            results.append(pending_decision(r, base, allow_c, stage_c_rows, stage_b_class, stage_b_report_pair, alias_results, scopes, why))
            continue
        phase = 'no_match_roadblock' if status == 'no_match' else 'ambiguous_revalidation'
        base.update(phase=phase, street=street, block=block, precision='block_approximate', provider=ROADBLOCK_PROVIDER)
        if why:
            outcome = 'invalid_source' if status == 'no_match' else 'unsupported_description'
            results.append(dict(base, outcome=outcome, validation_status='fail', exclusion_reasons=['source:' + why]))
            continue
        res = blocks[(street, block)]
        q = res['diagnostics']
        base.update(segment_ids=res['segment_ids'], name_sources=res['name_sources'], block_extent_m=q.get('extent_m'),
                    uncertainty_radius_m=q.get('uncertainty_radius_m'), address_ranges=q.get('address_ranges'))
        if res['status'] != 'accepted_candidate':
            outcome = res['status'] if status == 'no_match' else rb.AMBIGUOUS_OUTCOME[res['status']]
            severity = 'unknown' if res['status'] in ('street_not_found', 'insufficient_evidence') else 'fail'
            results.append(dict(base, outcome=outcome, validation_status=severity, exclusion_reasons=['block:' + res['reason']]))
            continue
        reasons, statuses = [], []
        base.update(longitude=res['longitude'], latitude=res['latitude'])
        relation, conflict = rb.census_relation(status, r['geocode_details'], street, block, set(q['official_names']))
        base['census_relation'] = relation
        base['census_distance_m'] = census_distance.get(rid)
        if conflict:
            reasons.append('census:' + relation)
            statuses.append('fail' if conflict in ('number_conflict', 'street_conflict') else 'unknown')
        elif relation in ('census_same_street_same_block', 'census_documented_equivalent_street'):
            d = census_distance.get(rid)
            if d is None or d > CENSUS_BLOCK_CONSISTENCY_M:
                conflict = 'source_conflict'
                reasons.append('census:point_not_on_sangis_block')
                statuses.append('fail')
        beat = str(r['police_beat'] or '').strip()
        if not beat.isdigit():
            base['beat_check'] = 'missing'
            reasons.append('source:police_beat_missing')
            statuses.append('unknown')
        elif int(beat) not in q['police_beats']:
            base['beat_check'] = 'disagrees'
            reasons.append('source:police_beat_disagrees_with_block')
            statuses.append('fail')
        else:
            base['beat_check'] = 'agrees'
        if r['has_location']:
            reasons.append('state:already_located')
            statuses.append('fail')
        names = {street} | set(q['official_names'])
        evidence = scopes[ev.scope_url(res['longitude'], res['latitude'])]
        independent = assess_road(names, res['longitude'], res['latitude'], evidence)
        base.update(independent_status=independent['status'], independent_reason=independent['reason'],
                    independent_evidence_sha256=(evidence.get('reference') or {}).get('sha256'))
        if independent['status'] != 'pass':
            reasons.append('independent:' + independent['reason'])
            statuses.append(independent['status'])
        final = max(statuses, key=SEVERITY.__getitem__) if statuses else 'pass'
        if status == 'no_match':
            outcome = 'accepted_candidate' if final == 'pass' else ('source_conflict' if conflict or 'source:police_beat_disagrees_with_block' in reasons else 'insufficient_evidence')
        else:
            if final == 'pass':
                outcome = 'documented_alias_resolved' if relation == 'census_documented_equivalent_street' or 'official_rd20full_abbreviation' in res['name_sources'] else 'accepted_candidate'
            else:
                outcome = conflict if conflict in ('number_conflict', 'street_conflict') else 'insufficient_evidence'
        base.update(outcome=outcome, validation_status=final, exclusion_reasons=reasons)
        if final == 'pass':
            base.update(deployable=True, deployment_route='final_allowlist', proposed_status='matched')
        results.append(base)
    results.sort(key=lambda x: x['report_id'])

    validation = None
    with connection() as conn:
        validation = method_validation(conn, sid, index, [dict(r) for r in matched])
        if audit(conn) != before:
            raise EvidenceError('Collision data changed during final candidate generation')
    return finish(output, cache, plan, results, before, digests, guard, validation, sid, store, budget,
                  current_audit_matches_stage_c, allow_c, stage_c_dir)


def pending_decision(r, base, allow_c, stage_c_rows, stage_b_class, report_pair, alias_results, scopes, why):
    rid = r['report_id']
    base.update(phase='pending_triage')
    if rid in allow_c:
        a = allow_c[rid]
        return dict(base, outcome='stage_c_intersection_allowlist', validation_status='pass', deployment_route='stage_c_allowlist', deployable=True,
                    proposed_status='matched', precision='intersection_approximate', provider=INTERSECTION_PROVIDER,
                    longitude=a['longitude'], latitude=a['latitude'], node_id=a['sangis_node_id'], segment_ids=a['incident_segment_ids'],
                    method_version=a['validation_version'], independent_status=a['independent_status'],
                    independent_evidence_sha256=a['independent_evidence_sha256'])
    if rid in stage_c_rows:
        reasons = json.loads(stage_c_rows[rid]['exclusion_reasons'])
        return dict(base, outcome='stage_c_excluded', validation_status=stage_c_rows[rid]['validation_status'],
                    exclusion_reasons=['stage_c:' + x for x in reasons])
    if rid in stage_b_class:
        pid = report_pair.get(rid)
        alias = alias_results.get(pid)
        if alias is None:
            return dict(base, outcome='stage_b_' + stage_b_class[rid], validation_status='unknown' if stage_b_class[rid] == 'street_not_found' else 'fail',
                        exclusion_reasons=['stage_b:' + stage_b_class[rid]])
        if alias['status'] != 'pass':
            reasons = alias.get('topology', {}).get('reasons') or [alias['outcome']]
            return dict(base, outcome='official_abbreviation_intersection_rejected', validation_status='fail',
                        exclusion_reasons=['topology:' + x for x in reasons])
        node = alias['node']
        primary, cross = alias['names']
        evidence = scopes[ev.scope_url(node['longitude'], node['latitude'])]
        independent = ev.assess(primary, cross, node['longitude'], node['latitude'], evidence)
        number = str(r['raw_record'].get('ADDRESS_NO_PRIMARY') or '').strip()
        reasons = [] if independent['status'] == 'pass' else ['independent:' + independent['reason']]
        if number not in ('', '0'):
            reasons.append('source:unresolved_block_number_qualifier')
        final = 'pass' if not reasons else (independent['status'] if independent['status'] != 'pass' else 'fail')
        out = dict(base, outcome='official_abbreviation_intersection', validation_status=final, precision='intersection_approximate',
                   provider=INTERSECTION_PROVIDER, longitude=node['longitude'], latitude=node['latitude'], node_id=node['node_id'],
                   segment_ids=node['incident_segment_ids'], street=primary + ' / ' + cross, name_sources=['official_rd20full_abbreviation'],
                   independent_status=independent['status'], independent_reason=independent['reason'],
                   independent_evidence_sha256=(evidence.get('reference') or {}).get('sha256'), exclusion_reasons=reasons)
        if final == 'pass':
            out.update(deployable=True, deployment_route='final_allowlist', proposed_status='matched')
        return out
    reason = why or 'not_in_stage_b_intersection_inventory'
    return dict(base, outcome='unresolved_no_block_or_cross_street', validation_status='unknown', exclusion_reasons=['source:' + reason])


def coverage_tables(results, before):
    total, matched = before['records'], before['matched_count']
    route = Counter(r['deployment_route'] for r in results)
    stage_c_n, final_n = route.get('stage_c_allowlist', 0), route.get('final_allowlist', 0)
    return {'denominator': total, 'actual_matched': matched, 'actual_coverage_percent': round(100 * matched / total, 4),
            'stage_c_allowlist_undeployed': stage_c_n, 'final_allowlist_undeployed': final_n,
            'projected_after_stage_c': matched + stage_c_n, 'projected_after_stage_c_percent': round(100 * (matched + stage_c_n) / total, 4),
            'projected_after_all_allowlists': matched + stage_c_n + final_n,
            'projected_after_all_allowlists_percent': round(100 * (matched + stage_c_n + final_n) / total, 4),
            'deployed_by_this_milestone': 0}


def finish(output, cache, plan, results, before, digests, guard, validation, sid, store, budget, audit_matches_stage_c, allow_c, stage_c_dir):
    allow = [r for r in results if r['deployable'] and r['deployment_route'] == 'final_allowlist']
    accepted = [r for r in results if r['deployment_route'] in ('final_allowlist', 'stage_c_allowlist')]
    excluded = [r for r in results if r['deployment_route'] == 'none']
    write_csv(output / 'geocoding_results.csv', RESULT_FIELDS, results)
    write_csv(output / 'accepted_candidates.csv', RESULT_FIELDS, accepted)
    write_csv(output / 'excluded_candidates.csv', RESULT_FIELDS, excluded)
    write_csv(output / 'deployment_allowlist.csv', ALLOWLIST_FIELDS, allow)
    (output / 'deployment_allowlist.sha256').write_text(sha(output / 'deployment_allowlist.csv') + '  deployment_allowlist.csv\n', encoding='utf-8')
    save(output / 'method-validation.json', validation)
    save_gzip_json(output / 'before-row-digests.json.gz', {'collision_audit': before, 'rows': digests})
    save(output / 'before-state.json', {'collision_audit': before, 'agent_reference_permissions': guard,
                                        'row_digests_file': 'before-row-digests.json.gz', 'row_count': len(digests),
                                        'matches_stage_c_before_state': audit_matches_stage_c})

    table = defaultdict(Counter)
    for r in results:
        table[(r['original_status'], r['phase'])][r['outcome']] += 1
    by_status = {f'{a}|{b}': dict(sorted(c.items())) for (a, b), c in sorted(table.items())}
    reasons = Counter(x.split(':', 1)[0] + ':' + x.split(':', 1)[1] for r in excluded for x in r['exclusion_reasons'][:1])

    def dist(key):
        t = defaultdict(Counter)
        for r in results:
            t[key(r)][r['deployment_route']] += 1
        return {k: dict(sorted(v.items())) for k, v in sorted(t.items())}
    sample = quality_sample(results)
    save(output / 'quality-sample.json', {'method_version': METHOD_VERSION, 'pairs': sample,
                                          'selection': 'Deterministic round robin over original status, outcome, name source, extent band, police division and year; includes rejected classes.'})
    review_path = output / 'quality-review.json'
    review = read(review_path) if review_path.exists() else None
    review_current = bool(review and review.get('quality_sample_sha256') == sha(output / 'quality-sample.json'))
    summary = {'method_version': METHOD_VERSION, 'snapshot_id': sid, 'validated_at': plan['created_at'],
               'baseline_statuses': before['statuses'], 'records': before['records'],
               'evaluated': dict(Counter(r['original_status'] for r in results)),
               'outcomes_by_status_and_phase': by_status,
               'deployment_routes': dict(Counter(r['deployment_route'] for r in results)),
               'final_allowlist_by_original_status': dict(Counter(r['original_status'] for r in allow)),
               'final_allowlist_by_outcome': dict(Counter(r['outcome'] for r in allow)),
               'primary_exclusion_reasons': dict(reasons.most_common()),
               'coverage': coverage_tables(results, before),
               'distribution': {'by_year': dist(lambda r: (r['occurred_at'] or 'unknown')[:4]),
                                'by_police_division': dist(lambda r: r['police_division']),
                                'by_original_status': dist(lambda r: r['original_status'])},
               'independent_evidence': {'scopes': store and len([e for e in store.ledger['entries'] if e['status'] == 'ok']),
                                        'live_attempts': store.live_attempts, 'budget': budget},
               'method_validation': validation['strata'],
               'quality': {'sample_reports': len(sample), 'review_recorded': review_current,
                           'systematic_issue_detected': review.get('systematic_issue_detected') if review_current else None},
               'collision_updates': 0, 'agent_reference_permissions': guard,
               'before_state_matches_stage_c': audit_matches_stage_c}
    ready = review_current and review.get('systematic_issue_detected') is False and bool(allow) and not any(guard.values())
    summary['deployment_gate'] = {'allowlist_ready_for_review': ready, 'requires_separate_explicit_approval': True,
                                  'blocking_reasons': [] if ready else [x for x, bad in [
                                      ('quality_review_missing_or_stale', not review_current),
                                      ('systematic_issue_detected', bool(review_current and review.get('systematic_issue_detected') is not False)),
                                      ('empty_allowlist', not allow), ('agent_reference_write_privilege', any(guard.values()))] if bad]}
    save(output / 'summary.json', summary)
    files = ['plan.json', 'geocoding_results.csv', 'accepted_candidates.csv', 'excluded_candidates.csv', 'deployment_allowlist.csv',
             'deployment_allowlist.sha256', 'method-validation.json', 'before-state.json', 'before-row-digests.json.gz',
             'quality-sample.json', 'summary.json'] + (['quality-review.json'] if review_current else [])
    save(output / 'validation-manifest.json', {'method_version': METHOD_VERSION, 'snapshot_id': sid,
                                               'files': {n: sha(output / n) for n in files}})
    save(output / 'source-manifest.json', {
        'method_version': METHOD_VERSION, 'sangis_snapshot_id': sid,
        'stage_b_run_manifest_sha256': sha(STAGE_B / 'run-manifest.json') if (STAGE_B / 'run-manifest.json').exists() else None,
        'stage_c_validation_manifest_sha256': sha(Path(stage_c_dir) / 'validation-manifest.json'),
        'stage_c_allowlist_sha256': sha(Path(stage_c_dir) / 'deployment_allowlist.csv'),
        'independent_source': ev.TIGER, 'independent_cache': 'backend/reference_data/historical_geocoding_final (ignored)',
        'request_ledger_sha256': sha(Path(cache) / 'requests.json') if (Path(cache) / 'requests.json').exists() else None,
        'not_used': ['Google Geocoding/Places', 'Census geocoder re-requests', 'LLM coordinates', 'parcel or Address Point centroids'],
        'implementation_sha256': {n: sha(Path(__file__).parent / n) for n in ('final_geocoding.py', 'roadblock.py', 'apply.py', 'rollout.py', 'stage_c.py')
                                  if (Path(__file__).parent / n).exists()}})
    save(output / 'run-log.json', {'finished_at': now(), 'live_attempts_total': store.live_attempts, 'budget': budget})
    return summary


def quality_sample(results, per_stratum=6, target=110):
    """Accepted and rejected road-block/alias decisions across strata."""
    pool = [r for r in results if r['phase'] in ('no_match_roadblock', 'ambiguous_revalidation') and r.get('segment_ids')
            or r['outcome'] == 'official_abbreviation_intersection']
    queues = defaultdict(list)
    for r in sorted(pool, key=lambda r: hashlib.sha256(('quality:' + r['report_id']).encode()).hexdigest()):
        extent = r.get('block_extent_m') or 0
        keys = [('status_outcome', r['original_status'] + ':' + r['outcome']), ('division', r['police_division']),
                ('year', (r['occurred_at'] or '')[:4]), ('extent', '<150' if extent < 150 else '150-300' if extent < 300 else '300+'),
                ('names', ','.join(r.get('name_sources') or []))]
        for k in keys:
            queues[k].append(r)
    chosen, seen = [], set()
    while len(chosen) < target:
        progressed = False
        for key in sorted(queues):
            q = queues[key]
            while q and q[0]['report_id'] in seen:
                q.pop(0)
            if q and len(chosen) < target:
                r = q.pop(0)
                seen.add(r['report_id'])
                chosen.append({k: r.get(k) for k in RESULT_FIELDS})
                progressed = True
        if not progressed:
            break
    return chosen


def approval_template(output=DEFAULT_OUTPUT):
    """Unsigned template; the decision value is deliberately invalid."""
    output = Path(output)
    summary = read(output / 'summary.json')
    template = {'decision': 'REVIEWER_MUST_REPLACE_WITH_approve_apply', 'reviewer': '', 'approved_at': '',
                'method_version': METHOD_VERSION, 'snapshot_id': summary['snapshot_id'],
                'final_validation_manifest_sha256': sha(output / 'validation-manifest.json'),
                'final_allowlist_sha256': sha(output / 'deployment_allowlist.csv'),
                'allowed_original_statuses': [], 'report_ids': [],
                'acknowledge_temporal_uncertainty': False, 'acknowledge_independent_validation_limitations': False,
                'acknowledge_approximate_precision': False, 'historical_review_basis': '', 'allow_precision_migration': False}
    save(output / 'approval-template.json', template)
    return template


def first_batch_proposal(output=DEFAULT_OUTPUT, size=100):
    """Suggested first final batch: inspected quality-sample reports first, then a
    deterministic spread across original statuses. A proposal, never an approval."""
    import csv
    output = Path(output)
    with (output / 'deployment_allowlist.csv').open(encoding='utf-8', newline='') as stream:
        allow = {r['report_id']: r for r in csv.DictReader(stream)}
    inspected = [r['report_id'] for r in read(output / 'quality-sample.json')['pairs'] if r['report_id'] in allow]
    rest = sorted((i for i in allow if i not in inspected), key=lambda i: hashlib.sha256(('first:' + i).encode()).hexdigest())
    ids = sorted((inspected + rest)[:size])
    proposal = {'status': 'proposal_only_not_approved', 'allowlist_sha256': sha(output / 'deployment_allowlist.csv'),
                'basis': 'All allowlisted reports inspected in quality-sample.json, filled deterministically to the suggested size.',
                'inspected_reports': len(inspected), 'report_ids': ids,
                'by_original_status': dict(Counter(allow[i]['original_status'] for i in ids)), 'suggested_transaction_batch_size': 50}
    save(output / 'first-batch-proposal.json', proposal)
    return proposal


def rollout_plan(output=DEFAULT_OUTPUT, stage_c_dir=stage_c.DEFAULT_STAGE_C, size=1000):
    """Deterministic batch plan for BOTH allowlists. Each batch still needs its
    own explicit approval file; nothing here approves anything."""
    import csv
    output, stage_c_dir = Path(output), Path(stage_c_dir)
    c_ids = sorted(stage_c.read_allowlist(stage_c_dir / 'deployment_allowlist.csv'))
    c_first = read(stage_c_dir / 'first-batch-proposal.json')['report_ids']
    with (output / 'deployment_allowlist.csv').open(encoding='utf-8', newline='') as stream:
        f_rows = {r['report_id']: r for r in csv.DictReader(stream)}
    f_first = read(output / 'first-batch-proposal.json')['report_ids']

    def chunks(ids):
        n = max(1, math.ceil(len(ids) / size))
        step = math.ceil(len(ids) / n) if ids else 0
        return [ids[i:i + step] for i in range(0, len(ids), step)] if ids else []
    batches = [{'allowlist': 'stage_c', 'batch': 1, 'report_ids': c_first, 'basis': 'inspected Stage C quality sample'}]
    for i, ids in enumerate(chunks([x for x in c_ids if x not in set(c_first)]), start=2):
        batches.append({'allowlist': 'stage_c', 'batch': i, 'report_ids': ids})
    batches.append({'allowlist': 'final', 'batch': 1, 'report_ids': f_first, 'allowed_original_statuses': sorted({f_rows[i]['original_status'] for i in f_first}),
                    'basis': 'inspected final quality sample, filled deterministically'})
    rest = [x for x in sorted(f_rows) if x not in set(f_first)]
    for i, ids in enumerate(chunks(rest), start=2):
        batches.append({'allowlist': 'final', 'batch': i, 'report_ids': ids, 'allowed_original_statuses': sorted({f_rows[x]['original_status'] for x in ids})})
    for b in batches:
        b['reports'] = len(b['report_ids'])
    plan = {'status': 'proposal_only_not_approved', 'max_batch_reports': size, 'transaction_batch_size': 50,
            'stage_c_allowlist_sha256': sha(stage_c_dir / 'deployment_allowlist.csv'), 'final_allowlist_sha256': sha(output / 'deployment_allowlist.csv'),
            'rule': 'Apply one approved batch, run the post-deployment checks, then approve the next. Any drift stops the rollout.',
            'batches': batches}
    save(output / 'rollout-plan.json', plan)
    return plan
