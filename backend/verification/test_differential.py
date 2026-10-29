"""Differential tests: PostgreSQL query functions vs the independent reference.

Both sides read the same synthetic CSV. PostgreSQL loads it through the
production CLI; the reference parses it with Pandas. Every query is also
checked against the hand-specified expectations in fixtures/expected.py, so a
disagreement can be attributed to the reference or to production.
"""
from __future__ import annotations

import json
import sys

import pandas as pd
import pytest

import dbtools
from fixtures import expected as E
from refimpl import collisions as C
from refimpl import geo, gid
from refimpl.result import Expected

pytestmark = pytest.mark.db
FX = E.FIXTURES
SQL_NAMES = {"radius_m": "radius_meters", "hit_run_level": "hit_run_level_filter"}


def _t(value):
    return pd.Timestamp(value).to_pydatetime()


def _ok(proc):
    assert proc.returncode == 0, proc.stderr[-3000:]


def sql_call(conn, function: str, start, end, columns="report_id", **kw):
    args = {"start": _t(start), "end": _t(end)}
    named = []
    for key, value in kw.items():
        if value is None:
            continue
        name = SQL_NAMES.get(key, key)
        args[name] = value
        named.append(f"{name} => %({name})s")
    sql = f"SELECT {columns} FROM citylens.{function}(%(start)s, %(end)s{''.join(', ' + n for n in named)})"
    return conn.execute(sql, args).fetchall()


def _assert_ids(label, expected: Expected, observed, ds_rows, id_col, params, observe):
    message, record = dbtools.diff_ids(label, expected, observed, ds_rows, id_col, params)
    if record["uncertain"]:
        observe("uncertain", record)
    assert message is None, message


# ===================================================================== GID
@pytest.fixture(scope="module")
def gid_loaded(test_db_url, admin_conn):
    dbtools.truncate_all(admin_conn, dbtools.GID_TABLES)
    _ok(dbtools.load_gid(test_db_url, FX / "gid_basic.csv"))
    _ok(dbtools.load_gid(test_db_url, FX / "gid_dst_overlap.csv"))
    ref_basic = gid.load_gid(FX / "gid_basic.csv")
    ref_dst = gid.load_gid(FX / "gid_dst_overlap.csv")
    return admin_conn, ref_basic, ref_dst


@pytest.mark.parametrize("q", E.GID_BASIC["queries"], ids=lambda q: q["name"])
def test_gid_count_matches_reference(gid_loaded, q, observe):
    conn, ref, _ = gid_loaded
    params = dict(lat=q["lat"], lng=q["lng"], radius=q["radius"], window=q["window"], exclude_linked=q["exclude_linked"])
    expected = gid.count_service_requests(ref, q["lat"], q["lng"], q["radius"], *q["window"], q["exclude_linked"])
    assert (expected.ids, expected.uncertain) == (q["ids"], q["uncertain"]), "reference disagrees with hand-specified output"
    observed = conn.execute("SELECT citylens.count_service_requests(%s, %s, %s, %s, %s, %s)",
                            (q["lat"], q["lng"], q["radius"], _t(q["window"][0]), _t(q["window"][1]),
                             q["exclude_linked"])).fetchone()[0]
    message = dbtools.count_in_range(f"count_service_requests[{q['name']}]", expected, observed, params)
    if expected.uncertain:
        observe("uncertain", {"label": q["name"], "observed_count": observed, "count_range": expected.count_range,
                              "uncertain": expected.uncertain})
    assert message is None, message


@pytest.mark.parametrize("q", E.GID_DST, ids=["ends_inside_overlap", "covers_overlap", "starts_inside_overlap"])
def test_gid_count_dst_overlap(gid_loaded, q, observe):
    conn, _, ref = gid_loaded
    expected = gid.count_service_requests(ref, *E.CENTER, 10, *q["window"])
    observed = conn.execute("SELECT citylens.count_service_requests(%s, %s, 10, %s, %s)",
                            (*E.CENTER, _t(q["window"][0]), _t(q["window"][1]))).fetchone()[0]
    if expected.uncertain:
        observe("uncertain", {"label": f"gid_dst {q['window']}", "observed_count": observed,
                              "count_range": expected.count_range, "uncertain": expected.uncertain})
    assert dbtools.count_in_range("count_service_requests[dst]", expected, observed, {"window": q["window"]}) is None


