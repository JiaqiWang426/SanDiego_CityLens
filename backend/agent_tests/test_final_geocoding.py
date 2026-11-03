"""Final historical geocoding: road-block semantics, Census evidence relations,
status-specific guarded deployment and Agent compatibility. Synthetic fixtures
only; the disposable PostGIS test never touches the development database."""
import json
from urllib.parse import parse_qs, urlparse

import psycopg
import pytest

from backend.agent_tests.test_integration import isolated_db  # noqa: F401  (fixture)
from backend.agent_tests.test_intersection_feasibility import segment
from backend.agent_tests.test_sangis_topology import insert_pending, road_row, synthetic_stage_b
from backend.agent_tests.test_stage_c import fake_fetch, line, stage_c_approval
from backend.sangis_topology import apply as deployment, final_geocoding as final, roadblock as rb, stage_c
from backend.sangis_topology.acquire import EvidenceError, read, save, sha


# ------------------------------------------------------------ source semantics

@pytest.mark.parametrize('street,number,expected', [
    ('C ST', '100', None), ('C ST', '2300', None),
    ('C ST', '2310', 'number_not_block_abstracted'),           # A house-like number is not reinterpreted.
    ('C ST', '0', 'no_positive_block_number'), ('C ST', 'X', 'no_positive_block_number'),
    ('10 FT E OF WCL OF 4600 NIMITZ BLVD', '10', 'descriptive_offset_or_compound_description'),
    ('S OF THE NCL 3800 QUARTER MILE DR', '100', 'descriptive_offset_or_compound_description'),
    ('I-5 SB OFF RAMP', '100', 'freeway_or_ramp_description'), ('SR 163', '100', 'freeway_or_ramp_description'),
    ('I ST', '100', None), ('PACIFIC HWY', '100', None), ('QUARTER MILE DR', '100', None),
])
def test_block_numbers_offsets_and_freeways(street, number, expected):
    assert rb.parse_source(street, number, '')[0] == expected


def test_cross_street_records_are_not_block_addresses():
    assert rb.parse_source('C ST', '100', 'D ST')[0] == 'cross_street_present_not_a_block_address'


def seg(i, name='C ST', lo=100, hi=198, rlo=101, rhi=199, **extra):
    base = {'roadsegid': i, 'rd30full': name, 'rd30pred': None, 'rd30name': name.rsplit(' ', 1)[0], 'rd30sfx': name.rsplit(' ', 1)[1],
            'rd30postd': None, 'rd20full': name, 'fnode': i * 10, 'tnode': i * 10 + 1, 'segstat': 'C', 'segclass': '5',
            'ljurisdic': 'SD', 'rjurisdic': 'SD', 'llowaddr': lo, 'lhighaddr': hi, 'rlowaddr': rlo, 'rhighaddr': rhi, 'l_beat': 521, 'r_beat': 521}
    base.update(extra)
    return base


def test_block_range_uses_either_side_and_ignores_parity_and_zero_ranges():
    assert rb.block_compatible(seg(1), 100) and rb.block_compatible(seg(1, lo=0, hi=0), 100)
    assert rb.block_compatible(seg(1, lo=150, hi=250, rlo=0, rhi=0), 200)   # Block 200..299 overlaps either side.
    assert not rb.block_compatible(seg(1), 200)
    assert not rb.block_compatible(seg(1, lo=0, hi=0, rlo=0, rhi=0), 100)
    assert rb.block_compatible(seg(1, lo=198, hi=100), 100)                  # Reversed range order.


def test_official_abbreviation_is_same_record_alias_and_fuzzy_names_never_match():
    index = rb.NameIndex([seg(1, name='BLACK MOUNTAIN RD', rd20full='BLACK MTN RD'),
                          seg(2, name='KEARNY AVE', rd20full='KEARNY AVE')])
    assert [how for _, how in index.lookup('BLACK MTN RD')] == ['official_rd20full_abbreviation']
    assert [how for _, how in index.lookup('BLACK MOUNTAIN RD')] == ['exact_rd30full']
    for fuzzy in ('BLACK MOUNTEN RD', 'KEARNEY AVE', 'BLACK MOUNTAIN', 'N BLACK MOUNTAIN RD', 'BLACK MOUNTAIN ST'):
        assert index.lookup(fuzzy) == []


