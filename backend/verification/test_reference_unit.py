"""Unit tests: the Pandas reference against hand-specified expected outputs.

No database. These anchor the reference before it is used as an oracle.
"""
import pandas as pd
import pytest

from fixtures import expected as E
from refimpl import collisions as C
from refimpl import geo, gid, timeutil

FX = E.FIXTURES
WIDE = ("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z")


# ---------------------------------------------------------------- time
@pytest.mark.parametrize("text,status,early,late", [
    ("2026-01-15 10:00:00.000", "ok", "2026-01-15T18:00:00Z", "2026-01-15T18:00:00Z"),
    ("2026-07-01 00:00:00", "ok", "2026-07-01T07:00:00Z", "2026-07-01T07:00:00Z"),
    ("2025-11-02 01:30:00.000", "ambiguous", "2025-11-02T08:30:00Z", "2025-11-02T09:30:00Z"),
    ("2026-03-08 02:30:00.000", "nonexistent", None, None),
    ("", "missing", None, None),
    ("2026-13-45 99:00:00.000", "invalid", None, None),
])
def test_parse_source_time(text, status, early, late):
    got_status, got_early, got_late = timeutil.parse_source_time(text)
    assert got_status == status
    if early:
        assert got_early == pd.Timestamp(early) and got_late == pd.Timestamp(late)


def test_query_bounds_require_offset():
    with pytest.raises(ValueError):
        timeutil.require_aware("2026-01-01T00:00:00")


def test_real_collision_file_has_18_ambiguous_timestamps():
    raw = pd.read_csv(FX.parent.parent / "Raw Data/Traffic collision/pd_collisions_datasd.csv",
                      dtype=str, keep_default_na=False, usecols=["DATE_TIME"])
    statuses = timeutil.parse_series(raw["DATE_TIME"])["time_status"].value_counts()
    assert statuses.get("ambiguous", 0) == 18
    assert statuses.get("nonexistent", 0) == 0


# ---------------------------------------------------------------- geo
def test_geodesic_distances_of_fixture_points():
    lats = pd.Series([32.80720463, 32.79999971, 32.79277733, 33.25083666])
    lngs = pd.Series([-117.2, -117.19145879, -117.2, -117.2])
    d = geo.distances_m(32.8, -117.2, lats, lngs)
    assert d.round(2).tolist() == pytest.approx([799.0, 800.0, 801.0, 50000.0], abs=0.01)


def test_radius_membership_tolerance_band():
    d = pd.Series([0.0, 799.4, 799.6, 800.0, 800.4, 800.6, float("nan")])
    assert geo.radius_membership(d, 800).tolist() == ["in", "in", "boundary", "boundary", "boundary", "out", "out"]


@pytest.mark.parametrize("lat,lng,r", [(91, 0, 1), (0, 181, 1), (0, 0, -1), (0, 0, 50_000.1), (float("nan"), 0, 1)])
def test_invalid_center_or_radius_rejected(lat, lng, r):
    with pytest.raises(ValueError):
        geo.validate_center(lat, lng, r)


# ---------------------------------------------------------------- Get It Done
@pytest.fixture(scope="module")
def gid_basic():
    return gid.load_gid(FX / "gid_basic.csv")


def test_gid_counts_and_dedup(gid_basic):
    spec = E.GID_BASIC
    assert gid_basic.source_rows == spec["source_rows"]
    assert gid_basic.distinct_requests == spec["distinct_requests"]
    r = gid_basic.records
    assert set(r.index[r["lat"].isna()]) == spec["no_location"]
    assert set(r.index[r["parent_id"].notna()]) == spec["with_parent"]
    for rid, status in spec["last_wins_status"].items():
        assert r.at[rid, "status"] == status


@pytest.mark.parametrize("q", E.GID_BASIC["queries"], ids=lambda q: q["name"])
def test_gid_count_reference(gid_basic, q):
    got = gid.count_service_requests(gid_basic, q["lat"], q["lng"], q["radius"], *q["window"], q["exclude_linked"])
    assert got.ids == q["ids"]
    assert got.uncertain == q["uncertain"]


@pytest.mark.parametrize("q", E.GID_BASIC["category_status"], ids=["service", "status", "service+status"])
def test_gid_category_status_reference(gid_basic, q):
    got = gid.select_requests(gid_basic, *WIDE, service_names=q["service_names"], statuses=q["statuses"])
    assert got.ids == q["ids"] and not got.uncertain


@pytest.mark.parametrize("q", E.GID_DST, ids=["ends_inside_overlap", "covers_overlap", "starts_inside_overlap"])
def test_gid_dst_overlap_marked_not_dropped(q):
    ds = gid.load_gid(FX / "gid_dst_overlap.csv")
    assert "9300001" in ds.records.index  # preserved, never discarded
    got = gid.count_service_requests(ds, *E.CENTER, 10, *q["window"])
    assert got.ids == q["ids"] and got.uncertain == q["uncertain"]


