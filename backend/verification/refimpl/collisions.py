"""Traffic Collisions reference: cleaning, filtering, retrieval and aggregation.

Contract sources: backend/TRAFFIC_COLLISIONS.md, backend/README.md and
backend/sql/traffic_collisions.sql. [ASSUMPTION] marks rules the docs do not
state; records whose expected result depends on one are reported as
'uncertain' instead of being asserted on.

Cleaning rules
--------------
* REPORT_ID: trimmed [ASSUMPTION]; empty -> archived but rejected (documented).
  Repeated IDs: last CSV occurrence wins (documented). Accepted rows include
  repeats; rejected rows are the missing-ID rows (documented).
* DATE_TIME: Los Angeles wall time; missing/invalid -> NULL, never matches a
  date filter (documented). DST ambiguity tracked by timeutil.
* INJURED / KILLED: trimmed digit string -> integer; blank or anything else ->
  NULL, never zero (documented: "Missing ... stay NULL, as do invalid
  nonnegative integer values"). Treating ' 2 ' as 2 and '2.0'/'+1' as invalid
  is [ASSUMPTION]; fixtures for strict tests avoid those spellings.
* HIT_RUN_LVL: '' -> hit_and_run false; FELONY / MISDEMEANOR -> true; any other
  non-empty label -> NULL with the label preserved (documented). Whitespace
  trimming and case handling are [ASSUMPTION].
* Street names: whitespace collapsed; full name = direction + road + suffix.
  Only DRIVE -> DR is documented (example 'LA JOLLA SCENIC DR'); other suffix
  abbreviations are [ASSUMPTION] and make street matches 'uncertain'.
* Locations come only from geocoding; the CSV has none. Tests attach synthetic
  geocoded points with ``with_locations``.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

from . import geo, timeutil
from .result import Expected, merge_reasons

SOURCE_COLUMNS = [
    "REPORT_ID", "DATE_TIME", "POLICE_BEAT", "ADDRESS_NO_PRIMARY", "ADDRESS_PD_PRIMARY",
    "ADDRESS_ROAD_PRIMARY", "ADDRESS_SFX_PRIMARY", "ADDRESS_PD_INTERSECTING",
    "ADDRESS_NAME_INTERSECTING", "ADDRESS_SFX_INTERSECTING", "VIOLATION_SECTION",
    "VIOLATION_TYPE", "CHARGE_DESC", "INJURED", "KILLED", "HIT_RUN_LVL",
]
HIT_RUN_TRUE_LEVELS = {"FELONY", "MISDEMEANOR"}
INT32_MAX = 2**31 - 1

# Documented: DRIVE -> DR. The remaining entries are USPS Publication 28
# standard abbreviations, used as an explicit [ASSUMPTION] only.
DOCUMENTED_SUFFIXES = {"DRIVE": "DR"}
USPS_SUFFIXES = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "BOULEVARD": "BLVD", "DRIVE": "DR",
    "WAY": "WAY", "PARKWAY": "PKWY", "PLACE": "PL", "COURT": "CT", "HIGHWAY": "HWY",
    "LANE": "LN", "CIRCLE": "CIR", "TERRACE": "TER", "TRAIL": "TRL", "ALLEY": "ALY",
    "LOOP": "LOOP", "ROW": "ROW", "WALK": "WALK", "SQUARE": "SQ", "PLAZA": "PLZ",
    "EXPRESSWAY": "EXPY", "FREEWAY": "FWY", "COVE": "CV", "POINT": "PT", "GLEN": "GLN",
    "PATH": "PATH", "CENTER": "CTR", "MALL": "MALL", "PASEO": "PASEO", "VIEW": "VW",
}

PERIODS = ("day", "week", "month", "quarter", "year")
LIMIT_RANGE, OFFSET_RANGE = (1, 1000), (0, 100_000)


class CollisionImportError(ValueError):
    """The reference expects the whole import to fail and roll back."""


@dataclass
class CollisionDataset:
    rows: pd.DataFrame      # every parsed CSV row (archive), plus row_number
    records: pd.DataFrame   # one per accepted REPORT_ID (last occurrence), cleaned

    @property
    def source_rows(self) -> int:
        return len(self.rows)

    @property
    def rejected_rows(self) -> int:
        return int((self.rows["REPORT_ID"].str.strip() == "").sum())

    @property
    def accepted_rows(self) -> int:
        return self.source_rows - self.rejected_rows


def _space(text: str) -> str:
    return " ".join((text or "").split())


def _count(text: str):
    value = (text or "").strip()
    if re.fullmatch(r"[0-9]+", value) and int(value) <= INT32_MAX:
        return int(value)
    return None


def _hit_run(text: str):
    value = (text or "").strip()
    if value == "":
        return None, False
    return value, (True if value in HIT_RUN_TRUE_LEVELS else None)


def street_names(direction: str, road: str, suffix: str) -> tuple[str, frozenset[str]]:
    """Return (preferred full name, every name the record could legitimately carry).

    More than one candidate means the spelling depends on an undocumented rule:
    the suffix abbreviation, or case folding of mixed-case source text (the
    docs only promise whitespace normalization).
    """
    direction, road, suffix = _space(direction), _space(road), _space(suffix)
    if not road:
        return "", frozenset({""})

    def join(d: str, r: str, sfx: str) -> str:
        return " ".join(part for part in (d, r, sfx) if part)

    case_variants = {(direction, road)} | {(direction.upper(), road.upper())}
    if suffix == "" or suffix in DOCUMENTED_SUFFIXES:
        sfx = DOCUMENTED_SUFFIXES.get(suffix, suffix)
        names = {join(d, r, sfx) for d, r in case_variants}
        return join(direction, road, sfx), frozenset(names)
    preferred = join(direction, road, USPS_SUFFIXES.get(suffix, suffix))
    names = {join(d, r, s) for d, r in case_variants for s in (USPS_SUFFIXES.get(suffix, suffix), suffix)}
    return preferred, frozenset(names)


def read_raw(path: str | Path) -> pd.DataFrame:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        data = list(reader)
    missing = [c for c in SOURCE_COLUMNS if c not in header]
    if missing:
        raise CollisionImportError(f"missing required headers {missing}")
    for number, row in enumerate(data, start=1):
        if len(row) != len(header):
            raise CollisionImportError(f"row {number}: {len(row)} fields, expected {len(header)}")
    frame = pd.DataFrame(data, columns=header, dtype=object)
    frame.insert(0, "row_number", range(1, len(frame) + 1))
    return frame


def clean(rows: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=rows.index)
    out["row_number"] = rows["row_number"]
    out["id"] = rows["REPORT_ID"].str.strip()
    out = out.join(timeutil.parse_series(rows["DATE_TIME"]))
    out["injured"] = rows["INJURED"].map(_count).astype("Int64")
    out["killed"] = rows["KILLED"].map(_count).astype("Int64")
    hr = rows["HIT_RUN_LVL"].map(_hit_run)
    out["hit_run_level"] = hr.map(lambda p: p[0])
    out["hit_and_run"] = hr.map(lambda p: p[1]).astype("boolean")
    primary = [street_names(d, r, s) for d, r, s in zip(
        rows["ADDRESS_PD_PRIMARY"], rows["ADDRESS_ROAD_PRIMARY"], rows["ADDRESS_SFX_PRIMARY"])]
    cross = [street_names(d, r, s) for d, r, s in zip(
        rows["ADDRESS_PD_INTERSECTING"], rows["ADDRESS_NAME_INTERSECTING"], rows["ADDRESS_SFX_INTERSECTING"])]
    out["primary_street"] = [p[0] for p in primary]
    out["intersecting_street"] = [c[0] for c in cross]
    out["street_candidates"] = [(p[1], c[1]) for p, c in zip(primary, cross)]
    out["lat"] = float("nan")
    out["lng"] = float("nan")
    return out


def load_collisions(path: str | Path) -> CollisionDataset:
    rows = read_raw(path)
    cleaned = clean(rows)
    accepted = cleaned[cleaned["id"] != ""]
    records = accepted.drop_duplicates("id", keep="last").set_index("id", drop=False).rename_axis(None).sort_index()
    return CollisionDataset(rows=rows, records=records)


def with_locations(ds: CollisionDataset, points: dict[str, tuple[float, float]]) -> CollisionDataset:
    """Attach synthetic geocoded (lat, lng) points, mirroring 'matched' rows."""
    records = ds.records.copy()
    for rid, (lat, lng) in points.items():
        records.at[rid, "lat"], records.at[rid, "lng"] = lat, lng
    return replace(ds, records=records)


def _validate(latitude, longitude, radius_m):
    supplied = [v is not None for v in (latitude, longitude, radius_m)]
    if any(supplied) and not all(supplied):
        raise ValueError("latitude, longitude and radius must be supplied together")
    if all(supplied):
        geo.validate_center(latitude, longitude, radius_m)


def _membership(ds, start, end, latitude, longitude, radius_m, min_injured, max_injured,
                min_killed, max_killed, hit_run, street_name, hit_run_level):
    """Per-record ('in'|'out'|'uncertain', reason) for the shared filter rules."""
    _validate(latitude, longitude, radius_m)
    r = ds.records
    in_time = timeutil.window_membership(r["t_early"], r["t_late"], start, end)
    keep = in_time != "out"
    spatial = pd.Series("in", index=r.index, dtype=object)
    if radius_m is not None:
        spatial = geo.radius_membership(geo.distances_m(latitude, longitude, r["lat"], r["lng"]), radius_m)
        keep &= spatial != "out"
    for column, low, high in (("injured", min_injured, max_injured), ("killed", min_killed, max_killed)):
        if low is not None:
            keep &= r[column].notna() & (r[column] >= low).fillna(False)
        if high is not None:
            keep &= r[column].notna() & (r[column] <= high).fillna(False)
    if hit_run is not None:
        keep &= r["hit_and_run"].notna() & (r["hit_and_run"] == hit_run).fillna(False)
    if hit_run_level is not None:
        keep &= r["hit_run_level"] == hit_run_level
    street_state = pd.Series("in", index=r.index, dtype=object)
    if street_name is not None:
        def state(cands):
            primary, cross = cands
            hits = {street_name in primary, street_name in cross}
            definite = (len(primary) == 1 and street_name in primary) or (len(cross) == 1 and street_name in cross)
            if definite:
                return "in"
            return "uncertain" if True in hits else "out"
        street_state = r["street_candidates"].map(state)
        keep &= street_state != "out"

    states = {}
    for rid in r.index[keep]:
        reason = merge_reasons(
            (in_time[rid] == "uncertain", f"time_{r.at[rid, 'time_status']}"),
            (spatial[rid] == "boundary", "radius_boundary"),
            (street_state[rid] == "uncertain", "street_spelling_undocumented"),
        )
        states[rid] = reason
    return states


def filter_collisions(ds, start, end, latitude=None, longitude=None, radius_m=None,
                      min_injured=None, max_injured=None, min_killed=None, max_killed=None,
                      hit_run=None, street_name=None, hit_run_level=None) -> Expected:
    """Reference for citylens.filter_traffic_collisions (uncapped selection)."""
    states = _membership(ds, start, end, latitude, longitude, radius_m, min_injured, max_injured,
                         min_killed, max_killed, hit_run, street_name, hit_run_level)
    expected = Expected()
    for rid, reason in states.items():
        if reason:
            expected.uncertain[rid] = reason
        else:
            expected.ids.add(rid)
    return expected


def ordered_ids(ds: CollisionDataset, ids) -> list[str]:
    """Order by occurrence instant, then report ID (Python code-point order).

    Uncertain-time records sort by their early candidate; callers only assert
    order when the page contains no uncertain records.
    """
    sub = ds.records.loc[list(ids), ["t_early", "id"]]
    return sub.sort_values(["t_early", "id"], kind="mergesort")["id"].tolist()


def get_collisions(ds, start, end, result_limit=100, result_offset=0, **filters) -> Expected:
    """Reference for citylens.get_traffic_collisions (one page)."""
    if not LIMIT_RANGE[0] <= result_limit <= LIMIT_RANGE[1]:
        raise ValueError("limit out of documented range 1-1000")
    if not OFFSET_RANGE[0] <= result_offset <= OFFSET_RANGE[1]:
        raise ValueError("offset out of documented range 0-100000")
    full = filter_collisions(ds, start, end, **filters)
    order = ordered_ids(ds, full.ids | set(full.uncertain))
    page = order[result_offset:result_offset + result_limit]
    expected = Expected(ordered=page)
    for rid in page:
        if rid in full.uncertain:
            expected.uncertain[rid] = full.uncertain[rid]
        else:
            expected.ids.add(rid)
    if full.uncertain:
        # A page boundary can move when uncertain rows shift; mark it unordered.
        expected.uncertain.setdefault("__page__", "page_boundary_depends_on_uncertain_rows")
    return expected


def period_start(local: pd.Series, period: str) -> pd.Series:
    """Truncate tz-aware local timestamps to the start of their calendar period."""
    naive = local.dt.tz_localize(None)
    if period == "day":
        start = naive.dt.normalize()
    elif period == "week":  # ISO weeks begin Monday (documented)
        start = naive.dt.normalize() - pd.to_timedelta(naive.dt.day_of_week, unit="D")
    elif period == "month":
        start = naive.dt.to_period("M").dt.start_time
    elif period == "quarter":
        start = naive.dt.to_period("Q").dt.start_time
    elif period == "year":
        start = naive.dt.to_period("Y").dt.start_time
    else:
        raise ValueError(f"unsupported period {period!r}")
    return start


def aggregate_collisions(ds, start, end, period="month", calendar_timezone="America/Los_Angeles", **filters):
    """Reference for citylens.aggregate_traffic_collisions.

    Returns (exact, uncertain):
    * exact: DataFrame indexed by period_start (UTC instant of local period
      start) with collision_count, located_count, unknown_injured_count,
      unknown_killed_count, built from records whose period is certain.
    * uncertain: {report_id: set of candidate period_start instants} for
      records whose inclusion or period depends on an ambiguity.
    """
    if period not in PERIODS:
        raise ValueError(f"unsupported period {period!r}")
    states = _membership(ds, start, end, filters.get("latitude"), filters.get("longitude"),
                         filters.get("radius_m"), filters.get("min_injured"), filters.get("max_injured"),
                         filters.get("min_killed"), filters.get("max_killed"), filters.get("hit_run"),
                         filters.get("street_name"), filters.get("hit_run_level"))
    r = ds.records.loc[list(states)]
    columns = ["collision_count", "located_count", "unknown_injured_count", "unknown_killed_count"]
    if r.empty:
        return pd.DataFrame(columns=columns).rename_axis("period_start"), {}

    def localized_start(col):
        local_start = period_start(r[col].dt.tz_convert(calendar_timezone), period)
        # Period starts are local wall times; resolve to an instant, preferring
        # the standard-time reading only if a period started inside a DST gap
        # or overlap (never happens at midnight in America/Los_Angeles).
        return local_start.dt.tz_localize(calendar_timezone, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")

    p_early, p_late = localized_start("t_early"), localized_start("t_late")
    certain = pd.Series([states[rid] == "" for rid in r.index], index=r.index) & (p_early == p_late)
    uncertain = {rid: {p_early[rid], p_late[rid]} for rid in r.index[~certain]}
    c = r[certain].assign(period_start=p_early[certain])
    exact = pd.DataFrame({
        "collision_count": c.groupby("period_start").size(),
        "located_count": c.assign(v=c["lat"].notna()).groupby("period_start")["v"].sum(),
        "unknown_injured_count": c.assign(v=c["injured"].isna()).groupby("period_start")["v"].sum(),
        "unknown_killed_count": c.assign(v=c["killed"].isna()).groupby("period_start")["v"].sum(),
    }).astype("int64").sort_index()
    return exact, uncertain
