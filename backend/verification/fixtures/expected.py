"""Manually specified expected outputs for the synthetic fixtures.

Every value here was worked out by hand from the fixture rows and the
documented contract; none is produced by running either implementation.
Geometry: center C = (32.8, -117.2). Point offsets were placed with WGS84
geodesics (e.g. 'in799' = 799 m north of C); see make-up notes per ID.
"""
FIXTURES = __import__("pathlib").Path(__file__).resolve().parent
CENTER = (32.8, -117.2)
JAN = ("2026-01-01T00:00:00-08:00", "2026-02-01T00:00:00-08:00")

# --------------------------------------------------------------------------
# Get It Done: gid_basic.csv
# 9100001 C (0 m)            9100002 799 m N         9100003 800 m E (edge)
# 9100004 801 m S            9100005 5000 m NE       9100006 799 m W
# 9100007 no location        9100008 100 m, parent 9100001 (in file)
# 9100009 200 m, parent 8000000 (outside file)
# 9100010 10 m at Jan 1 00:00 local (window start, inclusive)
# 9100011 10 m at Dec 31 23:59:59 (before window)
# 9100012 10 m at Feb 1 00:00 (window end, exclusive)
# 9100013 10 m at Jan 31 23:59:59 (last second inside)
# 9100014 first row 300 m, LAST row 1200 m  -> last wins: outside 800 m
# 9100015 first row 1200 m, LAST row 300 m  -> last wins: inside 800 m
# 9100016 49,999 m   9100017 50,001 m   9100018 50,000 m (edge)
# 9100019 0.3 m      9100020 1.0 m
# --------------------------------------------------------------------------
GID_BASIC = {
    "source_rows": 22,
    "distinct_requests": 20,
    "no_location": {"9100007"},
    "with_parent": {"9100008", "9100009"},
    "last_wins_status": {"9100014": "In Process", "9100015": "In Process"},
    "queries": [
        dict(name="jan_800m", lat=32.8, lng=-117.2, radius=800, window=JAN, exclude_linked=False,
             ids={"9100001", "9100002", "9100006", "9100008", "9100009", "9100010", "9100013",
                  "9100015", "9100019", "9100020"},
             uncertain={"9100003": "radius_boundary"}),
        dict(name="jan_800m_exclude_linked", lat=32.8, lng=-117.2, radius=800, window=JAN, exclude_linked=True,
             ids={"9100001", "9100002", "9100006", "9100010", "9100013", "9100015", "9100019", "9100020"},
             uncertain={"9100003": "radius_boundary"}),
        dict(name="jan_50km", lat=32.8, lng=-117.2, radius=50_000, window=JAN, exclude_linked=False,
             ids={"9100001", "9100002", "9100003", "9100004", "9100005", "9100006", "9100008", "9100009",
                  "9100010", "9100013", "9100014", "9100015", "9100016", "9100019", "9100020"},
             uncertain={"9100018": "radius_boundary"}),
        dict(name="jan_radius_0", lat=32.8, lng=-117.2, radius=0, window=JAN, exclude_linked=False,
             ids={"9100001"}, uncertain={"9100019": "radius_boundary"}),
        dict(name="jan_801m_edge_point_inside", lat=32.8, lng=-117.2, radius=801, window=JAN, exclude_linked=False,
             ids={"9100001", "9100002", "9100003", "9100006", "9100008", "9100009", "9100010", "9100013",
                  "9100015", "9100019", "9100020"},
             uncertain={"9100004": "radius_boundary"}),
        dict(name="exact_instant_window", lat=32.8, lng=-117.2, radius=800,
             window=("2026-01-15T10:00:00-08:00", "2026-01-15T10:00:01-08:00"), exclude_linked=False,
             ids={"9100001", "9100002", "9100006", "9100008", "9100009", "9100019", "9100020"},
             uncertain={"9100003": "radius_boundary"}),
    ],
    # reference-only category/status filters (no production tool exists)
    "category_status": [
        dict(service_names={"Graffiti"}, statuses=None, ids={"9100002", "9100005"}),
        dict(service_names=None, statuses={"In Process"}, ids={"9100002", "9100014", "9100015"}),
        dict(service_names={""}, statuses={"Referred"}, ids={"9100006"}),
    ],
}