def test_gid_cli_count_matches_sql(gid_loaded, test_db_url):
    conn, _, _ = gid_loaded
    start, end = E.JAN
    proc = dbtools.run_cli(test_db_url, str(dbtools.BACKEND / "db.py"), "count", "--lat", "32.8", "--lng", "-117.2",
                           "--radius", "800", "--start", start, "--end", end)
    _ok(proc)
    sql = conn.execute("SELECT citylens.count_service_requests(32.8, -117.2, 800, %s, %s)", (_t(start), _t(end))).fetchone()[0]
    assert json.loads(proc.stdout)["count"] == sql, proc.stdout


def test_gid_cli_rejects_timestamp_without_offset(gid_loaded, test_db_url):
    proc = dbtools.run_cli(test_db_url, str(dbtools.BACKEND / "db.py"), "count", "--lat", "32.8", "--lng", "-117.2",
                           "--radius", "800", "--start", "2026-01-01T00:00:00", "--end", "2026-02-01T00:00:00")
    assert proc.returncode != 0, "README: CLI timestamps require explicit UTC offsets"


def test_postgis_distance_agrees_with_independent_geodesic(gid_loaded):
    """Geospatial correctness: PostGIS geography distance vs pyproj, per stored point."""
    conn, ref, _ = gid_loaded
    rows = conn.execute(
        """SELECT service_request_id, ST_Distance(location, ST_SetSRID(ST_MakePoint(-117.2, 32.8), 4326)::geography)
           FROM citylens.service_requests WHERE location IS NOT NULL AND service_request_id LIKE '91%'""").fetchall()
    r = ref.records
    ours = geo.distances_m(32.8, -117.2, r["lat"], r["lng"])
    worst = max(abs(d - ours[rid]) for rid, d in rows)
    assert worst < 0.01, f"PostGIS vs pyproj distance differs by {worst:.4f} m (tolerance 0.01 m)"


@pytest.mark.probe
@pytest.mark.parametrize("args", [
    (32.8, -117.2, -1), (32.8, -117.2, 50_000.001), (91, -117.2, 800), (32.8, -181, 800), (32.8, -117.2, None)],
    ids=["radius_negative", "radius_above_max", "lat_out_of_range", "lng_out_of_range", "radius_null"])
def test_probe_gid_count_invalid_arguments(gid_loaded, args, observe):
    conn, _, _ = gid_loaded
    start, end = E.JAN
    conn.execute("SAVEPOINT p") if not conn.autocommit else None
    try:
        result = conn.execute("SELECT citylens.count_service_requests(%s, %s, %s, %s, %s)",
                              (*args, _t(start), _t(end))).fetchone()[0]
        observe("probe", {"call": f"count_service_requests{args}", "result": result})
    except Exception as exc:  # recorded, not asserted: behavior is undocumented
        observe("probe", {"call": f"count_service_requests{args}", "error": type(exc).__name__, "message": str(exc)[:200]})


@pytest.mark.probe
@pytest.mark.parametrize("window", [(E.JAN[1], E.JAN[0]), (E.JAN[0], E.JAN[0])], ids=["reversed", "empty"])
def test_probe_gid_degenerate_window(gid_loaded, window, observe):
    conn, _, _ = gid_loaded
    call = f"count_service_requests(start={window[0]}, end={window[1]})"
    try:
        result = conn.execute("SELECT citylens.count_service_requests(32.8, -117.2, 800, %s, %s)",
                              (_t(window[0]), _t(window[1]))).fetchone()[0]
        observe("probe", {"call": call, "result": result})
    except Exception as exc:
        observe("probe", {"call": call, "error": type(exc).__name__, "message": str(exc)[:200]})


# =============================================================== Collisions
@pytest.fixture(scope="module")
def col_loaded(test_db_url, admin_conn):
    dbtools.truncate_all(admin_conn, dbtools.COLLISION_TABLES)
    _ok(dbtools.load_collisions(test_db_url, FX / "col_basic.csv"))
    _ok(dbtools.load_collisions(test_db_url, FX / "col_dst_overlap.csv"))
    dbtools.set_locations(admin_conn, E.COL_POINTS)
    ref = C.with_locations(C.load_collisions(FX / "col_basic.csv"), E.COL_POINTS)
    return admin_conn, ref, C.load_collisions(FX / "col_dst_overlap.csv")


