"""Classification consistency, migration/backfill safety and ingestion tests.

Python rules are ported from the reference Explorer; they are checked against
its independent pandas audit. PostgreSQL tests are opt-in (AGENT_TEST_DOCKER=1
or TEST_AGENT_DATABASE_URL) and run only in an isolated *_test database.
"""
import csv
import json

import pandas as pd
import psycopg
import pytest

from backend import category_analysis as audit
from backend import explorer_data, service_classification
from backend.category_rules import MAPPING, UNMAPPED, load_category_rules
from backend.db import load_csv
from backend.agent_tests.test_integration import isolated_db  # noqa: F401 - opt-in PostGIS fixture

DERIVED = ('main_category', 'sub_category', 'classification_method')
REAL_2026_COUNTS = {
    'Sanitation & Waste': 9317, 'Traffic & Mobility': 2917, 'Parking & Vehicles': 7657,
    'Community & Property Services': 3603, 'Parks & Vegetation': 2632,
    'Roads & Sidewalks': 2293, 'Water & Drainage': 860, 'Graffiti': 915, 'Needs Review': 1473,
}


def mapping_samples():
    """Every condition and default, with whitespace noise and an unseen detail."""
    mapping = pd.read_csv(MAPPING, dtype=str, keep_default_na=False)
    samples = []
    for row in mapping.itertuples():
        details = row.detail_values.split('|') if row.detail_match == 'in' else ['', 'Unseen detail for regression']
        for detail in details:
            samples.append({'service_name': ' ' + row.service_name + ' ', 'service_name_detail': '\t' + detail + '  '})
    return mapping, pd.DataFrame(samples)


# ---------------------------------------------------------------- pure Python

def test_rules_agree_with_independent_pandas_audit_for_every_rule():
    mapping, sample = mapping_samples()
    sample['_service'] = audit.normalized(sample.service_name)
    sample['_detail'] = audit.normalized(sample.service_name_detail)
    expected = audit.classify(sample, mapping)
    rules = load_category_rules()
    for row in expected.itertuples():
        result = tuple(getattr(row, field) for field in DERIVED)
        assert rules.classify(row.service_name, row.service_name_detail) == result
        assert rules.classify_or_unmapped(row.service_name, row.service_name_detail) == result


def test_unknown_service_is_explicit_not_guessed():
    rules = load_category_rules()
    with pytest.raises(ValueError, match='Unmapped service_name'):
        rules.classify('Unknown future service', '')
    assert rules.classify_or_unmapped('Unknown future service', 'Graffiti') == UNMAPPED
    assert rules.classify_or_unmapped(None, None) == ('Needs Review', 'Missing service name', 'needs_review')
    assert rules.classify_or_unmapped('Other', '') == ('Needs Review', 'Source Other - unresolved', 'needs_review')


def test_hierarchy_and_rule_export_match_mapping():
    rules = load_category_rules()
    hierarchy = rules.hierarchy()
    assert len(hierarchy) == 9 and sum(len(r['subcategories']) for r in hierarchy) == 66
    extended = rules.hierarchy(include_unmapped=True)
    review = next(r for r in extended if r['main_category'] == 'Needs Review')
    assert review['subcategories'][-1] == UNMAPPED[1]
    assert rules.hierarchy() == hierarchy  # extension does not mutate the designed hierarchy
    rows = rules.rule_rows()
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1))
    defaults = {r[1] for r in rows if r[2] is None}
    assert len(defaults) == sum(1 for r in rows if r[2] is None)  # one default per service
    assert {(r[1], r[2]) for r in rows if r[2] is not None}.isdisjoint({(s, None) for s in defaults})


def test_real_2026_scope_assignments_match_pandas_audit():
    """Independent replay over the source CSV scope (31,667 records)."""
    raw = pd.read_csv(explorer_data.SOURCE, dtype=str, keep_default_na=False)
    scoped, _ = audit.scope_and_checks(raw)
    scoped['_service'] = audit.normalized(scoped.service_name)
    scoped['_detail'] = audit.normalized(scoped.service_name_detail)
    mapping = pd.read_csv(MAPPING, dtype=str, keep_default_na=False)
    expected = audit.classify(scoped, mapping)
    rules = load_category_rules()
    counts = {}
    for row in expected.itertuples():
        result = rules.classify(row.service_name, row.service_name_detail)
        assert result == (row.main_category, row.sub_category, row.classification_method)
        counts[result[0]] = counts.get(result[0], 0) + 1
    assert counts == REAL_2026_COUNTS


