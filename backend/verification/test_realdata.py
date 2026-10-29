"""Full raw-data differential (opt-in: pytest --run-realdata).

Loads both raw CSVs from backend/Raw Data into the isolated test database
through the production CLIs, then compares stored rows and query results with
the reference reading the same files. Records whose expectation depends on an
undocumented rule (DST ambiguity, radius boundary band, suffix spelling) are
reported in results/observations.json rather than asserted.
"""
import itertools

import numpy as np
import pandas as pd
import pytest

import dbtools
from refimpl import collisions as C
from refimpl import geo, gid
from test_differential import sql_call

pytestmark = [pytest.mark.db, pytest.mark.realdata]
RAW = dbtools.BACKEND / "Raw Data"
GID_CSV = RAW / "Get it done" / "get_it_done_requests_CD1_datasd.csv"
COL_CSV = RAW / "Traffic collision" / "pd_collisions_datasd.csv"


def _t(v):
    return pd.Timestamp(v).to_pydatetime()


@pytest.fixture(scope="module")
def real(test_db_url, admin_conn):
    dbtools.truncate_all(admin_conn)
    for proc in (dbtools.load_gid(test_db_url, GID_CSV), dbtools.load_collisions(test_db_url, COL_CSV)):
        assert proc.returncode == 0, proc.stderr[-3000:]
    return admin_conn, gid.load_gid(GID_CSV), C.load_collisions(COL_CSV)


def _mismatch_report(label, frame, mask, columns, limit=25):
    bad = frame[mask]
    return f"{label}: {int(mask.sum())} mismatching rows; first {limit}:\n" + bad[columns].head(limit).to_string()


# ===================================================================== GID
def test_gid_stored_rows_match_reference(real, observe):
    conn, ref, _ = real
    rows = conn.execute("""SELECT service_request_id, requested_at, parent_id, status,
                                  ST_Y(location::geometry), ST_X(location::geometry)
                           FROM citylens.service_requests""").fetchall()
    db = pd.DataFrame(rows, columns=["id", "requested_at", "parent_id", "status", "lat", "lng"]).set_index("id")
    r = ref.records
    assert set(db.index) == set(r.index), (
        f"ID sets differ: missing {sorted(set(r.index) - set(db.index))[:20]}, extra {sorted(set(db.index) - set(r.index))[:20]}")
    m = r.join(db.rename(columns={c: f"{c}_db" for c in ("parent_id", "status", "lat", "lng")}))
    m["requested_at"] = pd.to_datetime(m["requested_at"], utc=True)
    ok = m["time_status"] == "ok"
    bad_time = ok & (m["requested_at"] != m["t_early"])
    amb = m[~ok]
    resolved = np.where(amb["requested_at"] == amb["t_late"], "later (standard time)",
                        np.where(amb["requested_at"] == amb["t_early"], "earlier (daylight time)", "neither candidate"))
    observe("ambiguous_time", {"table": "service_requests", "rows": len(amb),
                               "resolution_counts": pd.Series(resolved).value_counts().to_dict()})
    bad_loc = (m["lat"].isna() != m["lat_db"].isna()) | ((m["lat"] - m["lat_db"]).abs() > 1e-9) | ((m["lng"] - m["lng_db"]).abs() > 1e-9)
    bad_parent = m["parent_id"].fillna("") != m["parent_id_db"].fillna("")
    bad_status = m["status"] != m["status_db"]
    problems = [_mismatch_report(n, m, k, c) for n, k, c in [
        ("requested_at", bad_time, ["t_early", "requested_at"]), ("location", bad_loc, ["lat", "lat_db", "lng", "lng_db"]),
        ("parent_id", bad_parent, ["parent_id", "parent_id_db"]), ("status", bad_status, ["status", "status_db"])] if k.any()]
    assert "neither candidate" not in set(resolved)
    assert not problems, "\n\n".join(problems)


def test_gid_duplicate_5449467_last_wins(real):
    conn, _, _ = real
    status, at = conn.execute("SELECT status, requested_at FROM citylens.service_requests "
                              "WHERE service_request_id = '5449467'").fetchone()
    assert status == "In Process" and pd.Timestamp(at).second == 34