def test_inconsistent_rd30_components_are_not_indexed():
    index = rb.NameIndex([seg(1, name='C ST', rd30name='D')])
    assert index.lookup('C ST') == []


def test_disconnected_distant_segments_stay_separate_but_carriageways_join():
    segments = {1: seg(1), 2: seg(2), 3: seg(3, fnode=11, tnode=99)}
    assert rb.components([1, 2], segments, {(1, 2): 900.0})[1] == [[1], [2]]
    assert rb.components([1, 2], segments, {(1, 2): 25.0}) == (2, [[1, 2]])      # Divided road, not node-connected.
    assert rb.components([1, 3], segments, {(1, 3): 0.0}) == (1, [[1, 3]])         # Shared endpoint ID 11.


# ------------------------------------------------------------ Census evidence

def details(reason, matched=None, point=(-117.15, 32.81)):
    d = {'reason': reason}
    if matched:
        d.update(matched_address=matched + ', SAN DIEGO, CA, 92101', raw_row=['id', 'x', 'Match', 'Non_Exact', matched, f'{point[0]},{point[1]}'])
    return d


@pytest.mark.parametrize('status,detail,relation,conflict', [
    ('no_match', {'reason': 'provider_nomatch'}, 'census_no_match', None),
    ('ambiguous', details('provider_tie'), 'census_tie_without_candidates', None),
    ('ambiguous', details('returned_number_differs', '6900 C ST'), 'census_number_in_different_block', 'number_conflict'),
    ('ambiguous', details('returned_number_differs', '150 C ST'), 'census_same_street_same_block', None),
    ('ambiguous', details('returned_street_components_differ', '100 BLACK MOUNTAIN RD'), 'census_documented_equivalent_street', None),
    ('ambiguous', details('returned_street_components_differ', '100 E C ST'), 'census_matched_different_street', 'street_conflict'),
    ('ambiguous', details('returned_street_components_differ'), 'census_evidence_incomplete', 'insufficient_evidence'),
])
def test_census_relations_never_repair_numbers_or_directions(status, detail, relation, conflict):
    street = 'BLACK MTN RD' if 'BLACK' in str(detail) else 'C ST'
    names = {'BLACK MTN RD', 'BLACK MOUNTAIN RD'} if street != 'C ST' else {'C ST'}
    assert rb.census_relation(status, detail, street, 100, names) == (relation, conflict)


def test_census_point_parsing():
    assert rb.census_point(details('x', '100 C ST')) == (-117.15, 32.81)
    assert rb.census_point({'raw_row': ['a', 'b', 'Tie']}) is None


def test_independent_road_check_requires_exact_name_near_point():
    near = {'status': 'ok', 'payload': {'results': [line('C St', [[-117.151, 32.81], [-117.149, 32.81]])]}, 'reason': None}
    assert final.assess_road({'C ST'}, -117.15, 32.81, near)['status'] == 'pass'
    assert final.assess_road({'C ST'}, -117.15, 32.8106, near)['status'] == 'fail'          # ~67 m away.
    assert final.assess_road({'D ST'}, -117.15, 32.81, near)['status'] == 'unknown'
    assert final.assess_road({'C ST'}, -117.15, 32.81, {'status': 'unknown', 'reason': 'request_budget_exhausted', 'payload': None})['status'] == 'unknown'


# ------------------------------------------------------------ disposable end-to-end

def tiger_everywhere(url, timeout):
    geometry = json.loads(parse_qs(urlparse(url).query)['geometry'][0])
    x, y = (geometry['xmin'] + geometry['xmax']) / 2, (geometry['ymin'] + geometry['ymax']) / 2
    names = ['A St', 'B St', 'C St', 'D St', 'E St']
    return json.dumps({'results': [line(n, [[x - .001, y], [x + .001, y]]) for n in names]}).encode()


