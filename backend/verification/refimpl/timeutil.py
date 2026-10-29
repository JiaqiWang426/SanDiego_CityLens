"""Source wall-time parsing with explicit DST ambiguity tracking.

Documented assumption (README, TRAFFIC_COLLISIONS.md): source timestamps have
no offset and are interpreted as America/Los_Angeles wall time. The docs do not
say how the repeated autumn hour or the skipped spring hour resolve, so every
parsed value keeps two candidate instants:

* ``ok``          -- one valid instant (early == late)
* ``ambiguous``   -- repeated hour; early = PDT reading, late = PST reading
* ``nonexistent`` -- skipped hour; both readings kept, neither is authoritative
* ``missing`` / ``invalid`` -- no instant (early = late = NaT)

A comparison is strict only when both candidates give the same answer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

SOURCE_TZ = ZoneInfo("America/Los_Angeles")
SOURCE_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")

OK, AMBIGUOUS, NONEXISTENT, MISSING, INVALID = "ok", "ambiguous", "nonexistent", "missing", "invalid"


@lru_cache(maxsize=None)
def parse_source_time(text: str) -> tuple[str, datetime | None, datetime | None]:
    """Return (status, early_utc, late_utc) for one source timestamp string."""
    value = (text or "").strip()
    if not value:
        return MISSING, None, None
    naive = None
    for fmt in SOURCE_FORMATS:
        try:
            naive = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if naive is None:
        return INVALID, None, None
    u0 = naive.replace(tzinfo=SOURCE_TZ, fold=0).astimezone(timezone.utc)
    u1 = naive.replace(tzinfo=SOURCE_TZ, fold=1).astimezone(timezone.utc)
    if u0 == u1:
        return OK, u0, u0
    early, late = min(u0, u1), max(u0, u1)
    round_trip = early.astimezone(SOURCE_TZ).replace(tzinfo=None)
    status = AMBIGUOUS if round_trip == naive else NONEXISTENT
    return status, early, late


def parse_series(values: pd.Series) -> pd.DataFrame:
    """Vector form: columns time_status, t_early, t_late (UTC, tz-aware)."""
    parsed = values.map(parse_source_time)
    return pd.DataFrame(
        {
            "time_status": parsed.map(lambda p: p[0]),
            "t_early": pd.to_datetime(parsed.map(lambda p: p[1]), utc=True),
            "t_late": pd.to_datetime(parsed.map(lambda p: p[2]), utc=True),
        },
        index=values.index,
    )


def require_aware(value) -> pd.Timestamp:
    """Query bounds must carry an explicit offset (README: 'require explicit UTC offsets')."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError(f"timestamp without UTC offset: {value!r}")
    return ts.tz_convert("UTC")


def window_membership(early: pd.Series, late: pd.Series, start, end) -> pd.Series:
    """'in' / 'out' / 'uncertain' for the half-open interval [start, end).

    Rows without a time are 'out' (they can never match a date filter).
    """
    start, end = require_aware(start), require_aware(end)
    early_in = (early >= start) & (early < end)
    late_in = (late >= start) & (late < end)
    result = pd.Series("out", index=early.index, dtype=object)
    result[early_in & late_in] = "in"
    result[early_in ^ late_in] = "uncertain"
    result[early.isna()] = "out"
    return result