@pytest.mark.parametrize("f", E.COL_BASIC["filters"], ids=lambda f: f["name"])
def test_filter_matches_reference(col_loaded, f, observe):
    conn, ref, _ = col_loaded
    window = f.get("window", E.H1)
    expected = C.filter_collisions(ref, *window, **f["kw"])
    assert (expected.ids, expected.uncertain) == (f["ids"], f.get("uncertain", {})), "reference vs hand spec"
    rows = sql_call(conn, "filter_traffic_collisions", *window, **f["kw"])
    _assert_ids(f"filter_traffic_collisions[{f['name']}]", expected, [r[0] for r in rows],
                ref.rows, "REPORT_ID", {"window": window, **f["kw"]}, observe)


@pytest.mark.parametrize("f", E.COL_BASIC["filters"], ids=lambda f: f["name"])
def test_get_matches_filter_set(col_loaded, f, observe):
    """The retrieval tool must return the same set as the shared filter (single page)."""
    conn, ref, _ = col_loaded
    window = f.get("window", E.H1)
    expected = C.get_collisions(ref, *window, result_limit=1000, **f["kw"])
    rows = sql_call(conn, "get_traffic_collisions", *window, result_limit=1000, **f["kw"])
    _assert_ids(f"get_traffic_collisions[{f['name']}]", expected, [r[0] for r in rows],
                ref.rows, "REPORT_ID", {"window": window, **f["kw"]}, observe)


@pytest.mark.parametrize("p", E.COL_BASIC["pages"], ids=lambda p: f"limit{p['limit']}_offset{p['offset']}")
def test_pagination_order(col_loaded, p):
    conn, ref, _ = col_loaded
    expected = C.get_collisions(ref, *E.H1, result_limit=p["limit"], result_offset=p["offset"])
    assert expected.ordered == p["ordered"]
    rows = sql_call(conn, "get_traffic_collisions", *E.H1, result_limit=p["limit"], result_offset=p["offset"])
    observed = [r[0] for r in rows]
    assert observed == p["ordered"], (f"MISMATCH page limit={p['limit']} offset={p['offset']}\n"
                                      f"  expected order: {p['ordered']}\n  observed order: {observed}")


def test_get_returns_full_rows(col_loaded):
    conn, _, _ = col_loaded
    rows = sql_call(conn, "get_traffic_collisions", *E.H1, columns="report_id, raw_record, geocode_status, location IS NOT NULL",
                    street_name="LA JOLLA SCENIC DR")
    by_id = {r[0]: r for r in rows}
    assert by_id["7100001"][1]["REPORT_ID"] == "7100001"
    assert by_id["7100001"][2] == "matched" and by_id["7100001"][3] is True


def _aggregate_rows(conn, period, tz, window=E.H1, **kw):
    rows = sql_call(conn, "aggregate_traffic_collisions", *window, columns="period_start, collision_count, located_count, "
                    "unknown_injured_count, unknown_killed_count", period=period, calendar_timezone=tz, **kw)
    return {pd.Timestamp(r[0]).tz_convert("UTC").isoformat(): tuple(int(v) for v in r[1:]) for r in rows}


@pytest.mark.parametrize("key", list(E.COL_BASIC["aggregates"]), ids=lambda k: f"{k[0]}_{k[1]}")
def test_aggregate_matches_reference(col_loaded, key):
    conn, ref, _ = col_loaded
    period, tz = key
    exact, uncertain = C.aggregate_collisions(ref, *E.H1, period=period, calendar_timezone=tz)
    expected = {ts.isoformat(): tuple(int(v) for v in row) for ts, row in exact.iterrows()}
    assert expected == E.COL_BASIC["aggregates"][key] and not uncertain
    observed = _aggregate_rows(conn, period, tz)
    assert observed == expected, (f"MISMATCH aggregate period={period} tz={tz}\n"
                                  f"  expected: {expected}\n  observed: {observed}")


@pytest.mark.parametrize("f", [f for f in E.COL_BASIC["filters"] if "window" not in f and not f.get("uncertain")],
                         ids=lambda f: f["name"])
