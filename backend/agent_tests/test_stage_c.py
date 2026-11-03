"""Stage C: strict validation rules, bounded evidence capture, allowlist gate,
and an opt-in disposable PostGIS staged rollout (never the real database)."""
from copy import deepcopy
import gzip
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

import psycopg
import pytest

from backend.agent_tests.test_integration import isolated_db  # noqa: F401  (fixture)
from backend.collision_addresses import normalize_collision_address
from backend.agent_tests.test_sangis_topology import insert_pending, road_row, stage_b_approval, synthetic_stage_b
from backend.sangis_topology import apply as deployment, batch, database, stage_c, stage_c_evidence as ev
from backend.sangis_topology.acquire import EvidenceError, read, save, sha
from backend.verification.intersection_feasibility import study

X, Y = -117.15, 32.8


def line(name, points, mtfcc='S1400', wkid=4326):
    return {'layerId': 8, 'geometryType': 'esriGeometryPolyline', 'attributes': {'NAME': name, 'MTFCC': mtfcc},
            'geometry': {'paths': [points], 'spatialReference': {'wkid': wkid, 'latestWkid': wkid}}}


def crossing(a='A St', b='B St', offset=0.0):
    return {'results': [line(a, [[X - .002, Y + offset], [X + .002, Y + offset]]), line(b, [[X, Y - .002], [X, Y + .002]])]}


def ok(payload):
    return {'status': 'ok', 'reason': None, 'payload': payload, 'reference': {'sha256': 'x'}}


# ------------------------------------------------------------ independent rules

def test_scope_url_is_exactly_the_stage_a_b_identify_request():
    seen = []

    class Recorder:
        def query(self, endpoint, params, kind):
            seen.append(endpoint + '?' + urlencode(params))
            return {'results': []}
    result = {'pair_id': 'p', 'primary': 'A ST', 'cross': 'B ST', 'status': 'accepted_candidate', 'grade_separated_conflicts': 0,
              'candidates': [{'node_id': 1, 'longitude': X, 'latitude': Y}]}
    study.independent_check(result, Recorder())
    assert seen == [ev.scope_url(X, Y)]


def test_both_exact_named_roads_near_the_node_pass():
    result = ev.assess('A ST', 'B ST', X, Y, ok(crossing()))
    assert result['status'] == 'pass' and result['diagnostics']['crossing_angle_degrees'] == 90.0
    assert all(r['distance_m'] < 1 for r in result['road_checks'].values())


def test_named_road_far_from_node_is_a_material_failure():
    result = ev.assess('A ST', 'B ST', X, Y, ok(crossing(offset=.0005)))  # ~55 m north
    assert result['status'] == 'fail' and result['reason'] == 'named_road_geometry_inconsistent_with_junction'


def test_missing_name_is_unknown_not_failure_and_no_fuzzy_alias():
    for payload in (crossing(b='Saint B St'), crossing(b='Null'), crossing(b='B Ave')):
        result = ev.assess('A ST', 'B ST', X, Y, ok(payload))
        assert result['status'] == 'unknown' and result['reason'] == 'road_name_not_identified_in_independent_source'


def test_different_explicit_direction_at_node_fails_but_missing_direction_is_unknown():
    assert ev.assess('A ST', 'N B ST', X, Y, ok(crossing(b='S B St')))['reason'] == 'conflicting_directional_name_at_junction'
    assert ev.assess('A ST', 'N B ST', X, Y, ok(crossing(b='B St')))['status'] == 'unknown'


@pytest.mark.parametrize('payload,status,reason', [
    ({'results': [], 'exceededTransferLimit': True}, 'unknown', 'possibly_incomplete_response'),
    ({'results': [line('A St', [[X, Y], [X + .001, Y]])] * 1000}, 'unknown', 'possibly_incomplete_response'),
    ({'results': [line('A St', [[X, Y], [X + .001, Y]], wkid=3857)]}, 'unknown', 'unconfirmed_response_crs'),
    ({'error': {'code': 400}}, 'provider_error', 'malformed_or_error_response'),
    ({'nope': 1}, 'provider_error', 'malformed_or_error_response'),
])
def test_incomplete_or_failed_responses_never_pass(payload, status, reason):
    result = ev.assess('A ST', 'B ST', X, Y, ok(payload))
    assert (result['status'], result['reason']) == (status, reason)


