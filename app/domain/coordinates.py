"""Domain layer for lunar calculations and spatial projections."""

import math
from app.core.constants import MOON_RADIUS_M


def normalize_longitude(lon: float) -> float:
    """Normalize longitude to [-180, 180) degrees."""
    lon = lon % 360.0
    if lon >= 180.0:
        lon -= 360.0
    return lon


def lat_lon_to_polar_xy(lat: float, lon: float) -> tuple[float, float]:
    """
    Project lunar latitude/longitude to South Pole stereographic (x, y) coordinates in meters.

    Matches the CRS of the NASA LOLA south pole products (LDEM_80S_*, LPSR_80S_*):
    +proj=stere +lat_0=-90 +lat_ts=-90 +lon_0=0 +R=1737400, i.e. +X toward 90 E
    and +Y toward 0 E (the Earth-facing meridian).
    """
    # Colatitude in radians: 90 - |lat| for southern hemisphere
    colat_deg = 90.0 + lat  # since lat is negative (-90 to -80)
    colat_rad = math.radians(colat_deg)

    # Stereographic projection radial distance
    # r = 2 * R * tan(colat / 2)
    r = 2.0 * MOON_RADIUS_M * math.tan(colat_rad / 2.0)

    # South pole orientation: x = r*sin(lon), y = r*cos(lon)
    lon_rad = math.radians(lon)
    x = round(r * math.sin(lon_rad), 1)
    y = round(r * math.cos(lon_rad), 1)
    return x, y