CENTERS = [(32.8, -117.2), (32.7978, -117.2400), (32.8473, -117.2742), (32.8328, -117.2713)]
RADII = [0, 100, 800, 5000, 50_000]
WINDOWS = [("2020-12-01T00:00:00-08:00", "2021-01-01T00:00:00-08:00"),
           ("2025-11-01T00:00:00-07:00", "2025-12-01T00:00:00-08:00"),
           ("2026-01-01T00:00:00-08:00", "2026-02-01T00:00:00-08:00"),
           ("2015-01-01T00:00:00Z", "2027-01-01T00:00:00Z")]


def test_gid_count_grid(real, observe):
    conn, ref, _ = real
    failures, checked = [], 0
    for (lat, lng), radius, window, linked in itertools.product(CENTERS, RADII, WINDOWS, (False, True)):
        expected = gid.count_service_requests(ref, lat, lng, radius, *window, linked)
        observed = conn.execute("SELECT citylens.count_service_requests(%s,%s,%s,%s,%s,%s)",
                                (lat, lng, radius, _t(window[0]), _t(window[1]), linked)).fetchone()[0]
        params = dict(lat=lat, lng=lng, radius=radius, window=window, exclude_linked=linked)
        checked += 1
        if expected.uncertain:
            observe("uncertain", {"label": "gid_count_grid", "params": params, "observed": observed,
                                  "count_range": expected.count_range, "uncertain": expected.uncertain})
        msg = dbtools.count_in_range("count_service_requests[real]", expected, observed, params)
        if msg:
            failures.append(msg)
    assert checked == 160
    assert not failures, f"{len(failures)} of {checked} queries mismatched\n" + "\n".join(failures[:10])


# =============================================================== Collisions
def test_collision_stored_rows_match_reference(real, observe):
    conn, _, ref = real
    rows = conn.execute("SELECT report_id, occurred_at, injured, killed, hit_and_run, hit_run_level, primary_street "
                        "FROM citylens.traffic_collisions").fetchall()
    db = pd.DataFrame(rows, columns=["id", "occurred_at", "injured_db", "killed_db", "hr_db", "level_db", "street_db"]).set_index("id")
    r = ref.records
    assert set(db.index) == set(r.index)
    m = r.join(db)
    m["occurred_at"] = pd.to_datetime(m["occurred_at"], utc=True)
    ok = m["time_status"] == "ok"
    amb = m[m["time_status"] == "ambiguous"]
    observe("ambiguous_time", {"table": "traffic_collisions", "rows": len(amb),
                               "ids": amb.index.tolist(),
                               "stored_later_candidate": int((amb["occurred_at"] == amb["t_late"]).sum())})
    checks = {
        "occurred_at": ok & (m["occurred_at"] != m["t_early"]),
        "injured": m["injured"].astype("float") .fillna(-1) != m["injured_db"].astype("float").fillna(-1),
        "killed": m["killed"].astype("float").fillna(-1) != m["killed_db"].astype("float").fillna(-1),
        "hit_and_run": m["hit_and_run"].astype("object").fillna("NULL") != m["hr_db"].astype("object").fillna("NULL"),
        "hit_run_level": m["hit_run_level"].fillna("") != m["level_db"].fillna(""),
    }
    # street names: strict only where the spelling is documented (blank suffix or DRIVE)
    documented = m["street_candidates"].map(lambda c: len(c[0]) == 1)
    checks["primary_street(documented suffixes)"] = documented & (m["primary_street"] != m["street_db"])
    usps_disagree = ~documented & (m["primary_street"] != m["street_db"])
    observe("probe", {"question": "primary_street vs USPS-abbreviation assumption (undocumented suffixes)",
                      "rows": int((~documented).sum()), "disagreements": int(usps_disagree.sum()),
                      "examples": m.loc[usps_disagree, ["primary_street", "street_db"]].drop_duplicates().head(30).values.tolist()})
    problems = [_mismatch_report(name, m, mask, [c for c in m.columns if c in ("t_early", "occurred_at", "injured", "injured_db",
                "killed", "killed_db", "hit_and_run", "hr_db", "hit_run_level", "level_db", "primary_street", "street_db")])
                for name, mask in checks.items() if mask.any()]
    assert not problems, "\n\n".join(problems)