def test_psql_script_is_one_transaction_with_literal_rules():
    text = service_classification.script()
    assert text.startswith('BEGIN;') and text.rstrip().endswith('COMMIT;')
    rules = load_category_rules()
    assert text.count('INSERT INTO citylens.service_category_rules') == len(rules.rule_rows())
    assert "'Parks Issue'" in text and rules.sha256 in text and '%s' not in text


# ----------------------------------------------------------------- PostGIS

@pytest.fixture(scope='module')
def db(isolated_db):  # noqa: F811
    admin_url, reader = isolated_db
    with psycopg.connect(admin_url) as conn:
        gid = conn.execute("""INSERT INTO citylens.ingestions(source_file,sha256,source_rows,distinct_requests)
            VALUES ('classification_synthetic.csv','synthetic',0,0) RETURNING id""").fetchone()[0]
    yield admin_url, reader, gid



def insert(conn, gid, rows):
    for request_id, service, detail in rows:
        raw = json.dumps({'service_name': service, 'service_name_detail': detail, 'council_district': '1'})
        conn.execute('''INSERT INTO citylens.service_requests
            (service_request_id, requested_at, service_name, status, location, raw_record, ingestion_id)
            VALUES (%s, '2026-02-01T12:00:00-08:00', %s, 'New',
                    ST_SetSRID(ST_MakePoint(-117.25, 32.85), 4326)::geography, %s, %s)''',
            (request_id, service, raw, gid))


def fingerprint(conn, prefix):
    return conn.execute('''SELECT md5(string_agg(concat_ws('|', service_request_id, parent_id, requested_at,
        service_name, status, ST_AsEWKB(location::geometry)::text, raw_record::text, ingestion_id), ',' ORDER BY service_request_id))
        FROM citylens.service_requests WHERE service_request_id LIKE %s''', (prefix + '%',)).fetchone()[0]


def test_sql_classifier_matches_python_for_every_rule_and_is_idempotent(db):
    admin, _, gid = db
    rules = load_category_rules()
    _, sample = mapping_samples()
    rows = [(f'rule_{i}', r.service_name, r.service_name_detail) for i, r in enumerate(sample.itertuples())]
    rows += [('rule_unknown', 'Unknown future service', 'Graffiti'), ('rule_null', None, None),
             ('rule_blank', '   ', ''), ('rule_case', 'parks issue', 'Graffiti'),
             ('rule_spaces', 'Parks   Issue', ' Trash/Litter ')]
    with psycopg.connect(admin) as conn:
        insert(conn, gid, rows)
        before = fingerprint(conn, 'rule_')
        first = service_classification.apply(conn)
        assert first['changed_rows'] >= len(rows) and first['mapping_sha256'] == rules.sha256
        stored = {r[0]: r[1:] for r in conn.execute('''SELECT service_request_id, main_category, sub_category,
            classification_method, classification_version FROM citylens.service_requests WHERE service_request_id LIKE 'rule_%' ''')}
        for request_id, service, detail in rows:
            assert stored[request_id] == (*rules.classify_or_unmapped(service, detail), rules.sha256), request_id
        assert stored['rule_unknown'][:3] == UNMAPPED
        assert stored['rule_case'][:3] == UNMAPPED  # matching stays case-sensitive, as in the source
        # Repeated migrations are no-ops and never alter source fields.
        again = service_classification.apply(conn)
        assert again['changed_rows'] == 0 and again['unclassified'] == 0
        assert fingerprint(conn, 'rule_') == before
        assert conn.execute('SELECT count(*) FROM citylens.service_category_mappings WHERE is_active').fetchone()[0] == 1


def test_mapping_change_reclassifies_with_new_version_and_restores(db, tmp_path):
    admin, _, gid = db
    changed = tmp_path / 'mapping.csv'
    changed.write_text(MAPPING.read_text(encoding='utf-8').replace(
        'Pothole,Roads & Sidewalks,Pothole,any', 'Pothole,Roads & Sidewalks,Pothole report,any'), encoding='utf-8')
    with psycopg.connect(admin) as conn:
        insert(conn, gid, [('version_a', 'Pothole', '')])
        service_classification.apply(conn)
        result = service_classification.apply(conn, changed)
        assert result['changed_rows'] >= 1 and result['not_current_version'] == 0
        assert conn.execute("SELECT sub_category FROM citylens.service_requests WHERE service_request_id='version_a'").fetchone()[0] == 'Pothole report'
        restored = service_classification.apply(conn)
        assert restored['mapping_sha256'] == load_category_rules().sha256 and restored['not_current_version'] == 0
        assert conn.execute("SELECT sub_category FROM citylens.service_requests WHERE service_request_id='version_a'").fetchone()[0] == 'Pothole'
        assert conn.execute('SELECT count(*) FROM citylens.service_category_mappings').fetchone()[0] >= 2