def test_unavailable_evidence_passes_through_without_geographic_judgement():
    for status in ('unknown', 'provider_error'):
        result = ev.assess('A ST', 'B ST', X, Y, {'status': status, 'reason': 'r', 'payload': None, 'reference': None})
        assert result['status'] == status


# ------------------------------------------------------------ bounded capture

def fake_fetch(responses):
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item).encode()
    return fetch, calls


def store(tmp_path, responses, **options):
    fetch, calls = fake_fetch(responses)
    options.setdefault('budget', 10)
    return ev.EvidenceStore(tmp_path / 'cache', live=True, fetch=fetch, sleep=lambda s: None, **options), calls


def test_budget_exhaustion_leaves_remaining_scopes_unknown(tmp_path):
    s, calls = store(tmp_path, [crossing(), crossing()], budget=1)
    assert s.get('u1')['status'] == 'ok'
    assert s.get('u2') == {'status': 'unknown', 'reason': 'request_budget_exhausted', 'payload': None, 'reference': None}
    assert calls == ['u1']
    with pytest.raises(EvidenceError):
        ev.EvidenceStore(tmp_path / 'cache', budget=5)  # Persisted budget cannot be reset.


def test_one_bounded_retry_for_transient_errors_only(tmp_path):
    s, calls = store(tmp_path, [URLError('down'), HTTPError('u', 503, 'x', {}, None), HTTPError('u', 404, 'x', {}, None)])
    assert s.get('u1')['status'] == 'provider_error' and calls == ['u1', 'u1']
    assert s.get('u1')['status'] == 'provider_error' and len(calls) == 2  # Preserved, not retried again.
    assert s.get('u2')['reason'] == 'http_404' and calls[-1] == 'u2' and len(calls) == 3
    assert [e['status'] for e in read(tmp_path / 'cache' / 'requests.json')['entries']] == ['transient_error', 'transient_error', 'provider_error']


def test_cached_responses_replay_offline_and_detect_corruption(tmp_path):
    s, calls = store(tmp_path, [crossing()])
    first = s.get('u1')
    offline = ev.EvidenceStore(tmp_path / 'cache', budget=10, live=False)
    assert offline.get('u1')['payload'] == first['payload'] and calls == ['u1']
    assert offline.get('u2')['reason'] == 'offline_evidence_missing'
    path = tmp_path / 'cache' / first['reference']['file']
    with gzip.open(path, 'wt', encoding='utf-8') as stream:
        json.dump({'url': 'u1', 'response': crossing(b='Other St')}, stream)
    with pytest.raises(EvidenceError):
        offline.get('u1')


def test_earlier_evidence_reused_only_with_identical_url_and_checksum(tmp_path):
    prior = tmp_path / 'prior'
    (prior / 'cache').mkdir(parents=True)
    good, bad = prior / 'cache' / 'good.json.gz', prior / 'cache' / 'bad.json.gz'
    for path, url in ((good, 'u1'), (bad, 'u2')):
        with gzip.open(path, 'wt', encoding='utf-8') as stream:
            json.dump({'url': url, 'response': crossing(), 'captured_at': 't'}, stream)
    save(prior / 'ledger.json', {'attempts': [{'url': 'u1', 'status': 'ok', 'file': 'cache/good.json.gz', 'sha256': sha(good)},
                                              {'url': 'u2', 'status': 'ok', 'file': 'cache/bad.json.gz', 'sha256': '0' * 64}]})
    source = ev.LedgerSource('prior', prior, 'ledger.json')
    s = ev.EvidenceStore(tmp_path / 'cache', budget=0, live=True, reuse=[source], fetch=lambda u, t: pytest.fail('network'))
    reused = s.get('u1')
    assert reused['status'] == 'ok' and reused['reference']['origin'] == 'reused:prior' and s.live_attempts == 0
    assert s.get('u2')['reason'] == 'request_budget_exhausted'  # Checksum mismatch is not reused.
    assert source.rejected == [{'url': 'u2', 'reason': 'missing_or_checksum_mismatch'}]


