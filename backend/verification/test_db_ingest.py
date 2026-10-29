"""Database integration: ingestion through the documented production CLIs.

Each test starts from empty tables in the isolated test database
(`clean_db`), loads fixtures with `db.py load` / `collision_ingest.py`, and
compares stored rows with the independent reference and hand-specified
expectations. Tests marked `probe` record behavior the docs leave undefined.
"""
import hashlib
import json

import pandas as pd
import pytest

import dbtools
from fixtures import expected as E
from refimpl import collisions as C
from refimpl import gid

pytestmark = pytest.mark.db
FX = E.FIXTURES


def _ok(proc):
    assert proc.returncode == 0, f"loader failed (exit {proc.returncode}):\n{proc.stderr[-3000:]}"


def _requests(conn) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT service_request_id, parent_id, requested_at, service_name, status,
                  ST_Y(location::geometry), ST_X(location::geometry), raw_record, ingestion_id
           FROM citylens.service_requests ORDER BY 1""").fetchall()
    return pd.DataFrame(rows, columns=["id", "parent_id", "requested_at", "service_name", "status",
                                       "lat", "lng", "raw_record", "ingestion_id"]).set_index("id", drop=False)


def _collisions(conn) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT report_id, occurred_at, police_beat, primary_street, intersecting_street, normalized_address,
                  address_key, injured, killed, hit_run_level, hit_and_run, raw_record, validation_issues,
                  import_id, location IS NOT NULL, geocode_status, geocode_provider, geocode_precision,
                  geocoded_at, geocode_details
           FROM citylens.traffic_collisions ORDER BY 1""").fetchall()
    cols = ["id", "occurred_at", "police_beat", "primary_street", "intersecting_street", "normalized_address",
            "address_key", "injured", "killed", "hit_run_level", "hit_and_run", "raw_record", "validation_issues",
            "import_id", "located", "geocode_status", "geocode_provider", "geocode_precision", "geocoded_at",
            "geocode_details"]
    return pd.DataFrame(rows, columns=cols).set_index("id", drop=False)


def _none(value):
    return None if value is None or (not isinstance(value, (dict, list)) and pd.isna(value)) else value


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ===================================================================== GID
def test_gid_load_matches_reference(clean_db):
    path = FX / "gid_basic.csv"
    _ok(dbtools.load_gid(dbtools.guarded_url(), path))
    ref = gid.load_gid(path)
    db = _requests(clean_db)

    assert set(db.index) == set(ref.records.index), "stored ID set differs from reference"
    problems = []
    for rid, r in ref.records.iterrows():
        row = db.loc[rid]
        if pd.Timestamp(row["requested_at"]) != r["t_early"]:
            problems.append((rid, "requested_at", r["t_early"], row["requested_at"]))
        if pd.isna(r["lat"]) != pd.isna(row["lat"]) or (
                not pd.isna(r["lat"]) and (abs(row["lat"] - r["lat"]) > 1e-9 or abs(row["lng"] - r["lng"]) > 1e-9)):
            problems.append((rid, "location", (r["lat"], r["lng"]), (row["lat"], row["lng"])))
        if _none(r["parent_id"]) != _none(row["parent_id"]):
            problems.append((rid, "parent_id", r["parent_id"], row["parent_id"]))
        if r["status"] != row["status"]:
            problems.append((rid, "status", r["status"], row["status"]))
        if str(row["raw_record"].get("service_request_id")) != rid:
            problems.append((rid, "raw_record.service_request_id", rid, row["raw_record"].get("service_request_id")))
    assert not problems, "field mismatches (id, field, expected, observed):\n" + "\n".join(map(str, problems))


def test_gid_last_occurrence_wins_including_raw_record(clean_db):
    _ok(dbtools.load_gid(dbtools.guarded_url(), FX / "gid_basic.csv"))
    db = _requests(clean_db)
    for rid, status in E.GID_BASIC["last_wins_status"].items():
        assert db.at[rid, "status"] == status
        assert db.at[rid, "raw_record"].get("status") == status, "raw_record must hold the current (last) row"
        assert str(db.at[rid, "raw_record"].get("date_requested")).startswith("2026-01-11")


def test_gid_ingestion_provenance(clean_db):
    path = FX / "gid_basic.csv"
    _ok(dbtools.load_gid(dbtools.guarded_url(), path))
    rows = clean_db.execute("SELECT id, source_file, sha256, source_rows, distinct_requests FROM citylens.ingestions").fetchall()
    assert len(rows) == 1
    iid, source_file, sha, source_rows, distinct = rows[0]
    assert source_file.replace("\\", "/").endswith("gid_basic.csv")
    assert sha.lower() == _sha256(path)
    assert (source_rows, distinct) == (E.GID_BASIC["source_rows"], E.GID_BASIC["distinct_requests"])
    assert set(_requests(clean_db)["ingestion_id"]) == {iid}