def block_road(i, name, points, lo, hi, **extra):
    return segment(i, name, i * 10 + 500, i * 10 + 501, points, llowaddr=lo, lhighaddr=hi - 1, rlowaddr=lo + 1, rhighaddr=hi,
                   ljurisdic='SD', rjurisdic='SD', l_beat=521, r_beat=521, rd20full=name, **extra)


ROADS = [block_road(101, 'C ST', [[-117.151, 32.81], [-117.149, 32.81]], 100, 199),
         block_road(102, 'D ST', [[-117.171, 32.82], [-117.169, 32.82]], 200, 299),     # Two distant D ST 200 blocks.
         block_road(103, 'D ST', [[-117.131, 32.82], [-117.129, 32.82]], 200, 299),
         block_road(104, 'E ST', [[-117.16, 32.83], [-117.147, 32.83]], 300, 399)]     # ~1.2 km block extent.


def block_raw(street, number):
    name, sfx = street.split()
    return {'ADDRESS_NO_PRIMARY': number, 'ADDRESS_PD_PRIMARY': ' ', 'ADDRESS_ROAD_PRIMARY': name, 'ADDRESS_SFX_PRIMARY': sfx,
            'ADDRESS_PD_INTERSECTING': ' ', 'ADDRESS_NAME_INTERSECTING': ' ', 'ADDRESS_SFX_INTERSECTING': ' '}


def raw_intersection():
    value = road_row()['raw_record']
    value['ADDRESS_NO_PRIMARY'] = '0'
    return value


def final_approval(output, ids, statuses, **extra):
    approval = read(output / 'approval-template.json')
    approval.update(decision='approve_apply', reviewer='synthetic fixture', approved_at='2026-11-03T00:00:00-08:00',
                    allowed_original_statuses=statuses, report_ids=ids, acknowledge_temporal_uncertainty=True,
                    acknowledge_independent_validation_limitations=True, acknowledge_approximate_precision=True,
                    historical_review_basis='Synthetic fixture only')
    approval.update(extra)
    return approval