# ------------------------------------------------------------ topology and provenance

def topology_inputs():
    node = {'node_id': 1, 'longitude': X, 'latitude': Y, 'primary_segment_ids': [11, 12], 'cross_segment_ids': [13, 14],
            'incident_segment_ids': [11, 12, 13, 14], 'endpoint_levels': [1], 'reasons': [], 'node_type': 'I', 'status': 'supported'}
    pair = {'diagnostics': [], 'grade_separated_conflicts': 0, 'candidates': [node]}
    replay = {'status': 'accepted_candidate', 'candidates': [deepcopy(node)], 'diagnostics': [], 'grade_separated_conflicts': 0}
    nodes = [{'interid': 1, 'objectid': 1001, 'globalid': 'g', 'node_type': 'I', 'valid': True, 'empty': False, 'other_nodes_within_1cm': 0}]
    segments = [{'roadsegid': i, 'fnode': i if i % 2 else 1, 'tnode': 1 if i % 2 else i, 'segstat': 'C', 'segclass': '5', 'f_level': 1,
                 't_level': 1, 'valid': True, 'simple': True, 'parts': 1, 'fnode_records': 1, 'tnode_records': 1,
                 'f_distance_ft': 0.0, 't_distance_ft': 0.0, 'overlaps_other_segment': False} for i in (11, 12, 13, 14)]
    return pair, node, replay, nodes, segments


def test_strict_topology_passes_an_ordinary_junction():
    result = stage_c.strict_topology(*topology_inputs())
    assert result['status'] == 'pass' and result['reasons'] == []


def mutate(kind, pair, node, replay, nodes, segments):
    if kind == 'grade_separation_evidence_in_pair':
        pair['grade_separated_conflicts'] = 1
    elif kind == 'additional_nonsupported_shared_node':
        pair['candidates'].append({'status': 'rejected'})
    elif kind == 'local_replay_differs_from_stage_b':
        replay['candidates'][0]['latitude'] += 1e-6
    elif kind == 'local_replay_not_uniquely_supported':
        replay['status'] = 'multiple_candidates'
    elif kind == 'node_identity_not_unique':
        nodes.append(dict(nodes[0], objectid=1002))
    elif kind == 'other_node_record_within_1cm':
        nodes[0]['other_nodes_within_1cm'] = 1
    elif kind == 'endpoint_alignment_exceeds_1cm':
        segments[1]['f_distance_ft'] = .05
    elif kind == 'incident_segment_overlaps_another_segment':
        segments[0]['overlaps_other_segment'] = True
    elif kind == 'incident_segment_invalid_or_nonsimple':
        segments[0]['simple'] = False
    elif kind == 'incident_segment_far_endpoint_unresolved':
        segments[0]['fnode_records'] = 2
    elif kind == 'incident_road_status_not_constructed_or_maintained':
        segments[0]['segstat'] = 'A'
    elif kind == 'incident_segment_set_mismatch':
        segments.pop()
    elif kind == 'incompatible_endpoint_levels':
        node['endpoint_levels'] = [1, 2]


@pytest.mark.parametrize('kind', ['grade_separation_evidence_in_pair', 'additional_nonsupported_shared_node', 'local_replay_differs_from_stage_b',
                                  'local_replay_not_uniquely_supported', 'node_identity_not_unique', 'other_node_record_within_1cm',
                                  'endpoint_alignment_exceeds_1cm', 'incident_segment_overlaps_another_segment',
                                  'incident_segment_invalid_or_nonsimple', 'incident_segment_far_endpoint_unresolved',
                                  'incident_road_status_not_constructed_or_maintained', 'incident_segment_set_mismatch',
                                  'incompatible_endpoint_levels'])
def test_each_topology_conflict_fails_closed(kind):
    inputs = topology_inputs()
    mutate(kind, *inputs)
    result = stage_c.strict_topology(*inputs)
    assert result['status'] == 'fail' and kind in result['reasons']