def test_gid_missing_coordinates_retained(clean_db):
    _ok(dbtools.load_gid(dbtools.guarded_url(), FX / "gid_basic.csv"))
    db = _requests(clean_db)
    assert "9100007" in db.index and pd.isna(db.at["9100007", "lat"])


def test_gid_reimport_upserts_and_retains_absent_ids(clean_db):
    url = dbtools.guarded_url()
    _ok(dbtools.load_gid(url, FX / "gid_upsert_a.csv"))
    db = _requests(clean_db)
    assert db["status"].to_dict() == E.GID_UPSERT["after_a"]
    assert set(db.index[db["lat"].notna()]) == E.GID_UPSERT["located_after_a"]
    _ok(dbtools.load_gid(url, FX / "gid_upsert_b.csv"))
    db = _requests(clean_db)
    assert db["status"].to_dict() == E.GID_UPSERT["after_b"]
    assert set(db.index[db["lat"].notna()]) == E.GID_UPSERT["located_after_b"]
    assert abs(db.at["9200002", "lat"] - 32.80720463) < 1e-9  # moved by the second file
    assert clean_db.execute("SELECT count(*) FROM citylens.ingestions").fetchone()[0] == 2


@pytest.mark.probe
def test_probe_gid_ingestion_id_after_upsert(clean_db, observe):
    url = dbtools.guarded_url()
    _ok(dbtools.load_gid(url, FX / "gid_upsert_a.csv"))
    _ok(dbtools.load_gid(url, FX / "gid_upsert_b.csv"))
    db = _requests(clean_db)
    observe("probe", {"question": "ingestion_id of retained vs updated rows",
                      "observed": db["ingestion_id"].to_dict()})


def test_gid_repeated_identical_import_is_idempotent(clean_db):
    url, path = dbtools.guarded_url(), FX / "gid_basic.csv"
    _ok(dbtools.load_gid(url, path))
    first = _requests(clean_db).drop(columns=["ingestion_id"])
    _ok(dbtools.load_gid(url, path))
    second = _requests(clean_db).drop(columns=["ingestion_id"])
    pd.testing.assert_frame_equal(first, second)
    assert clean_db.execute("SELECT count(*) FROM citylens.ingestions").fetchone()[0] == 2


@pytest.mark.parametrize("name", ["gid_invalid_lat_text", "gid_invalid_lat_range", "gid_invalid_date", "gid_missing_date"])
def test_gid_invalid_file_fails_and_rolls_back(clean_db, name):
    url = dbtools.guarded_url()
    with pytest.raises(gid.GidImportError):  # reference agrees the file is invalid
        gid.load_gid(FX / f"{name}.csv")
    _ok(dbtools.load_gid(url, FX / "gid_upsert_a.csv"))
    before = dbtools.snapshot(clean_db)
    proc = dbtools.load_gid(url, FX / f"{name}.csv")
    after = dbtools.snapshot(clean_db)
    assert proc.returncode != 0, f"{name}: import of an invalid file reported success"
    assert after == before, f"{name}: failed import changed stored data"


def test_gid_dst_overlap_row_imported_with_one_candidate(clean_db, observe):
    _ok(dbtools.load_gid(dbtools.guarded_url(), FX / "gid_dst_overlap.csv"))
    db = _requests(clean_db)
    got = pd.Timestamp(db.at["9300001", "requested_at"])
    candidates = {pd.Timestamp("2025-11-02T08:30:00Z"): "PDT (first occurrence)",
                  pd.Timestamp("2025-11-02T09:30:00Z"): "PST (second occurrence)"}
    assert got in candidates, f"ambiguous wall time stored as {got}, neither candidate"
    observe("ambiguous_time", {"table": "service_requests", "id": "9300001", "source": "2025-11-02 01:30:00.000",
                               "stored": got.isoformat(), "resolution": candidates[got]})


@pytest.mark.probe
@pytest.mark.parametrize("name", ["gid_dst_gap", "gid_probe_one_sided", "gid_probe_empty_id", "gid_probe_zero_coord"])
def test_probe_gid_undocumented_inputs(clean_db, observe, name):
    proc = dbtools.load_gid(dbtools.guarded_url(), FX / f"{name}.csv")
    db = _requests(clean_db)
    observe("probe", {"fixture": name, "exit_code": proc.returncode,
                      "stderr_tail": proc.stderr.strip().splitlines()[-1:] if proc.returncode else [],
                      "stored": {rid: {"requested_at": str(r["requested_at"]), "lat": r["lat"], "lng": r["lng"]}
                                 for rid, r in db.iterrows()}})


