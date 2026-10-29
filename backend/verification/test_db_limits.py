"""Database integration: retrieval cap and pagination completeness on 1,105 rows."""
import csv

import pandas as pd
import pytest

import dbtools
from refimpl import collisions as C
from test_differential import _ok, sql_call

pytestmark = pytest.mark.db


def test_limit_capped_at_1000(test_db_url, admin_conn, tmp_path):
    """README: 'at most 1,000 per call'. 1,105 generated rows; ask for more than the cap."""
    path = tmp_path / "col_many.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(C.SOURCE_COLUMNS)
        for i in range(1105):
            w.writerow([f"{8000000 + i}", f"2026-05-01 {i // 60 % 24:02d}:{i % 60:02d}:00.000", "811", "100", "",
                        "TEST", "STREET", "", "", "", "1", "VC", "X", "0", "0", ""])
    dbtools.truncate_all(admin_conn, dbtools.COLLISION_TABLES)
    _ok(dbtools.load_collisions(test_db_url, path))
    window = ("2026-05-01T00:00:00-07:00", "2026-05-02T00:00:00-07:00")
    assert len(sql_call(admin_conn, "get_traffic_collisions", *window, result_limit=1000)) == 1000
    assert len(sql_call(admin_conn, "filter_traffic_collisions", *window)) == 1105
    try:
        n = len(sql_call(admin_conn, "get_traffic_collisions", *window, result_limit=5000))
    except Exception:
        n = 0  # rejecting the request also honors the cap
    assert n <= 1000
    pages = [r[0] for off in range(0, 1105, 500)
             for r in sql_call(admin_conn, "get_traffic_collisions", *window, result_limit=500, result_offset=off)]
    assert len(pages) == len(set(pages)) == 1105, "paging must cover every match exactly once"