@pytest.mark.parametrize("name", ["gid_invalid_lat_text", "gid_invalid_lat_range", "gid_invalid_date", "gid_missing_date"])
def test_gid_invalid_rows_fail_whole_import(name):
    with pytest.raises(gid.GidImportError):
        gid.load_gid(FX / f"{name}.csv")


def test_gid_upsert_sequence_reference():
    a, b = gid.load_gid(FX / "gid_upsert_a.csv"), gid.load_gid(FX / "gid_upsert_b.csv")
    merged = pd.concat([a.records, b.records]).drop_duplicates("id", keep="last").set_index("id", drop=False)
    assert merged["status"].to_dict() == E.GID_UPSERT["after_b"]
    assert set(merged.index[merged["lat"].notna()]) == E.GID_UPSERT["located_after_b"]


# ---------------------------------------------------------------- Collisions
@pytest.fixture(scope="module")
def col_basic():
    return C.with_locations(C.load_collisions(FX / "col_basic.csv"), E.COL_POINTS)


def test_collision_import_counts(col_basic):
    spec = E.COL_BASIC
    assert (col_basic.source_rows, col_basic.accepted_rows, col_basic.rejected_rows) == (
        spec["source_rows"], spec["accepted_rows"], spec["rejected_rows"])
    assert len(col_basic.records) == spec["distinct"]
    r = col_basic.records
    assert set(r.index[r["t_early"].isna()]) == spec["null_time"]


@pytest.mark.parametrize("field", ["injured", "killed", "hit_and_run", "hit_run_level"])
def test_collision_field_cleaning(col_basic, field):
    for rid, value in E.COL_BASIC[field].items():
        got = col_basic.records.at[rid, field]
        assert (None if pd.isna(got) else got) == value, rid


def test_collision_street_names(col_basic):
    r = col_basic.records
    assert r.at["7100001", "primary_street"] == "LA JOLLA SCENIC DR"
    assert r.at["7100015", "primary_street"] == "LA JOLLA SCENIC DR"  # whitespace collapsed
    assert r.at["7100003", "intersecting_street"] == "LA JOLLA SCENIC DR"
    assert r.at["7100004", "primary_street"] == "S L"


@pytest.mark.parametrize("f", E.COL_BASIC["filters"], ids=lambda f: f["name"])
def test_collision_filter_reference(col_basic, f):
    got = C.filter_collisions(col_basic, *f.get("window", E.H1), **f["kw"])
    assert got.ids == f["ids"]
    assert got.uncertain == f.get("uncertain", {})


@pytest.mark.parametrize("p", E.COL_BASIC["pages"], ids=lambda p: f"limit{p['limit']}_offset{p['offset']}")
def test_collision_pagination_reference(col_basic, p):
    got = C.get_collisions(col_basic, *E.H1, result_limit=p["limit"], result_offset=p["offset"])
    assert got.ordered == p["ordered"] and got.is_exact()


@pytest.mark.parametrize("limit,offset", [(0, 0), (1001, 0), (10, -1), (10, 100_001)])
def test_collision_page_bounds_rejected(col_basic, limit, offset):
    with pytest.raises(ValueError):
        C.get_collisions(col_basic, *E.H1, result_limit=limit, result_offset=offset)


def test_collision_partial_spatial_arguments_rejected(col_basic):
    with pytest.raises(ValueError):
        C.filter_collisions(col_basic, *E.H1, latitude=32.8, longitude=-117.2)


@pytest.mark.parametrize("key", list(E.COL_BASIC["aggregates"]), ids=lambda k: f"{k[0]}_{k[1]}")
def test_collision_aggregate_reference(col_basic, key):
    period, tz = key
    exact, uncertain = C.aggregate_collisions(col_basic, *E.H1, period=period, calendar_timezone=tz)
    got = {ts.isoformat(): tuple(int(v) for v in row) for ts, row in exact.iterrows()}
    assert got == E.COL_BASIC["aggregates"][key] and uncertain == {}


def test_collision_dst_overlap_reference():
    ds = C.load_collisions(FX / "col_dst_overlap.csv")
    for q in E.COL_DST:
        got = C.filter_collisions(ds, *q["window"])
        assert got.ids == q["ids"] and got.uncertain == q["uncertain"]
    exact, uncertain = C.aggregate_collisions(ds, "2025-11-02T00:00:00-07:00", "2025-11-03T00:00:00-08:00", period="day")
    assert {ts.isoformat(): tuple(int(v) for v in row) for ts, row in exact.iterrows()} == E.COL_DST_DAY
    assert uncertain == {}


@pytest.mark.parametrize("name", ["col_missing_header", "col_bad_columns"])
def test_collision_structural_errors_fail_import(name):
    with pytest.raises(C.CollisionImportError):
        C.load_collisions(FX / f"{name}.csv")