@pytest.mark.probe
def test_probe_gid_empty_service_name_storage(clean_db, observe):
    _ok(dbtools.load_gid(dbtools.guarded_url(), FX / "gid_basic.csv"))
    db = _requests(clean_db)
    observe("probe", {"question": "empty service_name stored as", "observed": repr(db.at["9100006", "service_name"])})


# =============================================================== Collisions
def _col_import_rows(conn):
    return conn.execute("SELECT id, source_file, sha256, source_rows, accepted_rows, rejected_rows "
                        "FROM citylens.collision_imports ORDER BY id").fetchall()


def test_collision_load_matches_reference(clean_db):
    path = FX / "col_basic.csv"
    _ok(dbtools.load_collisions(dbtools.guarded_url(), path))
    ref = C.load_collisions(path)
    db = _collisions(clean_db)
    assert set(db.index) == set(ref.records.index)
    problems = []
    for rid, r in ref.records.iterrows():
        row = db.loc[rid]
        observed_time = None if pd.isna(row["occurred_at"]) else pd.Timestamp(row["occurred_at"])
        expected_time = None if pd.isna(r["t_early"]) else r["t_early"]
        if observed_time != expected_time:
            problems.append((rid, "occurred_at", expected_time, observed_time))
        for field in ("injured", "killed", "hit_and_run", "hit_run_level"):
            exp = None if pd.isna(r[field]) else r[field]
            obs = None if pd.isna(row[field]) else row[field]
            if exp != obs:
                problems.append((rid, field, exp, obs))
    assert not problems, "field mismatches (id, field, expected, observed):\n" + "\n".join(map(str, problems))


def test_collision_import_counts_and_archive(clean_db):
    path = FX / "col_basic.csv"
    _ok(dbtools.load_collisions(dbtools.guarded_url(), path))
    spec = E.COL_BASIC
    (iid, source_file, sha, source_rows, accepted, rejected), = _col_import_rows(clean_db)
    assert (source_rows, accepted, rejected) == (spec["source_rows"], spec["accepted_rows"], spec["rejected_rows"])
    assert sha.lower() == _sha256(path)
    assert source_file.replace("\\", "/").endswith("col_basic.csv")
    archived = clean_db.execute("SELECT raw_record FROM citylens.collision_source_rows WHERE import_id = %s", (iid,)).fetchall()
    ids = sorted(r[0].get("REPORT_ID", "") for r in archived)
    assert len(archived) == spec["source_rows"]
    assert ids.count("7100010") == 2 and ids.count("") == 1, "archive must keep duplicates and rejected rows"
    raw_15 = [r[0] for r in archived if r[0].get("REPORT_ID") == "7100015"][0]
    assert raw_15["ADDRESS_ROAD_PRIMARY"] == "LA  JOLLA   SCENIC", "archive must keep original whitespace"


def test_collision_current_row_and_geocode_defaults(clean_db):
    _ok(dbtools.load_collisions(dbtools.guarded_url(), FX / "col_basic.csv"))
    db = _collisions(clean_db)
    assert db.at["7100010", "raw_record"].get("INJURED") == "0"  # last occurrence is current
    assert set(db["geocode_status"]) == {"pending"} and not db["located"].any()
    assert db["geocode_provider"].isna().all() and db["geocoded_at"].isna().all()


def test_collision_validation_issues(clean_db):
    _ok(dbtools.load_collisions(dbtools.guarded_url(), FX / "col_basic.csv"))
    db = _collisions(clean_db)
    must_have_issue = {"7100004": "missing INJURED", "7100005": "missing KILLED + unknown HIT_RUN_LVL",
                       "7100006": "invalid INJURED/KILLED", "7100008": "missing DATE_TIME", "7100009": "invalid DATE_TIME"}
    lacking = {rid: why for rid, why in must_have_issue.items() if not db.at[rid, "validation_issues"]}
    assert not lacking, f"rows without validation issues: {lacking}"
    for rid in ("7100001", "7100002", "7100011"):
        assert db.at[rid, "validation_issues"] == [], f"{rid} is clean but has issues {db.at[rid, 'validation_issues']}"


def test_collision_street_normalization(clean_db):
    _ok(dbtools.load_collisions(dbtools.guarded_url(), FX / "col_basic.csv"))
    db = _collisions(clean_db)
    assert db.at["7100001", "primary_street"] == "LA JOLLA SCENIC DR"
    assert db.at["7100015", "primary_street"] == "LA JOLLA SCENIC DR"
    assert db.at["7100003", "intersecting_street"] == "LA JOLLA SCENIC DR"
    assert db.at["7100004", "primary_street"] == "S L"
    assert db.at["7100001", "address_key"] == db.at["7100015", "address_key"] or \
        db.at["7100001", "normalized_address"] != db.at["7100015", "normalized_address"]