def test_disposable_final_geocoding_and_status_specific_rollout(isolated_db, tmp_path, monkeypatch):  # noqa: F811
    admin_url, reader_url = isolated_db
    for key, value in (('DATABASE_URL', admin_url), ('AGENT_DATABASE_URL', reader_url), ('SANGIS_MAINTENANCE_DATABASE_URL', admin_url)):
        monkeypatch.setenv(key, value)
    data, b, c, f, cache = (tmp_path / n for n in ('data', 'b', 'c', 'f', 'cache'))
    data.mkdir()
    insert_pending(admin_url, [('c-1', raw_intersection()), ('c-2', raw_intersection()),
                               ('nm-ok', block_raw('C ST', '100')), ('nm-multi', block_raw('D ST', '200')),
                               ('nm-long', block_raw('E ST', '300')), ('nm-house', block_raw('C ST', '150')),
                               ('am-ok', block_raw('C ST', '100')), ('am-street', block_raw('C ST', '100')),
                               ('am-number', block_raw('C ST', '100'))])
    with psycopg.connect(admin_url) as conn:
        conn.execute("UPDATE citylens.traffic_collisions SET geocode_status='no_match', geocode_details='{\"reason\":\"provider_nomatch\"}' WHERE report_id LIKE 'nm-%'")
        for rid, d in (('am-ok', details('returned_number_differs', '120 C ST', (-117.15, 32.81))),
                       ('am-street', details('returned_street_components_differ', '100 E C ST')),
                       ('am-number', details('returned_number_differs', '700 C ST'))):
            conn.execute("UPDATE citylens.traffic_collisions SET geocode_status='ambiguous', geocode_details=%s WHERE report_id=%s",
                         (psycopg.types.json.Jsonb(d), rid))
    sid, _ = synthetic_stage_b(admin_url, data, b, extra_roads=ROADS)
    fetch_c, _ = fake_fetch([json.loads(tiger_everywhere(stage_c_url(), 1))])
    stage_c.run(b, c, cache / 'c', live=True, budget=3, fetch=fetch_c, sources=[], sleep=lambda s: None)
    save(c / 'quality-review.json', {'quality_sample_sha256': sha(c / 'quality-sample.json'), 'systematic_issue_detected': False})
    assert stage_c.run(b, c, cache / 'c', budget=3, sources=[])['deployment_gate']['allowlist_ready_for_review']

    summary = final.run(f, cache / 'f', stage_b=b, stage_c_dir=c, live=True, budget=10, fetch=tiger_everywhere, sleep=lambda s: None)
    rows = {r['report_id']: r for r in __import__('csv').DictReader((f / 'geocoding_results.csv').open(encoding='utf-8'))}
    assert {k: rows[k]['outcome'] for k in ('nm-ok', 'nm-multi', 'nm-long', 'nm-house', 'am-ok', 'am-street', 'am-number')} == {
        'nm-ok': 'accepted_candidate', 'nm-multi': 'multiple_candidates', 'nm-long': 'range_ambiguous',
        'nm-house': 'invalid_source', 'am-ok': 'accepted_candidate', 'am-street': 'street_conflict', 'am-number': 'number_conflict'}
    assert rows['c-1']['deployment_route'] == 'stage_c_allowlist' and rows['nm-ok']['deployment_route'] == 'final_allowlist'
    allow = deployment.read_final_allowlist(f / 'deployment_allowlist.csv')
    assert set(allow) == {'nm-ok', 'am-ok'} and allow['nm-ok']['precision'] == 'block_approximate'
    assert summary['collision_updates'] == 0 and summary['deployment_gate']['blocking_reasons'] == ['quality_review_missing_or_stale']
    save(f / 'quality-review.json', {'quality_sample_sha256': sha(f / 'quality-sample.json'), 'systematic_issue_detected': False})
    final.run(f, cache / 'f', stage_b=b, stage_c_dir=c, budget=10)
    manifest = sha(f / 'validation-manifest.json')
    assert final.run(f, cache / 'f', stage_b=b, stage_c_dir=c, budget=10)['deployment_gate']['allowlist_ready_for_review']
    assert sha(f / 'validation-manifest.json') == manifest                       # Read-only replay is deterministic.
    final.approval_template(f)

    # Gate refusals: template, wrong status scope, excluded report, tampered allowlist.
    with pytest.raises(EvidenceError):
        deployment.deploy_final(f, f / 'approval-template.json')
    for ids, statuses, extra in ((['am-ok'], ['no_match'], {}), (['nm-multi'], ['no_match'], {}),
                                 (['nm-ok'], ['no_match'], {'final_allowlist_sha256': '0' * 64}), (['nm-ok'], ['matched'], {})):
        save(tmp_path / 'bad.json', final_approval(f, ids, statuses, **extra))
        with pytest.raises(EvidenceError):
            deployment.deploy_final(f, tmp_path / 'bad.json')

    with psycopg.connect(admin_url, row_factory=psycopg.rows.dict_row) as conn:
        untouched = ['a1', 'a2', 'b1', 'unknown', 'nm-multi', 'nm-long', 'nm-house', 'am-street', 'am-number', 'c-2']
        before = {r['report_id']: r['d'] for r in conn.execute("SELECT report_id, md5(to_jsonb(c)::text) AS d FROM citylens.traffic_collisions c WHERE report_id=ANY(%s)", (untouched,))}

    # No-match batch with an interruption, recovery and idempotent rerun.
    first = tmp_path / 'final-1.json'
    save(first, final_approval(f, ['nm-ok'], ['no_match']))
    assert deployment.deploy_final(f, first)['counts'] == {'eligible': 1}
    real = deployment.final_eligibility
    monkeypatch.setattr(deployment, 'final_eligibility', lambda current, entry: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError):
        deployment.deploy_final(f, first, apply=True)
    monkeypatch.setattr(deployment, 'final_eligibility', real)  # Never undo(): that would drop the test-DB environment.
    with psycopg.connect(admin_url) as conn:
        assert conn.execute("SELECT geocode_status FROM citylens.traffic_collisions WHERE report_id='nm-ok'").fetchone()[0] == 'no_match'
    assert deployment.deploy_final(f, first, apply=True)['counts'] == {'updated': 1, 'rejected': 0, 'skipped': 0}
    assert deployment.deploy_final(f, first, apply=True)['invocation_counts'] == {'skipped': 1}

    # Stage C batch interleaves safely with the per-row baseline.
    stage_c_first = tmp_path / 'stage-c-1.json'
    save(stage_c_first, stage_c_approval(b, c, sid, ['c-1']))
    with pytest.raises(EvidenceError):
        deployment.deploy(b, stage_c_first, apply=True, stage_c=c)            # Old aggregate baseline correctly refuses.
    snapshot_sha = read(f / 'validation-manifest.json')['files']['before-row-digests.json.gz']
    assert deployment.deploy(b, stage_c_first, apply=True, stage_c=c, baseline_snapshot=f / 'before-row-digests.json.gz',
                             baseline_sha256=snapshot_sha)['counts']['updated'] == 1
    second = tmp_path / 'final-2.json'
    save(second, final_approval(f, ['am-ok'], ['ambiguous']))
    assert deployment.deploy_final(f, second, apply=True)['counts']['updated'] == 1

    with psycopg.connect(admin_url, row_factory=psycopg.rows.dict_row) as conn:
        after = {r['report_id']: r['d'] for r in conn.execute("SELECT report_id, md5(to_jsonb(c)::text) AS d FROM citylens.traffic_collisions c WHERE report_id=ANY(%s)", (untouched,))}
        assert after == before                                               # Matched, excluded and unapproved rows unchanged.
        new = {r['report_id']: r for r in conn.execute("""SELECT report_id, geocode_status, geocode_provider, geocode_precision, geocode_details
               FROM citylens.traffic_collisions WHERE report_id IN ('nm-ok','am-ok','c-1')""")}
        assert {k: (v['geocode_provider'], v['geocode_precision']) for k, v in new.items()} == {
            'nm-ok': ('SanGIS road centerline', 'block_approximate'), 'am-ok': ('SanGIS road centerline', 'block_approximate'),
            'c-1': ('SanGIS local topology', 'intersection_approximate')}
        assert new['am-ok']['geocode_details']['prior_geocoding']['geocode_details']['reason'] == 'returned_number_differs'
        assert new['nm-ok']['geocode_details']['final_geocoding']['validation_status'] == 'pass'
        # An outside edit stops every later batch of either rollout.
        conn.execute("UPDATE citylens.traffic_collisions SET injured=9 WHERE report_id='nm-long'")
    third = tmp_path / 'final-3.json'
    save(third, final_approval(f, ['nm-ok'], ['no_match'], historical_review_basis='third'))
    with pytest.raises(EvidenceError, match='changed'):
        deployment.deploy_final(f, third, apply=True)

    # The Agent reader sees new points through the unchanged deterministic SQL tools, read-only.
    with psycopg.connect(reader_url, row_factory=psycopg.rows.dict_row) as conn:
        counts = {radius: conn.execute("""SELECT coalesce(sum(collision_count),0) AS n, coalesce(sum(located_count),0) AS located
                     FROM citylens.aggregate_traffic_collisions('2021-01-01T00:00:00-08:00','2022-01-01T00:00:00-08:00',
                     latitude=>32.81, longitude=>-117.15, radius_meters=>%s, period=>'year')""", (radius,)).fetchone() for radius in (400, 800)}
        assert counts[400]['n'] == 2 and counts[800]['n'] >= 2                # nm-ok and am-ok (block points); c-1 is ~1.1 km away.
        far = conn.execute("""SELECT coalesce(sum(collision_count),0) AS n FROM citylens.aggregate_traffic_collisions(
                     '2021-01-01T00:00:00-08:00','2022-01-01T00:00:00-08:00', latitude=>32.8, longitude=>-117.15, radius_meters=>400, period=>'year')""").fetchone()
        assert far['n'] == 1                                                   # c-1 intersection point only.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE citylens.traffic_collisions SET location=NULL WHERE report_id='nm-ok'")


def stage_c_url():
    return stage_c.ev.scope_url(-117.15, 32.8)