def provenance_inputs(number='0'):
    row = road_row()
    row['raw_record']['ADDRESS_NO_PRIMARY'] = number
    address = normalize_collision_address(row['raw_record'])
    row.update(address_key=address.address_key, primary_street=address.primary_street, intersecting_street=address.intersecting_street)
    fingerprint = batch.source_fingerprint(row)
    candidate = {'longitude': X, 'latitude': Y, 'primary_segment_ids': [1], 'cross_segment_ids': [2], 'road_segment_evidence': [{}],
                 'source_address_fingerprint': fingerprint, 'snapshot_id': 's', 'method_version': batch.METHOD_VERSION,
                 'precision': 'intersection_approximate', 'srid': 4326, 'distinct_valid_candidate_nodes': 1}
    record = {'source_components_consistent': True, 'source_address_fingerprint': fingerprint, 'raw_record': row['raw_record']}
    return candidate, record, row


def test_provenance_requires_preserved_pending_source_and_no_block_qualifier():
    assert stage_c.provenance(*provenance_inputs(), 's') == {'status': 'pass', 'reasons': []}
    assert 'unresolved_block_number_qualifier' in stage_c.provenance(*provenance_inputs('100'), 's')['reasons']
    candidate, record, row = provenance_inputs()
    assert 'record_no_longer_pending_without_location' in stage_c.provenance(candidate, record, dict(row, has_location=True), 's')['reasons']
    changed = deepcopy(row)
    changed['raw_record']['ADDRESS_SFX_PRIMARY'] = 'AVE'
    assert 'source_address_changed_since_stage_b' in stage_c.provenance(candidate, record, changed, 's')['reasons']
    assert 'snapshot_method_or_precision_mismatch' in stage_c.provenance(candidate, record, row, 'other')['reasons']
    assert stage_c.provenance(None, record, row, 's')['status'] == 'unknown'


def test_status_precedence_keeps_failures_and_unknowns_distinct():
    p, u, e, f = ({'status': s} for s in ('pass', 'unknown', 'provider_error', 'fail'))
    assert stage_c.combine(p, p) == 'pass' and stage_c.combine(p, u) == 'unknown'
    assert stage_c.combine(u, e) == 'provider_error' and stage_c.combine(e, f, p) == 'fail'


def test_quality_sample_is_deterministic_and_covers_every_stratum():
    pairs, reports = [], {}
    for i in range(200):
        pairs.append({'pair_id': f'p{i}', 'primary': ['A ST', 'N A ST', 'CAMINO DE LA PLAYA GRANDE'][i % 3], 'cross': 'B ST',
                      'class_groups': [['local'], ['freeway_highway_ramp'], ['major_arterial_collector']][i % 3]})
        reports[f'p{i}'] = [{'police_division': ['Central', 'Western', 'Northern', 'Eastern'][i % 4], 'occurred_at': f'{2016 + i % 11}-01-01'}]
    chosen = stage_c.quality_sample(pairs, reports, target=64)
    assert len(chosen) == 64 and len({p['pair_id'] for p in chosen}) == 64
    assert [p['pair_id'] for p in chosen] == [p['pair_id'] for p in stage_c.quality_sample(list(reversed(pairs)), reports, target=64)]
    seen = {(d, str(v)) for p in chosen for d, values in stage_c.pair_strata(p, reports[p['pair_id']]).items() for v in values}
    universe = {(d, str(v)) for p in pairs for d, values in stage_c.pair_strata(p, reports[p['pair_id']]).items() for v in values}
    assert seen == universe


# ------------------------------------------------------------ disposable rollout

def raw(number='0'):
    value = road_row()['raw_record']
    value['ADDRESS_NO_PRIMARY'] = number
    return value


def row_digest(conn, ids):
    return conn.execute("SELECT md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id)) AS d FROM citylens.traffic_collisions c WHERE report_id=ANY(%s)",
                        (ids,)).fetchone()['d']


def stage_c_approval(output, stage_dir, sid, ids, **extra):
    fields = {'validation_version': stage_c.VALIDATION_VERSION,
              'stage_c_validation_manifest_sha256': sha(stage_dir / 'validation-manifest.json'),
              'stage_c_allowlist_sha256': sha(stage_dir / 'deployment_allowlist.csv'),
              'acknowledge_intersection_approximate_precision': True}
    fields.update(extra)
    return stage_b_approval(output, sid, ids, **fields)


