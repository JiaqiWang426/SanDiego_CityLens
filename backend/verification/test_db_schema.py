"""Database integration: the installed schema matches the table-definition contract.

Expected values are transcribed by hand from backend/sql/get_it_done.sql and
backend/sql/traffic_collisions.sql. Constraint probes run inside transactions
that are always rolled back.
"""
import psycopg
import pytest
from psycopg import errors

import dbtools

pytestmark = pytest.mark.db

# table -> {column: (format_type, not_null)}
COLUMNS = {
    "ingestions": {
        "id": ("bigint", True), "source_file": ("text", True), "sha256": ("text", True),
        "imported_at": ("timestamp with time zone", True), "source_rows": ("bigint", True),
        "distinct_requests": ("bigint", True)},
    "service_requests": {
        "service_request_id": ("text", True), "parent_id": ("text", False),
        "requested_at": ("timestamp with time zone", True), "service_name": ("text", False),
        "status": ("text", False), "location": ("geography(Point,4326)", False),
        "raw_record": ("jsonb", True), "ingestion_id": ("bigint", True)},
    "collision_imports": {
        "id": ("bigint", True), "source_file": ("text", True), "sha256": ("text", True),
        "imported_at": ("timestamp with time zone", True), "source_rows": ("bigint", True),
        "accepted_rows": ("bigint", True), "rejected_rows": ("bigint", True)},
    "collision_source_rows": {
        "import_id": ("bigint", True), "row_number": ("bigint", True),
        "raw_record": ("jsonb", True), "validation_issues": ("jsonb", True)},
    "traffic_collisions": {
        "report_id": ("text", True), "occurred_at": ("timestamp with time zone", False),
        "police_beat": ("text", False), "primary_street": ("text", True),
        "intersecting_street": ("text", True), "normalized_address": ("text", True),
        "address_key": ("text", True), "violation_section": ("text", False),
        "violation_type": ("text", False), "charge_desc": ("text", False),
        "injured": ("integer", False), "killed": ("integer", False), "hit_run_level": ("text", False),
        "hit_and_run": ("boolean", False), "raw_record": ("jsonb", True), "validation_issues": ("jsonb", True),
        "import_id": ("bigint", True), "location": ("geography(Point,4326)", False),
        "geocode_status": ("text", True), "geocode_provider": ("text", False),
        "geocode_precision": ("text", False), "geocoded_at": ("timestamp with time zone", False),
        "geocode_details": ("jsonb", False)},
}
PRIMARY_KEYS = {
    "ingestions": ["id"], "service_requests": ["service_request_id"], "collision_imports": ["id"],
    "collision_source_rows": ["import_id", "row_number"], "traffic_collisions": ["report_id"],
}
FOREIGN_KEYS = {
    ("service_requests", "ingestion_id", "ingestions", "id"),
    ("collision_source_rows", "import_id", "collision_imports", "id"),
    ("traffic_collisions", "import_id", "collision_imports", "id"),
}
INDEXES = {  # (table, column, access method)
    ("service_requests", "location", "gist"), ("service_requests", "requested_at", "btree"),
    ("traffic_collisions", "location", "gist"), ("traffic_collisions", "occurred_at", "btree"),
    ("traffic_collisions", "primary_street", "btree"), ("traffic_collisions", "intersecting_street", "btree"),
}


@pytest.mark.parametrize("table", list(COLUMNS))
def test_column_types_and_nullability(admin_conn, table):
    rows = admin_conn.execute(
        """SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
           FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped""",
        (f"citylens.{table}",)).fetchall()
    got = {name: (typ, notnull) for name, typ, notnull in rows}
    assert got == COLUMNS[table]


@pytest.mark.parametrize("table", list(PRIMARY_KEYS))
def test_primary_keys(admin_conn, table):
    cols = admin_conn.execute(
        """SELECT a.attname FROM pg_index i
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
           WHERE i.indrelid = %s::regclass AND i.indisprimary
           ORDER BY array_position(i.indkey::int2[], a.attnum)""", (f"citylens.{table}",)).fetchall()
    assert [c[0] for c in cols] == PRIMARY_KEYS[table]


