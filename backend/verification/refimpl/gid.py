"""Get It Done reference: CSV cleaning, deduplication and request counting.

Contract sources: backend/README.md ("Semantics and limitations") and
backend/sql/get_it_done.sql. Rules marked [ASSUMPTION] are not stated in the
documentation; each is listed in AMBIGUITIES.md and only exercised by probe
tests, never by strict differential assertions.

Cleaning rules
--------------
* service_request_id: whitespace-trimmed [ASSUMPTION]; empty -> import fails
  [ASSUMPTION: docs are silent; the column is the NOT NULL primary key].
* date_requested: Los Angeles wall time (documented assumption). Missing or
  unparseable -> the whole import fails (README: "Invalid ... dates fail the
  import"; requested_at is NOT NULL).
* lat/lng: both blank -> no location (retained, excluded from spatial queries).
  Non-numeric, out of WGS84 range, or only one of the pair -> the whole import
  fails (README: "Invalid coordinates ... fail the import"). One-sided pairs
  are [ASSUMPTION].
* service_request_parent_id: blank -> NULL; a parent may be outside the file.
* Repeated IDs: last CSV occurrence wins (README).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import geo, timeutil
from .result import Expected, merge_reasons

ID, PARENT, DATE, LAT, LNG = "service_request_id", "service_request_parent_id", "date_requested", "lat", "lng"


class GidImportError(ValueError):
    """The reference expects the whole import to fail and roll back."""


@dataclass
class GidDataset:
    rows: pd.DataFrame      # every CSV row as original strings, plus row_number
    records: pd.DataFrame   # one row per request ID (last occurrence), cleaned

    @property
    def source_rows(self) -> int:
        return len(self.rows)

    @property
    def distinct_requests(self) -> int:
        return len(self.records)


def read_raw(path: str | Path) -> pd.DataFrame:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        data = [row for row in reader]
    for number, row in enumerate(data, start=1):
        if len(row) != len(header):
            raise GidImportError(f"row {number}: {len(row)} fields, expected {len(header)}")
    frame = pd.DataFrame(data, columns=header, dtype=object)
    frame.insert(0, "row_number", range(1, len(frame) + 1))
    return frame


def _to_float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return np.nan


def clean(rows: pd.DataFrame) -> pd.DataFrame:
    for column in (ID, DATE, LAT, LNG):
        if column not in rows.columns:
            raise GidImportError(f"missing column {column}")
    out = pd.DataFrame(index=rows.index)
    out["row_number"] = rows["row_number"]
    out["id"] = rows[ID].str.strip()
    empty = out["id"] == ""
    if empty.any():
        raise GidImportError(f"empty service_request_id at rows {rows.loc[empty, 'row_number'].tolist()}")

    times = timeutil.parse_series(rows[DATE])
    bad_time = times["time_status"].isin([timeutil.MISSING, timeutil.INVALID])
    if bad_time.any():
        raise GidImportError(f"invalid date_requested at rows {rows.loc[bad_time, 'row_number'].tolist()}")
    out = out.join(times)

    lat_text, lng_text = rows[LAT].str.strip(), rows[LNG].str.strip()
    both_blank = (lat_text == "") & (lng_text == "")
    lat = lat_text.map(_to_float)
    lng = lng_text.map(_to_float)
    valid = pd.Series([geo.valid_coordinate(a, b) for a, b in zip(lat, lng)], index=rows.index, dtype=bool)
    bad_coord = ~both_blank & ~valid
    if bad_coord.any():
        raise GidImportError(f"invalid coordinates at rows {rows.loc[bad_coord, 'row_number'].tolist()}")
    out["lat"] = lat.where(~both_blank)
    out["lng"] = lng.where(~both_blank)

    parent = rows[PARENT].str.strip() if PARENT in rows.columns else pd.Series("", index=rows.index)
    out["parent_id"] = parent.where(parent != "", None)
    for column in ("service_name", "status"):
        out[column] = rows[column] if column in rows.columns else None
    return out


def load_gid(path: str | Path) -> GidDataset:
    rows = read_raw(path)
    cleaned = clean(rows)
    records = cleaned.drop_duplicates("id", keep="last").set_index("id", drop=False).rename_axis(None).sort_index()
    return GidDataset(rows=rows, records=records)


def select_requests(
    ds: GidDataset,
    start,
    end,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_m: float | None = None,
    exclude_linked_duplicates: bool = False,
    service_names: set[str] | None = None,
    statuses: set[str] | None = None,
) -> Expected:
    """General reference selection. Category/status filters have no production
    counterpart (README documents only count_service_requests) and are verified
    by unit tests only."""
    r = ds.records
    in_time = timeutil.window_membership(r["t_early"], r["t_late"], start, end)
    keep = in_time != "out"
    spatial = pd.Series("in", index=r.index, dtype=object)
    if latitude is not None or longitude is not None or radius_m is not None:
        geo.validate_center(latitude, longitude, radius_m)
        spatial = geo.radius_membership(geo.distances_m(latitude, longitude, r["lat"], r["lng"]), radius_m)
        keep &= spatial != "out"
    if exclude_linked_duplicates:
        keep &= r["parent_id"].isna()
    if service_names is not None:
        keep &= r["service_name"].isin(service_names)
    if statuses is not None:
        keep &= r["status"].isin(statuses)

    expected = Expected()
    for rid in r.index[keep]:
        reason = merge_reasons(
            (in_time[rid] == "uncertain", f"time_{r.at[rid, 'time_status']}"),
            (spatial[rid] == "boundary", "radius_boundary"),
        )
        if reason:
            expected.uncertain[rid] = reason
        else:
            expected.ids.add(rid)
    return expected


def count_service_requests(ds, latitude, longitude, radius_m, start, end, exclude_linked_duplicates=False) -> Expected:
    """Reference for citylens.count_service_requests: distinct request IDs with a
    location within radius_m (inclusive) created in [start, end)."""
    return select_requests(ds, start, end, latitude, longitude, radius_m, exclude_linked_duplicates)