def test_collision_import_counts(real):
    conn, _, ref = real
    source, accepted, rejected = conn.execute(
        "SELECT source_rows, accepted_rows, rejected_rows FROM citylens.collision_imports ORDER BY id DESC LIMIT 1").fetchone()
    assert (source, accepted, rejected) == (ref.source_rows, ref.accepted_rows, ref.rejected_rows)
    archived = conn.execute("SELECT count(*) FROM citylens.collision_source_rows").fetchone()[0]
    assert archived == ref.source_rows


FILTERS = [
    {}, dict(min_injured=1), dict(min_killed=1), dict(max_injured=0, max_killed=0), dict(min_injured=2, max_injured=2),
    dict(hit_run=True), dict(hit_run=False), dict(hit_run_level="FELONY"), dict(hit_run_level="MISDEMEANOR"),
    dict(street_name="LA JOLLA SCENIC DR"), dict(street_name="CAM RUIZ"), dict(street_name="GARNET AVE"),
    dict(hit_run=True, min_injured=1),
]
COL_WINDOWS = [("2015-01-01T00:00:00Z", "2027-01-01T00:00:00Z"),
               ("2016-11-06T00:00:00-07:00", "2016-11-07T00:00:00-08:00"),
               ("2016-11-06T08:30:00Z", "2016-11-06T09:30:00Z"),
               ("2024-01-01T00:00:00-08:00", "2025-01-01T00:00:00-08:00")]


@pytest.mark.parametrize("window", COL_WINDOWS, ids=["all", "dst_day_2016", "dst_hour_2016", "y2024"])
def test_collision_filter_grid(real, window, observe):
    conn, _, ref = real
    failures = []
    for kw in FILTERS:
        expected = C.filter_collisions(ref, *window, **kw)
        observed = [r[0] for r in sql_call(conn, "filter_traffic_collisions", *window, **kw)]
        msg, record = dbtools.diff_ids(f"filter_traffic_collisions[real]", expected, observed, ref.rows,
                                       "REPORT_ID", {"window": window, **kw})
        if record["uncertain"]:
            observe("uncertain", {"label": "collision_filter_grid", "params": record["params"],
                                  "uncertain_count": len(record["uncertain"]),
                                  "observed_present": sum(v["observed"] for v in record["uncertain"].values()),
                                  "sample": dict(list(record["uncertain"].items())[:10])})
        if msg:
            failures.append(msg)
    assert not failures, "\n\n".join(f[:4000] for f in failures)


@pytest.mark.parametrize("period,tz", [(p, tz) for p in C.PERIODS if p != "day" for tz in ("America/Los_Angeles", "UTC")] +
                         [("day", "America/Los_Angeles")])
def test_collision_aggregate_grid(real, period, tz, observe):
    conn, _, ref = real
    window = COL_WINDOWS[0]
    exact, uncertain = C.aggregate_collisions(ref, *window, period=period, calendar_timezone=tz)
    rows = sql_call(conn, "aggregate_traffic_collisions", *window, columns="period_start, collision_count, located_count, "
                    "unknown_injured_count, unknown_killed_count", period=period, calendar_timezone=tz)
    observed = pd.DataFrame(rows, columns=["period_start", *exact.columns])
    observed["period_start"] = pd.to_datetime(observed["period_start"], utc=True)
    observed = observed.set_index("period_start").sort_index().astype("int64")
    # uncertain rows may land in any of their candidate periods
    slack = pd.Series(0, index=observed.index.union(exact.index))
    for candidates in uncertain.values():
        for p in candidates:
            slack[p] = slack.get(p, 0) + 1
    if uncertain:
        observe("uncertain", {"label": f"aggregate {period} {tz}", "rows": len(uncertain)})
    exp = exact.reindex(slack.index, fill_value=0)
    obs = observed.reindex(slack.index, fill_value=0)
    diff = obs["collision_count"] - exp["collision_count"]
    bad = (diff < 0) | (diff > slack)
    if not uncertain:
        bad |= (obs != exp).any(axis=1)
    assert not bad.any(), "aggregate mismatch:\n" + pd.concat({"expected": exp[bad], "observed": obs[bad]}, axis=1).to_string()