def test_foreign_keys(admin_conn):
    rows = admin_conn.execute(
        """SELECT cl.relname, a.attname, rf.relname, af.attname
           FROM pg_constraint c
           JOIN pg_class cl ON cl.oid = c.conrelid JOIN pg_namespace n ON n.oid = cl.relnamespace
           JOIN pg_class rf ON rf.oid = c.confrelid
           JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
           JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = c.confkey[1]
           WHERE c.contype = 'f' AND n.nspname = 'citylens'""").fetchall()
    assert set(rows) == FOREIGN_KEYS


def test_service_request_parent_has_no_foreign_key(admin_conn):
    # get_it_done.sql: "No foreign key: a parent can be outside the imported subset."
    rows = admin_conn.execute(
        """SELECT 1 FROM pg_constraint WHERE conrelid = 'citylens.service_requests'::regclass
           AND contype = 'f' AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
               WHERE attrelid = 'citylens.service_requests'::regclass AND attname = 'parent_id')]""").fetchall()
    assert rows == []


def test_indexes(admin_conn):
    rows = admin_conn.execute(
        """SELECT t.relname, a.attname, am.amname FROM pg_index i
           JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace
           JOIN pg_class ic ON ic.oid = i.indexrelid JOIN pg_am am ON am.oid = ic.relam
           JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
           WHERE n.nspname = 'citylens' AND NOT i.indisprimary""").fetchall()
    assert INDEXES <= set(rows)


def test_postgis_and_schema_installed(admin_conn):
    assert admin_conn.execute("SELECT extname FROM pg_extension WHERE extname = 'postgis'").fetchone()
    assert admin_conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'citylens'").fetchone()


def test_init_is_repeatable(test_db_url, admin_conn):
    before = dbtools.snapshot(admin_conn)
    dbtools.init_schema(test_db_url)
    assert dbtools.snapshot(admin_conn) == before


# ------------------------------------------------------ constraint behavior
def _seed(tx):
    gid_import = tx.execute(
        "INSERT INTO citylens.ingestions (source_file, sha256, source_rows, distinct_requests) "
        "VALUES ('t.csv', 'x', 1, 1) RETURNING id").fetchone()[0]
    col_import = tx.execute(
        "INSERT INTO citylens.collision_imports (source_file, sha256) VALUES ('t.csv', 'x') RETURNING id").fetchone()[0]
    return gid_import, col_import


def _collision(tx, imp, report_id="C1", **overrides):
    values = dict(report_id=report_id, primary_street="X ST", intersecting_street="", normalized_address="X",
                  address_key="k", raw_record="{}", validation_issues="[]", import_id=imp)
    values.update(overrides)
    cols = ", ".join(values)
    marks = ", ".join(f"%({k})s" for k in values)
    tx.execute(f"INSERT INTO citylens.traffic_collisions ({cols}) VALUES ({marks})", values)


def _request(tx, import_id, rid="R1", **overrides):
    values = dict(service_request_id=rid, requested_at="2026-01-01T00:00:00Z", raw_record="{}", ingestion_id=import_id)
    values.update(overrides)
    cols = ", ".join(values)
    marks = ", ".join(f"%({k})s" for k in values)
    tx.execute(f"INSERT INTO citylens.service_requests ({cols}) VALUES ({marks})", values)


def _expect(tx, error, fn):
    tx.execute("SAVEPOINT probe")
    with pytest.raises(error):
        fn()
    tx.execute("ROLLBACK TO SAVEPOINT probe")


def test_duplicate_primary_keys_rejected(tx):
    g, c = _seed(tx)
    _request(tx, g)
    _collision(tx, c)
    _expect(tx, errors.UniqueViolation, lambda: _request(tx, g))
    _expect(tx, errors.UniqueViolation, lambda: _collision(tx, c))