# gid_upsert_a.csv then gid_upsert_b.csv
GID_UPSERT = {
    "after_a": {"9200001": "Closed", "9200002": "New", "9200003": "New"},
    "after_b": {"9200001": "Closed", "9200002": "Closed", "9200003": "In Process", "9200004": "Closed"},
    "located_after_a": {"9200001", "9200002"},
    "located_after_b": {"9200001", "9200002", "9200003", "9200004"},
}

# gid_dst_overlap.csv: 9300001 at 01:30 on 2025-11-02 (repeated hour:
# 08:30Z if PDT, 09:30Z if PST); 9300002 at 00:30 PDT = 07:30Z;
# 9300003 at 02:30 PST = 10:30Z. All at C.
GID_DST = [
    dict(window=("2025-11-02T07:00:00Z", "2025-11-02T09:00:00Z"),
         ids={"9300002"}, uncertain={"9300001": "time_ambiguous"}),
    dict(window=("2025-11-02T07:00:00Z", "2025-11-02T10:00:00Z"),
         ids={"9300001", "9300002"}, uncertain={}),
    dict(window=("2025-11-02T09:00:00Z", "2025-11-02T11:00:00Z"),
         ids={"9300003"}, uncertain={"9300001": "time_ambiguous"}),
]

# --------------------------------------------------------------------------
# Traffic collisions: col_basic.csv (window H1 = Jan 1 - Jul 1 2026, local)
# Synthetic geocoded points attached for spatial tests (COL_POINTS).
# --------------------------------------------------------------------------
H1 = ("2026-01-01T00:00:00-08:00", "2026-07-01T00:00:00-07:00")
COL_POINTS = {
    "7100001": (32.80000000, -117.20000000),  # C
    "7100002": (32.80720463, -117.20000000),  # 799 m
    "7100003": (32.79999971, -117.19145879),  # 800 m edge
    "7100004": (32.79277733, -117.20000000),  # 801 m
    "7100006": (32.80135253, -117.19722612),  # 300 m
    "7100015": (32.80009017, -117.20000000),  # 10 m
}
H1_ALL = ["7100011", "7100001", "7100015", "7100002", "7100003", "7100004",
          "7100010", "7100005", "7100006", "7100007", "7100014"]  # documented order