def test_disposable_stage_c_validation_allowlist_and_staged_rollout(isolated_db, tmp_path, monkeypatch):  # noqa: F811
    admin_url, reader_url = isolated_db
    monkeypatch.setenv('DATABASE_URL', admin_url)
    monkeypatch.setenv('AGENT_DATABASE_URL', reader_url)
    monkeypatch.setenv('SANGIS_MAINTENANCE_DATABASE_URL', admin_url)
    data, output, stage_dir, cache = tmp_path / 'data', tmp_path / 'b', tmp_path / 'c', tmp_path / 'cache'
    data.mkdir()
    reversed_raw = raw()  # Source orientation B/A differs from the canonical sorted pair A/B.
    reversed_raw.update(ADDRESS_ROAD_PRIMARY='B', ADDRESS_NAME_INTERSECTING='A')
    insert_pending(admin_url, [('c-1', raw()), ('c-2', raw()), ('c-3', raw()), ('c-q', raw('100')), ('c-nm', raw()), ('c-r', reversed_raw)])
    with psycopg.connect(admin_url) as conn:
        conn.execute("UPDATE citylens.traffic_collisions SET geocode_status='no_match' WHERE report_id='c-nm'")
    sid, _ = synthetic_stage_b(admin_url, data, output)

    fetch, calls = fake_fetch([crossing()])
    summary = stage_c.run(output, stage_dir, cache, live=True, budget=3, fetch=fetch, sources=[], sleep=lambda s: None)
    assert len(calls) == 1 and summary['collision_updates'] == 0
    assert summary['pairs'] == {'checked': 1, 'pass': 1, 'unknown': 0, 'provider_error': 0, 'fail': 0}
    assert summary['reports'] == {'checked': 5, 'pass': 4, 'unknown': 0, 'provider_error': 0, 'fail': 1}
    assert stage_c.read_allowlist(stage_dir / 'deployment_allowlist.csv').keys() == {'c-1', 'c-2', 'c-3', 'c-r'}
    assert 'provenance:unresolved_block_number_qualifier' in (stage_dir / 'excluded_candidates.csv').read_text()
    assert summary['deployment_gate']['blocking_reasons'] == ['quality_review_missing_or_stale']

    # The synthetic quality review stands in for the bounded human inspection.
    save(stage_dir / 'quality-review.json', {'quality_sample_sha256': sha(stage_dir / 'quality-sample.json'),
                                             'systematic_issue_detected': False, 'synthetic_test_fixture': True})
    assert stage_c.run(output, stage_dir, cache, budget=3, sources=[])['deployment_gate']['allowlist_ready_for_review']
    manifest = sha(stage_dir / 'validation-manifest.json')
    assert stage_c.run(output, stage_dir, cache, budget=3, sources=[]) and sha(stage_dir / 'validation-manifest.json') == manifest
    assert len(calls) == 1  # Offline replay reproduced byte-identical artifacts with no new request.

    pre = stage_c.allowlist_preflight(stage_dir)
    assert pre['eligibility'] == {'eligible': 4} and pre['non_allowlist_rows_unchanged'] and pre['collision_updates'] == 0
    template = stage_c.approval_template(stage_dir, output)
    save(tmp_path / 'template.json', template)
    with pytest.raises(EvidenceError):
        deployment.deploy(output, tmp_path / 'template.json', stage_c=stage_dir)
    for ids, extra in ((['c-q'], {}), (['c-1'], {'acknowledge_intersection_approximate_precision': False}),
                       (['c-1'], {'stage_c_allowlist_sha256': '0' * 64})):
        save(tmp_path / 'bad.json', stage_c_approval(output, stage_dir, sid, ids, **extra))
        with pytest.raises(EvidenceError):
            deployment.deploy(output, tmp_path / 'bad.json', stage_c=stage_dir)

    with psycopg.connect(admin_url, row_factory=psycopg.rows.dict_row) as conn:
        untouched = ['a1', 'a2', 'b1', 'unknown', 'c-q', 'c-nm']
        before = row_digest(conn, untouched)
        before_sources = batch.audit(conn)['source_fields']
    first = tmp_path / 'approval-1.json'
    save(first, stage_c_approval(output, stage_dir, sid, ['c-1']))
    assert deployment.deploy(output, first, stage_c=stage_dir)['counts'] == {'eligible': 1}
    assert deployment.deploy(output, first, apply=True, stage_c=stage_dir)['counts']['updated'] == 1
    with psycopg.connect(admin_url) as conn:
        assert conn.execute("SELECT count(*) FROM citylens.traffic_collisions WHERE geocode_status='pending' AND report_id IN ('c-2','c-3') AND location IS NULL").fetchone()[0] == 2

    second = tmp_path / 'approval-2.json'
    save(second, stage_c_approval(output, stage_dir, sid, ['c-2', 'c-3', 'c-r']))
    real = deployment.eligibility
    seen = []

    def interrupt(current, candidate):
        seen.append(current['report_id'])
        if len(seen) == 2:
            raise RuntimeError('synthetic interruption after one committed batch')
        return real(current, candidate)
    monkeypatch.setattr(deployment, 'eligibility', interrupt)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        deployment.deploy(output, second, apply=True, batch_size=1, stage_c=stage_dir)
    monkeypatch.setattr(deployment, 'eligibility', real)
    with psycopg.connect(admin_url) as conn:
        assert conn.execute("SELECT count(*) FROM citylens_ref.geocode_apply_history WHERE outcome='updated'").fetchone()[0] == 2
    assert deployment.deploy(output, second, apply=True, batch_size=1, stage_c=stage_dir)['counts'] == {'inserted': 0, 'updated': 3, 'rejected': 0, 'skipped': 1}
    again = deployment.deploy(output, second, apply=True, batch_size=1, stage_c=stage_dir)
    assert again['counts']['updated'] == 3 and again['invocation_counts'] == {'skipped': 3}

    with psycopg.connect(admin_url, row_factory=psycopg.rows.dict_row) as conn:
        assert row_digest(conn, untouched) == before  # Matched, no-match, excluded and other Pending rows unchanged.
        assert batch.audit(conn)['source_fields'] == before_sources
        rows = conn.execute("""SELECT report_id,geocode_status,geocode_precision,geocode_provider,ST_X(location::geometry) AS x,ST_Y(location::geometry) AS y,
                 geocode_details FROM citylens.traffic_collisions WHERE report_id IN ('c-1','c-2','c-3','c-r') ORDER BY report_id""").fetchall()
        assert len(rows) == 4
        for r in rows:
            assert (r['geocode_status'], r['geocode_precision'], r['geocode_provider']) == ('matched', 'intersection_approximate', 'SanGIS local topology')
            assert abs(r['x'] - X) < 1e-9 and abs(r['y'] - Y) < 1e-9
            details = r['geocode_details']
            assert details['stage_c']['validation_status'] == 'pass' and details['historical_position_verified'] is False
            assert details['prior_geocoding']['geocode_details'] == {'previous_attempt': {'evidence': 'keep'}}
        assert 'intersection_approximate' in conn.execute("""SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint
             WHERE conname='traffic_collisions_geocode_precision_check'""").fetchone()['d']
        # A change outside the allowlist blocks any later rollout batch.
        conn.execute("UPDATE citylens.traffic_collisions SET geocode_details='{\"edited\":true}' WHERE report_id='c-nm'")
    after = stage_c.allowlist_preflight(stage_dir)
    assert after['eligibility'] == {'not_pending_or_already_located': 4} and not after['non_allowlist_rows_unchanged']
    third = tmp_path / 'approval-3.json'
    save(third, stage_c_approval(output, stage_dir, sid, ['c-1'], historical_review_basis='third'))
    with pytest.raises(EvidenceError, match='outside the Stage C allowlist'):
        deployment.deploy(output, third, apply=True, stage_c=stage_dir)

    with batch.connection() as conn:
        assert database.reference_reader_guard(conn) == {'schema_write': False, 'table_write': False}
    with psycopg.connect(reader_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE citylens.traffic_collisions SET geocode_status='pending' WHERE report_id='c-1'")