def test_text_ids_are_case_and_space_sensitive_keys(tx):
    g, _ = _seed(tx)
    _request(tx, g, "R1")
    _request(tx, g, " R1")  # distinct text values: PK is plain text, no normalization
    assert tx.execute("SELECT count(*) FROM citylens.service_requests").fetchone()[0] == 2


@pytest.mark.parametrize("override,error", [
    (dict(injured=-1), errors.CheckViolation),
    (dict(killed=-1), errors.CheckViolation),
    (dict(geocode_status="bogus"), errors.CheckViolation),
    (dict(geocode_precision="exact"), errors.CheckViolation),
    (dict(geocode_status="matched"), errors.CheckViolation),  # matched without a location
    (dict(primary_street=None), errors.NotNullViolation),
    (dict(address_key=None), errors.NotNullViolation),
    (dict(import_id=999_999_999), errors.ForeignKeyViolation),
], ids=["neg_injured", "neg_killed", "bad_status", "bad_precision", "matched_no_location",
        "null_street", "null_address_key", "missing_import"])
def test_collision_constraints(tx, override, error):
    _, c = _seed(tx)
    _expect(tx, error, lambda: _collision(tx, c, **override))


def test_location_requires_matched_and_precision(tx):
    _, c = _seed(tx)
    point = "SRID=4326;POINT(-117.2 32.8)"
    _expect(tx, errors.CheckViolation, lambda: _collision(tx, c, location=point))  # status pending
    _expect(tx, errors.CheckViolation, lambda: _collision(tx, c, location=point, geocode_status="matched"))
    _collision(tx, c, location=point, geocode_status="matched", geocode_precision="block_approximate")
    assert tx.execute("SELECT geocode_status FROM citylens.traffic_collisions WHERE report_id = 'C1'").fetchone()[0] == "matched"


def test_zero_counts_allowed_and_default_status_pending(tx):
    _, c = _seed(tx)
    _collision(tx, c, injured=0, killed=0)
    assert tx.execute("SELECT injured, killed, geocode_status FROM citylens.traffic_collisions "
                      "WHERE report_id = 'C1'").fetchone() == (0, 0, "pending")


@pytest.mark.parametrize("override,error", [
    (dict(requested_at=None), errors.NotNullViolation),
    (dict(raw_record=None), errors.NotNullViolation),
    (dict(ingestion_id=999_999_999), errors.ForeignKeyViolation),
], ids=["null_requested_at", "null_raw_record", "missing_ingestion"])
def test_service_request_constraints(tx, override, error):
    g, _ = _seed(tx)
    _expect(tx, error, lambda: _request(tx, g, **override))


def test_parent_outside_subset_allowed(tx):
    g, _ = _seed(tx)
    _request(tx, g, parent_id="does-not-exist")


def test_source_row_archive_key(tx):
    _, c = _seed(tx)
    tx.execute("INSERT INTO citylens.collision_source_rows VALUES (%s, 1, '{}', '[]')", (c,))
    _expect(tx, errors.UniqueViolation,
            lambda: tx.execute("INSERT INTO citylens.collision_source_rows VALUES (%s, 1, '{}', '[]')", (c,)))


def test_transaction_rollback_discards_all_writes(test_db_url, admin_conn):
    before = dbtools.snapshot(admin_conn)
    conn = psycopg.connect(test_db_url)
    try:
        g, c = _seed(conn)
        _request(conn, g, "ROLLBACK-1")
        _collision(conn, c, "ROLLBACK-1")
        with pytest.raises(errors.CheckViolation):
            _collision(conn, c, "ROLLBACK-2", injured=-5)
        with pytest.raises(errors.InFailedSqlTransaction):  # aborted transaction accepts nothing more
            conn.execute("SELECT 1")
    finally:
        conn.rollback()
        conn.close()
    assert dbtools.snapshot(admin_conn) == before