COL_BASIC = {
    "source_rows": 17, "accepted_rows": 16, "rejected_rows": 1, "distinct": 15,
    "null_time": {"7100008", "7100009"},
    "injured": {"7100001": 1, "7100002": 0, "7100003": 2, "7100004": None, "7100005": 0,
                "7100006": None, "7100007": 3, "7100008": 1, "7100009": 1, "7100010": 0,
                "7100011": 0, "7100012": 0, "7100013": 0, "7100014": 10, "7100015": 0},
    "killed": {"7100001": 0, "7100002": 0, "7100003": 1, "7100004": 0, "7100005": None,
               "7100006": None, "7100007": 0, "7100014": 2},
    "hit_and_run": {"7100001": True, "7100002": False, "7100003": True, "7100005": None,
                    "7100006": True, "7100010": False},
    "hit_run_level": {"7100001": "FELONY", "7100002": None, "7100005": "UNKNOWN", "7100010": None},
    "filters": [
        dict(name="date_only", kw={}, ids=set(H1_ALL)),
        dict(name="radius_800", kw=dict(latitude=32.8, longitude=-117.2, radius_m=800),
             ids={"7100001", "7100002", "7100006", "7100015"}, uncertain={"7100003": "radius_boundary"}),
        dict(name="min_injured_1", kw=dict(min_injured=1), ids={"7100001", "7100003", "7100007", "7100014"}),
        dict(name="max_injured_0", kw=dict(max_injured=0), ids={"7100002", "7100005", "7100010", "7100011", "7100015"}),
        dict(name="injured_exactly_2", kw=dict(min_injured=2, max_injured=2), ids={"7100003"}),
        dict(name="min_killed_1", kw=dict(min_killed=1), ids={"7100003", "7100014"}),
        dict(name="max_killed_0", kw=dict(max_killed=0),
             ids={"7100001", "7100002", "7100004", "7100007", "7100010", "7100011", "7100015"}),
        dict(name="hit_run_true", kw=dict(hit_run=True), ids={"7100001", "7100003", "7100006", "7100007", "7100014"}),
        dict(name="hit_run_false", kw=dict(hit_run=False), ids={"7100002", "7100004", "7100010", "7100011", "7100015"}),
        dict(name="level_felony", kw=dict(hit_run_level="FELONY"), ids={"7100001", "7100006", "7100014"}),
        dict(name="level_misdemeanor", kw=dict(hit_run_level="MISDEMEANOR"), ids={"7100003", "7100007"}),
        dict(name="level_unknown_label", kw=dict(hit_run_level="UNKNOWN"), ids={"7100005"}),
        dict(name="street_documented", kw=dict(street_name="LA JOLLA SCENIC DR"), ids={"7100001", "7100003", "7100015"}),
        dict(name="street_direction_no_suffix", kw=dict(street_name="S L"), ids={"7100004"}),
        dict(name="street_undocumented_suffix", kw=dict(street_name="W MARKET ST"),
             ids=set(), uncertain={"7100014": "street_spelling_undocumented"}),
        dict(name="radius_and_hit_run", kw=dict(latitude=32.8, longitude=-117.2, radius_m=800, hit_run=True),
             ids={"7100001", "7100006"}, uncertain={"7100003": "radius_boundary"}),
        dict(name="end_exclusive", kw={}, window=("2026-01-01T00:00:00-08:00", "2026-01-05T08:00:00-08:00"),
             ids={"7100011"}),
        dict(name="start_inclusive", kw={}, window=("2026-01-05T08:00:00-08:00", "2026-01-05T09:00:00-08:00"),
             ids={"7100001", "7100015"}),
    ],
    "pages": [
        dict(limit=100, offset=0, ordered=H1_ALL),
        dict(limit=3, offset=2, ordered=["7100015", "7100002", "7100003"]),
        dict(limit=1, offset=10, ordered=["7100014"]),
        dict(limit=5, offset=11, ordered=[]),
    ],
    # period_start (UTC ISO) -> (collision, located, unknown_injured, unknown_killed); located uses COL_POINTS
    "aggregates": {
        ("month", "America/Los_Angeles"): {
            "2026-01-01T08:00:00+00:00": (8, 5, 1, 1), "2026-02-01T08:00:00+00:00": (1, 1, 1, 1),
            "2026-03-01T08:00:00+00:00": (1, 0, 0, 0), "2026-04-01T07:00:00+00:00": (1, 0, 0, 0)},
        ("month", "UTC"): {
            "2026-01-01T00:00:00+00:00": (7, 5, 1, 0), "2026-02-01T00:00:00+00:00": (2, 1, 1, 2),
            "2026-04-01T00:00:00+00:00": (2, 0, 0, 0)},
        ("quarter", "America/Los_Angeles"): {
            "2026-01-01T08:00:00+00:00": (10, 6, 2, 2), "2026-04-01T07:00:00+00:00": (1, 0, 0, 0)},
        ("year", "America/Los_Angeles"): {"2026-01-01T08:00:00+00:00": (11, 6, 2, 2)},
        ("week", "America/Los_Angeles"): {
            "2025-12-29T08:00:00+00:00": (1, 0, 0, 0), "2026-01-05T08:00:00+00:00": (4, 4, 0, 0),
            "2026-01-12T08:00:00+00:00": (1, 1, 1, 0), "2026-01-19T08:00:00+00:00": (1, 0, 0, 0),
            "2026-01-26T08:00:00+00:00": (1, 0, 0, 1), "2026-02-09T08:00:00+00:00": (1, 1, 1, 1),
            "2026-03-30T07:00:00+00:00": (1, 0, 0, 0), "2026-04-06T07:00:00+00:00": (1, 0, 0, 0)},
    },
}

# col_dst_overlap.csv: same layout as GID_DST
COL_DST = [
    dict(window=("2025-11-02T07:00:00Z", "2025-11-02T09:00:00Z"),
         ids={"7300002"}, uncertain={"7300001": "time_ambiguous"}),
    dict(window=("2025-11-02T07:00:00Z", "2025-11-02T10:00:00Z"), ids={"7300001", "7300002"}, uncertain={}),
]
COL_DST_DAY = {"2025-11-02T07:00:00+00:00": (3, 0, 0, 0)}  # window covers whole local day