def test_invalid_mapping_rolls_back_and_keeps_existing_classification(db, tmp_path):
    admin, _, gid = db
    bad = tmp_path / 'bad.csv'
    text = MAPPING.read_text(encoding='utf-8')
    line = next(l for l in text.splitlines() if l.startswith('Parks Issue,Graffiti,Park graffiti,'))
    bad.write_text(text + line + '\n', encoding='utf-8')
    with psycopg.connect(admin) as conn:
        insert(conn, gid, [('rollback_a', 'Parks Issue', 'Graffiti')])
        service_classification.apply(conn)
    with pytest.raises(ValueError, match='Overlapping'):
        with psycopg.connect(admin) as conn:
            service_classification.apply(conn, bad)
    with psycopg.connect(admin) as conn:
        assert conn.execute("SELECT main_category FROM citylens.service_requests WHERE service_request_id='rollback_a'").fetchone()[0] == 'Graffiti'


def test_constraint_rejects_partial_classification(db):
    admin, _, gid = db
    with psycopg.connect(admin) as conn:
        insert(conn, gid, [('partial_a', 'Pothole', '')])
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE citylens.service_requests SET main_category='Graffiti' WHERE service_request_id='partial_a' AND classification_method IS NULL")


def test_csv_ingestion_classifies_and_reimport_is_stable(db, tmp_path):
    admin, _, _ = db
    fields = sorted(explorer_data.REQUIRED | {'service_name_detail', 'council_district'})
    base = dict.fromkeys(fields, '')
    base.update(date_requested='2026-03-01 10:00:00', council_district='1', lat='32.8', lng='-117.2', status='New')
    rows = [{**base, 'service_request_id': 'ingest_a', 'service_name': 'Parks Issue', 'service_name_detail': 'Graffiti'},
            {**base, 'service_request_id': 'ingest_b', 'service_name': 'Parking', 'service_name_detail': 'Red Zone'},
            {**base, 'service_request_id': 'ingest_c', 'service_name': '', 'lat': '', 'lng': ''},
            {**base, 'service_request_id': 'ingest_d', 'service_name': 'Brand new source label'},
            # Last occurrence wins, unchanged semantics: ingest_b becomes a pothole.
            {**base, 'service_request_id': 'ingest_b', 'service_name': 'Pothole'}]
    source = tmp_path / 'requests.csv'
    with source.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    expected = {'ingest_a': ('Graffiti', 'Park graffiti', 'detail_match'),
                'ingest_b': ('Roads & Sidewalks', 'Pothole', 'service_default'),
                'ingest_c': ('Needs Review', 'Missing service name', 'needs_review'),
                'ingest_d': UNMAPPED}
    query = '''SELECT service_request_id, main_category, sub_category, classification_method, location IS NULL
        FROM citylens.service_requests WHERE service_request_id LIKE 'ingest_%' ORDER BY 1'''
    with psycopg.connect(admin) as conn:
        load_csv(conn, source)
        first = conn.execute(query).fetchall()
        before = fingerprint(conn, 'ingest_')
    assert {r[0]: r[1:4] for r in first} == expected
    assert [r[4] for r in first] == [False, False, True, False]  # missing coordinates stay NULL, not dropped
    with psycopg.connect(admin) as conn:
        load_csv(conn, source)
        assert conn.execute(query).fetchall() == first
        assert service_classification.status(conn)['unclassified'] == 0
        # Same rows, new ingestion ID only (existing upsert semantics).
        assert fingerprint(conn, 'ingest_') != before
        assert conn.execute("SELECT count(*) FROM citylens.service_requests WHERE service_request_id LIKE 'ingest_%'").fetchone()[0] == 4


def test_reader_can_read_mapping_but_not_write(db):
    _, reader, _ = db
    with psycopg.connect(reader) as conn:
        assert conn.execute('SELECT count(*) FROM citylens.service_category_rules').fetchone()[0] > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM citylens.service_category_rules")
    with psycopg.connect(reader) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('SELECT citylens.backfill_service_request_classification()')