def test_aggregate_total_equals_filter_count(col_loaded, f):
    """Aggregation counts all matches, independent of retrieval pagination."""
    conn, _, _ = col_loaded
    total = sum(v[0] for v in _aggregate_rows(conn, "year", "America/Los_Angeles", **f["kw"]).values())
    assert total == len(f["ids"])


def test_dst_overlap_filter_and_aggregate(col_loaded, observe):
    conn, _, ref = col_loaded
    for q in E.COL_DST:
        expected = C.filter_collisions(ref, *q["window"])
        rows = sql_call(conn, "filter_traffic_collisions", *q["window"])
        _assert_ids(f"filter[dst {q['window']}]", expected, [r[0] for r in rows], ref.rows, "REPORT_ID",
                    {"window": q["window"]}, observe)
    observed = _aggregate_rows(conn, "day", "America/Los_Angeles",
                               window=("2025-11-02T00:00:00-07:00", "2025-11-03T00:00:00-08:00"))
    assert observed == E.COL_DST_DAY


@pytest.mark.probe
@pytest.mark.parametrize("kw", [
    dict(result_limit=0), dict(result_limit=1001), dict(result_offset=-1), dict(result_offset=100_001),
    dict(latitude=32.8, longitude=-117.2), dict(latitude=32.8, longitude=-117.2, radius_m=50_001),
    dict(min_injured=3, max_injured=1), dict(min_injured=-1)],
    ids=["limit0", "limit1001", "offset_neg", "offset_above", "partial_spatial", "radius_above", "min_gt_max", "negative_min"])
def test_probe_collision_invalid_arguments(col_loaded, kw, observe):
    conn, _, _ = col_loaded
    try:
        n = len(sql_call(conn, "get_traffic_collisions", *E.H1, **kw))
        observe("probe", {"call": f"get_traffic_collisions{kw}", "rows": n})
    except Exception as exc:
        observe("probe", {"call": f"get_traffic_collisions{kw}", "error": type(exc).__name__, "message": str(exc)[:200]})


@pytest.mark.probe
@pytest.mark.parametrize("period", ["hour", "Month"])
def test_probe_aggregate_unsupported_period(col_loaded, period, observe):
    conn, _, _ = col_loaded
    try:
        observe("probe", {"call": f"aggregate period={period}", "rows": _aggregate_rows(conn, period, "UTC")})
    except Exception as exc:
        observe("probe", {"call": f"aggregate period={period}", "error": type(exc).__name__, "message": str(exc)[:200]})


# ------------------------------------------------- documented Python wrappers
@pytest.fixture(scope="module")
def wrappers():
    sys.path.insert(0, str(dbtools.BACKEND))
    import collision_queries  # documented public API, used as a black box
    return collision_queries


def test_wrapper_street_name_normalized(col_loaded, wrappers, test_db_url):
    import psycopg
    conn, ref, _ = col_loaded
    expected = C.filter_collisions(ref, *E.H1, street_name="LA JOLLA SCENIC DR")
    with psycopg.connect(test_db_url) as wconn:
        rows = wrappers.get_collisions(wconn, *E.H1, street_name="La Jolla Scenic Drive")
    ids = {(r["report_id"] if isinstance(r, dict) else r[0]) for r in rows}
    assert ids == expected.ids


def test_wrapper_rejects_naive_timestamps(col_loaded, wrappers, test_db_url):
    import psycopg
    with psycopg.connect(test_db_url) as wconn, pytest.raises(Exception):
        wrappers.get_collisions(wconn, "2026-01-01T00:00:00", "2026-07-01T00:00:00")


def test_wrapper_aggregate_matches_sql(col_loaded, wrappers, test_db_url):
    import psycopg
    conn, _, _ = col_loaded
    with psycopg.connect(test_db_url) as wconn:
        rows = wrappers.aggregate_collisions(wconn, *E.H1, period="month", hit_run_level="FELONY")
    def as_tuple(r):
        r = r if isinstance(r, dict) else dict(zip(["period_start", "collision_count", "located_count",
                                                     "unknown_injured_count", "unknown_killed_count"], r))
        return pd.Timestamp(r["period_start"]).tz_convert("UTC").isoformat(), int(r["collision_count"])
    got = dict(as_tuple(r) for r in rows)
    sql = {k: v[0] for k, v in _aggregate_rows(conn, "month", "America/Los_Angeles", hit_run_level="FELONY").items()}
    assert got == sql
