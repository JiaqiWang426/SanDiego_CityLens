"""Independent geodesic distance calculation and tolerance classification.

Distances are WGS84 ellipsoidal geodesics (Karney's algorithm via pyproj.Geod),
which is the documented PostGIS geography model but computed independently.

Tolerance contract
------------------
DISTANCE_TOLERANCE_M = 0.5 m. A point whose reference distance d satisfies
|d - r| <= 0.5 m is 'boundary': expected membership is undefined and the point
is excluded from strict ID-set comparison and reported separately. Points with
d < r - 0.5 are 'in', points with d > r + 0.5 are 'out'. Coordinates stored as
float8 degrees, and pyproj vs PostGIS spheroid differences are well below this
(both < 1 mm at the radii used here), so 0.5 m leaves wide headroom while still
catching a spherical or planar approximation (errors of meters at 800 m).

Documented radius domain (README): 0 to 50,000 meters, both inclusive.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from pyproj import Geod

DISTANCE_TOLERANCE_M = 0.5
MIN_RADIUS_M, MAX_RADIUS_M = 0.0, 50_000.0
_GEOD = Geod(ellps="WGS84")


def validate_center(latitude: float, longitude: float, radius_m: float) -> None:
    for name, value in (("latitude", latitude), ("longitude", longitude), ("radius", radius_m)):
        if value is None or not np.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
    if not -90 <= latitude <= 90:
        raise ValueError("latitude out of range")
    if not -180 <= longitude <= 180:
        raise ValueError("longitude out of range")
    if not MIN_RADIUS_M <= radius_m <= MAX_RADIUS_M:
        raise ValueError("radius out of documented range 0-50,000 m")


def distances_m(latitude: float, longitude: float, lats: pd.Series, lngs: pd.Series) -> pd.Series:
    """Geodesic distance from the center to each point; NaN where a coordinate is missing."""
    out = pd.Series(np.nan, index=lats.index, dtype=float)
    mask = lats.notna() & lngs.notna()
    if mask.any():
        n = int(mask.sum())
        _, _, dist = _GEOD.inv(
            np.full(n, longitude), np.full(n, latitude),
            lngs[mask].to_numpy(dtype=float), lats[mask].to_numpy(dtype=float),
        )
        out[mask] = dist
    return out


def radius_membership(dist: pd.Series, radius_m: float, tol: float = DISTANCE_TOLERANCE_M) -> pd.Series:
    """'in' / 'out' / 'boundary'; missing locations are always 'out' (README)."""
    result = pd.Series("out", index=dist.index, dtype=object)
    result[dist < radius_m - tol] = "in"
    result[(dist - radius_m).abs() <= tol] = "boundary"
    result[dist == 0] = "in"  # identical coordinates: exact, no rounding involved
    result[dist.isna()] = "out"
    return result


def valid_coordinate(lat: float, lng: float) -> bool:
    return bool(np.isfinite(lat) and np.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180)