def test_collision_batch_size_does_not_change_result(clean_db):
    url, path = dbtools.guarded_url(), FX / "col_basic.csv"
    _ok(dbtools.load_collisions(url, path))
    default = _collisions(clean_db).drop(columns=["import_id"])
    dbtools.truncate_all(clean_db)
    _ok(dbtools.load_collisions(url, path, "--batch-size", "2"))
    pd.testing.assert_frame_equal(default, _collisions(clean_db).drop(columns=["import_id"]))


def test_collision_reimport_upsert_and_geocode_preservation(clean_db):
    url = dbtools.guarded_url()
    _ok(dbtools.load_collisions(url, FX / "col_reimport_a.csv"))
    dbtools.set_locations(clean_db, {"7200001": (32.8, -117.2), "7200002": (32.81, -117.21)})
    before = _collisions(clean_db)
    _ok(dbtools.load_collisions(url, FX / "col_reimport_b.csv"))
    after = _collisions(clean_db)

    assert set(after.index) == {"7200001", "7200002", "7200003", "7200004"}
    # unchanged address: geocoding survives, attributes update
    for field in ("located", "geocode_status", "geocode_provider", "geocode_precision", "geocoded_at", "address_key"):
        assert after.at["7200001", field] == before.at["7200001", field], field
    assert after.at["7200001", "injured"] == 4
    # changed address: geocoding fully reset
    row = after.loc["7200002"]
    assert (row["located"], row["geocode_status"]) == (False, "pending")
    assert row[["geocode_provider", "geocode_precision", "geocoded_at", "geocode_details"]].isna().all()
    assert row["address_key"] != before.at["7200002", "address_key"]
    # absent from later file: retained untouched
    assert after.at["7200003", "import_id"] == before.at["7200003", "import_id"]
    assert len(_col_import_rows(clean_db)) == 2
    assert clean_db.execute("SELECT count(*) FROM citylens.collision_source_rows").fetchone()[0] == 6


def test_collision_repeated_identical_import(clean_db):
    url, path = dbtools.guarded_url(), FX / "col_basic.csv"
    _ok(dbtools.load_collisions(url, path))
    first = _collisions(clean_db).drop(columns=["import_id"])
    _ok(dbtools.load_collisions(url, path))
    pd.testing.assert_frame_equal(first, _collisions(clean_db).drop(columns=["import_id"]))
    assert len(_col_import_rows(clean_db)) == 2
    assert clean_db.execute("SELECT count(*) FROM citylens.collision_source_rows").fetchone()[0] == 2 * E.COL_BASIC["source_rows"]


@pytest.mark.parametrize("name", ["col_missing_header", "col_bad_columns"])
def test_collision_structural_error_rolls_back(clean_db, name):
    url = dbtools.guarded_url()
    _ok(dbtools.load_collisions(url, FX / "col_reimport_a.csv"))
    before = dbtools.snapshot(clean_db)
    proc = dbtools.load_collisions(url, FX / f"{name}.csv")
    assert proc.returncode != 0, f"{name}: structurally invalid file imported successfully"
    assert dbtools.snapshot(clean_db) == before, f"{name}: failed import changed stored data"


def test_collision_dst_overlap_preserved(clean_db, observe):
    _ok(dbtools.load_collisions(dbtools.guarded_url(), FX / "col_dst_overlap.csv"))
    db = _collisions(clean_db)
    got = pd.Timestamp(db.at["7300001", "occurred_at"])
    candidates = {pd.Timestamp("2025-11-02T08:30:00Z"): "PDT (first occurrence)",
                  pd.Timestamp("2025-11-02T09:30:00Z"): "PST (second occurrence)"}
    assert got in candidates
    observe("ambiguous_time", {"table": "traffic_collisions", "id": "7300001", "source": "2025-11-02 01:30:00.000",
                               "stored": got.isoformat(), "resolution": candidates[got]})


@pytest.mark.probe
def test_probe_collision_undocumented_values(clean_db, observe):
    proc = dbtools.load_collisions(dbtools.guarded_url(), FX / "col_probes.csv")
    if proc.returncode:
        observe("probe", {"fixture": "col_probes", "exit_code": proc.returncode,
                          "stderr_tail": proc.stderr.strip().splitlines()[-1:]})
        return
    db = _collisions(clean_db)
    stored = {rid: {k: (None if pd.isna(row[k]) else row[k]) for k in
                    ("injured", "hit_run_level", "hit_and_run", "primary_street", "normalized_address", "occurred_at")}
              for rid, row in db.iterrows()}
    imp = _col_import_rows(clean_db)[0]
    observe("probe", {"fixture": "col_probes", "stored_ids": [repr(i) for i in db.index],
                      "import_counts": {"source": imp[3], "accepted": imp[4], "rejected": imp[5]},
                      "stored": json.loads(json.dumps(stored, default=str))})
